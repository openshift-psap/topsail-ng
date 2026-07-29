"""
Notification handling for Caliper export operations.

This module provides notification functionality for export completion,
including GitHub notifications and Slack notifications via project providers.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from projects.caliper.orchestration.censoring import censor_text
from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME
from projects.core.library import ci as ci_lib
from projects.core.library import config, env
from projects.core.library.step_status import StepStatus

logger = logging.getLogger(__name__)


def _censor_notification_text(text: str, verbose: bool = False) -> str:
    """
    Censor sensitive content in notification text using caliper orchestration.

    Args:
        text: The notification text to censor
        verbose: Enable verbose logging

    Returns:
        Censored notification text with sensitive content replaced
    """
    return censor_text(text, verbose=verbose)


def send_notification(
    artifact_dir: Path | None,
    status: dict[str, Any],
    notification_provider=None,
    dry_run: bool = False,
) -> bool:
    """Send job completion notifications based on caliper export status.

    Args:
        artifact_dir: Directory to browse to find the artifacts
        status: Caliper export status object containing backend results and metadata
        notification_provider: Optional per-project SlackNotificationProvider instance
        dry_run: If True, only build and log notification content without sending

    Returns:
        bool: True if notifications were sent successfully, False otherwise
    """
    # Extract notification parameters from status object
    project = config.project.get_config("project.name")
    finish_reason = _extract_finish_reason_from_status(status)

    # Build enhanced notification with fournos job info and artifact links
    notification_status, notification_success = _build_enhanced_notification(
        artifact_dir, project, finish_reason, status
    )

    # Apply censoring to notification content before sending
    notification_status = _censor_notification_text(notification_status, verbose=dry_run)

    # Send actual notifications
    if dry_run:
        logger.info("DRY RUN: Would send notification")
        logger.info(f"DRY RUN: Notification content:\n{notification_status}")
    else:
        logger.info("Sending notification ...")

    # Write notification to file for GitHub pickup (always generate, even in dry-run)
    try:
        if env.ARTIFACT_DIR:
            notification_file = Path(env.ARTIFACT_DIR) / "NOTIFICATION-github.md"
            with open(notification_file, "w", encoding="utf-8") as f:
                f.write(notification_status + "\n")
            if dry_run:
                logger.info(f"DRY RUN: Generated notification file {notification_file}")
            else:
                logger.info(f"Wrote export notification file {notification_file}")
        else:
            logger.warning("ARTIFACT_DIR not available, skipping notification file")
    except Exception as e:
        logger.exception(f"Failed to write notification file: {e}")

    # Actually send notification through GitHub API
    try:
        from projects.core.notifications.send import send_notification as send_github_notification

        # Get notification vault from configuration
        notification_vault = None
        try:
            notification_config = config.project.get_config("caliper.export.notifications", {})
            notification_vault = notification_config.get("vault")
            if notification_vault:
                logger.info(f"Using notification vault from config: {notification_vault}")
        except Exception as e:
            logger.warning(f"Failed to get notification vault from config: {e}")

        success = send_github_notification(
            message=notification_status,
            github=True,
            slack=False,
            dry_run=dry_run,
            notification_vault=notification_vault,
        )
        if success:
            logger.info("Successfully sent GitHub notification")
        else:
            logger.error("GitHub notification sending failed")
            notification_success = False
    except Exception as e:
        logger.error(f"Failed to send GitHub notification: {e}")
        notification_success = False

    # Per-project Slack notification via provider
    if notification_provider:
        if not dry_run:
            try:
                from projects.core.notifications.provider import NotificationContext

                artifact_dir = Path(env.ARTIFACT_DIR) if env.ARTIFACT_DIR else None
                context = NotificationContext(
                    status=status,
                    finish_reason=str(finish_reason),
                    project_name=project or "unknown",
                    pr_number=os.environ.get("PULL_NUMBER"),
                    job_type=os.environ.get("JOB_TYPE"),
                    artifact_dir=artifact_dir,
                )
                ok = notification_provider.notify(context)
                if ok:
                    logger.info("Successfully sent per-project Slack notification")
                else:
                    logger.warning("Per-project Slack notification failed")
                    notification_success = False
            except Exception as e:
                logger.warning(f"Failed to send per-project Slack notification: {e}")
                notification_success = False
        else:
            logger.info("DRY RUN: Would send per-project Slack notification")

    return notification_success


def _get_project_and_args(project: str) -> tuple[str, str]:
    """Extract project name and args from fournos job or config."""
    fjob_project = project
    fjob_args_str = ""

    try:
        metadata_dir = ci_lib.get_ci_metadata_dir()
        fournos_fjob_path = metadata_dir / "fournos_fjob.yaml"
        if not fournos_fjob_path.exists():
            return fjob_project, fjob_args_str

        with open(fournos_fjob_path, encoding="utf-8") as f:
            fjob_data = yaml.safe_load(f)

        display_name = fjob_data.get("spec", {}).get("displayName", "")
        if not display_name:
            return fjob_project, fjob_args_str

        parts = display_name.split()
        if not parts:
            return fjob_project, fjob_args_str

        fjob_project = parts[0]
        fjob_args_str = " ".join(parts[1:]) if len(parts) > 1 else ""
    except Exception as e:
        logger.warning(f"Failed to read fournos job for project/args: {e}")

    if fjob_args_str:
        fjob_args_str = f"with `{fjob_args_str}`"

    return fjob_project, fjob_args_str


def _extract_finish_reason_from_status(status: dict[str, Any]) -> str:
    """Extract finish reason from status."""
    if not status.get("success", False):
        return "failed"
    elif status.get("censoring_occurred", False):
        return "completed with censoring"
    else:
        return "completed"


def _build_enhanced_notification(
    artifact_dir: Path,
    project: str,
    finish_reason: str,
    status: dict[str, Any],
) -> tuple[str, bool]:
    """Build enhanced notification with fournos job config and artifact links."""
    fjob_project, fjob_args_str = _get_project_and_args(project)

    status_emoji = "✅" if status.get("success", False) else "❌"
    if status.get("censoring_occurred", False):
        status_emoji = "⚠️"

    if finish_reason == "failed":
        status_emoji = "❌"

    base_status = f"**{status_emoji} Execution of `{fjob_project}` {fjob_args_str} {status_emoji}**"
    notification_parts = [base_status]

    # Add job abort message right below overall status if applicable
    shutdown_status = status.get("job_shutdown")
    if shutdown_status and shutdown_status.get("is_aborted"):
        shutdown_value = shutdown_status.get("shutdown_value", "Stop")
        notification_parts.append(f"🛑 **JOB ABORTED** - `spec.shutdown={shutdown_value}`")

    notification_parts.append("---")
    notification_parts.append("")

    execution_engine_config = _get_execution_engine_config()
    if execution_engine_config:
        notification_parts.append("**Execution Engine Configuration**")
        notification_parts.append(execution_engine_config)

    notification_success = True
    try:
        artifact_links, mlflow_run_url = _extract_artifact_links(status)

        test_status_section = _extract_test_status_section(status)
        step_status = _get_step_status_section(artifact_dir, mlflow_run_url)
        postprocess_status_links = _get_postprocess_status_links(artifact_dir, mlflow_run_url)

        if test_status_section:
            notification_parts.append("")
            notification_parts.append("---")
            notification_parts.extend(test_status_section)

        if artifact_links:
            notification_parts.append("")
            notification_parts.append("---")
            notification_parts.append("**Artifact Links**")
            notification_parts.extend([f"* {link}" for link in artifact_links])
        else:
            notification_parts.append("**Artifact Links:** No direct links available")

        if step_status:
            notification_parts.append("")
            notification_parts.append("---")
            notification_parts.append("**Step details**")
            for link in step_status:
                notification_parts.append(link)

        if postprocess_status_links:
            notification_parts.append("")
            notification_parts.append("---")
            notification_parts.append("**Post-processing Status** ✅")
            notification_parts.extend(postprocess_status_links)

    except Exception as e:
        logger.exception(f"Failed to build the extended notifications: {e}")
        notification_parts.append("**Artifact Links:** Error extracting links")
        notification_success = False

    notification_parts.append("")
    notification_parts.append("---")

    return "\n".join(notification_parts), notification_success


def _get_execution_engine_config() -> str | None:
    """Get execution engine configuration for notification."""
    try:
        cluster_config = config.project.get_config("cluster", None, warn=False)
        if not cluster_config:
            return None

        config_parts = []
        for key, value in cluster_config.items():
            if value:
                config_parts.append(f"`{key}`: {value}")

        if config_parts:
            return "* " + "  \n* ".join(config_parts)
    except Exception:
        pass
    return None


def _extract_test_status_section(status: dict[str, Any]) -> list[str] | None:
    """Extract test status section from status."""
    test_phase = status.get("test_phase")
    if not test_phase:
        return None

    phase = test_phase.get("phase", "").upper()
    message = test_phase.get("message", "")
    test_status_emoji = "✅" if phase == "PASSED" else "❌" if phase == "FAILED" else "⚠️"

    return [
        f"**{test_status_emoji} Test Status: {phase} {test_status_emoji}**",
        f"**Message:** {message}",
    ]


def _get_step_status_section(artifact_dir: Path | None, mlflow_run_url: str | None) -> list[str]:
    """Get step status section with links."""
    if not artifact_dir or not artifact_dir.exists():
        return []

    step_status = []
    for step_dir in sorted(artifact_dir.glob("*")):
        if not step_dir.is_dir() or step_dir.name.startswith("."):
            continue

        step_name = step_dir.name

        # Skip special directories
        if step_name == CI_METADATA_DIRNAME:
            continue

        exit_status_file = step_dir / CI_METADATA_DIRNAME / "exit_status.yaml"
        exit_status_emoji = "❓"

        if exit_status_file.exists():
            try:
                with open(exit_status_file, encoding="utf-8") as f:
                    exit_data = yaml.safe_load(f)
                exit_code = exit_data.get("return_code", 999)
                if exit_code == 0:
                    exit_status_emoji = "✅"
                else:
                    exit_status_emoji = "❌"
            except Exception:
                exit_status_emoji = "❓"

        # Count ERROR and WARNING messages in run.log
        log_counts = _count_log_messages(step_dir)
        log_summary = _format_log_summary(log_counts)

        mlflow_log_url = (
            f"{mlflow_run_url}/artifacts/{step_name}/run.log" if mlflow_run_url else "#"
        )
        if mlflow_run_url:
            step_title = f"#### {exit_status_emoji} [{step_name}]({mlflow_log_url}){log_summary}"
            step_status.append(step_title)

        step_status.extend(_process_notification_files(step_dir))

        step_details = _process_step_details(step_dir, mlflow_run_url)
        if step_details:
            step_status.extend(step_details)

    return step_status


def _count_log_messages(step_dir: Path) -> dict[str, int]:
    """Count ERROR and WARNING messages in run.log file.

    Args:
        step_dir: Directory containing the run.log file

    Returns:
        Dict with 'errors' and 'warnings' counts
    """
    log_file = step_dir / "run.log"
    counts = {"errors": 0, "warnings": 0}

    if not log_file.exists():
        return counts

    try:
        with open(log_file, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ERROR:"):
                    counts["errors"] += 1
                elif line.startswith("WARNING:"):
                    counts["warnings"] += 1
    except Exception as e:
        logger.warning(f"Failed to read log file {log_file}: {e}")

    return counts


def _format_log_summary(log_counts: dict[str, int]) -> str:
    """Format log counts for display in step title.

    Args:
        log_counts: Dict with 'errors' and 'warnings' counts

    Returns:
        Formatted string to append to step title, or empty string if no issues
    """
    errors = log_counts.get("errors", 0)
    warnings = log_counts.get("warnings", 0)

    if errors == 0 and warnings == 0:
        return ""

    parts = []
    if errors > 0:
        parts.append(f"🔴 {errors}E")
    if warnings > 0:
        parts.append(f"🟡 {warnings}W")

    return f" ({', '.join(parts)})"


def _get_postprocess_status_links(
    artifact_dir: Path | None, mlflow_run_url: str | None
) -> list[str]:
    """Get postprocess status links."""
    if not artifact_dir or not artifact_dir.exists():
        return []

    step_log_links = []

    # Look for postprocess results in step directories
    for step_dir in sorted(artifact_dir.glob("*")):
        if not step_dir.is_dir() or step_dir.name.startswith("."):
            continue

        step_name = step_dir.name

        # Skip special directories
        if step_name == CI_METADATA_DIRNAME:
            continue

        try:
            postprocess_status_file = step_dir / "postprocess_status.yaml"
            if not postprocess_status_file.exists():
                continue

            with open(postprocess_status_file, encoding="utf-8") as f:
                status_data = yaml.safe_load(f.read())

            if not status_data:
                continue

            # Add job shutdown status if available
            if "job_shutdown" in status_data:
                shutdown_status = status_data["job_shutdown"]
                status_data["job_shutdown"] = shutdown_status

            # Import notification functions from caliper
            from projects.caliper.orchestration.notification import (
                format_postprocess_status_notification,
                parse_postprocess_result,
            )

            # Parse postprocess result
            result = parse_postprocess_status(status_data)
            if not result:
                continue

            # Create file link function for this step
            def get_file_link(file_path: Path, step_subdir: str = step_name) -> str:
                if mlflow_run_url:
                    # Create MLflow artifact URL
                    return _create_mlflow_file_url_for_step(
                        mlflow_run_url, step_subdir, str(file_path)
                    )

            # Generate notification text from the structured result
            notification_text = format_postprocess_status_notification(result, get_file_link)
            if notification_text:
                step_log_links.append(notification_text)

        except Exception as e:
            logger.exception(f"Failed to process postprocess status for {step_name}: {e}")

    return step_log_links


def _extract_artifact_links(status: dict[str, Any]) -> tuple[list[str], str | None]:
    """Extract artifact links and MLflow URL from status."""
    artifact_links = []
    mlflow_run_url = None

    caliper_export = status.get("caliper_artifacts_export", {})
    backends = caliper_export.get("backends", {})

    for backend_name, backend_result in backends.items():
        if not isinstance(backend_result, dict):
            continue

        if backend_result.get("experiment_url"):
            artifact_links.append(
                f"[{backend_name} Experiment]({backend_result['experiment_url']})"
            )

        if backend_result.get("run_url"):
            mlflow_run_url = backend_result["run_url"]
            artifact_links.append(f"[{backend_name} Results]({mlflow_run_url})")
        elif backend_result.get("artifact_url"):
            artifact_links.append(f"[{backend_name} Artifacts]({backend_result['artifact_url']})")
        elif backend_result.get("dashboard_url"):
            artifact_links.append(f"[{backend_name} Dashboard]({backend_result['dashboard_url']})")

    if status.get("artifact_url"):
        artifact_links.append(f"[Artifacts]({status['artifact_url']})")

    return artifact_links, mlflow_run_url


def _create_mlflow_file_url_for_step(
    mlflow_run_url: str, step_dir_name: str, file_path: str
) -> str:
    """Create MLflow URL for a specific file within a step directory.

    Args:
        mlflow_run_url: Base MLflow run URL
        step_dir_name: Name of the step directory
        file_path: Relative path to file from step directory

    Returns:
        Full MLflow URL to the file

    Raises:
        ValueError: If URL format is unexpected
    """
    if "/artifacts" not in mlflow_run_url:
        raise ValueError(f"Unexpected MLflow URL format: {mlflow_run_url}")

    # Clean file path
    file_clean = file_path.lstrip("/")

    if "#" in mlflow_run_url:
        base_domain, hash_fragment = mlflow_run_url.split("#", 1)
        if "/artifacts" not in hash_fragment:
            raise ValueError("Artifacts not found in hash fragment")

        hash_base, artifacts_part = hash_fragment.split("/artifacts", 1)
        # Extract workspace parameter if present, ignoring existing path
        workspace_param = ""
        if "?workspace=" in artifacts_part:
            workspace_param = artifacts_part[artifacts_part.find("?") :]
        return f"{base_domain}#{hash_base}/artifacts/{step_dir_name}/{file_clean}{workspace_param}"
    else:
        base_url, artifacts_part = mlflow_run_url.split("/artifacts", 1)
        # Extract workspace parameter if present, ignoring existing path
        workspace_param = ""
        if "?workspace=" in artifacts_part:
            workspace_param = artifacts_part[artifacts_part.find("?") :]
        return f"{base_url}/artifacts/{step_dir_name}/{file_clean}{workspace_param}"


def _process_notification_files(step_dir: Path) -> list[str]:
    """Process notification files from step directory."""
    notifications_dir = step_dir / CI_METADATA_DIRNAME / "notifications"
    if not (notifications_dir.exists() and notifications_dir.is_dir()):
        return []

    notifications_from_files = []
    for notification_file in sorted(notifications_dir.glob("*.txt")):
        with open(notification_file, encoding="utf-8") as f:
            content = f.read().strip()

        if not content:
            continue

        subtitle = notification_file.stem.replace("__", " ").replace("_", " ").title()
        subtitle = re.sub(r"^\d+\s+", "", subtitle)
        notifications_from_files.append(f"##### {subtitle}")

        for line in content.splitlines():
            notifications_from_files.append(f"> {line}")

    return notifications_from_files


def _extract_test_labels_info(artifact_dir: Path, mlflow_run_url: str | None = None) -> list[str]:
    """Extract test execution information from __test_labels__.yaml files.

    Args:
        artifact_dir: Directory to search for __test_labels__.yaml files
        mlflow_run_url: Optional MLflow run URL for creating links

    Returns:
        List of formatted strings with test information (directory, labels, success, message)
    """
    test_info_lines = []

    # Search for __test_labels__.yaml files recursively
    test_labels_files = list(artifact_dir.glob("**/__test_labels__.yaml"))

    if not test_labels_files:
        return []

    for test_labels_file in test_labels_files:
        try:
            with open(test_labels_file, encoding="utf-8") as f:
                test_data = yaml.safe_load(f) or {}

            # Extract directory relative to artifact_dir - use just the immediate directory name
            relative_dir = test_labels_file.parent.relative_to(artifact_dir)
            dir_name = relative_dir.name if relative_dir != Path(".") else "root"

            # Extract completion info
            completion = test_data.get("completion", {})
            success = completion.get("success")
            message = completion.get("message")

            if message:
                message = f": `{message}`"

            # Format status
            if success:
                status_emoji = "✅"
            elif success is False:
                status_emoji = "❌"
            else:
                status_emoji = "❓"

            # Create link to __test_labels__.yaml file if MLflow URL is available
            if mlflow_run_url:
                try:
                    # Get the step directory name (relative to parent)
                    step_dir_name = str(relative_dir)
                    test_labels_url = _create_mlflow_file_url_for_step(
                        mlflow_run_url, step_dir_name, "__test_labels__.yaml"
                    )
                    dir_link = f"[**{dir_name}**]({test_labels_url})"
                except Exception as e:
                    logger.warning(f"Failed to create MLflow link for {test_labels_file}: {e}")
                    dir_link = f"**{dir_name}**"
            else:
                dir_link = f"**{dir_name}**"

            test_info_lines.append(f"* {status_emoji} {dir_link}{message}")

        except Exception as e:
            test_info_lines.append(f"**{test_labels_file.name}**: Error reading file - {e}")

    return test_info_lines


def _extract_postprocess_status_info(artifact_dir: Path) -> list[str]:
    """Extract post-processing status information from postprocess_status.yaml files.

    Returns:
        List of formatted strings with postprocess step status (success only, no details)
    """
    postprocess_info_lines = []

    # Search for postprocess_status.yaml files recursively
    postprocess_files = list(artifact_dir.glob("**/postprocess_status.yaml"))

    if not postprocess_files:
        return []

    for postprocess_file in postprocess_files:
        try:
            with open(postprocess_file, encoding="utf-8") as f:
                postprocess_data = yaml.safe_load(f) or {}

            # Extract directory relative to artifact_dir
            relative_dir = postprocess_file.parent.relative_to(artifact_dir)
            dir_name = str(relative_dir) if relative_dir != Path(".") else "root"

            # Extract overall status
            overall_success = postprocess_data.get("success", False)
            final_status = postprocess_data.get("final_status", "unknown")

            # Extract individual step statuses
            steps = postprocess_data.get("steps", [])
            step_statuses = []

            for step_dict in steps:
                for step_name, step_data in step_dict.items():
                    if isinstance(step_data, dict):
                        status = step_data.get("status", "unknown")
                        status_emoji = (
                            "✅" if status == "success" else "❌" if status == "failed" else "⚪"
                        )
                        step_statuses.append(f"{status_emoji} {step_name}")

            # Format overall line
            overall_emoji = "✅" if overall_success else "❌"
            if step_statuses:
                steps_str = " " + " • ".join(step_statuses)
            else:
                steps_str = f" {final_status}"

            postprocess_info_lines.append(f"**{dir_name}**: {overall_emoji}{steps_str}")

        except Exception as e:
            postprocess_info_lines.append(f"**{postprocess_file.name}**: Error reading file - {e}")

    return postprocess_info_lines


def _process_step_details(step_dir: Path, mlflow_run_url: str | None = None) -> list[str]:
    """Process test labels and postprocess status for a single step directory."""
    step_details = []

    # Extract test labels for this specific step
    try:
        test_labels_info = _extract_test_labels_info(step_dir, mlflow_run_url)
        if test_labels_info:
            step_details.extend(test_labels_info)
    except Exception as e:
        logger.warning(f"Failed to extract test labels for step {step_dir.name}: {e}")

    # Extract postprocess status for this specific step
    try:
        postprocess_info = _extract_postprocess_status_info(step_dir)
        if postprocess_info:
            step_details.extend(postprocess_info)
    except Exception as e:
        logger.warning(f"Failed to extract postprocess status for step {step_dir.name}: {e}")

    return step_details


def _check_job_shutdown_status() -> dict[str, Any] | None:
    """Check if the job has been aborted via spec.shutdown field."""
    try:
        from projects.core.library import ci as ci_lib

        metadata_dir = ci_lib.get_ci_metadata_dir()
        fournos_fjob_path = metadata_dir / "fournos_fjob.yaml"
        if not fournos_fjob_path.exists():
            return None

        with open(fournos_fjob_path, encoding="utf-8") as f:
            fjob_data = yaml.safe_load(f)

        shutdown_value = fjob_data.get("spec", {}).get("shutdown")
        if shutdown_value:
            return {
                "shutdown_detected": True,
                "shutdown_value": shutdown_value,
                "is_aborted": shutdown_value.lower() == "stop",
            }

        return {"shutdown_detected": False, "shutdown_value": None, "is_aborted": False}
    except Exception as e:
        logger.warning(f"Failed to check job shutdown status: {e}")
        return None


def _read_step_exit_status(
    step_dir: Path, current_step_name: str | None = None
) -> tuple[str, StepStatus]:
    """Read exit status from step directory and return emoji and status enum."""
    from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME

    try:
        exit_status_file = step_dir / CI_METADATA_DIRNAME / "exit_status.yaml"
        if not exit_status_file.exists():
            # Check if this is the current ongoing step
            if current_step_name and step_dir.name == current_step_name:
                return "🔄", StepStatus.ONGOING  # Ongoing step
            return "❓", StepStatus.UNKNOWN  # Unknown status if file doesn't exist

        with open(exit_status_file, encoding="utf-8") as f:
            exit_data = yaml.safe_load(f)

        return_code = exit_data.get("return_code")
        if return_code is None or return_code == 0:
            return "✅", StepStatus.SUCCESS
        else:
            return "❌", StepStatus.FAILURE
    except Exception as e:
        logger.warning(f"Failed to read exit status from {step_dir}: {e}")
        # Check if this is the current ongoing step even on error
        if current_step_name and step_dir.name == current_step_name:
            return "🔄", StepStatus.ONGOING  # Ongoing step
        return "❓", StepStatus.UNKNOWN  # Unknown status on error


def _check_postprocess_warnings(step_dir: Path) -> StepStatus:
    """Check for warning status in postprocess status file."""

    status = StepStatus.SUCCESS  # No postprocess warning/error, assume no warnings
    for status_file in step_dir.glob("**/postprocess_status.yaml"):
        try:
            with open(status_file, encoding="utf-8") as f:
                status_data = yaml.safe_load(f)
        except Exception as e:
            logging.error(f"Failed to read {status_file} as yaml: {e}")
            status = StepStatus.WARNING
            continue

        if not status_data:
            continue

        # Check top-level success field for warning value
        success_value = status_data.get("success")
        if success_value == "warning":
            logging.warning(
                f"Post-process warning detected in {status_file}, setting the WARNING flag"
            )
            status = StepStatus.WARNING

        if success_value in ("failure", "error"):
            logging.error(f"Post-process {success_value} detected, raising the FAILURE flag")
            return StepStatus.FAILURE

    return status


def _get_overall_status_from_steps(artifact_dir: Path) -> str:
    """Check all step exit statuses and return overall status emoji."""
    from projects.core.library import env
    from projects.core.library.export import StepStatus

    try:
        current_step_name = Path(env.BASE_ARTIFACT_DIR).name

        step_statuses = []

        for step_dir in sorted(artifact_dir.iterdir()):
            if not step_dir.is_dir():
                continue
            if step_dir.name.startswith("."):
                continue

            # Only check directories that have run.log (actual steps)
            run_log = step_dir / "run.log"
            if not run_log.exists():
                continue

            _emoji, status = _read_step_exit_status(step_dir, current_step_name)

            step_statuses.append(status)

            # Check for postprocess warnings in this step (always check, regardless of exit status)
            postprocess_status = _check_postprocess_warnings(step_dir)
            step_statuses.append(postprocess_status)

        # Priority: failure > ongoing > warning > unknown > success
        if StepStatus.FAILURE in step_statuses:
            return "🔴"  # Any failure = red
        elif StepStatus.WARNING in step_statuses:
            return "🟠"  # Warning = orange
        elif StepStatus.UNKNOWN in step_statuses:
            return "🟠"  # Unknown = orange
        elif StepStatus.ONGOING in step_statuses:
            return "🟢"  # Ongoing --> success
        else:
            return "🟢"  # All successful = green

    except Exception as e:
        logger.exception(f"Failed to check step statuses: {e}")
        return "🔴"  # Error checking = red


def _create_mlflow_url(mlflow_run_url: str, step_dir_name: str) -> str | None:
    """Create MLflow URL for step logs."""

    if not mlflow_run_url:
        return f"BASE_URL_MISSING/{step_dir_name}"

    if "/artifacts" not in mlflow_run_url:
        logger.warning(f"Unexpected MLflow URL format: {mlflow_run_url}")
        return None

    if "#" in mlflow_run_url:
        base_domain, hash_fragment = mlflow_run_url.split("#", 1)
        if "/artifacts" not in hash_fragment:
            raise ValueError("Artifacts not found in hash fragment")

        hash_base, params = hash_fragment.split("/artifacts", 1)
        workspace_param = params if "?workspace=" in params else ""
        return f"{base_domain}#{hash_base}/artifacts/{step_dir_name}/run.log{workspace_param}"
    else:
        base_url, params = mlflow_run_url.split("/artifacts", 1)
        workspace_param = params if "?workspace=" in params else ""
        return f"{base_url}/artifacts/{step_dir_name}/run.log{workspace_param}"


def _create_mlflow_step_url(mlflow_run_url: str, step_dir_name: str) -> str | None:
    """Create MLflow URL for step directory (for file access)."""
    if "/artifacts" not in mlflow_run_url:
        logger.warning(f"Unexpected MLflow URL format: {mlflow_run_url}")
        return None

    if "#" in mlflow_run_url:
        base_domain, hash_fragment = mlflow_run_url.split("#", 1)
        if "/artifacts" not in hash_fragment:
            raise ValueError("Artifacts not found in hash fragment")

        hash_base, artifacts_part = hash_fragment.split("/artifacts", 1)
        # Extract workspace parameter if present, ignoring existing path
        workspace_param = ""
        if "?workspace=" in artifacts_part:
            workspace_param = artifacts_part[artifacts_part.find("?") :]
        return f"{base_domain}#{hash_base}/artifacts/{step_dir_name}{workspace_param}"
    else:
        base_url, artifacts_part = mlflow_run_url.split("/artifacts", 1)
        # Extract workspace parameter if present, ignoring existing path
        workspace_param = ""
        if "?workspace=" in artifacts_part:
            workspace_param = artifacts_part[artifacts_part.find("?") :]
        return f"{base_url}/artifacts/{step_dir_name}{workspace_param}"


def _read_step_duration(step_dir: Path) -> str:
    """Read step duration from timing file."""
    from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME

    timing_file = step_dir / CI_METADATA_DIRNAME / "test_duration.yaml"
    if not timing_file.exists():
        return ""

    try:
        with open(timing_file, encoding="utf-8") as f:
            timing_data = yaml.safe_load(f)

        formatted_duration = timing_data.get("duration", {}).get("formatted")
        return formatted_duration or ""
    except Exception as timing_error:
        logger.warning(f"Failed to read timing file {timing_file}: {timing_error}")
        return ""


def _process_step_status(artifact_dir: Path, mlflow_run_url: str) -> list[str]:
    """Process step logs from parent directory."""
    from projects.core.library import env

    if not mlflow_run_url:
        logging.warning("mlflow_run_url not set. Will generate dummy links.")

    step_status = []

    current_step_name = Path(env.BASE_ARTIFACT_DIR).name

    for step_dir in sorted(artifact_dir.iterdir()):
        if not step_dir.is_dir():
            continue
        if step_dir.name.startswith("."):
            continue

        run_log = step_dir / "run.log"
        if not run_log.exists():
            continue

        mlflow_log_url = _create_mlflow_url(mlflow_run_url, step_dir.name)
        if not mlflow_log_url:
            mlflow_log_url = "NO_URL"

        step_name = step_dir.name.replace("__", " ").replace("_", " ").title()
        duration_str = _read_step_duration(step_dir)
        exit_status_emoji, exit_status = _read_step_exit_status(step_dir, current_step_name)

        step_status.append("")
        if duration_str:
            step_status.append(
                f"#### {exit_status_emoji} [{step_name}]({mlflow_log_url}) `{duration_str}`"
            )
        else:
            step_status.append(f"#### {exit_status_emoji} [{step_name}]({mlflow_log_url})")

        step_status.extend(_process_notification_files(step_dir))

        step_details = _process_step_details(step_dir, mlflow_run_url)

        # Add test execution info for this step
        if step_details["test_labels_info"]:
            # Add Test Execution Overview if we have any step info
            step_status.append("")
            step_status.append("**Test Execution Overview**")

            step_status.extend([f"* {info}" for info in step_details["test_labels_info"]])

        # Add postprocess info for this step
        if step_details["postprocess_info"]:
            step_status.extend([f"* {info}" for info in step_details["postprocess_info"]])

    return step_status


def _extract_duration_from_status(status: dict[str, Any]) -> str:
    """Extract duration from status object."""
    # Look for duration in status
    duration = status.get("duration")
    if duration:
        return f" after {duration}"
    return ""
