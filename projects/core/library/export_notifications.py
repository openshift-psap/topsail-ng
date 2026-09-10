"""
Notification handling for Caliper export operations.

This module provides notification functionality for export completion,
including GitHub notifications and Slack notifications via project providers.
"""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from projects.caliper.engine.constants import METADATA_FILE
from projects.caliper.engine.kpi.dataclasses import CaliperTestMetadata
from projects.caliper.orchestration.censoring import censor_text
from projects.caliper.orchestration.postprocess import POSTPROCESS_STATUS_FILENAME
from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME
from projects.core.library import ci as ci_lib
from projects.core.library import config, env
from projects.core.library.step_status import StepStatus
from projects.core.notifications.provider import NotificationContext
from projects.core.notifications.send import send_notification as send_github_notification

logger = logging.getLogger(__name__)


@dataclass
class BackendResult:
    """Result from a single backend export."""

    success: bool = False
    run_id: str | None = None
    experiment_url: str | None = None
    run_url: str | None = None
    tracking_uri: str | None = None
    detail: str | None = None
    status: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackendResult":
        """Create BackendResult from raw dict."""
        # Infer success from status if not explicitly provided
        success = data.get("success")
        if success is None:
            status = data.get("status", "")
            success = status == "success"

        return cls(
            success=success,
            run_id=data.get("run_id"),
            experiment_url=data.get("experiment_url"),
            run_url=data.get("run_url"),
            tracking_uri=data.get("tracking_uri"),
            detail=data.get("detail"),
            status=data.get("status"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result = {"success": self.success}
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.experiment_url is not None:
            result["experiment_url"] = self.experiment_url
        if self.run_url is not None:
            result["run_url"] = self.run_url
        if self.tracking_uri is not None:
            result["tracking_uri"] = self.tracking_uri
        if self.detail is not None:
            result["detail"] = self.detail
        if self.status is not None:
            result["status"] = self.status
        return result


@dataclass
class CaliperArtifactsExport:
    """Caliper artifacts export information."""

    version: int = 1
    backends: dict[str, BackendResult] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaliperArtifactsExport":
        """Create CaliperArtifactsExport from raw dict."""
        backends_data = data.get("backends", {})
        backends = {}
        for backend_name, backend_data in backends_data.items():
            if isinstance(backend_data, dict):
                backends[backend_name] = BackendResult.from_dict(backend_data)
            else:
                backends[backend_name] = backend_data  # Keep non-dict values as-is

        return cls(
            version=data.get("version", 1),
            backends=backends if backends else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result = {"version": self.version}
        if self.backends:
            backends_dict = {}
            for backend_name, backend_result in self.backends.items():
                if isinstance(backend_result, BackendResult):
                    backends_dict[backend_name] = backend_result.to_dict()
                else:
                    backends_dict[backend_name] = backend_result
            result["backends"] = backends_dict
        return result


@dataclass
class TestPhase:
    """Test execution phase information."""

    phase: str
    message: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TestPhase":
        """Create TestPhase from raw dict."""
        return cls(
            phase=data.get("phase", "UNKNOWN"),
            message=data.get("message", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        return {
            "phase": self.phase,
            "message": self.message,
        }


@dataclass
class JobShutdown:
    """Job shutdown/abort information."""

    is_aborted: bool = False
    shutdown_value: str | None = None
    shutdown_detected: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobShutdown":
        """Create JobShutdown from raw dict."""
        return cls(
            is_aborted=data.get("is_aborted", False),
            shutdown_value=data.get("shutdown_value"),
            shutdown_detected=data.get("shutdown_detected", False),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        result = {
            "is_aborted": self.is_aborted,
            "shutdown_detected": self.shutdown_detected,
        }
        if self.shutdown_value is not None:
            result["shutdown_value"] = self.shutdown_value
        return result


@dataclass
class ExportStatus:
    """Dataclass for caliper export status."""

    success: bool
    final_status: str
    censoring_occurred: bool = False
    duration: str | None = None
    caliper_artifacts_export: CaliperArtifactsExport | None = None
    test_phase: TestPhase | None = None
    job_shutdown: JobShutdown | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExportStatus":
        """Create ExportStatus from raw dict, providing proper defaults."""
        # Convert nested structures to typed objects first
        caliper_export = None
        if data.get("caliper_artifacts_export"):
            caliper_export = CaliperArtifactsExport.from_dict(data["caliper_artifacts_export"])

        test_phase = None
        if data.get("test_phase"):
            test_phase = TestPhase.from_dict(data["test_phase"])

        job_shutdown = None
        if data.get("job_shutdown"):
            job_shutdown = JobShutdown.from_dict(data["job_shutdown"])

        # Extract success from various possible sources
        success = data.get("success")
        final_status = data.get("final_status", "")

        if success is None:
            # Try to determine success from final_status first
            if final_status in ("success", "completed"):
                success = True
            elif final_status in ("failed", "failure", "error"):
                success = False
            elif caliper_export and caliper_export.backends:
                # Infer success from backend results if no explicit status
                backend_successes = []
                for backend_result in caliper_export.backends.values():
                    if isinstance(backend_result, BackendResult):
                        backend_successes.append(backend_result.success)
                    elif isinstance(backend_result, dict):
                        # Handle raw dict backends (shouldn't happen after conversion)
                        backend_status = backend_result.get("status", "")
                        backend_successes.append(backend_status == "success")

                # Overall success if all backends succeeded
                success = all(backend_successes) if backend_successes else False

                # Set final_status based on inferred success if it wasn't set
                if not final_status or final_status == "unknown":
                    final_status = "success" if success else "failed"
            else:
                # Default to failed if we can't determine success
                success = False
                final_status = final_status or "unknown"

        return cls(
            success=success,
            final_status=final_status,
            censoring_occurred=data.get("censoring_occurred", False),
            duration=data.get("duration"),
            caliper_artifacts_export=caliper_export,
            test_phase=test_phase,
            job_shutdown=job_shutdown,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert back to dict for compatibility."""
        result = {
            "success": self.success,
            "final_status": self.final_status,
            "censoring_occurred": self.censoring_occurred,
        }

        if self.duration is not None:
            result["duration"] = self.duration
        if self.caliper_artifacts_export is not None:
            result["caliper_artifacts_export"] = self.caliper_artifacts_export.to_dict()
        if self.test_phase is not None:
            result["test_phase"] = self.test_phase.to_dict()
        if self.job_shutdown is not None:
            result["job_shutdown"] = self.job_shutdown.to_dict()

        return result


def _censor_notification_text(text: str, verbose: bool = False) -> str:
    """
    Censor sensitive content in notification text using caliper orchestration.

    Args:
        text: The notification text to censor
        verbose: Enable verbose logging

    Returns:
        Censored notification text with sensitive content replaced

    Raises:
        Exception: If vault discovery fails, preventing notification delivery
    """
    try:
        return censor_text(text, verbose=verbose)
    except Exception as e:
        logger.error(f"Censoring failed during notification preparation: {e}")
        # Re-raise to abort GitHub and Slack notification delivery
        # rather than sending potentially uncensored text
        raise


def send_notification(
    artifact_dir: Path | None,
    status: ExportStatus,
    notification_provider=None,
    dry_run: bool = False,
) -> bool:
    """Send job completion notifications based on caliper export status.

    Args:
        artifact_dir: Directory to browse to find the artifacts
        status: Caliper export status dataclass
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
    try:
        notification_status = _censor_notification_text(notification_status, verbose=dry_run)
    except Exception as e:
        logger.error(f"Notification censoring failed, aborting notification delivery: {e}")
        return False

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
                artifact_dir = Path(env.ARTIFACT_DIR) if env.ARTIFACT_DIR else None
                # Censor status fields before creating NotificationContext
                try:
                    censored_status = yaml.safe_load(
                        _censor_notification_text(yaml.dump(status.to_dict()), verbose=dry_run)
                    )
                except Exception as e:
                    logger.error(
                        f"Slack notification censoring failed, aborting Slack notification: {e}"
                    )
                    notification_success = False
                else:
                    # Only proceed with Slack notification if censoring succeeded
                    context = NotificationContext(
                        status=censored_status,
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


def _extract_finish_reason_from_status(status: ExportStatus) -> str:
    """Extract finish reason from status."""
    logger.info(f"DEBUG: Export status object: {status}")

    logger.info(
        f"DEBUG: Status analysis - success={status.success}, censoring_occurred={status.censoring_occurred}"
    )

    if not status.success:
        logger.info(f"DEBUG: Finish reason: failed (success={status.success})")
        return "failed"
    elif status.censoring_occurred:
        logger.info("DEBUG: Finish reason: completed with censoring")
        return "completed with censoring"
    else:
        logger.info("DEBUG: Finish reason: completed")
        return "completed"


def _build_enhanced_notification(
    artifact_dir: Path,
    project: str,
    finish_reason: str,
    status: ExportStatus,
) -> tuple[str, bool]:
    """Build enhanced notification with fournos job config and artifact links."""
    fjob_project, fjob_args_str = _get_project_and_args(project)

    success = status.success
    censoring_occurred = status.censoring_occurred

    logger.info(
        f"Building notification - success={success}, censoring_occurred={censoring_occurred}, finish_reason='{finish_reason}'"
    )

    status_emoji = "✅" if success else "❌"
    logger.info(f"Initial emoji based on success: {status_emoji}")

    if censoring_occurred:
        status_emoji = "⚠️"
        logger.info(f"Changed emoji to warning due to censoring: {status_emoji}")

    if finish_reason == "failed":
        status_emoji = "❌"
        logger.info(f"Changed emoji to failed due to finish_reason: {status_emoji}")

    logger.info(f"DEBUG: Final status emoji: {status_emoji}")

    base_status = f"{status_emoji} **Execution of `{fjob_project}` {fjob_args_str}** {status_emoji}"
    notification_parts = [base_status]

    # Add job abort message right below overall status if applicable
    shutdown_status = status.job_shutdown
    if shutdown_status and shutdown_status.is_aborted:
        notification_parts += ["", "---"]
        shutdown_value = shutdown_status.shutdown_value or "Stop"
        notification_parts.append(f"🛑 **JOB ABORTED** - `spec.shutdown={shutdown_value}`")

    execution_engine_config = _get_execution_engine_config()
    if execution_engine_config:
        notification_parts += ["", "---"]
        notification_parts.append("**Execution Engine Configuration**")
        notification_parts.append(execution_engine_config)

    notification_success = True

    # Extract artifact links (with error handling)
    try:
        artifact_links, mlflow_run_url = _extract_artifact_links(status)
    except Exception as e:
        logger.exception(f"Failed to extract artifact links: {e}")
        artifact_links, mlflow_run_url = [], None
        notification_success = False

    # Extract test status section (with error handling)
    try:
        test_status_section = _extract_test_status_section(status)
    except Exception as e:
        logger.exception(f"Failed to extract test status: {e}")
        test_status_section = None

    # Extract step status (with error handling)
    try:
        step_status = _get_step_status_section(artifact_dir, mlflow_run_url)
    except Exception as e:
        logger.exception(f"Failed to extract step status: {e}")
        step_status = None

    # Extract postprocess status (with error handling - don't let this break the notification)
    try:
        postprocess_status_links = _get_postprocess_status_links(artifact_dir, mlflow_run_url)
    except Exception as e:
        logger.exception(f"Failed to extract postprocess status: {e}")
        postprocess_status_links = []

    # Extract censoring report (with error handling)
    try:
        censoring_report_section = _get_censoring_report_section(artifact_dir)
    except Exception as e:
        logger.exception(f"Failed to extract censoring report: {e}")
        censoring_report_section = None

    # Build notification sections
    if test_status_section:
        notification_parts += ["", "---"]
        notification_parts.extend(test_status_section)

    if artifact_links:
        notification_parts += ["", "---"]
        notification_parts.append("**Artifact Links**")
        notification_parts.extend([f"* {link}" for link in artifact_links])
    else:
        if notification_success:
            notification_parts.append("**Artifact Links:** No direct links available")
        else:
            notification_parts.append("**Artifact Links:** Error extracting links")

    if step_status:
        notification_parts += ["", "---"]
        notification_parts.append("**Pipeline Step Details**")
        for link in step_status:
            notification_parts.append(link)

    if postprocess_status_links:
        notification_parts += ["", "---"]
        notification_parts.extend(postprocess_status_links)

    if censoring_report_section:
        notification_parts += ["", "---"]
        notification_parts.append("**Censoring Report**")
        notification_parts.extend(censoring_report_section)

    return "\n".join(notification_parts), notification_success


def _get_censoring_report_section(artifact_dir: Path) -> list[str] | None:
    """
    Parse censoring_report.yaml if it exists and return notification section.

    Returns:
        List of notification lines or None if no report exists
    """
    if not artifact_dir:
        return None

    # Look for censoring_report.yaml at the top level of the step files
    censoring_report_path = artifact_dir / "censoring_report.yaml"

    if not censoring_report_path.exists():
        return None

    try:
        with open(censoring_report_path) as f:
            report_data = yaml.safe_load(f)

        if not report_data:
            return None

        notification_lines = []

        # Show number of files scanned
        total_files = report_data.get("total_files", 0)
        clean_files = report_data.get("clean_files", 0)
        safe_censored_files = report_data.get("safe_censored_files", 0)
        censored_files = report_data.get("censored_files", 0)  # Only unexpected now

        notification_lines.append(f"📊 **Files scanned:** {total_files}")
        notification_lines.append(
            f"✅ Clean: {clean_files}, 🔒 Safe replacements: {safe_censored_files}"
        )

        # Show unexpected censoring issues if any
        censored_by_reason = report_data.get("censored_by_reason", {})
        if censored_files > 0 and censored_by_reason:
            notification_lines.append(
                f"⚠️ **Unexpected sensitive content:** {censored_files} file(s)"
            )
            notification_lines.append("")

            for reason, files in censored_by_reason.items():
                # Shorten long reasons for notification
                short_reason = reason
                if len(reason) > 60:
                    short_reason = reason[:57] + "..."

                file_count = len(files)
                if file_count <= 3:
                    # Show individual files for small counts
                    file_list = ", ".join([f"`{f}`" for f in files])
                    notification_lines.append(f"* {short_reason}: {file_list}")
                else:
                    # Show count for large lists
                    first_files = ", ".join([f"`{f}`" for f in files[:2]])
                    notification_lines.append(
                        f"* {short_reason}: {first_files} and {file_count - 2} more"
                    )
        elif censored_files == 0:
            notification_lines.append("✅ **No unexpected sensitive content found**")

        return notification_lines

    except Exception as e:
        logger.warning(f"Failed to parse censoring report: {e}")
        return [f"⚠️ **Censoring report parsing failed:** {e}"]


def _get_execution_engine_config() -> str | None:
    """Read and format execution engine configuration."""
    try:
        metadata_dir = ci_lib.get_ci_metadata_dir()
        fournos_fjob_path = metadata_dir / "fournos_fjob.yaml"
        if not fournos_fjob_path.exists():
            return f"* FournosJob not found at `{fournos_fjob_path}`"

        with open(fournos_fjob_path, encoding="utf-8") as f:
            fjob_data = yaml.safe_load(f)

        execution_engine = fjob_data.get("spec", {}).get("executionEngine", {})

        config = {}
        config["executionEngine"] = execution_engine
        config["author"] = fjob_data.get("spec", {}).get("author")

        if cluster := fjob_data.get("spec", {}).get("cluster"):
            config["cluster"] = cluster

        if pipeline := fjob_data.get("spec", {}).get("pipeline"):
            config["pipeline"] = pipeline

        config_yaml = yaml.dump(config, default_flow_style=False, sort_keys=True)
        return f"```yaml\n{config_yaml.strip()}\n```"
    except Exception as e:
        return f"* Failed to read fournos job config: `{e}`"


def _extract_test_status_section(status: ExportStatus) -> list[str] | None:
    """Extract test status section from status."""
    test_phase = status.test_phase
    if not test_phase:
        return None

    phase = test_phase.phase.upper()
    message = test_phase.message
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
        if not step_dir.is_dir() or step_dir.name.startswith(".") or step_dir.name == "lost+found":
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

        if step_dir.name.endswith("__export-artifacts"):
            exit_status_emoji = "📤"

        # Count ERROR and WARNING messages in run.log
        log_counts = _count_log_messages(step_dir)
        log_summary = _format_log_summary(log_counts)

        # Create step title - linked if MLflow URL available, plain-text otherwise
        if mlflow_run_url:
            mlflow_log_url = _create_mlflow_url(mlflow_run_url, step_name)
            step_title = f"#### {exit_status_emoji} [{step_name}]({mlflow_log_url}){log_summary}"
        else:
            step_title = f"#### {exit_status_emoji} {step_name}{log_summary}"

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
    logger.info(
        f"DEBUG: Postprocess status - artifact_dir={artifact_dir}, mlflow_run_url={mlflow_run_url}"
    )

    if not artifact_dir or not artifact_dir.exists():
        logger.info("DEBUG: Postprocess status - artifact_dir missing or doesn't exist")
        return []

    step_log_links = []

    # Look for postprocess results in step directories
    step_dirs = sorted(artifact_dir.glob("*"))
    logger.info(f"DEBUG: Postprocess status - found {len(step_dirs)} directories in {artifact_dir}")

    for step_dir in step_dirs:
        if not step_dir.is_dir() or step_dir.name.startswith("."):
            continue

        step_name = step_dir.name

        # Skip special directories
        if step_name == CI_METADATA_DIRNAME:
            continue

        try:
            # Search for postprocess status files recursively within this step directory
            step_postprocess_files = list(step_dir.glob(f"**/{POSTPROCESS_STATUS_FILENAME}"))

            if not step_postprocess_files:
                logger.info(
                    f"Postprocess status - {step_name}: no {POSTPROCESS_STATUS_FILENAME} files found"
                )
                continue

            # Import notification functions from caliper (inside function to avoid circular imports)
            from projects.caliper.orchestration.notification import (
                format_postprocess_status_notification,
                parse_postprocess_status,
            )

            # Process ALL postprocess status files found
            for postprocess_status_file in step_postprocess_files:
                with open(postprocess_status_file, encoding="utf-8") as f:
                    status_data = yaml.safe_load(f.read())

                if not status_data:
                    logger.warning(
                        f"Postprocess status - {postprocess_status_file} status_data is empty, skipping"
                    )
                    continue

                result = parse_postprocess_status(status_data)
                if not result:
                    logger.warning(
                        f"Postprocess status - {postprocess_status_file} parse_postprocess_status returned None/empty"
                    )
                    continue

                logger.info(
                    f"Postprocess status - {postprocess_status_file} parsed result successfully"
                )

                # Create file link function for this step
                def get_file_link(file_path: Path, step_subdir: str = step_name) -> str:
                    if mlflow_run_url:
                        # Create MLflow artifact URL
                        return _create_mlflow_file_url_for_step(
                            mlflow_run_url, step_subdir, str(file_path)
                        )
                    else:
                        # Fallback: just return the file path as text
                        return str(file_path)

                # Generate notification text from the structured result
                logger.info(
                    f"Postprocess status - {postprocess_status_file} generating notification text..."
                )
                notification_text = format_postprocess_status_notification(result, get_file_link)
                if notification_text:
                    logger.info(
                        f"Postprocess status - {postprocess_status_file} notification text generated, adding to list"
                    )
                    step_log_links.append(notification_text)
                else:
                    logger.info(
                        f"Postprocess status - {postprocess_status_file} notification text is empty"
                    )

        except Exception as e:
            logger.exception(f"Failed to process postprocess status for {step_name}: {e}")

    return step_log_links


def _extract_artifact_links(status: ExportStatus) -> tuple[list[str], str | None]:
    """Extract artifact links and MLflow URL from status."""
    artifact_links = []
    mlflow_run_url = None

    caliper_export = status.caliper_artifacts_export
    if not caliper_export or not caliper_export.backends:
        return artifact_links, mlflow_run_url

    for backend_name, backend_result in caliper_export.backends.items():
        if isinstance(backend_result, BackendResult):
            # Use typed access for BackendResult objects
            if backend_result.experiment_url:
                artifact_links.append(
                    f"[{backend_name} Experiment]({backend_result.experiment_url})"
                )

            if backend_result.run_url:
                mlflow_run_url = backend_result.run_url
                artifact_links.append(f"[{backend_name} Results]({mlflow_run_url})")

        elif isinstance(backend_result, dict):
            # Legacy support for dict-based backend results
            if backend_result.get("experiment_url"):
                artifact_links.append(
                    f"[{backend_name} Experiment]({backend_result['experiment_url']})"
                )

            if backend_result.get("run_url"):
                mlflow_run_url = backend_result["run_url"]
                artifact_links.append(f"[{backend_name} Results]({mlflow_run_url})")
            elif backend_result.get("artifact_url"):
                artifact_links.append(
                    f"[{backend_name} Artifacts]({backend_result['artifact_url']})"
                )
            elif backend_result.get("dashboard_url"):
                artifact_links.append(
                    f"[{backend_name} Dashboard]({backend_result['dashboard_url']})"
                )

    # Note: artifact_url is not part of the main export status
    # This appears to be legacy code - removing for now

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
            else:
                message = ""

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
    """Extract post-processing status information from POSTPROCESS_STATUS_FILENAME files.

    Returns:
        List of formatted strings with postprocess step status (success only, no details)
    """
    postprocess_info_lines = []

    # Search for POSTPROCESS_STATUS_FILENAME files recursively
    postprocess_files = list(artifact_dir.glob(f"**/{POSTPROCESS_STATUS_FILENAME}"))

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
                            "✔️" if status == "success" else "❌" if status == "failed" else "⚪"
                        )
                        step_statuses.append(f"{status_emoji} {step_name}")

            # Format overall line
            overall_emoji = "✅" if overall_success else "❌"
            if step_statuses:
                steps_str = " " + " | ".join(step_statuses)
            else:
                steps_str = f" {final_status}"

            postprocess_info_lines.append(f"* {overall_emoji} **{dir_name}**: {steps_str}")

        except Exception as e:
            postprocess_info_lines.append(f"**{postprocess_file.name}**: Error reading file - {e}")

    return postprocess_info_lines


def _process_step_details(step_dir: Path, mlflow_run_url: str | None = None) -> list[str]:
    """Process test labels, caliper metadata, and postprocess status for a single step directory."""
    step_details = []

    # Extract test labels for this specific step
    try:
        test_labels_info = _extract_test_labels_info(step_dir, mlflow_run_url)
        if test_labels_info:
            step_details.extend(test_labels_info)
    except Exception as e:
        logger.warning(f"Failed to extract test labels for step {step_dir.name}: {e}")

    # Create file link function for this step (shared by multiple extractors)
    def get_file_link(file_path: Path) -> str:
        if mlflow_run_url:
            # Create MLflow artifact URL
            return _create_mlflow_file_url_for_step(mlflow_run_url, step_dir.name, str(file_path))
        else:
            # Fallback: just return the file path as text
            return str(file_path)

    # Extract caliper metadata for this specific step (before postprocess status)
    try:
        metadata_files = _search_caliper_metadata_files(step_dir)
        metadata_info = _format_caliper_metadata_info_for_step(
            metadata_files, get_file_link, step_dir.parent if step_dir.parent else step_dir
        )
        if metadata_info:
            step_details.extend(metadata_info)
    except Exception as e:
        logger.warning(f"Failed to extract caliper metadata for step {step_dir.name}: {e}")

    # Extract censoring report for this specific step (before postprocess status)
    try:
        censoring_info = _format_censoring_report_info_for_step(step_dir, get_file_link)
        if censoring_info:
            step_details.extend(censoring_info)
    except Exception as e:
        logger.warning(f"Failed to extract censoring report for step {step_dir.name}: {e}")

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
    for status_file in step_dir.glob(f"**/{POSTPROCESS_STATUS_FILENAME}"):
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


def _extract_duration_from_status(status: ExportStatus) -> str:
    """Extract duration from status object."""
    # Look for duration in status
    duration = status.duration
    if duration:
        return f" after {duration}"
    return ""


def _search_caliper_metadata_files(step_dir: Path) -> list[Path]:
    """Search for caliper metadata files in the step directory."""

    if not step_dir.exists():
        return []

    metadata_files = []
    # Search for METADATA_FILE (__caliper_test_metadata__.yaml) recursively
    for metadata_file in step_dir.rglob(METADATA_FILE):
        metadata_files.append(metadata_file)

    return metadata_files


def _format_caliper_metadata_info(metadata_files: list[Path], get_file_link: Any) -> str:
    """Format caliper metadata information for display."""
    if not metadata_files:
        return ""

    metadata_lines = []
    metadata_lines.append("**Test Directories**")

    for metadata_file in metadata_files:
        try:
            # Load and parse metadata
            with open(metadata_file, encoding="utf-8") as f:
                metadata_dict = yaml.safe_load(f)

            metadata = CaliperTestMetadata.from_dict(metadata_dict)

            # Get parent directory path
            parent_dir = metadata_file.parent

            # Extract information
            labels = metadata.labels or {}
            kpi_labels = metadata.kpi_labels or {}
            timing = metadata.timing or {}

            # Format path with link to metadata file
            if get_file_link:
                try:
                    metadata_file_link = get_file_link(metadata_file)
                    path_info = f"Path: [`{parent_dir}`]({metadata_file_link})"
                except Exception as e:
                    logger.warning(f"Failed to create link for metadata file {metadata_file}: {e}")
                    path_info = f"Path: `{parent_dir}`"
            else:
                path_info = f"Path: `{parent_dir}`"
            metadata_lines.append(f"* {path_info}")

            # Format labels
            if labels:
                label_items = [f"`{k}={v}`" for k, v in labels.items()]
                metadata_lines.append(f"* Labels: {', '.join(label_items)}")

            # Format KPI labels
            if kpi_labels:
                kpi_label_items = [f"`{k}={v}`" for k, v in kpi_labels.items()]
                metadata_lines.append(f"* KPI Labels: {', '.join(kpi_label_items)}")

            # Look for completion information in timing
            completion_success = timing.get("completion", {}).get("success")
            completion_message = timing.get("completion", {}).get("message", "")

            if completion_success is not None:
                status_emoji = "✅" if completion_success else "❌"
                message_part = f": `{completion_message}`" if completion_message else ""
                metadata_lines.append(f"* Completion: {status_emoji}{message_part}")

            # Format test and benchmark duration
            test_duration = timing.get("test_duration")
            benchmark_duration = timing.get("benchmark_duration")

            if test_duration or benchmark_duration:
                duration_parts = []
                if test_duration:
                    duration_parts.append(f"test: `{test_duration}`")
                if benchmark_duration:
                    duration_parts.append(f"benchmark: `{benchmark_duration}`")
                metadata_lines.append(f"* Duration: {', '.join(duration_parts)}")

        except Exception as e:
            logger.warning(f"Failed to process caliper metadata file {metadata_file}: {e}")
            metadata_lines.append(f"* `{metadata_file.parent}`: Error reading metadata - {e}")

    return "\n".join(metadata_lines)


def _format_censoring_report_info_for_step(step_dir: Path, get_file_link: Any) -> list[str]:
    """Format censoring report information for integration within step details."""
    censoring_report_path = step_dir / "censoring_report.yaml"

    if not censoring_report_path.exists():
        return []

    try:
        with open(censoring_report_path, encoding="utf-8") as f:
            report_data = yaml.safe_load(f)

        if not report_data:
            return []

        censoring_lines = []

        # Get censoring statistics
        total_files = report_data.get("total_files", 0)
        clean_files = report_data.get("clean_files", 0)
        safe_censored_files = report_data.get("safe_censored_files", 0)
        censored_files = report_data.get("censored_files", 0)  # Unexpected censoring

        censoring_lines.append("* 🔒 Censoring Report")
        # Create link to censoring report file
        if get_file_link:
            try:
                report_link = get_file_link(censoring_report_path)
                first_line = "[📊 {total_files} files scanned]({report_link})"
            except Exception as e:
                logger.warning(
                    f"Failed to create link for censoring report {censoring_report_path}: {e}"
                )
                first_line = f"📊 {total_files} files scanned"
        else:
            first_line = f"📊 {total_files} files scanned"

        censoring_lines.append(f"  * {first_line}")

        # Show breakdown of file types
        if clean_files > 0:
            censoring_lines.append(f"  * ✅ Clean files: {clean_files}")

        if safe_censored_files > 0:
            censoring_lines.append(f"  * 🔐 Safe replacements: {safe_censored_files}")

        if censored_files > 0:
            censoring_lines.append(f"  * ⚠️ Unexpected censoring: {censored_files}")

            # Show details for unexpected censoring if available
            censored_by_reason = report_data.get("censored_by_reason", {})
            if censored_by_reason:
                for reason, files in censored_by_reason.items():
                    file_count = len(files)
                    # Truncate reason if too long
                    display_reason = reason[:50] + "..." if len(reason) > 50 else reason
                    censoring_lines.append(f"    * {display_reason}: {file_count} file(s)")

        return censoring_lines

    except Exception as e:
        logger.warning(f"Failed to process censoring report {censoring_report_path}: {e}")
        return [f"* 🔒 Censoring Report: Error reading report - `{e}`"]


def _format_caliper_metadata_info_for_step(
    metadata_files: list[Path], get_file_link: Any, base_dir: Path
) -> list[str]:
    """Format caliper metadata information for integration within step details."""
    if not metadata_files:
        return []

    metadata_lines = []

    for metadata_file in metadata_files:
        try:
            # Load and parse metadata
            with open(metadata_file, encoding="utf-8") as f:
                metadata_dict = yaml.safe_load(f)

            metadata = CaliperTestMetadata.from_dict(metadata_dict)

            # Get relative path from base directory
            try:
                relative_path = metadata_file.parent.relative_to(base_dir)
                display_path = str(relative_path) if str(relative_path) != "." else "root"
            except ValueError:
                # If relative_to fails, use the full path
                display_path = str(metadata_file.parent)

            # Extract information
            labels = metadata.labels or {}
            kpi_labels = metadata.kpi_labels or {}
            timing = metadata.timing or {}

            # Format path with link to metadata file
            if get_file_link:
                metadata_file_link = get_file_link(metadata_file)
                path_info = f"* 📊 Test directory: [`{display_path}`]({metadata_file_link})"
            else:
                path_info = f"* 📊 Test directory: `{display_path}`"
            metadata_lines.append(path_info)

            # Format labels
            if labels:
                label_items = [f"`{k}={v}`" for k, v in labels.items()]
                metadata_lines.append(f"    * {', '.join(label_items)}")

            # Format KPI labels
            if kpi_labels:
                kpi_label_items = [f"`{k}={v}`" for k, v in kpi_labels.items()]
                metadata_lines.append(f"  * KPI extra labels: {', '.join(kpi_label_items)}")

            # Look for completion information in timing
            completion_success = timing.get("completion", {}).get("success")
            completion_message = timing.get("completion", {}).get("message", "")

            if completion_success is not None:
                status_emoji = "✅" if completion_success else "❌"
                message_part = f": `{completion_message}`" if completion_message else ""
                metadata_lines.append(f"    * Completion: {status_emoji}{message_part}")

            # Format test and benchmark duration
            test_duration = timing.get("test_duration")
            benchmark_duration = timing.get("benchmark_duration")

            if test_duration or benchmark_duration:
                duration_parts = []
                if test_duration:
                    duration_parts.append(f"test: `{test_duration}`")
                if benchmark_duration:
                    duration_parts.append(f"benchmark: `{benchmark_duration}`")
                metadata_lines.append(f"    * Duration: {', '.join(duration_parts)}")

        except Exception as e:
            logger.warning(f"Failed to process caliper metadata file {metadata_file}: {e}")
            relative_path = (
                metadata_file.parent.relative_to(base_dir) if base_dir else metadata_file.parent
            )
            metadata_lines.append(
                f"* 📊 Test directory: `{relative_path}` - Error reading metadata: {e}"
            )

    return metadata_lines
