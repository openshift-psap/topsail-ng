#!/usr/bin/env python3
"""
GitHub PR Arguments Parser

Fetches GitHub PR comments, finds the last comment from the PR author or CONTRIBUTOR,
and extracts test configuration from special directives.

Converts the bash script pr_args.sh to Python with enhanced error handling and structure.
"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Callable
import urllib.request
import urllib.error

REQUIRED_AUTHOR_ASSOCIATION = 'CONTRIBUTOR'

def setup_logging():
    """Set up logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s',
        handlers=[logging.StreamHandler(sys.stderr)]
    )


def get_directive_handlers() -> Dict[str, Callable[[str], Dict[str, Any]]]:
    """
    Get a mapping of directive prefixes to their handler functions.

    Returns:
        Dictionary mapping directive prefixes to handler functions
    """

    return {
        '/test': handle_test_directive,
        '/var': handle_var_directive,
        '/skip': handle_skip_directive,
        '/only': handle_only_directive,
        '/project': handle_project_directive,
        '/cluster': handle_cluster_directive,
    }


def handle_test_directive(line: str) -> Dict[str, Any]:
    """
    Handle /test directive for test commands and generate PR positional arguments.

    Format: /test test_name project arg1 arg2

    Args:
        line: The directive line

    Returns:
        Dictionary with test information and PR positional arguments
    """
    # Extract test name and arguments
    parts = line[6:].strip().split()
    if not parts:
        raise ValueError("Found an empty /test directive")

    test_name = parts.pop(0)

    if not parts:
        raise ValueError(f"Found a /test directive without a project ('{line.strip()}')")

    project_name = parts.pop(0)

    args = parts # allowed to be empty
    result = {}
    # Special handling for jump CI - extract cluster and target project info
    if test_name.endswith('jump-ci'):
        # Format: /test jump-ci target_project [additional_args...]
        target_project = project_name

        result.update({
            'project.name': target_project,
            'project.args': args,
        })

        logging.info(f"Jump CI configuration: target_project={target_project}, args={args}")
    else:
        # Build result with test info and PR positional arguments
        result.update({
            'ci_job.name': test_name,
            'ci_job.project': project_name,
            'ci_job.args': args,
        })

    return result


def handle_var_directive(line: str) -> Dict[str, Any]:
    """
    Handle /var directive for setting variables.

    Format: /var key: value

    Args:
        line: The directive line

    Returns:
        Dictionary with parsed variables

    Raises:
        Exception: If the directive format is invalid
    """
    var_content = line[5:].strip()

    # Validate basic YAML format
    if ':' not in var_content:
        raise Exception(f"Invalid /var directive format: {line} (expected 'key: value')")

    try:
        if ': ' in var_content:
            key, value = var_content.split(': ', 1)
            return {key.strip(): value.strip()}
        else:
            # Fallback for other formats
            return {var_content: var_content}
    except Exception as e:
        raise Exception(f"Invalid /var directive: {line} - {e}")


def handle_skip_directive(line: str) -> Dict[str, Any]:
    """
    Handle /skip directive for disabling test executions.

    Format: /skip test1 test2 test3

    Args:
        line: The directive line

    Returns:
        Dictionary with execution control flags
    """

    skip_items = line[6:].split()
    result = {}
    for item in skip_items:
        result[f'exec_list.{item}'] = False

    return result


def handle_only_directive(line: str) -> Dict[str, Any]:
    """
    Handle /only directive for enabling only specific test executions.

    Format: /only test1 test2

    Args:
        line: The directive line

    Returns:
        Dictionary with execution control flags
    """
    only_items = line[6:].split()
    result = {'exec_list._only_': True}
    for item in only_items:
        result[f'exec_list.{item}'] = True
    return result


def handle_project_directive(line: str) -> Dict[str, Any]:
    """
    Handle /project directive for setting project override.

    Format: /project project_name

    Args:
        line: The directive line

    Returns:
        Dictionary with project configuration
    """
    project_name = line[9:].strip()
    return {'project.name': project_name}


def handle_cluster_directive(line: str) -> Dict[str, Any]:
    """
    Handle /cluster directive for setting cluster override.

    Format: /cluster cluster_name

    Args:
        line: The directive line

    Returns:
        Dictionary with cluster configuration
    """
    cluster_name = line[9:].strip()
    return {'cluster.name': cluster_name}


def get_directive_prefixes() -> List[str]:
    """
    Get a list of supported directive prefixes.

    Returns:
        List of directive prefixes (e.g., ['/var', '/skip', '/only', ...])
    """
    return list(get_directive_handlers().keys())


