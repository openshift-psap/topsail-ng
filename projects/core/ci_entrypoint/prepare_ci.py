#!/usr/bin/env python3
"""
FORGE CI Preparation Module

This module handles all preparation tasks needed before executing CI operations,
including parsing GitHub PR arguments and setting up the execution environment.
"""

import logging
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import requests
import yaml

import projects.core.ci_entrypoint.fournos as fournos
import projects.core.ci_entrypoint.github.pr_args as github_pr_args
from projects.core.library import ci as ci_lib
from projects.core.library import run

IS_LIGHTWEIGHT_IMAGE = os.environ.get("FORGE_LIGHT_IMAGE")

DEFAULT_REPO_OWNER = "openshift-psap"
DEFAULT_REPO_NAME = "forge"
CI_METADATA_DIRNAME = "000__ci_metadata"


class FinishReason(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    OTHER = "other"


# Set up logging
logger = logging.getLogger(__name__)

# Dual output global state
_dual_output_state = None


class DualOutputState:
    """Manages dual output (console + file) state for proper cleanup."""

    def __init__(
        self,
        daemon_thread,
        original_stdout_fd,
        original_stderr_fd,
        write_fd,
        stop_event,
    ):
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

    artifact_dir = os.environ.get("ARTIFACT_DIR")

    if not artifact_dir:
        logger.warning("ARTIFACT_DIR not defined, not saving $ARTIFACT_DIR/run.log")
        return None

    log_file_path = Path(artifact_dir) / "run.log"

    try:
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(f"Failed to create directory: {e}")
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
    sys.stdout = os.fdopen(sys.stdout.fileno(), "w", buffering=1)
    sys.stderr = os.fdopen(sys.stderr.fileno(), "w", buffering=1)

    # Create stop event for clean thread shutdown
    stop_event = threading.Event()

    def communicate():
        import select

        with (
            open(log_file_path, "a", buffering=1) as log_file,
            os.fdopen(original_stdout_fd, "w", buffering=1) as terminal,
        ):
            try:
                while not stop_event.is_set():
                    # Use select to check if data is available with timeout
                    ready, _, _ = select.select([read_fd], [], [], 0.5)
                    if ready:
                        # Data available, read a line
                        try:
                            line = os.read(read_fd, 4096).decode("utf-8", errors="replace")
                            if not line:  # EOF
                                break
                            terminal.write(line)
                            log_file.write(line)
                            terminal.flush()
                            log_file.flush()
                        except (OSError, ValueError) as e:
                            # Pipe was closed, exit gracefully
                            logger.exception(f"Dual output thread file operations failed: {e}")
                            break
                    # If no data, loop continues and checks stop_event
            except Exception as e:
                logger.exception(f"Dual output thread failed: {e}")
                pass  # Exit gracefully on any error

    # 4. Start a background thread to act as the 'tee' process
    daemon = threading.Thread(target=communicate, daemon=True)
    daemon.start()

    # Store state for cleanup
    _dual_output_state = DualOutputState(
        daemon, original_stdout_fd, original_stderr_fd, write_fd, stop_event
    )
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
def parse_and_save_pr_arguments() -> Path | None:
    """
    Parse GitHub PR arguments and save to variable overrides file.

    Behavior depends on CI environment:
    - FOURNOS_CI=true: FOURNOS-specific PR argument parsing (to be implemented)
    - Otherwise: OpenShift CI PR argument parsing

    Returns:
        Path to saved file if successful, None otherwise
    """
    # Check which CI environment we're in
    if os.environ.get("FOURNOS_CI") == "true":
        fournos.parse_and_save_pr_arguments_fournos()
    else:
        parse_and_save_pr_arguments_ocpci()


def parse_and_save_pr_arguments_ocpci() -> Path | None:
    """
    Parse GitHub PR arguments for OpenShift CI environment.

    Returns:
        Path to saved file if successful, None otherwise
    """

    # Check if we're in a PR context
    repo_owner = os.environ.get("REPO_OWNER")
    repo_name = os.environ.get("REPO_NAME")
    pull_number_str = os.environ.get("PULL_NUMBER")
    artifact_dir = os.environ.get("ARTIFACT_DIR")

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
    test_name = os.environ.get("TEST_NAME")

    logger.info(f"Parsing GitHub PR arguments for {repo_owner}/{repo_name}#{pull_number}")

    try:
        # Save to YAML file
        artifact_path = Path(artifact_dir)
        artifact_path.mkdir(parents=True, exist_ok=True)
        (artifact_path / CI_METADATA_DIRNAME).mkdir(parents=True, exist_ok=True)

        # Parse PR arguments
        config, found_directives = github_pr_args.parse_pr_arguments(
            repo_owner=repo_owner,
            repo_name=repo_name,
            pull_number=pull_number,
            test_name=test_name,
            artifact_path=artifact_path,
        )

        output_file = artifact_path / CI_METADATA_DIRNAME / "variable_overrides.yaml"

        # Filter out help output before saving to variable overrides
        config_for_overrides = {k: v for k, v in config.items() if k != "__help_output__"}

        with open(output_file, "w") as f:
            yaml.dump(config_for_overrides, f, default_flow_style=False, sort_keys=True)

        logger.info(f"Saved PR arguments to {output_file}")
        logger.info(f"Configuration contains {len(config_for_overrides)} override(s)")

        # Save directives to text file
        pr_config_file = artifact_path / CI_METADATA_DIRNAME / "pr_config.txt"
        with open(pr_config_file, "w") as f:
            if found_directives:
                for directive in found_directives:
                    if directive.startswith("/help"):
                        # Write help text from config if available
                        if "__help_output__" in config:
                            f.write(config["__help_output__"])
                        else:
                            f.write(f"{directive}\n")
                    else:
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
    artifact_dir = os.environ.get("ARTIFACT_DIR")

    if artifact_dir:
        logger.info(f"Using ARTIFACT_DIR={artifact_dir}.")
        return

    if os.environ.get("OPENSHIFT_CI") == "true":
        raise RuntimeError("ARTIFACT_DIR not set, cannot proceed without it in OpenShift CI.")

    logger.info("ARTIFACT_DIR not set, but not running in a CI. Creating a directory for it ...")

    # Create default ARTIFACT_DIR
    default_dir = f"/tmp/forge_{datetime.now().strftime('%Y%m%d')}"
    os.environ["ARTIFACT_DIR"] = default_dir
    Path(default_dir).mkdir(parents=True, exist_ok=True)
    logger.info(f"Using ARTIFACT_DIR={default_dir} as default artifacts directory.")


def ci_banner(project: str, operation: str, args: list[str]):
    """
    Display CI execution banner with git information.

    Args:
        project: Project name being executed
        operation: Operation being executed
        args: Additional arguments
    """

    base_sha = os.environ.get("PULL_BASE_SHA", "main")
    if base_sha == "main":
        logger.info("PULL_BASE_SHA not set. Showing the last commits from main.")
    pull_sha = os.environ.get("PULL_PULL_SHA", "")
    if not pull_sha:
        logger.info("PULL_PULL_SHA not set. Showing the last commits from main.")

    logger.info(f"Git command will be: git show --quiet --oneline {base_sha}..{pull_sha}")

    try:
        result = subprocess.run(
            ["git", "show", "--quiet", "--oneline", f"{base_sha}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        logger.info(f"Git command returncode: {result.returncode}")
        logger.info(f"Git stdout: {result.stdout}")
        logger.info(f"Git stderr: {result.stderr}")

        if result.returncode == 0:
            lines = result.stdout.split("\n")[:10]  # head 10
            for line in lines:
                logger.info(line)
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
    artifact_dir = os.environ.get("ARTIFACT_DIR")
    if not artifact_dir:
        raise ValueError("ARTIFACT_DIR not set, cannot perform prechecks")

    artifact_path = Path(artifact_dir)

    # Check for existing failures
    failures_file = artifact_path / "FAILURES.txt"
    if failures_file.exists() and not os.environ.get("FORGE_IGNORE_FAILURES_FILE"):
        raise ValueError(
            f"File '{failures_file}' already exists, cannot continue. Set FORGE_IGNORE_FAILURES_FILE=1 to ignore this."
        )

    # Handle OpenShift CI PR arguments (already handled by parse_and_save_pr_arguments)
    if (
        os.environ.get("OPENSHIFT_CI") == "true"
        and os.environ.get("FORGE_JUMP_CI_INSIDE_JUMP_HOST") != "true"
    ):
        if not os.environ.get("FORGE_OPENSHIFT_CI_STEP_DIR"):
            hostname = os.environ.get("HOSTNAME", "")
            job_name_safe = os.environ.get("JOB_NAME_SAFE", "")
            if hostname and job_name_safe:
                step_dir = hostname.replace(f"{job_name_safe}-", "") + "/artifacts"
                os.environ["FORGE_OPENSHIFT_CI_STEP_DIR"] = step_dir

    # Remove any old failure markers
    old_failure = artifact_path / "FAILURE.txt"
    if old_failure.exists():
        old_failure.unlink()

    # Store git versions
    try:
        # FORGE git version
        result = subprocess.run(
            ["git", "describe", "HEAD", "--long", "--always"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        forge_version = result.stdout.strip() if result.returncode == 0 else "git missing"
        (artifact_path / CI_METADATA_DIRNAME).mkdir(parents=True, exist_ok=True)
        (artifact_path / CI_METADATA_DIRNAME / "forge.git_version").write_text(forge_version + "\n")
        logger.info(
            f"Saving FORGE git version into {artifact_path}/{CI_METADATA_DIRNAME}/forge.git_version"
        )
    except Exception as e:
        logger.warning(f"Could not store git versions: {e}")

    # Save environment variables
    try:
        (artifact_path / CI_METADATA_DIRNAME).mkdir(parents=True, exist_ok=True)
        env_file = artifact_path / CI_METADATA_DIRNAME / "env.sh"

        with open(env_file, "w") as f:
            f.write("#!/bin/bash\n")
            f.write("# Environment variables from CI execution\n")
            f.write(f"# Generated on: {datetime.now().isoformat()}\n\n")

            # Export all environment variables, sorted for consistency
            for key, value in sorted(os.environ.items()):
                # Escape shell special characters in values
                escaped_value = value.replace("'", "'\\''")
                f.write(f"export {key}='{escaped_value}'\n")

        logger.info(f"Saved environment variables to {env_file}")
    except Exception as e:
        logger.warning(f"Could not save environment variables: {e}")

    # Download PR information if available
    download_pr_information()


def download_pr_information():
    """
    Download PR information from GitHub API if available.

    Downloads PR data and comments to the CI metadata directory.
    """
    pull_number = os.environ.get("PULL_NUMBER")
    if not pull_number:
        return

    artifact_dir = os.environ.get("ARTIFACT_DIR")
    if not artifact_dir:
        logger.warning("ARTIFACT_DIR not set, cannot download PR information")
        return

    artifact_path = Path(artifact_dir)
    repo_owner = os.environ.get("REPO_OWNER", DEFAULT_REPO_OWNER)
    repo_name = os.environ.get("REPO_NAME", DEFAULT_REPO_NAME)

    pr_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls/{pull_number}"
    pr_comments_url = (
        f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues/{pull_number}/comments"
    )

    try:
        # Ensure metadata directory exists
        (artifact_path / CI_METADATA_DIRNAME).mkdir(parents=True, exist_ok=True)

        # Download PR data
        try:
            response = requests.get(pr_url, timeout=30)
            response.raise_for_status()

            with open(artifact_path / CI_METADATA_DIRNAME / "pull_request.json", "w") as f:
                f.write(response.text)
        except requests.exceptions.RequestException as e:
            logger.warning(f"Failed to download the PR from {pr_url}: {e}")

        # Download PR comments
        try:
            response = requests.get(pr_comments_url, timeout=30)
            response.raise_for_status()

            with open(artifact_path / CI_METADATA_DIRNAME / "pull_request-comments.json", "w") as f:
                f.write(response.text)

            logger.info(f"Downloaded PR #{pull_number} information from {repo_owner}/{repo_name}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Failed to download the PR comments from {pr_comments_url}: {e}")

    except Exception as e:
        logger.warning(f"Could not download PR information: {e}")


def setup_environment_variables():
    """
    Set up any additional environment variables needed for CI execution.
    """
    # Add any environment setup logic here
    logger.debug("Setting up environment variables")

    # Example: Ensure FORGE_HOME is set
    if not os.environ.get("FORGE_HOME"):
        forge_home = Path(__file__).resolve().parent.parent.parent
        os.environ["FORGE_HOME"] = str(forge_home)
        logger.debug(f"Set FORGE_HOME={forge_home}")


def validate_prerequisites():
    """
    Validate that all necessary prerequisites are available.

    Returns:
        bool: True if all prerequisites are met, False otherwise
    """
    logger.debug("Validating CI prerequisites")

    # Check for required tools
    if not shutil.which("jq"):
        raise RuntimeError("jq not found. Can't continue.")

    if IS_LIGHTWEIGHT_IMAGE:
        return

    # Check for required tools
    if not shutil.which("oc"):
        raise RuntimeError("oc not found. Can't continue.")


def wait_for_step_completion():
    """
    Wait for a dependent step to complete if PSAP_FORGE_WAIT_FOR_STEP is configured.

    Waits for the specified step's test_duration.yaml file to appear, indicating
    that the step has completed.
    """
    wait_for_step = os.environ.get("PSAP_FORGE_WAIT_FOR_STEP")
    if not wait_for_step:
        return

    # Exit early if explicitly disabled
    if wait_for_step.lower() in ("false", "no", "0", "disabled", "off"):
        logger.info(f"Step waiting disabled (PSAP_FORGE_WAIT_FOR_STEP={wait_for_step})")
        return

    artifact_dir = os.environ.get("ARTIFACT_DIR")
    if not artifact_dir:
        logger.warning("PSAP_FORGE_WAIT_FOR_STEP set but ARTIFACT_DIR not available, skipping wait")
        return

    # Construct path to the completion indicator file
    completion_file = (
        Path(artifact_dir).parent / wait_for_step / CI_METADATA_DIRNAME / "test_duration.yaml"
    )

    logger.info(f"Waiting for step '{wait_for_step}' to complete...")
    logger.info(f"Monitoring file: {completion_file}")

    step_dir = completion_file.parent.parent  # Remove /000__ci_metadata/test_duration.yaml

    def _wait_for_path(path_to_check, timeout_seconds, description, wait_action):
        """Helper function to wait for a path to exist with timeout handling."""
        wait_start = time.time()

        while not path_to_check.exists():
            elapsed = time.time() - wait_start
            if elapsed >= timeout_seconds:
                logger.error(f"❌ Timeout: {description} did not appear within {timeout_seconds}s")
                # Create failure notification
                failure_message = (
                    f"Step sync failed: {description} did not appear within {timeout_seconds}s\n"
                    f"Waited for step: {wait_for_step}\n"
                    f"Expected file: {completion_file}"
                )
                ci_lib.add_notification_file(
                    "STEP_SYNC_FAILED", failure_message, base_ci_dir=os.environ["ARTIFACT_DIR"]
                )
                return False
            logger.info(f"⏳ {wait_action} ({elapsed:.0f}s/{timeout_seconds}s)...")
            time.sleep(10)
        return True

    # First wait for the step directory to appear (1 minute timeout)
    if not _wait_for_path(
        step_dir,
        60,
        f"Step directory {step_dir}",
        f"Waiting for {wait_for_step} directory to appear",
    ):
        return

    # Then wait for the completion file to appear (10 minutes timeout)
    if not _wait_for_path(
        completion_file, 600, "Completion file", f"Waiting for {wait_for_step} to finish"
    ):
        return

    logger.info(f"✅ Step '{wait_for_step}' completed, proceeding with current step")


def generate_duration_and_timing_file(start_time: float | None, artifact_path: Path) -> str:
    """Generate duration string and timing file.

    Args:
        start_time: Unix timestamp when execution started (None if unknown)
        artifact_path: Path to artifact directory

    Returns:
        Duration string for status message
    """
    if not start_time:
        return " (duration unknown)"

    end_time = time.time()
    duration_seconds = int(end_time - start_time)
    duration_str = f" {format_duration(duration_seconds)}"

    # Generate timing file in CI metadata directory
    try:
        metadata_dir = artifact_path / CI_METADATA_DIRNAME
        metadata_dir.mkdir(parents=True, exist_ok=True)

        timing_data = {
            "start_time": {
                "timestamp": start_time,
                "iso": datetime.fromtimestamp(start_time).isoformat(),
                "human": datetime.fromtimestamp(start_time).strftime("%Y-%m-%d %H:%M:%S"),
            },
            "end_time": {
                "timestamp": end_time,
                "iso": datetime.fromtimestamp(end_time).isoformat(),
                "human": datetime.fromtimestamp(end_time).strftime("%Y-%m-%d %H:%M:%S"),
            },
            "duration": {
                "seconds": duration_seconds,
                "formatted": format_duration(duration_seconds),
            },
        }

        timing_file = metadata_dir / "test_duration.yaml"
        with open(timing_file, "w", encoding="utf-8") as f:
            yaml.dump(timing_data, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Generated timing file: {timing_file}")

    except Exception as e:
        logger.warning(f"Failed to generate timing file: {e}")

    return duration_str


def record_test_start_time(start_time: float | None = None):
    """Record test start time to test_start.yaml file.

    Writes to test_start.yaml (not test_duration.yaml) so the wait mechanism
    in wait_for_step_completion() — which polls for test_duration.yaml — is not
    triggered prematurely. test_duration.yaml is only created at step completion
    by generate_duration_and_timing_file().

    Args:
        start_time: Unix timestamp when execution started. If None, uses current time.
    """
    try:
        artifact_dir = os.environ.get("ARTIFACT_DIR")
        if not artifact_dir:
            logger.warning("ARTIFACT_DIR not set, cannot record start time")
            return

        artifact_path = pathlib.Path(artifact_dir)
        metadata_dir = artifact_path / CI_METADATA_DIRNAME
        metadata_dir.mkdir(parents=True, exist_ok=True)

        timing_file = metadata_dir / "test_start.yaml"

        # Use provided start_time or current time
        start_timestamp = start_time if start_time is not None else time.time()
        logger.info(f"Recording start time: {start_timestamp}")

        timing_data = {
            "start_time": {
                "timestamp": start_timestamp,
                "iso": datetime.fromtimestamp(start_timestamp).isoformat(),
                "human": datetime.fromtimestamp(start_timestamp).strftime("%Y-%m-%d %H:%M:%S"),
            }
        }

        with open(timing_file, "w", encoding="utf-8") as f:
            yaml.dump(timing_data, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Recorded test start time: {timing_file}")

    except Exception as e:
        logger.warning(f"Failed to record test start time: {e}")


def save_pip_freeze():
    """Save pip freeze output to metadata directory.

    Runs 'uv pip freeze' and saves the output to pip_freeze.txt in the CI metadata directory.
    """
    try:
        artifact_dir = os.environ.get("ARTIFACT_DIR")
        if not artifact_dir:
            logger.warning("ARTIFACT_DIR not set, cannot save pip freeze")
            return

        artifact_path = pathlib.Path(artifact_dir)
        metadata_dir = artifact_path / CI_METADATA_DIRNAME
        metadata_dir.mkdir(parents=True, exist_ok=True)

        pip_freeze_file = metadata_dir / "pip_freeze.txt"

        # Check if uv is available
        if not shutil.which("uv"):
            logger.warning("uv not found, cannot run pip freeze")
            return

        # Run uv pip freeze using the run library
        result = run.run(
            "uv pip freeze",
            capture_stdout=True,
            check=False,
        )

        if result.returncode == 0:
            with open(pip_freeze_file, "w", encoding="utf-8") as f:
                f.write(f"# Generated on: {datetime.now().isoformat()}\n")
                f.write("# Command: uv pip freeze\n\n")
                f.write(result.stdout)

            logger.info(f"Saved pip freeze output to {pip_freeze_file}")
        else:
            logger.warning(f"uv pip freeze failed (exit code {result.returncode}): {result.stderr}")
            # Save the error information
            with open(pip_freeze_file, "w", encoding="utf-8") as f:
                f.write(f"# Generated on: {datetime.now().isoformat()}\n")
                f.write("# Command: uv pip freeze\n")
                f.write(f"# ERROR: Command failed with exit code {result.returncode}\n")
                f.write(f"# STDERR: {result.stderr}\n")

    except Exception as e:
        logger.error(f"Failed to save pip freeze: {e}")


def prepare(
    verbose: bool = False,
    project: str = "",
    operation: str = "",
    args: list[str] = None,
):
    """
    Execute all CI preparation tasks.

    Args:
        verbose: Enable verbose output
        project: Project name being executed
        operation: Operation being executed
        args: Additional arguments"""
    if args is None:
        args = []

    logger.info("Starting CI preparation")

    if forge_home := os.environ.get("FORGE_HOME"):
        logger.info(f"Switching to FORGE_HOME={forge_home} ...")
        os.chdir(forge_home)
    elif os.environ.get("FORGE_LIGHT_IMAGE"):
        os.chdir("/app")

    try:
        # Display CI banner
        if project and operation:
            ci_banner(project, operation, args)

        # Set up environment variables
        setup_environment_variables()

        # Record test start time
        record_test_start_time()

        # Perform prechecks
        system_prechecks()

        # Validate prerequisites
        validate_prerequisites()

        # Parse and save PR arguments if in PR context
        parse_and_save_pr_arguments()

        # Save pip freeze output to metadata
        save_pip_freeze()

        # Wait for dependent step completion if configured
        wait_for_step_completion()

        logger.info("CI preparation completed successfully")

    except Exception as e:
        logger.error(f"CI preparation failed: {e}")
        raise


def format_duration(duration_seconds: int) -> str:
    """Format duration in seconds to human readable format."""
    hours = duration_seconds // 3600
    minutes = (duration_seconds % 3600) // 60
    seconds = duration_seconds % 60

    parts = []
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds > 0:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")

    if not parts:
        return "0 seconds"

    return ", ".join(parts)


def postchecks(
    project: str,
    operation: str,
    start_time: float | None,
    finish_reason: FinishReason,
    args: list[str] | None = None,
) -> str:
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
    artifact_dir = os.environ.get("ARTIFACT_DIR")

    # Build command string for display
    command_str = f"{project} {operation}"
    if args:
        command_str += f" {' '.join(args)}"

    if not artifact_dir:
        # No artifact dir, just return simple status
        return (
            f"✅ {command_str} completed successfully"
            if finish_reason == FinishReason.SUCCESS
            else f"❌ {command_str} failed"
        )

    artifact_path = Path(artifact_dir)
    if finish_reason == FinishReason.SUCCESS:
        pass
    elif finish_reason == FinishReason.ERROR:
        # Find all FAILURE files and consolidate them
        failure_files = list(artifact_path.glob("**/FAILURE.txt"))
        failures_file = artifact_path / "FAILURES.txt"

        with failures_file.open("w") as f:
            for failure_file in sorted(failure_files):
                try:
                    f.write(f"## {failure_file} \n")
                    f.write(failure_file.read_text().strip())
                    f.write("\n")
                    f.write("\n")
                except Exception as e:
                    f.write(f"{failure_file} | Error reading file: {e}\n")

    else:
        # placeholder for future exist status (eg, performance regression, flake, ...)
        logger.warning(f"postchecks: unhandled finish reason: {finish_reason}")

    # Generate duration string and timing file
    duration_str = generate_duration_and_timing_file(start_time, artifact_path)

    # Check if there were failures
    failures_file = artifact_path / "FAILURES.txt"
    if finish_reason != FinishReason.SUCCESS or (
        failures_file.exists() and failures_file.stat().st_size > 0
    ):
        status = f"❌ Execution of '{command_str}' failed after{duration_str}."
    else:
        status = f"✅ Execution of '{command_str}' succeeded after{duration_str}."

    return status
