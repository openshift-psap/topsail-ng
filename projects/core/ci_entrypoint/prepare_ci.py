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
import time
import subprocess
import glob
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from enum import StrEnum

IS_LIGHTWEIGHT_IMAGE = os.environ.get("TOPSAIL_LIGHT_IMAGE")

class FinishReason(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    OTHER = "other"

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

def load_notification_module():
    # Import notifications module
    try:
        # Add the notifications directory to Python path
        notifications_dir = Path(__file__).parent.parent / "notifications"
        if str(notifications_dir) not in sys.path:
            sys.path.insert(0, str(notifications_dir))

        import projects.core.notifications.send as send
        logger.info("Notifications module imported successfully")
        return send.send_job_completion_notification
    except ImportError as e:
        logger.exception(f"Notifications module not available: {e}")
        return None

# Dual output global state
_dual_output_state = None

class DualOutputState:
    """Manages dual output (console + file) state for proper cleanup."""
    def __init__(self, daemon_thread, original_stdout_fd, original_stderr_fd, write_fd, stop_event):
        self.daemon_thread = daemon_thread
        self.original_stdout_fd = original_stdout_fd
        self.original_stderr_fd = original_stderr_fd
        self.write_fd = write_fd
        self.stop_event = stop_event

def setup_dual_output():
    """
    Set up stdout/stderr to write to both console and log file.

    If ARTIFACT_DIR is set, all output will go to both console and $ARTIFACT_DIR/run.log
    This is permanent for the rest of the program execution.

    Returns:
        DualOutputState object for cleanup, or None if setup failed
    """
    global _dual_output_state

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

    # 1. Save the original terminal stdout/stderr so we can restore them
    original_stdout_fd = os.dup(sys.stdout.fileno())
    original_stderr_fd = os.dup(sys.stderr.fileno())

    # 2. Create a pipe: (read_fd, write_fd)
    read_fd, write_fd = os.pipe()

    # 3. Replace the process's ACTUAL stdout and stderr with the write-end of our pipe
    os.dup2(write_fd, sys.stdout.fileno())
    os.dup2(write_fd, sys.stderr.fileno())

    # 4. Make stdout and stderr line-buffered (unbuffered for text streams)
    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
    sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

    # Create stop event for clean thread shutdown
    stop_event = threading.Event()

    def communicate():
        import select
        with open(log_file_path, "a", buffering=1) as log_file, os.fdopen(original_stdout_fd, "w", buffering=1) as terminal:
            try:
                while not stop_event.is_set():
                    # Use select to check if data is available with timeout
                    ready, _, _ = select.select([read_fd], [], [], 0.5)
                    if ready:
                        # Data available, read a line
                        try:
                            line = os.read(read_fd, 4096).decode('utf-8', errors='replace')
                            if not line:  # EOF
                                break
                            terminal.write(line)
                            log_file.write(line)
                            terminal.flush()
                            log_file.flush()
                        except (OSError, ValueError) as e:
                            # Pipe was closed, exit gracefully
                            logging.exception(f"Dual output thread file operations failed: {e}")
                            break
                    # If no data, loop continues and checks stop_event
            except Exception as e:
                logging.exception(f"Dual output thread failed: {e}")
                pass  # Exit gracefully on any error

    # 4. Start a background thread to act as the 'tee' process
    daemon = threading.Thread(target=communicate, daemon=True)
    daemon.start()

    # Store state for cleanup
    _dual_output_state = DualOutputState(daemon, original_stdout_fd, original_stderr_fd, write_fd, stop_event)
    return _dual_output_state


def shutdown_dual_output():
    """
    Shutdown dual output system and flush all buffers.
    """
    global _dual_output_state

    if not _dual_output_state:
        return

    try:
        # Flush any pending output
        sys.stdout.flush()
        sys.stderr.flush()

        # Signal the daemon thread to stop
        _dual_output_state.stop_event.set()

        # Wait for daemon thread to finish processing (so files get flushed)
        _dual_output_state.daemon_thread.join(timeout=3.0)

        if _dual_output_state.daemon_thread.is_alive():
            print("Warning: Dual output daemon thread did not finish in time")

    except Exception as e:
        print(f"Warning: Error during dual output shutdown: {e}")

    # Clear state
    _dual_output_state = None

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
        config, found_directives = parse_pr_arguments(
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

        # Save directives to text file
        pr_config_file = artifact_path / "pr_config.txt"
        with open(pr_config_file, 'w') as f:
            f.write(f"# GitHub PR Directives Found\n")
            f.write(f"# Repository: {repo_owner}/{repo_name}#{pull_number}\n")
            f.write(f"# Test: {test_name}\n")
            f.write(f"# Generated at: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n\n")

            if found_directives:
                for directive in found_directives:
                    f.write(f"{directive}\n")
            else:
                f.write("# No directives found\n")

        logger.info(f"Saved PR directives to {pr_config_file}")
        logger.info(f"Found {len(found_directives)} directive(s)")

        return output_file

    except Exception as e:
        logger.exception(f"Failed to parse PR arguments: {e}")
        raise


def precheck_artifact_dir() -> bool:
    """
    Ensure ARTIFACT_DIR is set up and accessible.

    Returns:
        bool: True if ARTIFACT_DIR is ready, False otherwise
    """
    artifact_dir = os.environ.get('ARTIFACT_DIR')

    if artifact_dir:
        logger.info(f"Using ARTIFACT_DIR={artifact_dir}.")
        return

    if os.environ.get('OPENSHIFT_CI') == 'true':
        raise RuntimeError("ARTIFACT_DIR not set, cannot proceed without it in OpenShift CI.")

    logger.info("ARTIFACT_DIR not set, but not running in a CI. Creating a directory for it ...")

    # Create default ARTIFACT_DIR
    default_dir = f"/tmp/topsail_{datetime.now().strftime('%Y%m%d')}"
    os.environ['ARTIFACT_DIR'] = default_dir
    Path(default_dir).mkdir(parents=True, exist_ok=True)
    logger.info(f"Using ARTIFACT_DIR={default_dir} as default artifacts directory.")


def ci_banner(project: str, operation: str, args: List[str]):
    """
    Display CI execution banner with git information.

    Args:
        project: Project name being executed
        operation: Operation being executed
        args: Additional arguments
    """
    print(f"""\
===> Running PSAP CI Test suite <===
===> {project} {operation} {' '.join(args)} <===
""")

    base_sha = os.environ.get("PULL_BASE_SHA", "main")
    if base_sha == "main":
        logger.warning(f"PULL_BASE_SHA not set. Showing the last commits from main.")
    pull_sha = os.environ.get("PULL_PULL_SHA", "")
    if not pull_sha:
        logger.warning(f"PULL_PULL_SHA not set. Showing the last commits from main.")

    logger.info(f"Git command will be: git show --quiet --oneline {base_sha}..{pull_sha}")

    try:
        result = subprocess.run(
            ["git", "show", "--quiet", "--oneline", f"{base_sha}"],
            capture_output=True,
            text=True,
            timeout=10
        )
        logger.info(f"Git command returncode: {result.returncode}")
        logger.info(f"Git stdout: {result.stdout}")
        logger.info(f"Git stderr: {result.stderr}")

        if result.returncode == 0:
            lines = result.stdout.split('\n')[:10]  # head 10
            for line in lines:
                logging.info(line)
        else:
            logger.warning("Could not access git history (main..) ...")
    except Exception as e:
        logger.warning(f"Could not access git history: {e}")


def system_prechecks() -> bool:
    """
    Perform pre-execution checks and setup.

    Returns:
        bool: True if all prechecks pass, False otherwise
    """
    artifact_dir = os.environ.get('ARTIFACT_DIR')
    if not artifact_dir:
        raise ValueError("ARTIFACT_DIR not set, cannot perform prechecks")

    artifact_path = Path(artifact_dir)

    # Check for existing failures
    failures_file = artifact_path / "FAILURES"
    if failures_file.exists() and not os.environ.get("TOPSAIL_IGNORE_FAILURES_FILE"):
        raise ValueError(f"File '{failures_file}' already exists, cannot continue. Set TOPSAIL_IGNORE_FAILURES_FILE=1 to ignore this.")

    # Handle OpenShift CI PR arguments (already handled by parse_and_save_pr_arguments)
    if (os.environ.get('OPENSHIFT_CI') == 'true' and
        os.environ.get('TOPSAIL_LOCAL_CI_MULTI') != 'true' and
        os.environ.get('TOPSAIL_JUMP_CI_INSIDE_JUMP_HOST') != 'true'):

        if not os.environ.get('TOPSAIL_OPENSHIFT_CI_STEP_DIR'):
            hostname = os.environ.get('HOSTNAME', '')
            job_name_safe = os.environ.get('JOB_NAME_SAFE', '')
            if hostname and job_name_safe:
                step_dir = hostname.replace(f"{job_name_safe}-", "") + "/artifacts"
                os.environ['TOPSAIL_OPENSHIFT_CI_STEP_DIR'] = step_dir

    # Remove any old failure markers
    old_failure = artifact_path / "FAILURE"
    if old_failure.exists():
        old_failure.unlink()

    # Store git versions
    try:
        # TOPSAIL git version
        result = subprocess.run(
            ["git", "describe", "HEAD", "--long", "--always"],
            capture_output=True,
            text=True,
            timeout=10
        )
        topsail_version = result.stdout.strip() if result.returncode == 0 else "git missing"
        (artifact_path / "topsail.git_version").write_text(topsail_version + "\n")
        logger.info(f"Saving TOPSAIL git version into {artifact_path}/topsail.git_version")

        # Matrix-benchmarking git version (if exists)
        matbench_dir = Path(__file__).parent.parent.parent / "matrix_benchmarking" / "subproject"
        if matbench_dir.exists():
            result = subprocess.run(
                ["git", "-C", str(matbench_dir), "describe", "HEAD", "--long", "--always"],
                capture_output=True,
                text=True,
                timeout=10
            )
            matbench_version = result.stdout.strip() if result.returncode == 0 else "git missing"
            (artifact_path / "matbench.git_version").write_text(matbench_version + "\n")
    except Exception as e:
        logger.warning(f"Could not store git versions: {e}")

    # Download PR information if available
    pull_number = os.environ.get('PULL_NUMBER')
    if pull_number:
        repo_owner = os.environ.get('REPO_OWNER', 'openshift-psap')
        repo_name = os.environ.get('REPO_NAME', 'topsail-ng')

        pr_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls/{pull_number}"
        pr_comments_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues/{pull_number}/comments"

        try:
            # Download PR data
            result = subprocess.run(
                ["curl", "-sSf", pr_url, "-o", str(artifact_path / "pull_request.json")],
                timeout=30
            )
            if result.returncode != 0:
                logger.warning(f"Failed to download the PR from {pr_url}")

            # Download PR comments
            result = subprocess.run(
                ["curl", "-sSf", pr_comments_url, "-o", str(artifact_path / "pull_request-comments.json")],
                timeout=30
            )
            if result.returncode != 0:
                logger.warning(f"Failed to download the PR comments from {pr_comments_url}")
        except Exception as e:
            logger.warning(f"Could not download PR information: {e}")


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

    # Check for required tools
    if not shutil.which('jq'):
        raise RuntimeError("jq not found. Can't continue.")

    if IS_LIGHTWEIGHT_IMAGE:
        return

    # Check for required tools
    if not shutil.which('oc'):
        raise RuntimeError("oc not found. Can't continue.")


def prepare(verbose: bool = False, project: str = "", operation: str = "", args: List[str] = None):
    """
    Execute all CI preparation tasks.

    Args:
        verbose: Enable verbose output
        project: Project name being executed
        operation: Operation being executed
        args: Additional arguments    """
    if args is None:
        args = []

    logger.info("Starting CI preparation")

    if topsail_home := os.environ.get("TOPSAIL_HOME"):
        logger.info(f"Switching to TOPSAIL_HOME={topsail_home} ...")
        os.chdir(topsail_home)
    elif os.environ.get("TOPSAIL_LIGHT_IMAGE"):
        os.chdir("/app")

    try:
        # Set up ARTIFACT_DIR
        precheck_artifact_dir()

        # Display CI banner
        if project and operation:
            ci_banner(project, operation, args)

        # Set up environment variables
        setup_environment_variables()

        # Perform prechecks
        system_prechecks()

        # Validate prerequisites
        validate_prerequisites()

        # Parse and save PR arguments if in PR context
        pr_args_file = parse_and_save_pr_arguments()
        if pr_args_file and verbose:
            logger.info(f"PR arguments saved to: {pr_args_file}")
        elif pr_args_file:
            logger.debug(f"PR arguments saved to: {pr_args_file}")

        logger.info("CI preparation completed successfully")

    except Exception as e:
        logger.error(f"CI preparation failed: {e}")
        raise


def format_duration(duration_seconds: int) -> str:
    """Format duration in seconds to human readable format."""
    hours = duration_seconds // 3600
    minutes = (duration_seconds % 3600) // 60
    seconds = duration_seconds % 60
    return f"after {hours:02d} hours {minutes:02d} minutes {seconds:02d} seconds"

def send_notification(project: str, operation: str, finish_reason: FinishReason, duration: str):
    send_job_completion_notification = load_notification_module()
    if not send_job_completion_notification:
        logger.info("Notifications module not available, skipping notification sending")
        return
    try:
        # Determine notification parameters
        success = finish_reason == FinishReason.SUCCESS
        notification_status = f"Test of '{project} {operation}' {('succeeded' if success else 'failed')}{duration}"

        # Skip notifications for successful non-test steps
        if success and operation != "test":
            logger.info(f"Skipping notification for successful '{operation}' step (only 'test' steps notify on success)")
            return

        # Enable GitHub notifications by default, Slack can be enabled via environment variable
        github_notifications = True
        slack_notifications = True

        # Check for dry run mode
        dry_run = os.environ.get('TOPSAIL_NOTIFICATION_DRY_RUN', 'false').lower() == 'true'

        logger.info(f"Sending notifications - finish_reason: {finish_reason} | GitHub: {github_notifications}, Slack: {slack_notifications}, dry_run: {dry_run}")

        # Send the notification
        notification_failed = send_job_completion_notification(
            finish_reason=finish_reason,
            status=notification_status,
            github=github_notifications,
            slack=slack_notifications,
            dry_run=dry_run
        )
        if notification_failed:
            logger.warning("Some notifications failed to send")
        else:
            logger.info("Notifications sent successfully")

    except Exception as e:
        logger.exception(f"Failed to send notifications")
        # Don't fail the entire job if notifications fail

def postchecks(project: str, operation: str, start_time: Optional[float], finish_reason: FinishReason, args: Optional[List[str]] = None) -> str:
    """
    Post-execution checks and status reporting.

    Args:
        project: Project name that was executed
        operation: Operation that was executed
        start_time: Unix timestamp when execution started (None if unknown)
        success: False for failure, "True" for normal completion

    Returns:
        Status message string
    """
    artifact_dir = os.environ.get('ARTIFACT_DIR')

    if not artifact_dir:
        # No artifact dir, just return simple status
        return f"✅ {project} {operation} completed successfully" \
            if finish_reason == FinishReason.SUCCESS \
               else f"❌ {project} {operation} failed"

    artifact_path = Path(artifact_dir)
    if finish_reason == FinishReason.SUCCESS:
        pass
    elif finish_reason == FinishReason.ERROR:
        # Find all FAILURE files and consolidate them
        failure_files = list(artifact_path.glob("**/FAILURE"))
        failures_file = artifact_path / "FAILURES"

        with failures_file.open("w") as f:
            for failure_file in sorted(failure_files):
                try:
                    f.write(f"{failure_file} | ")
                    f.write(failure_file.read_text().strip())
                    f.write("\n")
                except Exception as e:
                    f.write(f"{failure_file} | Error reading file: {e}\n")

    else:
        # placeholder for future exist status (eg, performance regression, flake, ...)
        logger.warning(f"postchecks: unhandled finish reason: {finish_reason}")

    # Normal exit handling
    duration_str = ""
    if start_time:
        end_time = time.time()
        duration_seconds = int(end_time - start_time)
        duration_str = f" {format_duration(duration_seconds)}"
    else:
        duration_str = " (duration unknown)"

    # Check if there were failures
    failures_file = artifact_path / "FAILURES"
    if finish_reason != FinishReason.SUCCESS or (failures_file.exists() and failures_file.stat().st_size > 0):
        status = f"❌ Test of '{project} {operation}' failed{duration_str}."
    else:
        status = f"✅ Test of '{project} {operation}' succeeded{duration_str}."

    # Write status to FINISHED file
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    finished_content = f"{timestamp} {status}"
    (artifact_path / "FINISHED").write_text(finished_content + "\n")


    # Send notifications for job completion
    # Get the actual step from args (like "test", "lock_cluster", "prepare")
    actual_step = args[0] if args and len(args) > 0 else operation
    send_notification(project, actual_step, finish_reason, duration_str)

    # Properly shutdown dual output to flush all buffers and terminate daemon
    shutdown_dual_output()

    return status