def get_supported_directives() -> Dict[str, str]:
    """
    Get a dictionary of supported directives and their comprehensive descriptions.

    Returns:
        Dictionary mapping directive names to detailed descriptions
    """
    return {
        '/var': '''Set configuration variables in YAML format.
                   Format: /var key: value
                   Example: /var debug: true
                            /var timeout: 300
                            /var cluster.size: large
                   Note: Variables are merged into the final configuration and can override defaults.''',

        '/skip': '''Skip specific test executions by name.
                    Format: /skip test1 test2 test3
                    Example: /skip unit-tests integration-tests
                    Effect: Sets exec_list.{test_name}: false for each specified test.
                    Use case: Temporarily disable failing or unnecessary test components.''',

        '/only': '''Enable only specific test executions, disabling all others.
                    Format: /only test1 test2
                    Example: /only smoke-tests performance-tests
                    Effect: Sets exec_list._only_: true and exec_list.{test_name}: true
                    Note: This is exclusive - all non-specified tests will be disabled.''',

        '/project': '''Override the project name for the test execution.
                       Format: /project project_name
                       Example: /project llm-load-test
                                /project custom-benchmark
                       Effect: Sets project.name in configuration.
                       Use case: Run tests against a different project than the default.''',

        '/cluster': '''Override the target cluster name for test execution.
                       Format: /cluster cluster_name
                       Example: /cluster production-cluster
                                /cluster staging-env
                       Effect: Sets cluster.name in configuration.
                       Use case: Target tests at specific cluster environments.''',

        '/test': '''Execute a test command with optional arguments.
                    Format: /test test_name project_name [arg1] [arg2] ...
                    Example: /test jump-ci cluster target_project
                             /test llm-d skeleton arg1 arg2
                    Effect: Extracts ci_job.{name,project,args}.
                    Special: For jump-ci, format is /test jump-ci cluster target_project [args]
                             which also sets jump_ci.{cluster,project,args} for remote execution.
                    Note: This is the primary directive for triggering CI test runs.''',
    }


def parse_directives(text: str) -> Tuple[Dict[str, Any], List[str]]:
    """
    Parse all directives from the given text using handler mapping.

    Supported directives are defined in get_directive_handlers().
    See get_supported_directives() for format documentation.

    Args:
        text: Text containing directives (PR body + comments)

    Returns:
        Tuple of (configuration dictionary, list of found directive lines)

    Raises:
        Exception: If any directive has invalid format
    """
    config = {}
    found_directives = []
    directive_handlers = get_directive_handlers()

    has_test = False
    # Process each line for directives
    for line in text.split('\n'):
        line = line.strip()

        # Skip empty lines and non-directive lines
        if not line or not line.startswith('/'):
            continue

        if line.startswith("/test"):
            has_test = True

        # Find matching directive handler
        handler = None
        for prefix, handler_func in directive_handlers.items():
            if line.startswith(prefix + ' ') or line == prefix:
                handler = handler_func
                break

        if handler:
            try:
                # Call handler and merge results
                result = handler(line)
                config.update(result)
                found_directives.append(line)
            except Exception as e:
                raise ValueError(f"Error parsing directive '{line}': {e}")
        else:
            # Unknown directive - log warning but still track it
            logging.warning(f"Unknown directive ignored: {line}")
            found_directives.append(f"# UNKNOWN: {line}")

    if not has_test:
        raise ValueError("/test directive not found in the PR last comment")

    return config, found_directives


def fetch_url(url: str, cache_file: Optional[Path] = None) -> Dict[str, Any]:
    """
    Fetch JSON data from URL with optional caching.

    Args:
        url: URL to fetch
        cache_file: Optional file path to cache the response

    Returns:
        JSON data as dictionary

    Raises:
        Exception: If HTTP request fails
    """

    # Check cache first
    if cache_file and cache_file.exists():
        logging.info(f"Using cached file: {cache_file}")
        with open(cache_file, 'r') as f:
            return json.load(f)

    # Fetch from URL
    logging.info(f"Fetching from URL: {url}")
    try:
        with urllib.request.urlopen(url) as response:
            data = json.load(response)

        # Save to cache if specified
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_file, 'w') as f:
                json.dump(data, f, indent=2)

        return data

    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to fetch {url}: {e}")


