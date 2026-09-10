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

from projects.caliper.orchestration.export import (
    ExportFailedException,
    run_from_orchestration_config,
)
from projects.caliper.orchestration.postprocess import POSTPROCESS_STATUS_FILENAME
from projects.core.library import ci as ci_lib
from projects.core.library import config, env, run
from projects.core.library.export_notifications import (
    _check_job_shutdown_status,
    _create_mlflow_file_url_for_step,
    send_notification,
)

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


def _process_caliper_postprocess_status(
    step_dir: Path, step_log_links: list[str], mlflow_run_url: str | None = None
) -> None:
    """Search for and process POSTPROCESS_STATUS_FILENAME files in step directory."""
    status_files = list(step_dir.glob(f"**/{POSTPROCESS_STATUS_FILENAME}"))

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


def run_caliper_orchestration_export(
    *, artifact_dir: Path, disable_censoring: bool = False, disable_file_export: bool = False
):

    # Use FJOB_NAME as fallback for mlflow run_name if not configured
    run_name = config.project.get_config(
        "caliper.export.backend.mlflow.config.run_name", None, print=False, warn=False
    )
    if run_name is None and "FJOB_NAME" in os.environ:
        config.project.set_config(
            "caliper.export.backend.mlflow.config.run_name", os.environ["FJOB_NAME"], print=False
        )

    # Initialize vaults needed for export operations
    try:
        from projects.core.library import vault

        # Get export-specific vaults (MLflow, S3, notifications)
        export_vaults = caliper_export_list_vaults()

        if export_vaults:
            vault.init(vaults=export_vaults)
            logger.info(f"Initialized vault manager with {len(export_vaults)} vaults for export")
        else:
            logger.info("No vaults needed for export operation")

    except Exception as e:
        logger.warning(f"Failed to initialize vaults for export: {e}")
        logger.warning("Continuing with export operation - some features may not work")

    caliper_cfg = config.project.get_config("caliper", print=False)

    return run_from_orchestration_config(
        caliper_cfg, disable_censoring=disable_censoring, disable_file_export=disable_file_export
    )


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
@click.option(
    "--disable-notification",
    "disable_notification",
    is_flag=True,
    default=False,
    help="Skip sending completion notifications.",
)
@click.option(
    "--disable-censoring",
    "disable_censoring",
    is_flag=True,
    default=False,
    help="Skip censoring sensitive artifacts before export.",
)
@click.option(
    "--disable-file-export",
    "disable_file_export",
    is_flag=True,
    default=False,
    help="Skip artifact file upload but still run notifications with mock status.",
)
@click.pass_context
@ci_lib.safe_ci_entrypoint
def caliper_export_entrypoint(
    _ctx,
    artifact_dir: Path | None,
    dry_run: bool,
    disable_notification: bool,
    disable_censoring: bool,
    disable_file_export: bool,
):
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

    # Normalize artifact_dir to a pathlib.Path after precedence resolution
    artifact_dir = Path(artifact_dir)

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
                            "experiment_url": "http://DRY_RUN_MLFLOW_FAKE_URL/#/experiments/123",
                            "run_url": "http://DRY_RUN_MLFLOW_FAKE_URL/#/experiments/123/runs/dry-run-mock-id/artifacts?workspace=forge-dry-run",
                            "tracking_uri": "http://DRY_RUN_MLFLOW_FAKE_URL",
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
            status = run_caliper_orchestration_export(
                artifact_dir=artifact_dir,
                disable_censoring=disable_censoring,
                disable_file_export=disable_file_export,
            )
            logger.info("Export status:\n" + yaml.dump(status, indent=4))

            # Update fjob status with export results (only if file export is not disabled)
            if not disable_file_export:
                _update_fjob_export_status(status)
            else:
                logger.info("Skipping fjob status update due to --disable-file-export flag")

    except ExportFailedException as e:
        logger.exception(f"Export failed: {e}")
        export_failed = True
        # Create failure status for notification
        status = {"success": False, "error": str(e), "backends": {}}
    except Exception as e:
        logger.exception(f"Export failed with unexpected error: {e}")
        export_failed = True
        # Create failure status for notification
        status = {"success": False, "error": str(e), "backends": {}}

    finally:
        # Send completion notifications regardless of success/failure
        if status and not disable_notification:
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
        elif disable_notification:
            logger.info("Notifications disabled via --disable-notification flag")

        if not disable_file_export:
            if not dry_run:
                _update_final_artifacts(artifact_dir, status)
            else:
                logger.info("DRY RUN: Skipping final artifacts update to MLflow")
        else:
            logger.info("Skipping final artifacts upload due to --disable-file-export flag")

    if export_failed or notification_failed:
        return 1, "failed"

    # Check if censoring occurred and return exit code 1 if so
    if status and status.get("censoring_occurred", False):
        return 1, "censoring_occurred"

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
