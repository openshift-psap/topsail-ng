import os
import pathlib
import sys
import subprocess
import json
import urllib.request
import urllib.error
import logging

TOOLBOX_THIS_DIR = pathlib.Path(__file__).absolute().parent
PROJECT_DIR = TOOLBOX_THIS_DIR.parent

logger = logging.getLogger(__name__)

class Repo:
    """
    Commands to perform consistency validations on this repo itself
    """

    @staticmethod
    def validate_no_wip():
        """
        Ensures that none of the commits have the WIP flag in their
        message title.
        """
        WIP_MARKER = "WIP"

        # Check if running in GitHub Actions
        github_ref = os.environ.get('GITHUB_REF')
        if not github_ref:
            logger.error("GITHUB_REF not set, cannot run outside of a GitHub action.")
            sys.exit(1)

        github_repository = os.environ.get('GITHUB_REPOSITORY')
        if not github_repository:
            logger.error("GITHUB_REPOSITORY not set.")
            sys.exit(1)

        try:
            # Extract PR number from GITHUB_REF (format: refs/pull/123/merge)
            pr_number = github_ref.split('/')[2]
            pr_url = f"https://api.github.com/repos/{github_repository}/pulls/{pr_number}"

            logger.info(f"Fetching the PR from '{pr_url}' ...")

            # Fetch PR data from GitHub API
            with urllib.request.urlopen(pr_url) as response:
                pr_data = json.loads(response.read().decode())

            pr_title = pr_data['title']
            logger.info(f"PR title: {pr_title}")

            # Get commit messages using git
            try:
                # Get first and second parent from latest commit
                result = subprocess.run(['git', 'log', '--pretty=%P', '-n', '1'],
                                      capture_output=True, text=True, check=True)
                parents = result.stdout.strip().split()

                if len(parents) >= 2:
                    first_parent = parents[0]
                    second_parent = parents[1]

                    # Get commits between the parents
                    result = subprocess.run(['git', 'log', '--pretty=format:%h - %s', '--abbrev-commit',
                                           f'{first_parent}..{second_parent}'],
                                          capture_output=True, text=True, check=True)
                    commits = result.stdout.strip()

                    logger.info("PR commits:")
                    logger.info(commits)
                else:
                    commits = ""
                    logger.info("No merge commits found")

            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to get git commits: {e}")
                sys.exit(1)

            # Check for WIP marker in PR title
            if WIP_MARKER in pr_title:
                logger.error(f"Found the '{WIP_MARKER}' marker in the PR title")
                sys.exit(1)

            # Check for WIP marker in commits
            if commits and WIP_MARKER in commits:
                logger.error(f"Found the '{WIP_MARKER}' marker in the PR commits")
                sys.exit(1)

            logger.info("No WIP markers found")
            sys.exit(0)

        except urllib.error.HTTPError as e:
            logger.error(f"Failed to fetch PR data: {e}")
            sys.exit(1)
        except (KeyError, IndexError, ValueError) as e:
            logger.error(f"Failed to parse GitHub data: {e}")
            sys.exit(1)

    @staticmethod
    def validate_no_broken_link():
        """
        Ensure that all the symlinks point to a file
        """
        broken_links = []

        def check_directory(directory):
            """Recursively check directory for broken symlinks"""
            try:
                for item in directory.iterdir():
                    if item.is_symlink():
                        # Check if the symlink target exists
                        if not item.exists():
                            broken_links.append(item)
                    elif item.is_dir():
                        # Recursively check subdirectories
                        try:
                            check_directory(item)
                        except PermissionError:
                            # Skip directories we can't access
                            pass
            except PermissionError:
                # Skip directories we can't access
                pass

        logger.info("Checking for broken symlinks...")
        check_directory(pathlib.Path('.'))

        if broken_links:
            logger.error("Found broken symlinks:")
            for link in broken_links:
                try:
                    target = link.readlink()
                    logger.error(f"  {link} -> {target} (broken)")
                except OSError as e:
                    logger.error(f"  {link} (error reading target: {e})")
            sys.exit(1)
        else:
            logger.info("No broken symlinks found")
            sys.exit(0)
