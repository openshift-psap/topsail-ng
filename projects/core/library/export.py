"""
Shared Caliper "artifacts export" CLI for FORGE project orchestration.

Registers a :mod:`click` subcommand that reads ``caliper`` from project config and runs
:func:`projects.caliper.orchestration.export.run_from_orchestration_config`.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import click
import yaml

from projects.caliper.orchestration.export import run_from_orchestration_config
from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME
from projects.core.library import ci as ci_lib
from projects.core.library import config, env, run


class StepStatus(StrEnum):
    """Status of a step execution."""

    SUCCESS = "success"
    FAILURE = "failure"
    ONGOING = "ongoing"
    UNKNOWN = "unknown"
    WARNING = "warning"


logger = logging.getLogger(__name__)


class FinishReason(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    OTHER = "other"


def _update_fjob_export_status(status: dict):
    """Update FournosJob status with export artifacts status."""
    if not env.running_inside_fournos():
        return

    # Unset KUBECONFIG to use the pod SA access
    original_kubeconfig = os.environ.get("KUBECONFIG")
    if "KUBECONFIG" in os.environ:
        del os.environ["KUBECONFIG"]

    try:
        import json

        fjob_name = os.environ["FJOB_NAME"]
        namespace = os.environ["FOURNOS_WORKLOAD_NAMESPACE"]

        # Get current fjob status
        get_cmd = f"oc get fjob/{fjob_name} -n {namespace} -ojson"
        result = run.run(get_cmd, capture_stdout=True, check=False)

        if result.returncode != 0:
            logger.warning(f"Failed to get fjob/{fjob_name}")
            return

        fjob_data = json.loads(result.stdout)

        # Initialize status.engine.status if it doesn't exist
        if "status" not in fjob_data:
            fjob_data["status"] = {}
        if "engineStatus" not in fjob_data["status"]:
            fjob_data["status"]["engineStatus"] = {}
        if "forge" not in fjob_data["status"]["engineStatus"]:
            fjob_data["status"]["engineStatus"]["forge"] = {}
        if "status" not in fjob_data["status"]["engineStatus"]["forge"]:
            fjob_data["status"]["engineStatus"]["forge"]["status"] = {}

        # Update with export-artifacts status
        fjob_data["status"]["engineStatus"]["forge"]["exportArtifacts"] = status

        # Patch the fjob
        patch_data = {"status": fjob_data["status"]}
        patch_cmd = f"oc patch fjob/{fjob_name} -n {namespace} --type=merge --subresource=status -p '{json.dumps(patch_data)}'"

        patch_result = run.run(patch_cmd, check=False)
        if patch_result.returncode == 0:
            logger.info(f"Updated fjob/{fjob_name} status with export artifacts status")
        else:
            logger.warning(f"Failed to update fjob status: {patch_cmd}")

    except Exception as e:
        logger.warning(f"Failed to update fjob status: {e}")
    finally:
        # Restore KUBECONFIG if it was set
        if original_kubeconfig is not None:
            os.environ["KUBECONFIG"] = original_kubeconfig


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
    duration_str = _extract_duration_from_status(status)

    # Build enhanced notification with fournos job info and artifact links
    notification_status, notification_success = _build_enhanced_notification(
        artifact_dir, project, finish_reason, duration_str, status
    )

    # Send actual notifications
    if dry_run:
        logger.info(f"DRY RUN: Would send notification: {project} {finish_reason}{duration_str}")
        logger.info(f"DRY RUN: Notification content:\n{notification_status}")
        return notification_success
    else:
        logger.info(f"Sending notification: {project} {finish_reason}{duration_str}")

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
        return fjob_project, fjob_args_str

    try:
        from projects.core.library import config

        job_args = config.project.get_config("ci_job.args", [], warn=False)
        fjob_args_str = " ".join(job_args) if job_args else ""
    except Exception as e:
        logger.warning(f"Failed to get args from config: {e}")

    return fjob_project, fjob_args_str


def _get_execution_engine_config() -> str | None:
    """Read and format execution engine configuration."""
    try:
        metadata_dir = ci_lib.get_ci_metadata_dir()
        fournos_fjob_path = metadata_dir / "fournos_fjob.yaml"
        if not fournos_fjob_path.exists():
            return None

        with open(fournos_fjob_path, encoding="utf-8") as f:
            fjob_data = yaml.safe_load(f)

        execution_engine = fjob_data.get("spec", {}).get("executionEngine", {})
        if not execution_engine:
            return None

        engine_yaml = yaml.dump(execution_engine, default_flow_style=False, sort_keys=True)
        return f"```yaml\n{engine_yaml.strip()}\n```"
    except Exception as e:
        logger.warning(f"Failed to read fournos job config: {e}")
        return None


def _check_job_shutdown_status() -> dict[str, Any] | None:
    """Check if the job has been aborted via spec.shutdown field."""
    try:
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


def _read_step_duration(step_dir: Path) -> str:
    """Read step duration from timing file."""
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


def _process_caliper_postprocess_status(
    step_dir: Path, step_log_links: list[str], mlflow_run_url: str | None = None
) -> None:
    """Search for and process postprocess_status.yaml files in step directory."""
    status_files = list(step_dir.glob("**/postprocess_status.yaml"))

    for status_file in status_files:
        try:
            with open(status_file, encoding="utf-8") as f:
                status_data = yaml.safe_load(f)

            if not status_data:
                continue

            # Check for job shutdown/abort status
            shutdown_status = _check_job_shutdown_status()
            if shutdown_status:
                # Add shutdown information to status data
                status_data["job_shutdown"] = shutdown_status

            # Import notification functions from caliper
            from projects.caliper.orchestration.notification import (
                format_postprocess_status_notification,
                parse_postprocess_status,
            )

            # Parse status data into structured object
            result = parse_postprocess_status(status_data)
            if not result:
                continue

            # Create file link generator function
            get_file_link = None
            if mlflow_run_url:
                # Use base_directory from status data for MLflow URL construction
                base_directory = result.base_directory

                if base_directory:
                    # Calculate path relative to BASE_ARTIFACT_DIR.parent
                    # e.g., "/workspace/artifacts/000__replot/postprocess_output" -> "000__replot/postprocess_output"
                    from projects.core.library import env

                    base_path = Path(base_directory)

                    # Calculate step subdirectory relative to BASE_ARTIFACT_DIR.parent
                    # e.g., "/workspace/artifacts/000__replot/postprocess_output" relative to "/workspace/artifacts" = "000__replot/postprocess_output"
                    try:
                        step_subdir = str(base_path.relative_to(env.BASE_ARTIFACT_DIR.parent))
                    except ValueError as e:
                        # Path resolution failed (common in dry-run or different directory contexts)
                        logger.warning(
                            f"Failed to resolve path {base_path} relative to {env.BASE_ARTIFACT_DIR.parent}: {e}"
                        )
                        # Fallback: use the step directory name
                        step_subdir = step_dir.name
                else:
                    # Fallback to step_dir.name for backward compatibility
                    step_subdir = step_dir.name

                def get_file_link(file_path: str, step_subdir=step_subdir) -> str:
                    return _create_mlflow_file_url_for_step(mlflow_run_url, step_subdir, file_path)

            # Generate notification text from the structured result
            notification_text = format_postprocess_status_notification(result, get_file_link)
            if notification_text:
                step_log_links.append(notification_text)

        except Exception as e:
            logger.error(f"Failed to process caliper postprocess status file {status_file}: {e}")
            raise


def _process_notification_files(step_dir: Path) -> None:
    """Process notification files from step directory."""
    notifications_dir = step_dir / CI_METADATA_DIRNAME / "notifications"
    if not (notifications_dir.exists() and notifications_dir.is_dir()):
        return []

    import re

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


def _process_step_status(artifact_dir: Path, mlflow_run_url: str) -> list[str]:
    """Process step logs from parent directory."""

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


def _process_postprocess_status(mlflow_run_url: str | None = None) -> list[str]:
    """Process post-processing status from all step directories."""
    if not mlflow_run_url:
        return []

    postprocess_links = []
    parent_dir = Path(env.BASE_ARTIFACT_DIR).parent

    for step_dir in sorted(parent_dir.iterdir()):
        if not step_dir.is_dir():
            continue
        if step_dir.name.startswith("."):
            continue

        try:
            _process_caliper_postprocess_status(step_dir, postprocess_links, mlflow_run_url)
        except Exception as e:
            logger.error(f"Failed to process postprocess status for {step_dir.name}: {e}")
            raise

    return postprocess_links


def _read_step_exit_status(
    step_dir: Path, current_step_name: str | None = None
) -> tuple[str, StepStatus]:
    """Read exit status from step directory and return emoji and status enum."""
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


def _get_overall_status_from_steps(artifact_dir) -> str:
    """Check all step exit statuses and return overall status emoji."""
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


def _build_enhanced_notification(
    artifact_dir: Path,
    project: str,
    finish_reason: FinishReason,
    duration_str: str,
    status: dict[str, Any],
) -> str:
    """Build enhanced notification with fournos job config and artifact links."""
    fjob_project, fjob_args_str = _get_project_and_args(project)

    # Check for job shutdown first (takes highest priority)
    shutdown_status = _check_job_shutdown_status()
    if shutdown_status and shutdown_status.get("is_aborted"):
        status_emoji = "🛑"  # Abort status overrides everything
    else:
        # Check all step statuses for overall status emoji (takes priority over finish_reason)
        status_emoji = _get_overall_status_from_steps(artifact_dir)

    base_status = f"**{status_emoji} Execution of `{fjob_project}` {fjob_args_str} {status_emoji}**"

    notification_parts = [base_status, "---"]

    # Add job abort message right below overall status if applicable
    if shutdown_status and shutdown_status.get("is_aborted"):
        shutdown_value = shutdown_status.get("shutdown_value", "Stop")
        notification_parts.append(f"🛑 **JOB ABORTED** - `spec.shutdown={shutdown_value}`")
        notification_parts.append("")

    execution_engine_config = _get_execution_engine_config()
    if execution_engine_config:
        notification_parts.append("**Execution Engine Configuration**")
        notification_parts.append(execution_engine_config)

    notification_success = True
    try:
        artifact_links, mlflow_run_url = _extract_artifact_links(status)
        step_status = _process_step_status(artifact_dir, mlflow_run_url)
        postprocess_status_links = _process_postprocess_status(mlflow_run_url)
        test_status_section = _build_test_status_section(status, artifact_dir, mlflow_run_url)

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
            notification_parts.extend(postprocess_status_links)

    except Exception as e:
        logger.exception(f"Failed to build the extended notifications: {e}")
        notification_parts.append("**Artifact Links:** Error extracting links")
        notification_success = False

    notification_parts.append("")
    notification_parts.append("---")

    return "\n".join(notification_parts), notification_success


def _extract_test_labels_info(artifact_dir: Path, mlflow_run_url: str | None = None) -> list[str]:
    """Extract test execution information from __test_labels__.yaml files.

    Args:
        artifact_dir: Directory to search for __test_labels__.yaml files
        mlflow_run_url: Optional MLflow run URL for creating links

    Returns:
        List of formatted strings with test information (directory, labels, success, message)
    """
    from pathlib import Path

    import yaml

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
    from pathlib import Path

    import yaml

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
                        step_statuses.append(f"{step_name}:{status_emoji}")

            # Format overall line
            overall_emoji = "✅" if overall_success else "❌"
            if step_statuses:
                steps_str = " " + " ".join(step_statuses)
            else:
                steps_str = f" {final_status}"

            postprocess_info_lines.append(f"**{dir_name}**: {overall_emoji}{steps_str}")

        except Exception as e:
            postprocess_info_lines.append(f"**{postprocess_file.name}**: Error reading file - {e}")

    return postprocess_info_lines


def _process_step_details(step_dir: Path, mlflow_run_url: str | None = None) -> dict[str, Any]:
    """Process test labels and postprocess status for a single step directory."""
    step_info = {"step_name": step_dir.name, "test_labels_info": [], "postprocess_info": []}

    # Extract test labels for this specific step
    try:
        step_info["test_labels_info"] = _extract_test_labels_info(step_dir, mlflow_run_url)
    except Exception as e:
        logger.warning(f"Failed to extract test labels for step {step_dir.name}: {e}")

    # Extract postprocess status for this specific step
    try:
        step_info["postprocess_info"] = _extract_postprocess_status_info(step_dir)
    except Exception as e:
        logger.warning(f"Failed to extract postprocess status for step {step_dir.name}: {e}")

    return step_info


def _build_test_status_section(
    status: dict[str, Any], artifact_dir: Path, mlflow_run_url: str | None = None
) -> list[str]:
    """Build comprehensive test status section with step-by-step execution and post-processing overviews."""
    try:
        test_phase = status.get("test_phase", {})
        if not test_phase:
            return []

        test_status = test_phase.get("phase", "UNKNOWN")
        test_message = test_phase.get("message", "")

        status_lines = [f"**Test status**: {test_status}"]

        if test_message:
            # Format message with blockquote-style prefix
            status_lines.append(f"> {test_message}")

        return status_lines

    except Exception as e:
        logger.warning(f"Failed to build test status section: {e}")
        return []


def _extract_finish_reason_from_status(status: dict[str, Any]) -> FinishReason:
    """Extract finish reason from status object."""
    # Check if any backend failed in the status
    if not status:
        return FinishReason.ERROR

    # Look for backend results
    backends = status.get("backends", {})
    for backend_name, backend_result in backends.items():
        # Check both explicit success flag and status field
        if backend_result.get("success") is False or backend_result.get("status") not in (
            None,
            "success",
        ):
            logger.info(f"Backend {backend_name} failed, marking as error")
            return FinishReason.ERROR

    return FinishReason.SUCCESS


def _extract_duration_from_status(status: dict[str, Any]) -> str:
    """Extract duration from status object."""
    # Look for duration in status
    duration = status.get("duration")
    if duration:
        return f" after {duration}"
    return ""


def run_caliper_orchestration_export(*, artifact_dir: Path):

    # Use FJOB_NAME as fallback for mlflow run_name if not configured
    run_name = config.project.get_config(
        "caliper.export.backend.mlflow.config.run_name", None, print=False, warn=False
    )
    if run_name is None and "FJOB_NAME" in os.environ:
        config.project.set_config(
            "caliper.export.backend.mlflow.config.run_name", os.environ["FJOB_NAME"], print=False
        )

    # Initialize vaults needed for export operations
    logger.info("Checking vaults for export operations")
    try:
        # Get export-specific vaults (MLflow, S3, notifications)
        export_vaults = caliper_export_list_vaults()
        logger.info(f"Export vaults needed: {len(export_vaults)} - {export_vaults}")

        # Initialize vaults if any are needed
        if export_vaults:
            from projects.core.library import vault

            # Check if vault manager is already initialized
            try:
                vault.get_vault_manager()
                logger.info(
                    f"Vault manager already initialized, checking {len(export_vaults)} export vaults"
                )
                manager_already_initialized = True
            except RuntimeError:
                logger.info(f"Initializing vault manager with {len(export_vaults)} export vaults")
                manager_already_initialized = False

            vault.init(vaults=export_vaults)

            if manager_already_initialized:
                logger.info(f"Export vault check completed for {len(export_vaults)} vaults")
            else:
                logger.info(
                    f"Successfully initialized vault manager with {len(export_vaults)} vaults for export"
                )
        else:
            logger.info("No vaults needed for export operation")

    except Exception as e:
        logger.warning(f"Failed to initialize vaults for export: {e}")
        logger.warning("Continuing with export operation - some features may not work")

    caliper_cfg = config.project.get_config("caliper", print=False)

    return run_from_orchestration_config(caliper_cfg)


@click.command("export-artifacts")
@click.option(
    "--artifact-dir",
    "artifact_dir",
    type=click.Path(path_type=Path, exists=False, file_okay=True, dir_okay=True),
    default=None,
    help="If set, overrides caliper.export.from (artifact root directory).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show what would be exported and notified without actually performing operations.",
)
@click.pass_context
@ci_lib.safe_ci_entrypoint
def caliper_export_entrypoint(_ctx, artifact_dir: Path | None, dry_run: bool):
    """Export the file artifacts."""

    notification_provider = getattr(getattr(_ctx, "obj", None), "notification_provider", None)

    status = None
    export_failed = False
    notification_failed = False

    # Determine artifact directory with proper precedence and FOURNOS_CI handling
    if not artifact_dir:
        # First try the config field
        artifact_dir = config.project.get_config(
            "caliper.export.from", None, print=False, warn=False
        )

    if not artifact_dir and env.ARTIFACT_DIR:
        artifact_dir = env.ARTIFACT_DIR
        logger.info(f"Using ARTIFACT_DIR from environment: {artifact_dir}")
        # Apply FOURNOS_CI logic only when using ARTIFACT_DIR
        if os.environ.get("FOURNOS_CI") == "true":
            artifact_dir = Path(artifact_dir).parent
            logger.info(f"FOURNOS_CI=true: using parent directory: {artifact_dir}")

    if not artifact_dir:
        logger.error(
            "No artifact directory found. Please set --artifact-dir parameter, "
            "caliper.export.from config, ARTIFACT_DIR, or ARTIFACT_BASE_DIR environment variable."
        )
        return 1

    if dry_run:
        logging.info(f"DRY RUN: Building caliper notification from {artifact_dir}")
    else:
        logging.info(f"Building caliper notification from {artifact_dir}")

    # Set the config so other functions can access it
    config.project.set_config("caliper.export.from", str(artifact_dir))

    try:
        if dry_run:
            logger.info(
                "DRY RUN: Skipping actual caliper export, creating mock status for notification"
            )
            # Create a realistic mock status for notification testing
            status = {
                "success": True,
                "final_status": "success",
                "backends": {},
                "caliper_artifacts_export": {
                    "backends": {
                        "mlflow": {
                            "success": True,
                            "run_id": "dry-run-mock-id",
                            "experiment_url": "http://localhost:5000/#/experiments/123",
                            "run_url": "http://localhost:5000/#/experiments/123/runs/dry-run-mock-id/artifacts?workspace=forge-dry-run",
                            "tracking_uri": "http://localhost:5000",
                        }
                    }
                },
                "duration": "15 minutes, 30 seconds",
                "test_phase": {
                    "phase": "FAILED",
                    "message": "Test execution completed with failures",
                },
            }
        else:
            status = run_caliper_orchestration_export(artifact_dir=artifact_dir)
            logger.info("Export status:\n" + yaml.dump(status, indent=4))

            # Update fjob status with export results
            _update_fjob_export_status(status)

    except Exception as e:
        logger.error(f"Export failed: {e}")
        export_failed = True
        # Create failure status for notification
        status = {"success": False, "error": str(e), "backends": {}}

    finally:
        # Send completion notifications regardless of success/failure
        if status:
            try:
                notification_success = send_notification(
                    artifact_dir,
                    status,
                    notification_provider=notification_provider,
                    dry_run=dry_run,
                )
                if not notification_success:
                    logger.error("Notification sending failed")
                    notification_failed = True
            except Exception as e:
                logger.exception(f"Failed to send notifications: {e}")
                notification_failed = True

        if not dry_run:
            _update_final_artifacts(artifact_dir, status)
        else:
            logger.info("DRY RUN: Skipping final artifacts update to MLflow")

    if export_failed or notification_failed:
        return 1

    return 0


def _update_final_artifacts(artifact_dir, export_status: dict[str, Any] | None) -> None:
    """Update the final artifacts (run.log, notifications) to MLflow after all post-export work is done."""
    if not export_status:
        logger.warning("No export status received, cannot update the final artifacts")
        return

    try:
        caliper_export = export_status.get("caliper_artifacts_export", {})
        backends = caliper_export.get("backends", {})
        mlflow_meta = backends.get("mlflow")
        if not isinstance(mlflow_meta, dict):
            logger.warning(
                "Export status don't have the mlflow backend, cannot update the final artifacts"
            )
            return

        run_id = mlflow_meta.get("run_id")
        if not run_id:
            return

        artifact_root = Path(artifact_dir)
        artifact_path = str(env.ARTIFACT_DIR.relative_to(artifact_root))

        tracking_uri = mlflow_meta.get("tracking_uri")

        vault_name = config.project.get_config(
            "caliper.export.backend.mlflow.secrets.vault.name", None, print=False, warn=False
        )
        vault_key = config.project.get_config(
            "caliper.export.backend.mlflow.secrets.vault.mlflow_secret",
            None,
            print=False,
            warn=False,
        )

        connection = None
        if vault_name and vault_key:
            from projects.caliper.engine.file_export.mlflow_secrets import (
                load_mlflow_secrets_yaml,
            )
            from projects.core.library import vault as vault_lib

            secrets_path = vault_lib.get_vault_content_path(vault_name, vault_key)
            if secrets_path and secrets_path.exists():
                connection = load_mlflow_secrets_yaml(secrets_path)

        # Upload session artifacts using MLflow backend

        _update_artifacts(
            run_id=run_id,
            artifact_dir=env.ARTIFACT_DIR,
            artifact_path=artifact_path,
            tracking_uri=tracking_uri,
            connection=connection,
        )
        logger.info("Updated final artifacts in MLflow run %s", run_id)
    except Exception as e:
        logger.warning("Failed to update final artifacts in MLflow: %s", e)


def _update_artifacts(
    *,
    run_id: str,
    artifact_dir: str,
    artifact_path: str | None = None,
    tracking_uri: str | None = None,
    connection: dict[str, Any] | None = None,
) -> None:
    """Re-upload artifacts to an existing MLflow run.

    Args:
        run_id: The MLflow run ID to update
        artifact_dir: Directory containing the artifacts to upload
        artifact_path: Artifact path within the MLflow run (None for root)
        tracking_uri: MLflow tracking URI
        connection: MLflow connection configuration

    Intended to be called after all post-export work (notifications, etc.) completes,
    so uploaded files contain the full session output.
    """

    artifact_dir_path = Path(artifact_dir)

    # Collect files to upload
    files_to_upload = []

    # Check for run.log
    log_file = artifact_dir_path / "run.log"
    if log_file.is_file():
        files_to_upload.append(log_file)

    # Check for notification file
    notif_file = artifact_dir_path / "NOTIFICATION-github.md"
    if notif_file.is_file():
        files_to_upload.append(notif_file)

    # Upload files if any exist
    if files_to_upload:
        from projects.caliper.engine.file_export.mlflow_backend import update_artifacts

        update_artifacts(
            run_id=run_id,
            files=dict.fromkeys(files_to_upload, artifact_path),
            tracking_uri=tracking_uri,
            connection=connection,
        )


def caliper_export_list_vaults() -> list[str]:
    """List vaults required for Caliper export operations.

    Returns:
        List of vault names needed for export functionality
    """
    # STUB: This function determines which vaults are needed for export operations
    # Currently returns the S3 export vault if S3 export is enabled in the project config

    export_vaults = []

    try:
        from projects.core.library import config

        # Check if S3 export or import is enabled in the project configuration
        s3_parent_config = config.project.get_config("caliper.postprocess.s3", {})
        s3_export_config = config.project.get_config("caliper.postprocess.s3.export", {})
        s3_import_config = config.project.get_config("caliper.postprocess.s3.import", {})

        s3_export_enabled = s3_export_config.get("enabled", False)
        s3_import_enabled = s3_import_config.get("enabled", False)

        if s3_export_enabled or s3_import_enabled:
            # Add the configured vault for S3 credentials (shared between import and export)
            vault_config = s3_parent_config.get("vault", {})
            vault_name = (
                vault_config.get("name") if isinstance(vault_config, dict) else vault_config
            )
            if vault_name:
                export_vaults.append(vault_name)
                logger.info(f"Added S3 vault: {vault_name}")
            else:
                logger.warning(
                    "S3 import/export enabled but no vault specified in caliper.postprocess.s3.vault.name"
                )

        # Check if MLflow export is enabled and add its vault
        mlflow_config = config.project.get_config("caliper.export.backend.mlflow", {})
        if mlflow_config.get("enabled", False):
            mlflow_vault = mlflow_config.get("secrets", {}).get("vault", {}).get("name")
            if mlflow_vault:
                export_vaults.append(mlflow_vault)
                logger.info(f"Added MLflow export vault: {mlflow_vault}")
            else:
                logger.warning(
                    "MLflow export enabled but no vault specified in caliper.export.backend.mlflow.secrets.vault.name"
                )

        # Check if notifications are enabled and any export backend is enabled
        config.project.get_config("caliper.export.notifications", {})
        (s3_export_enabled or s3_import_enabled or mlflow_config.get("enabled", False))

        # Note: notification vault is handled separately as optional vault
        # See caliper_export_list_optional_vaults() function

        # STUB: Could add other export-related vaults here in the future
        # e.g., for different cloud providers, artifact repositories, etc.

    except Exception as e:
        logger.warning(f"Failed to determine export vaults from config: {e}")
        # Return empty list on error - export operations will handle missing vaults gracefully

    logger.info(f"Export vault list: {export_vaults}")
    return export_vaults


def caliper_export_list_optional_vaults() -> list[str]:
    """List optional vaults for Caliper export operations.

    Returns:
        List of optional vault names for export functionality (e.g., notifications)
    """
    optional_vaults = []

    try:
        from projects.core.library import config

        # Check if notifications are enabled and any export backend is enabled
        notification_config = config.project.get_config("caliper.export.notifications", {})

        # Check if any export operations are enabled
        s3_export_config = config.project.get_config("caliper.postprocess.s3.export", {})
        s3_import_config = config.project.get_config("caliper.postprocess.s3.import", {})
        mlflow_config = config.project.get_config("caliper.export.backend.mlflow", {})

        any_export_enabled = (
            s3_export_config.get("enabled", False)
            or s3_import_config.get("enabled", False)
            or mlflow_config.get("enabled", False)
        )

        if notification_config.get("enabled", False) and any_export_enabled:
            # Add notification vault for export completion notifications
            notification_vault = notification_config.get("vault")
            if notification_vault:
                optional_vaults.append(notification_vault)
                logger.info(f"Added notification vault (export enabled): {notification_vault}")
            else:
                logger.warning(
                    "Export notifications enabled but no vault specified in caliper.export.notifications.vault"
                )

    except Exception as e:
        logger.warning(f"Failed to determine optional export vaults from config: {e}")

    logger.info(f"Optional export vault list: {optional_vaults}")
    return optional_vaults


def caliper_agentic_list_vaults() -> list[str]:
    """List vaults required for agentic operations (config review, on failure analysis).

    Returns:
        List of vault names needed for agentic functionality
    """
    agentic_vaults = []

    try:
        from projects.core.library import config

        # Check if agentic features are enabled in the project configuration
        agentic_config = config.project.get_config("agentic", {})

        # Check if any agentic feature is enabled
        any_agentic_enabled = (
            agentic_config.get("enabled", False)
            or agentic_config.get("on_failure", {}).get("enabled", False)
            or agentic_config.get("config_review", {}).get("enabled", False)
        )

        if any_agentic_enabled:
            # Add the models vault for agentic operations
            models_vault = "psap-models-corp-rh"
            agentic_vaults.append(models_vault)
            logger.info(f"Added agentic models vault: {models_vault}")

        # STUB: Could add other agentic-related vaults here in the future

    except Exception as e:
        logger.warning(f"Failed to determine agentic vaults from config: {e}")
        # Return empty list on error - agentic operations will handle missing vaults gracefully

    logger.info(f"Agentic vault list: {agentic_vaults}")
    return agentic_vaults