def parse_pr_arguments(
    repo_owner: str,
    repo_name: str,
    pull_number: int,
    test_name: Optional[str] = None,
    shared_dir: Optional[Path] = None
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Parse GitHub PR arguments and configuration from comments.

    Args:
        repo_owner: GitHub repository owner
        repo_name: GitHub repository name
        pull_number: Pull request number
        test_name: Test name to search for (if not provided, derived from environment)
        shared_dir: Shared directory for caching (OpenShift CI)

    Returns:
        Tuple of (configuration dictionary with parsed arguments and directives, list of found directive lines)

    Raises:
        Exception: If required data cannot be found or parsed
    """
    # Determine test name
    if not test_name:
        if os.environ.get('OPENSHIFT_CI') == 'true':
            job_name = os.environ.get('JOB_NAME', '')
            job_name_prefix = f"pull-ci-{repo_owner}-{repo_name}-main"
            test_name = job_name.replace(f"{job_name_prefix}-", "")
            if not test_name:
                raise Exception(f"Could not derive test name from JOB_NAME: {job_name}")
        else:
            test_name = os.environ.get('TEST_NAME')
            if not test_name:
                raise Exception("TEST_NAME not defined and not in OpenShift CI")

    # Build URLs
    pr_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls/{pull_number}"
    pr_comments_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues/{pull_number}/comments"

    logging.info(f"# PR URL: {pr_url}")
    logging.info(f"# PR comments URL: {pr_comments_url}")

    # Set up caching for OpenShift CI
    pr_cache_file = None
    comments_cache_file = None
    if shared_dir:
        pr_cache_file = shared_dir / "pr.json"
        comments_cache_file = shared_dir / "pr_last_comment_page.json"

    # Fetch PR data
    pr_data = fetch_url(pr_url, pr_cache_file)

    # Calculate last comment page
    pr_comments_count = pr_data.get('comments', 0)
    comments_per_page = 30  # GitHub default
    last_comment_page = (pr_comments_count // comments_per_page) + (1 if pr_comments_count % comments_per_page else 0)
    if last_comment_page == 0:
        last_comment_page = 1

    # Fetch last comment page
    last_comment_page_url = f"{pr_comments_url}?page={last_comment_page}"
    last_comment_page_data = fetch_url(last_comment_page_url, comments_cache_file)

    # Find the last relevant comment
    pr_author = pr_data['user']['login']

    test_anchor = f"/test {test_name}"

    logging.info(f"# Looking for comments from author '{pr_author}' or '{REQUIRED_AUTHOR_ASSOCIATION}' containing '{test_anchor}'")

    # Search comments in reverse order (most recent first)
    last_user_test_comment = None
    for comment in reversed(last_comment_page_data):
        author_login = comment.get('user', {}).get('login', '')
        author_association = comment.get('author_association', '')
        comment_body = comment.get('body', '')

        # Check if this is from the PR author or a contributor
        if author_login == pr_author or author_association == REQUIRED_AUTHOR_ASSOCIATION:
            if test_anchor in comment_body:
                last_user_test_comment = comment_body
                break

    if not last_user_test_comment:
        raise ValueError(f"No comment found from '{pr_author}' or '{REQUIRED_AUTHOR_ASSOCIATION}' containing '{test_anchor}'")

    # Parse all directives from PR body and last comment
    combined_text = (pr_data.get('body', '') or '') + '\n' + last_user_test_comment

    # Parse directives using the modular parser
    config, found_directives = parse_directives(combined_text)

    return config, found_directives


def main():
    """
    Main function for testing the PR arguments parser.

    Reads environment variables and logging.infos the parsed configuration to stdout.
    Special argument '--help-directives' shows supported directives.
    """
    # Handle special help argument
    if len(sys.argv) > 1 and sys.argv[1] == '--help-directives':
        logging.info("Supported GitHub PR directives:")
        for directive, description in get_supported_directives().items():
            logging.info(f"  {directive}: {description}")
        logging.info(f"\nSupported prefixes: {', '.join(get_directive_prefixes())}")
        return

    try:
        # Get required environment variables
        repo_owner = os.environ.get('REPO_OWNER') or "openshift-psap"
        repo_name = os.environ.get('REPO_NAME') or "topsail-ng"
        pull_number_str = os.environ.get('PULL_NUMBER') or 1

        if not repo_owner:
            logging.error("REPO_OWNER environment variable not defined")
            sys.exit(1)

        if not repo_name:
            logging.error("REPO_NAME environment variable not defined")
            sys.exit(1)

        if not pull_number_str:
            logging.error("PULL_NUMBER environment variable not defined")
            sys.exit(1)

        try:
            pull_number = int(pull_number_str)
        except ValueError:
            logging.error(f"PULL_NUMBER must be an integer, got: {pull_number_str}")
            sys.exit(1)

        # Optional parameters
        test_name = os.environ.get('TEST_NAME') or "jump-ci"
        shared_dir_str = os.environ.get('SHARED_DIR')
        shared_dir = Path(shared_dir_str) if shared_dir_str else None

        # Handle TOPSAIL local CI
        if os.environ.get('TOPSAIL_LOCAL_CI') == 'true' and not shared_dir:
            shared_dir = Path('/tmp/shared')
            logging.info(f"TOPSAIL local CI detected, using SHARED_DIR={shared_dir}")
            shared_dir.mkdir(parents=True, exist_ok=True)

        # Parse PR arguments
        config, found_directives = parse_pr_arguments(
            repo_owner=repo_owner,
            repo_name=repo_name,
            pull_number=pull_number,
            test_name=test_name,
            shared_dir=shared_dir
        )

        # Output configuration in YAML-like format (matching original script)
        for key, value in config.items():
            if isinstance(value, bool):
                print(f"{key}: {str(value).lower()}")
            elif isinstance(value, str):
                print(f"{key}: {value}")
            else:
                print(f"{key}: {value}")

    except Exception as e:
        logging.error(f"ERROR: {e}")
        sys.exit(1)


if __name__ == '__main__':
    setup_logging()
    main()
