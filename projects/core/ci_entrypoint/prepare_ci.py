#!/usr/bin/env python3
"""
TOPSAIL-NG CI Preparation Module

This module handles all preparation tasks needed before executing CI operations,
including parsing GitHub PR arguments and setting up the execution environment.
"""

import os
import sys
import logging
import yaml
import threading
from pathlib import Path
from typing import Optional

# Set up logging
logger = logging.getLogger(__name__)

# Import pr_args functionality
try:
    # Add the github directory to Python path
    github_dir = Path(__file__).parent / "github"
    if str(github_dir) not in sys.path:
        sys.path.insert(0, str(github_dir))

    from pr_args import parse_pr_arguments
    logger.info("GitHub PR arguments parser imported successfully")
except ImportError as e:
    logger.warning(f"GitHub PR arguments parser not available: {e}")
    parse_pr_arguments = None


# Dual output
def setup_dual_output():
    """
    Set up stdout/stderr to write to both console and log file.

    If ARTIFACT_DIR is set, all output will go to both console and $ARTIFACT_DIR/run.log
    This is permanent for the rest of the program execution.
    """
    artifact_dir = os.environ.get('ARTIFACT_DIR')

    if not artifact_dir:
        logging.warning("ARTIFACT_DIR not defined, not saving $ARTIFACT_DIR/run.log")
        return None

    log_file_path = Path(artifact_dir) / "run.log"

    try:
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logging.warning(f"Failed to create directory: {e}")
        return None

    if log_file_path.exists():
        with log_file_path.open(mode="a", encoding="utf-8") as f:
            f.write("--------------\n")
            f.write("| New CI run |\n")
            f.write("--------------\n")

    # 1. Save the original terminal stdout so we can still write to it
    original_stdout_fd = os.dup(sys.stdout.fileno())

    # 2. Create a pipe: (read_fd, write_fd)
    read_fd, write_fd = os.pipe()

    # 3. Replace the process's ACTUAL stdout and stderr with the write-end of our pipe
    os.dup2(write_fd, sys.stdout.fileno())
    os.dup2(write_fd, sys.stderr.fileno())

    def communicate():
        with open(log_file_path, "a") as log_file, os.fdopen(original_stdout_fd, "w") as terminal:
            # Open the read-end of the pipe
            with os.fdopen(read_fd, "r") as pipe_in:
                for line in pipe_in:
                    terminal.write(line) # Send to console
                    log_file.write(line) # Send to file
                    terminal.flush()
                    log_file.flush()

    # 4. Start a background thread to act as the 'tee' process
    daemon = threading.Thread(target=communicate, daemon=True)
    daemon.start()

# PR arguments
def parse_and_save_pr_arguments() -> Optional[Path]:
    """
    Parse GitHub PR arguments and save to variable overrides file.

    Returns:
        Path to saved file if successful, None otherwise
    """
    if not parse_pr_arguments:
        logger.warning("PR arguments parser not available")
        return None

    # Check if we're in a PR context
    repo_owner = os.environ.get('REPO_OWNER')
    repo_name = os.environ.get('REPO_NAME')
    pull_number_str = os.environ.get('PULL_NUMBER')
    artifact_dir = os.environ.get('ARTIFACT_DIR')

    if not all([repo_owner, repo_name, pull_number_str]):
        logger.info("Not in GitHub PR context - missing environment variables")
        return None

    if not artifact_dir:
        logger.warning("ARTIFACT_DIR not set, cannot save PR arguments")
        return None

    try:
        pull_number = int(pull_number_str)
    except ValueError:
        logger.error(f"Invalid PULL_NUMBER: {pull_number_str}")
        return None

    # Optional parameters
    test_name = os.environ.get('TEST_NAME')
    shared_dir_str = os.environ.get('SHARED_DIR')
    shared_dir = Path(shared_dir_str) if shared_dir_str else None

    # Handle TOPSAIL local CI
    if os.environ.get('TOPSAIL_LOCAL_CI') == 'true' and not shared_dir:
        shared_dir = Path('/tmp/shared')
        logger.info(f"TOPSAIL local CI detected, using SHARED_DIR={shared_dir}")
        shared_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Parsing GitHub PR arguments for {repo_owner}/{repo_name}#{pull_number}")

    try:
        # Parse PR arguments
        config = parse_pr_arguments(
            repo_owner=repo_owner,
            repo_name=repo_name,
            pull_number=pull_number,
            test_name=test_name,
            shared_dir=shared_dir
        )

        # Save to YAML file
        artifact_path = Path(artifact_dir)
        artifact_path.mkdir(parents=True, exist_ok=True)
        output_file = artifact_path / "variable_overrides.yaml"

        with open(output_file, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=True)

        logger.info(f"Saved PR arguments to {output_file}")
        logger.info(f"Configuration contains {len(config)} override(s)")

        return output_file

    except Exception as e:
        logger.error(f"Failed to parse PR arguments: {e}")
        return None


def setup_environment_variables():
    """
    Set up any additional environment variables needed for CI execution.
    """
    # Add any environment setup logic here
    logger.debug("Setting up environment variables")

    # Example: Ensure TOPSAIL_HOME is set
    if not os.environ.get('TOPSAIL_HOME'):
        topsail_home = Path(__file__).resolve().parent.parent.parent
        os.environ['TOPSAIL_HOME'] = str(topsail_home)
        logger.debug(f"Set TOPSAIL_HOME={topsail_home}")


def validate_prerequisites():
    """
    Validate that all necessary prerequisites are available.

    Returns:
        bool: True if all prerequisites are met, False otherwise
    """
    logger.debug("Validating CI prerequisites")

    # Add validation logic here
    # For now, just return True
    return True


def prepare(verbose: bool = False) -> bool:
    """
    Execute all CI preparation tasks.

    Args:
        verbose: Enable verbose output

    Returns:
        bool: True if preparation was successful, False otherwise
    """
    logger.info("Starting CI preparation")

    try:
        # Set up environment
        setup_environment_variables()

        # Validate prerequisites
        if not validate_prerequisites():
            logger.error("Prerequisites validation failed")
            return False

        # Parse and save PR arguments if in PR context
        pr_args_file = parse_and_save_pr_arguments()
        if pr_args_file and verbose:
            logger.info(f"PR arguments saved to: {pr_args_file}")
        elif pr_args_file:
            logger.debug(f"PR arguments saved to: {pr_args_file}")

        logger.info("CI preparation completed successfully")
        return True

    except Exception as e:
        logger.error(f"CI preparation failed: {e}")
        return False
