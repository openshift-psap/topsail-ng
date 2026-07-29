#!/usr/bin/env python3
"""
Shared CI utilities for FORGE projects

Provides common CI functionality including error handling, logging,
and tooling setup for consistent behavior across all projects.
"""

import functools
import logging
import pathlib
import sys
import traceback

import click

from projects.core.dsl import toolbox as dsl_toolbox
from projects.core.dsl.runtime import TaskExecutionError
from projects.core.library import env

logger = logging.getLogger(__name__)


# CI metadata directory path
def get_ci_metadata_dir(base_ci_dir=None):
    """Get the CI metadata directory path.

    Args:
        base_ci_dir: Optional base directory. If not provided, uses env.BASE_ARTIFACT_DIR

    Returns:
        Path to the CI metadata directory

    Raises:
        ValueError: If both base_ci_dir and env.BASE_ARTIFACT_DIR are None
    """
    if base_ci_dir is not None:
        return pathlib.Path(base_ci_dir) / "000__ci_metadata"

    if env.BASE_ARTIFACT_DIR is not None:
        return env.BASE_ARTIFACT_DIR / "000__ci_metadata"

    raise ValueError(
        "Cannot determine CI metadata directory: both base_ci_dir parameter and env.BASE_ARTIFACT_DIR are None"
    )


class HelpfulGroup(click.Group):
    """
    A Click group that automatically shows help when a command is not found.
    """

    def get_command(self, ctx, cmd_name):
        rv = click.Group.get_command(self, ctx, cmd_name)
        if rv is not None:
            return rv

        # Command not found - show help
        print(f"Error: No such command '{cmd_name}'.\n", file=sys.stderr)
        print(ctx.get_help(), file=sys.stderr)
        ctx.exit(2)


def handle_ci_exception(e: Exception) -> None:
    """
    Handle CI exceptions with comprehensive logging and failure file creation.

    Args:
        e: The exception that occurred
    """

    # Display error on screen and write to FAILURES file
    _display_error_summary(e)


def _display_error_summary(e: Exception) -> None:
    """Display a comprehensive error summary on screen and write to FAILURES file."""

    # Build the error summary as a list of lines
    logger.error("")
    logger.error("=" * 80)
    logger.error("🚨 CI EXECUTION FAILED")
    logger.error("=" * 80)
    logger.error("")

    summary_lines = []
    if isinstance(e, TaskExecutionError):
        summary_lines += dsl_toolbox.get_task_execution_error(e)
        summary_lines.append("")
        summary_lines.append("---")

    # mind that any thing below a '---\n' will be cut in the notification

    # Add the full stacktrace
    summary_lines.append(f"--- 📍{e.__class__.__name__} STACKTRACE ---")
    summary_lines.append(f"--- 📍{str(e)}")
    summary_lines.append("")

    full_traceback = traceback.format_exc().splitlines()
    # Add each line of the stacktrace with proper indentation
    for line in full_traceback:
        summary_lines.append(f"   {line}")

    if not isinstance(e, TaskExecutionError):
        summary_lines.append("---")  # add the details marker after the stacktrace

    # Display on screen
    for line in summary_lines:
        logger.error(line)

    # Attempt to write to FAILURES file if environment is initialized
    try:
        if env.ARTIFACT_DIR is not None:
            _write_error_summary_to_file(summary_lines)
        else:
            logger.warning(
                "Environment not fully initialized - error summary displayed above but not saved to file"
            )
    except Exception as write_error:
        logger.warning(f"Could not save error summary to file: {write_error}")

    logger.error("=" * 80)


def _write_error_summary_to_file(summary_lines: list) -> None:
    """Write the error summary to FAILURE file."""
    failures_file = env.ARTIFACT_DIR / "FAILURE.txt"

    try:
        content = "\n".join(summary_lines)
        failures_file.write_text(content + "\n")
        logger.info(f"Error summary written to: {failures_file}")
    except Exception as write_error:
        logger.error(f"Failed to write error summary to file: {write_error}")


def safe_ci_command(command_func):
    """
    Decorator/wrapper for CI commands to provide consistent error handling.

    Args:
        command_func: Function to execute safely
    """

    @functools.wraps(command_func)
    def wrapper(*args, **kwargs):
        try:
            exit_code = command_func(*args, **kwargs)
            sys.exit(exit_code)
        except Exception as e:
            handle_ci_exception(e)
            sys.exit(1)

    return wrapper


def safe_ci_entrypoint(command_func):
    """
    Decorator/wrapper for CI entrypoints to provide consistent error handling and exit status saving.

    Saves the return code to exit_status.yaml in the CI metadata directory.

    Args:
        command_func: Function to execute safely
    """

    import yaml

    @functools.wraps(command_func)
    def wrapper(*args, **kwargs):
        exit_code = 0
        reason = None
        try:
            result = command_func(*args, **kwargs)
            if result is None:
                exit_code = 0
            elif isinstance(result, tuple) and len(result) == 2:
                exit_code, reason = result
            else:
                exit_code = result
        except Exception as e:
            handle_ci_exception(e)
            exit_code = 1
            reason = str(e)

        # Save exit status to YAML file
        try:
            metadata_dir = get_ci_metadata_dir()
            metadata_dir.mkdir(parents=True, exist_ok=True)

            exit_status_file = metadata_dir / "exit_status.yaml"
            exit_status_data = {"return_code": exit_code}
            if reason is not None:
                exit_status_data["reason"] = reason

            with open(exit_status_file, "w", encoding="utf-8") as f:
                yaml.dump(exit_status_data, f, default_flow_style=False)

            if reason:
                logger.info(
                    f"Exit status saved: {exit_status_file} (return_code: {exit_code}, reason: {reason})"
                )
            else:
                logger.info(f"Exit status saved: {exit_status_file} (return_code: {exit_code})")
        except Exception as save_error:
            logger.warning(f"Failed to save exit status: {save_error}")

        sys.exit(exit_code)

    return wrapper


def add_notification_file(
    name: str, message: str, start_index: int = 0, base_ci_dir=None
) -> str | None:
    """
    Add a notification file in CI metadata notifications directory with the next available index.

    Args:
        name: Base name for the notification file (without extension)
        message: Content to write to the file
        start_index: Starting index to search for available slot
        base_ci_dir: Optional base directory. If not provided, uses env.BASE_ARTIFACT_DIR

    Returns:
        Path to the created notification file, or None if creation failed
    """
    metadata_dir = get_ci_metadata_dir(base_ci_dir)
    notifications_dir = metadata_dir / "notifications"
    notifications_dir.mkdir(parents=True, exist_ok=True)

    # Find the next available index
    index = start_index
    while True:
        filename = f"{index:03d}__{name}.txt"
        file_path = notifications_dir / filename

        if not file_path.exists():
            break

        index += 1

    # Write the notification file
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(message)

        logger.info(f"Created notification file: {file_path}")
        logger.info(f"{message}")
        return str(file_path)

    except Exception as e:
        logger.error(f"Failed to create notification file {file_path}: {e}")
        return None


def safe_ci_function(command_func):
    """
    Decorator/wrapper for CI commands to provide consistent error handling.
    This version does NOT exit on success.

    Args:
        command_func: Function to execute safely
    """

    @functools.wraps(command_func)
    def wrapper(*args, **kwargs):
        try:
            return command_func(*args, **kwargs)
        except Exception as e:
            handle_ci_exception(e)
            sys.exit(1)

    return wrapper
