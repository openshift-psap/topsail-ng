"""
Caliper CLI command invocation functions.

This module contains functions that build and execute caliper CLI commands
using subprocess execution with proper logging and status handling.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any

from projects.caliper.orchestration.cli_builder import (
    build_ai_eval_export_command,
    build_analyse_kpis_command,
    build_kpi_csv_export_command,
    build_kpi_generate_command,
    build_parse_command,
    build_s3_export_command,
    build_s3_import_command,
    build_visualize_command,
)
from projects.caliper.orchestration.postprocess_config import (
    CaliperOrchestrationPostprocessConfig,
)
from projects.caliper.public import KpiAnalysisStatus, StatusLevel
from projects.caliper.public.status_models import create_disabled_status
from projects.core.library import env

logger = logging.getLogger(__name__)


def _generate_automatic_status_file_path(artifacts_dir: Path, operation: str) -> Path:
    """Generate automatic status file path in format: <artifacts_dir>/status_files/<idx>__<operation>.yaml"""
    status_dir = artifacts_dir / "status_files"
    status_dir.mkdir(parents=True, exist_ok=True)

    # Find next available index
    existing_files = list(status_dir.glob("*__*.yaml"))
    if existing_files:
        # Extract indices from existing files
        indices = []
        for f in existing_files:
            try:
                idx_str = f.name.split("__")[0]
                indices.append(int(idx_str))
            except (ValueError, IndexError):
                continue
        next_idx = max(indices) + 1 if indices else 0
    else:
        next_idx = 0

    return status_dir / f"{next_idx:03d}__{operation}.yaml"


def _make_path_relative_to_base(file_path: str | Path, base_dir: Path) -> str:
    """Convert absolute path to relative path from base directory.

    Args:
        file_path: Absolute or relative file path
        base_dir: Base directory to make path relative to

    Returns:
        Relative path as string
    """
    try:
        path_obj = Path(file_path)
        if path_obj.is_absolute() and base_dir:
            base_obj = Path(base_dir)
            if base_obj.is_absolute():
                try:
                    relative = path_obj.relative_to(base_obj)
                    return str(relative)
                except ValueError:
                    # Path is not relative to base_dir, return as-is
                    return str(file_path)
        return str(file_path)
    except Exception:
        return str(file_path)


def _execute_caliper_command(
    command: list[str],
    step_name: str,
    status_file: Path,
    step_logs_dir: Path,
) -> tuple[subprocess.CompletedProcess, dict[str, Any], Path]:
    """Execute a Caliper CLI command and return result and status data.

    Args:
        command: CLI command arguments list
        step_name: Name for logging banners (e.g., "caliper parse", "caliper visualize")
        status_file: Path to read status YAML from
        step_logs_dir: Directory to create log file in

    Returns:
        Tuple of (subprocess result, parsed status data, log file path)
    """
    from projects.caliper.orchestration.cli_builder import log_caliper_start_banner
    from projects.caliper.orchestration.postprocess_logging import (
        _handle_caliper_output_and_completion_with_header,
        _write_step_footer_to_log_file,
        _write_step_header_to_log_file,
    )
    from projects.caliper.orchestration.step_logging import _get_next_step_index

    # Create log file path with proper step index
    step_index = _get_next_step_index(step_logs_dir)
    step_name_safe = step_name.replace(" ", "_")
    log_file = step_logs_dir / f"{step_index:03d}__{step_name_safe}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Create script file for debugging in step_scripts directory with index prefix
    step_scripts_dir = env.ARTIFACT_DIR / "step_scripts"
    step_scripts_dir.mkdir(parents=True, exist_ok=True)
    script_file = step_scripts_dir / f"{step_index:03d}__{step_name_safe}.sh.txt"

    # Log start banner and save script
    log_caliper_start_banner(command, script_file, step_name.upper())

    # Write header to log file
    _write_step_header_to_log_file(log_file, command, step_name.upper())

    # Execute command with combined stdout/stderr.
    # Ensure the forge root is on PYTHONPATH so the caliper entry-point script
    # can import `projects` even when invoked as a bare binary from a venv.
    forge_root = str(env.FORGE_HOME)
    existing_pp = os.environ.get("PYTHONPATH", "")
    subprocess_env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [forge_root, existing_pp])),
    }
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Combine stderr into stdout
        text=True,
        env=subprocess_env,
    )

    # Handle output, status parsing, and log completion banner
    status_data = _handle_caliper_output_and_completion_with_header(
        result, log_file, status_file, step_name.upper()
    )

    # Write footer to log file
    _write_step_footer_to_log_file(log_file, result, step_name.upper())

    return result, status_data, log_file


def run_artifacts_to_kpis(
    postprocess_config: CaliperOrchestrationPostprocessConfig,
    plugin,
    model,
    output_dir: Path,
    plugin_module: str,
    base_dir: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
) -> dict[str, Any]:
    """Generate KPI JSON using fork/exec subprocess execution."""

    if not postprocess_config.kpi.enabled:
        return {
            "status": "disabled",
            "reason": "kpi disabled",
            "completed_at": time.time(),
            "log_file": None,
        }
    if not postprocess_config.kpi.artifacts_to_kpis.enabled:
        return {
            "status": "disabled",
            "reason": "kpi.artifacts_to_kpis disabled",
            "completed_at": time.time(),
            "log_file": None,
        }

    try:
        # Prepare paths
        output_file = output_dir / postprocess_config.kpi.artifacts_to_kpis.output
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Create automatic status file path
        status_file = _generate_automatic_status_file_path(output_dir, "kpi_generate")

        # Build CLI command
        command = build_kpi_generate_command(
            config=postprocess_config,
            tree_root=base_dir,
            manifest_path=manifest_path,
            status_file=status_file,
            output_file=output_file,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper kpi generate",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        # Convert to expected format
        if result.returncode == 0 and status_data.get("success"):
            relative_path = _make_path_relative_to_base(output_file, env.ARTIFACT_DIR)
            logger.info(
                f"KPI generate: output_file={output_file}, env.ARTIFACT_DIR={env.ARTIFACT_DIR}, relative_path={relative_path}"
            )
            return {
                "status": "success",
                "output_file": relative_path,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            return {
                "status": "failed",
                "error": status_data.get("error", "Unknown error"),
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        # Log the full traceback to help with debugging
        full_traceback = traceback.format_exc()
        logger.error(f"KPI generation failed: {e}")
        logger.error(f"Full traceback:\n{full_traceback}")
        return {"status": "failed", "error": str(e), "completed_at": time.time(), "log_file": None}


def run_artifacts_to_ai_data(
    postprocess_config,
    plugin,
    model,
    output_dir: Path,
    plugin_module: str,
    base_dir: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
) -> dict[str, Any]:
    """Export AI evaluation data using fork/exec subprocess execution."""

    if not postprocess_config.kpi.artifacts_to_ai_data.enabled:
        return {
            "status": "disabled",
            "reason": "kpi.artifacts_to_ai_data disabled",
            "completed_at": time.time(),
            "log_file": None,
        }

    try:
        # Prepare paths
        output_file = output_dir / postprocess_config.kpi.artifacts_to_ai_data.output_dir
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Create automatic status file path
        status_file = _generate_automatic_status_file_path(output_dir, "ai_eval_export")

        # Build CLI command
        command = build_ai_eval_export_command(
            config=postprocess_config,
            tree_root=base_dir,
            manifest_path=manifest_path,
            status_file=status_file,
            output_file=output_file,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper ai-eval-export",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        # Convert to expected format
        if result.returncode == 0 and status_data.get("success"):
            relative_path = _make_path_relative_to_base(output_file, env.ARTIFACT_DIR)
            return {
                "status": "success",
                "output_file": relative_path,
                "ai_data_dir": relative_path,  # Expected by postprocess.py
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            return {
                "status": "failed",
                "error": status_data.get("error", "Unknown error"),
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        # Log the full traceback to help with debugging
        full_traceback = traceback.format_exc()
        logger.error(f"AI evaluation export failed: {e}")
        logger.error(f"Full traceback:\n{full_traceback}")
        return {"status": "failed", "error": str(e), "completed_at": time.time(), "log_file": None}


def run_dashboard_csv(
    postprocess_config: CaliperOrchestrationPostprocessConfig,
    plugin,
    model,
    output_dir: Path,
    kpi_json_path: Path,
    base_dir: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
) -> dict[str, Any]:
    """Export dashboard CSV independently from model data using fork/exec subprocess execution."""

    if not postprocess_config.kpi.dashboard_csv.enabled:
        return {
            "status": "disabled",
            "reason": "kpi.dashboard_csv disabled",
            "completed_at": time.time(),
            "log_file": None,
        }

    try:
        # Prepare paths
        output_file = output_dir / postprocess_config.kpi.dashboard_csv.output
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Create automatic status file path
        status_file = _generate_automatic_status_file_path(output_dir, "kpi_csv_export")

        # Build CLI command
        command = build_kpi_csv_export_command(
            config=postprocess_config,
            tree_root=base_dir,
            manifest_path=manifest_path,
            status_file=status_file,
            output_file=output_file,
            kpi_json_path=kpi_json_path,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper kpi csv-export",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        # Convert to expected format
        if result.returncode == 0 and status_data.get("success"):
            relative_path = _make_path_relative_to_base(output_file, env.ARTIFACT_DIR)
            return {
                "status": "success",
                "output_file": relative_path,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            return {
                "status": "failed",
                "error": status_data.get("error", "Unknown error"),
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        # Log the full traceback to help with debugging
        full_traceback = traceback.format_exc()
        logger.error(f"KPI CSV export failed: {e}")
        logger.error(f"Full traceback:\n{full_traceback}")
        return {"status": "failed", "error": str(e), "completed_at": time.time(), "log_file": None}


def run_analyse_kpis(
    postprocess_config: CaliperOrchestrationPostprocessConfig,
    output_dir: Path,
    plugin_module: str,
    base_dir: Path,
    manifest_path: Path | None,
    current_kpis_file: Path,
    step_logs_dir: Path,
) -> KpiAnalysisStatus:
    """Analyze KPIs using subprocess execution."""

    if not postprocess_config.analyze.enabled:
        return create_disabled_status(
            KpiAnalysisStatus,
            reason="analyze disabled",
        )

    try:
        # Prepare paths
        output_file = output_dir / postprocess_config.analyze.output
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Calculate historical KPIs directory path
        historical_kpis_dir = Path(postprocess_config.analyze.historical_kpis)
        if not historical_kpis_dir.is_absolute():
            historical_kpis_dir = output_dir / historical_kpis_dir

        # Create automatic status file path
        status_file = _generate_automatic_status_file_path(output_dir, "kpi_analysis")

        # Build CLI command
        command = build_analyse_kpis_command(
            config=postprocess_config,
            tree_root=base_dir,
            manifest_path=manifest_path,
            status_file=status_file,
            output_file=output_file,
            current_kpis_file=current_kpis_file,
            historical_kpis_dir=historical_kpis_dir,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper kpi analyse-kpis",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        # Convert dict status to KpiAnalysisStatus object
        if status_data:
            # Convert relative paths back to absolute for proper path handling
            if status_data.get("output_file"):
                status_data["output_file"] = str(Path(status_data["output_file"]).resolve())
            if status_data.get("html_file"):
                # Make html_file relative to output_dir for MLflow compatibility
                html_path = Path(status_data["html_file"])
                if html_path.is_absolute():
                    try:
                        status_data["html_file"] = str(html_path.relative_to(output_dir))
                    except ValueError:
                        # If can't make relative, use resolved absolute path as fallback
                        status_data["html_file"] = str(html_path.resolve())
                else:
                    status_data["html_file"] = str(html_path)

            # Ensure required fields are present with defaults
            status_data.setdefault("completed_at", time.time())

            # Map status field if needed
            if "status" not in status_data:
                if status_data.get("success"):
                    status_data["status"] = StatusLevel.SUCCESS.value
                else:
                    status_data["status"] = StatusLevel.FAILED.value
            elif isinstance(status_data["status"], str):
                # Convert string status back to StatusLevel enum (from YAML deserialization)
                status_data["status"] = StatusLevel(status_data["status"])

            # Create status object with filtered fields
            filtered_data = {
                k: v for k, v in status_data.items() if k in KpiAnalysisStatus.__dataclass_fields__
            }
            # Explicitly add log_file from _execute_caliper_command result
            if "log_file" in KpiAnalysisStatus.__dataclass_fields__:
                filtered_data["log_file"] = str(log_file) if log_file else None

            status = KpiAnalysisStatus(**filtered_data)
        else:
            from projects.caliper.public.status_models import create_failure_status

            return create_failure_status(
                KpiAnalysisStatus,
                error="No status data returned from CLI command",
                exit_code=result.returncode,
            )

        # Handle fail_on_regression policy
        if (
            status.status == StatusLevel.REGRESSION_DETECTED
            and not postprocess_config.analyze.fail_on_regression
        ):
            # Convert regression to warning if fail_on_regression is False
            status.status = StatusLevel.WARNING
            status.success = True

        # Make output file path relative to artifacts directory
        if status.output_file:
            status.output_file = str(Path(status.output_file).relative_to(env.ARTIFACT_DIR))

        return status

    except Exception as e:
        from projects.caliper.public.status_models import create_failure_status

        logger.exception("KPI analysis failed in run_analyse_kpis")
        return create_failure_status(
            KpiAnalysisStatus,
            error=str(e),
            exit_code=1,
        )


def run_parse_step(
    config: CaliperOrchestrationPostprocessConfig,
    tree_root: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
) -> tuple[bool, dict[str, Any]]:
    """Execute the parse step if enabled.

    Returns:
        Tuple of (success, step_result)
    """
    if not config.parse.enabled:
        return True, {"status": "disabled", "reason": "parse disabled", "completed_at": time.time()}

    # Create automatic status file path
    status_file = _generate_automatic_status_file_path(tree_root, "parse")

    try:
        # Build CLI command
        command = build_parse_command(
            config=config,
            tree_root=tree_root,
            manifest_path=manifest_path,
            status_file=status_file,
            use_cache=not config.parse.no_cache,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper parse",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        if result.returncode == 0 and status_data and status_data.get("success", False):
            return True, {
                "status": "success",
                "detail": "Parse completed successfully",
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            error_msg = (status_data or {}).get(
                "error", f"Command failed with exit code {result.returncode}"
            )
            logger.error("Caliper parse failed: %s", error_msg)
            return False, {
                "status": "failed",
                "detail": error_msg,
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        logger.exception("Parse step execution failed")
        return False, {
            "status": "failed",
            "detail": f"Exception during parse: {str(e)}",
            "completed_at": time.time(),
        }


def run_visualize_step(
    config: CaliperOrchestrationPostprocessConfig,
    tree_root: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
    output_dir: Path,
) -> tuple[bool, dict[str, Any]]:
    """Execute the visualize step if enabled.

    Returns:
        Tuple of (success, step_result)
    """
    if not config.visualize.enabled:
        return True, {
            "status": "disabled",
            "reason": "visualize disabled",
            "completed_at": time.time(),
        }

    # Create automatic status file path
    status_file = _generate_automatic_status_file_path(tree_root, "visualize")

    try:
        # Build CLI command
        command = build_visualize_command(
            config=config,
            tree_root=tree_root,
            manifest_path=manifest_path,
            status_file=status_file,
            output_dir=output_dir,
            use_cache=not config.parse.no_cache,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper visualize",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        if result.returncode == 0 and status_data and status_data.get("success", False):
            return True, {
                "status": "success",
                "detail": "Visualize completed successfully",
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            error_msg = (status_data or {}).get(
                "error", f"Command failed with exit code {result.returncode}"
            )
            logger.error("Caliper visualize failed: %s", error_msg)
            return False, {
                "status": "failed",
                "detail": error_msg,
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        logger.exception("Visualize step execution failed")
        return False, {
            "status": "failed",
            "detail": f"Exception during visualize: {str(e)}",
            "completed_at": time.time(),
        }


def run_s3_import_step(
    config: CaliperOrchestrationPostprocessConfig,
    output_dir: Path,
    step_logs_dir: Path,
) -> tuple[bool, dict[str, Any]]:
    """Execute the S3 import step.

    Returns:
        Tuple of (success, step_result)
    """
    if not config.s3.import_.enabled:
        return True, {
            "status": "disabled",
            "reason": "s3_import disabled",
            "completed_at": time.time(),
        }

    # Create automatic status file path
    status_file = _generate_automatic_status_file_path(output_dir, "s3_import")

    try:
        # Build CLI command
        command = build_s3_import_command(
            config=config,
            status_file=status_file,
            output_dir=output_dir,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper s3-import",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        if result.returncode == 0 and status_data and status_data.get("success", False):
            return True, {
                "status": "success",
                "detail": "S3 import completed successfully",
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            error_msg = (status_data or {}).get(
                "error", f"Command failed with exit code {result.returncode}"
            )
            logger.error("S3 import failed: %s", error_msg)
            return False, {
                "status": "failed",
                "detail": error_msg,
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        logger.exception("S3 import step execution failed")
        return False, {
            "status": "failed",
            "detail": f"Exception during s3-import: {str(e)}",
            "completed_at": time.time(),
        }


def run_s3_export_step(
    config: CaliperOrchestrationPostprocessConfig,
    step_logs_dir: Path,
    kpis_file: Path | None = None,
    csv_file: Path | None = None,
    ai_data_dir: Path | None = None,
    analysis_file: Path | None = None,
) -> dict[str, Any]:
    """Execute the S3 export step.

    Returns:
        Step result dictionary
    """
    if not config.s3.export.enabled:
        return {
            "status": "disabled",
            "reason": "s3_export disabled",
            "completed_at": time.time(),
        }

    # Create automatic status file path (use step_logs_dir.parent as base artifacts dir)
    status_file = _generate_automatic_status_file_path(step_logs_dir.parent, "s3_export")

    try:
        # Build CLI command
        command = build_s3_export_command(
            config=config,
            status_file=status_file,
            kpis_file=kpis_file,
            csv_file=csv_file,
            ai_data_dir=ai_data_dir,
            analysis_file=analysis_file,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper s3-export",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        if result.returncode == 0 and status_data and status_data.get("success", False):
            return {
                "status": "success",
                "detail": "S3 export completed successfully",
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }
        else:
            error_msg = (status_data or {}).get(
                "error", f"Command failed with exit code {result.returncode}"
            )
            logger.error("S3 export failed: %s", error_msg)
            return {
                "status": "failed",
                "detail": error_msg,
                "exit_code": result.returncode,
                "completed_at": time.time(),
                "log_file": log_file,
            }

    except Exception as e:
        logger.exception("S3 export step execution failed")
        return {
            "status": "failed",
            "detail": f"Exception during s3-export: {str(e)}",
            "completed_at": time.time(),
        }
