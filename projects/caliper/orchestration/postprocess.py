"""
Config-driven Caliper parse / visualize / KPI / AI eval for FORGE orchestration.

KPI generation and AI evaluation export are now implemented. Regression analyze is still
a stub. All steps maintain a stable ``steps`` shape for caller compatibility.

Computes ``final_status`` from the FORGE test phase outcome plus all enabled step results.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from projects.caliper.engine.constants import LEGACY_METADATA_FILE, METADATA_FILE
from projects.caliper.orchestration.caliper_invocation import (
    _execute_caliper_command,
    _generate_automatic_status_file_path,
    run_analyse_kpis,
)
from projects.caliper.orchestration.cli_builder import (
    build_ai_eval_export_command,
    build_analyse_kpis_command,
    build_kpi_csv_export_command,
    build_kpi_generate_command,
    build_kpis_to_mlflow_command,
    build_parse_command,
    build_s3_export_command,
    build_s3_import_command,
    build_visualize_command,
)
from projects.caliper.orchestration.postprocess_config import (
    CaliperOrchestrationPostprocessConfig,
)
from projects.caliper.orchestration.postprocess_outcome import (
    FINAL_KPI_PIPELINE_FAILED,
    FINAL_SUCCESS,
    TestPhaseOutcome,
    compute_final_postprocess_status,
)
from projects.caliper.orchestration.step_logging import (
    cleanup_step_logging,
)

# Import step result dataclasses
from projects.caliper.public import (
    AiDataStepResult,
    BaseStepResult,
    CsvExportStepResult,
    KpiAnalysisStepResult,
    KpiGenerateStepResult,
    ParseStepResult,
    S3StepResult,
    StepStatus,
    VisualizeStepResult,
)
from projects.core.library import env

logger = logging.getLogger(__name__)

# Postprocess status filename constant
POSTPROCESS_STATUS_FILENAME = "postprocess_status.yaml"


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
        if path_obj.is_absolute():
            return str(path_obj.relative_to(base_dir))
        else:
            return str(path_obj)
    except (ValueError, TypeError):
        # If can't make relative, return the filename
        return Path(file_path).name


_STUB_REASON_ANALYZE = "orchestration stub: regression analyze is not wired here (use Caliper CLI or extend orchestration)."


def _resolve_paths(
    postprocess_config: CaliperOrchestrationPostprocessConfig,
    *,
    artifacts_dir: Path,
) -> tuple[Path, Path | None, Path | None]:
    manifest_path = (
        Path(postprocess_config.postprocess_config).expanduser().resolve()
        if postprocess_config.postprocess_config
        else None
    )
    # Always use default cache behavior - store cache files with each test result
    cache_path = None
    return artifacts_dir.resolve(), manifest_path, cache_path


def _resolve_visualize_config_path(
    raw: str | None,
    *,
    artifact_tree: Path,
) -> Path | None:
    if raw is None or not str(raw).strip():
        return None
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()

    from projects.core.library import env

    return (env.FORGE_HOME / p).resolve()

    # _transform_kpis_to_hierarchical_format function removed - CLI commands now handle format conversion


def _run_artifacts_to_kpis(
    postprocess_config: CaliperOrchestrationPostprocessConfig,
    output_dir: Path,
    plugin_module: str,
    base_dir: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
) -> dict[str, Any]:
    """Generate KPI JSON using fork/exec subprocess execution."""

    if not postprocess_config.kpi.enabled:
        result = KpiGenerateStepResult(
            status=StepStatus.DISABLED,
            completed_at=time.time(),
            reason="kpi disabled",
            log_file=None,
            html_file=None,
        )
        return result
    if not postprocess_config.kpi.artifacts_to_kpis.enabled:
        result = KpiGenerateStepResult(
            status=StepStatus.DISABLED,
            completed_at=time.time(),
            reason="kpi.artifacts_to_kpis disabled",
            log_file=None,
            html_file=None,
        )
        return result

    try:
        # Prepare paths — reject absolute or parent-traversal output names
        configured_output = postprocess_config.kpi.artifacts_to_kpis.output
        if Path(configured_output).is_absolute() or ".." in Path(configured_output).parts:
            raise ValueError(
                f"kpi.artifacts_to_kpis.output must be a relative path without '..': {configured_output}"
            )
        output_file = output_dir / configured_output
        output_file.parent.mkdir(parents=True, exist_ok=True)

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
        if status_data.get("success"):
            relative_path = _make_path_relative_to_base(output_file, env.ARTIFACT_DIR)
            logger.info(
                f"KPI generate: output_file={output_file}, env.ARTIFACT_DIR={env.ARTIFACT_DIR}, relative_path={relative_path}"
            )

            # Handle HTML file path if available
            html_file = None
            if status_data.get("html_file"):
                html_file_path = Path(status_data["html_file"])
                html_file = _make_path_relative_to_base(html_file_path, env.ARTIFACT_DIR)

            result = KpiGenerateStepResult(
                status=StepStatus.SUCCESS,
                completed_at=time.time(),
                output_file=relative_path,
                log_file=log_file,
                html_file=html_file,
            )
            return result
        else:
            result = KpiGenerateStepResult(
                status=StepStatus.FAILED,
                completed_at=time.time(),
                error=status_data.get("error", "Unknown error"),
                log_file=log_file,
                html_file=None,
            )
            return result

    except Exception as e:
        # Log the full traceback to help with debugging
        import traceback

        full_traceback = traceback.format_exc()
        logger.error(f"KPI generation failed: {e}")
        logger.error(f"Full traceback:\n{full_traceback}")
        result = KpiGenerateStepResult(
            status=StepStatus.FAILED,
            completed_at=time.time(),
            error=str(e),
            log_file=None,
            html_file=None,
        )
        return result


def _run_artifacts_to_ai_data(
    postprocess_config,
    output_dir: Path,
    plugin_module: str,
    base_dir: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
) -> AiDataStepResult:
    """Export AI evaluation payload using fork/exec subprocess execution."""
    try:
        # Create AI data directory and output file path
        ai_data_dir = output_dir / postprocess_config.kpi.artifacts_to_ai_data.output_dir
        ai_data_dir.mkdir(parents=True, exist_ok=True)
        output_file = ai_data_dir / "ai_data_payload.json"

        # Create temporary status file for subprocess communication
        status_file = _generate_automatic_status_file_path(output_dir, "ai_eval_export")

        # Build CLI command
        command = build_ai_eval_export_command(
            config=postprocess_config,
            tree_root=base_dir,
            manifest_path=manifest_path,
            status_file=status_file,
            output_file=output_file,
            use_cache=True,  # Default to using cache in orchestration
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper ai-eval-export",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        # Convert to expected format
        if status_data.get("success"):
            result = AiDataStepResult(
                status=StepStatus.SUCCESS,
                completed_at=time.time(),
                output_file=status_data.get("output_file", str(ai_data_dir)),
                ai_data_dir=_make_path_relative_to_base(ai_data_dir, env.ARTIFACT_DIR),
                log_file=log_file,
            )
            return result
        else:
            result = AiDataStepResult(
                status=StepStatus.FAILED,
                completed_at=time.time(),
                error=status_data.get("error", "Unknown error"),
                log_file=log_file,
            )
            return result

    except Exception as e:
        # Log the full traceback to help with debugging
        import traceback

        full_traceback = traceback.format_exc()
        logger.error(f"AI eval export failed: {e}")
        logger.error(f"Full traceback:\n{full_traceback}")
        result = AiDataStepResult(
            status=StepStatus.FAILED,
            completed_at=time.time(),
            error=str(e),
            log_file=None,
        )
        return result


def _load_test_labels(test_dir: Path) -> dict[str, Any]:
    """Load test labels from metadata file (new format preferred, legacy fallback).

    Args:
        test_dir: Directory to search for metadata files

    Returns:
        Dictionary containing test labels, or empty dict if no file exists
    """
    import yaml

    # Try new format first
    metadata_file = test_dir / METADATA_FILE
    if metadata_file.exists():
        try:
            with open(metadata_file, encoding="utf-8") as f:
                labels = yaml.safe_load(f)
                logger.debug(f"Loaded test metadata from {metadata_file}: {labels}")
                return labels or {}
        except Exception as e:
            logger.error(f"Failed to load test metadata from {metadata_file}: {e}")

    # Fallback to legacy format
    legacy_file = test_dir / LEGACY_METADATA_FILE
    if legacy_file.exists():
        try:
            with open(legacy_file, encoding="utf-8") as f:
                labels = yaml.safe_load(f)
                logger.debug(f"Loaded test labels from legacy file {legacy_file}: {labels}")
                return labels or {}
        except Exception as e:
            logger.error(f"Failed to load test labels from {legacy_file}: {e}")

    logger.debug(f"No metadata or test labels file found in {test_dir}")
    return {}


def _run_dashboard_csv(
    postprocess_config: CaliperOrchestrationPostprocessConfig,
    output_dir: Path,
    base_dir: Path,
    manifest_path: Path | None,
    step_logs_dir: Path,
) -> dict[str, Any]:
    """Export dashboard CSV independently from model data using fork/exec subprocess execution."""

    if not postprocess_config.kpi.enabled:
        result = CsvExportStepResult(
            status=StepStatus.DISABLED,
            completed_at=time.time(),
            reason="kpi disabled",
            log_file=None,
        )
        return result
    if not postprocess_config.kpi.dashboard_csv.enabled:
        result = CsvExportStepResult(
            status=StepStatus.DISABLED,
            completed_at=time.time(),
            reason="kpi.dashboard_csv disabled",
            log_file=None,
        )
        return result

    try:
        csv_output = postprocess_config.kpi.dashboard_csv.output
        if Path(csv_output).is_absolute() or ".." in Path(csv_output).parts:
            raise ValueError(
                f"kpi.dashboard_csv.output must be a relative path without '..': {csv_output}"
            )
        output_file = output_dir / csv_output
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Create temporary status file for subprocess communication
        status_file = _generate_automatic_status_file_path(output_dir, "kpi_csv_export")

        # Build CLI command
        command = build_kpi_csv_export_command(
            config=postprocess_config,
            tree_root=base_dir,
            manifest_path=manifest_path,
            status_file=status_file,
            output_file=output_file,
        )

        # Execute command using generic function
        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper kpi csv-export",
            status_file=status_file,
            step_logs_dir=step_logs_dir,
        )

        # Convert to expected format
        if status_data.get("success"):
            result = CsvExportStepResult(
                status=StepStatus.SUCCESS,
                completed_at=time.time(),
                kpi_count=status_data.get("kpi_count", 0),
                output_file=_make_path_relative_to_base(output_file, env.ARTIFACT_DIR),
                log_file=log_file,
            )
            return result
        else:
            result = CsvExportStepResult(
                status=StepStatus.FAILED,
                completed_at=time.time(),
                error=status_data.get("error", "Unknown error"),
                log_file=log_file,
            )
            return result

    except Exception as e:
        # Log the full traceback to help with debugging
        import traceback

        full_traceback = traceback.format_exc()
        logger.error(f"KPI CSV export failed: {e}")
        logger.error(f"Full traceback:\n{full_traceback}")
        result = CsvExportStepResult(
            status=StepStatus.FAILED,
            completed_at=time.time(),
            error=str(e),
            log_file=None,
        )
        return result


def _run_analyse_kpis(
    postprocess_config: CaliperOrchestrationPostprocessConfig,
    output_dir: Path,
    plugin_module: str,
    base_dir: Path,
    manifest_path: Path | None,
    current_kpis_file: Path,
    step_logs_dir: Path,
) -> dict[str, Any]:
    """Analyze KPIs using fork/exec subprocess execution."""

    if not postprocess_config.analyze.enabled:
        result = BaseStepResult(
            status=StepStatus.DISABLED,
            completed_at=time.time(),
            reason="analyze disabled",
            log_file=None,
        )
        return result

    try:
        # Prepare paths
        output_file = output_dir / postprocess_config.analyze.output
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Calculate historical KPIs directory path
        historical_kpis_dir = Path(postprocess_config.analyze.historical_kpis)
        if not historical_kpis_dir.is_absolute():
            historical_kpis_dir = output_dir / historical_kpis_dir

        # Create temporary status file for subprocess communication
        status_file = _generate_automatic_status_file_path(output_dir, "analyse_kpis")

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

        # Convert to expected format
        result_data = {
            "status": status_data.get(
                "status", "failed" if not status_data.get("success") else "success"
            ),
            "completed_at": time.time(),
            "log_file": log_file,
        }

        # Add success-specific fields
        if status_data.get("success"):
            result_data["output_file"] = _make_path_relative_to_base(output_file, env.ARTIFACT_DIR)

        # Add optional fields if present
        if status_data.get("message"):
            result_data["message"] = status_data["message"]
        if status_data.get("error"):
            result_data["error"] = status_data["error"]

        return result_data

    except Exception as e:
        logger.exception("KPI analysis failed in _run_analyse_kpis")
        return {
            "status": StepStatus.FAILED,
            "message": str(e),
            "completed_at": time.time(),
            "log_file": None,
        }


class CaliperPostprocessOrchestrator:
    """
    Orchestrator for running Caliper postprocessing steps in sequence.

    Manages the execution of parse, visualize, KPI, AI evaluation, and analysis steps
    with proper state management, logging, and error handling.
    """

    def __init__(
        self,
        postprocess_config_raw: dict[str, Any] | None,
        *,
        artifacts_dir: Path,
        output_dir: Path | None = None,
        test_outcome: TestPhaseOutcome | None = None,
    ):
        self.artifacts_dir = artifacts_dir
        self.output_dir = output_dir or env.ARTIFACT_DIR
        self.test_outcome = test_outcome or TestPhaseOutcome("NOT_AVAILABLE")

        # State tracking
        self.steps: list[dict[str, Any]] = []
        # Initialize failure flags to None (skipped), will be set to False (success) or True (failure)
        self.parse_failed = None
        self.visualize_failed = None
        self.artifacts_to_kpis_failed = None
        self.ai_data_failed = None
        self.s3_import_failed = None
        self.analyze_failed = None
        self.s3_export_failed = None

        # Configuration
        try:
            self.config = CaliperOrchestrationPostprocessConfig.model_validate(
                postprocess_config_raw or {}
            )
        except ValidationError as e:
            logger.error("Invalid caliper postprocess config: %s", e)
            raise

        # Resolved paths - will be set in _setup_paths()
        self.tree_root: Path
        self.manifest_path: Path | None
        self.cache_path: Path
        self.step_logs_dir: Path

    def run(self) -> dict[str, Any]:
        """
        Run enabled parse / visualize steps and compute ``final_status``.

        Returns:
            Dictionary containing final_status, success flag, test_phase info, and step results
        """
        try:
            return self._execute_orchestration()
        finally:
            cleanup_step_logging()

    def _execute_orchestration(self) -> dict[str, Any]:
        """Main orchestration logic."""
        test_block = {"phase": self.test_outcome.phase, "message": self.test_outcome.message}

        # Check if postprocessing is enabled
        if not self.config.enabled:
            logger.info("caliper.postprocess.enabled is false — skipping post-processing steps")
            return self._build_result(
                compute_final_postprocess_status(
                    test_outcome=self.test_outcome,
                    parse_failed=False,
                    visualize_failed=False,
                    artifacts_to_kpis_failed=False,
                    ai_data_failed=False,
                    s3_import_failed=False,
                    analyze_failed=False,
                    s3_export_failed=False,
                    has_regression=False,
                    has_improvement=False,
                ),
                test_block,
            )

        # Setup paths and directories
        self._setup_paths()

        # Check if any steps are enabled
        if not self._any_step_enabled():
            logger.info("caliper.postprocess: no parse/visualize/kpi/analyze steps enabled")
            return self._build_result(
                compute_final_postprocess_status(
                    test_outcome=self.test_outcome,
                    parse_failed=False,
                    visualize_failed=False,
                    artifacts_to_kpis_failed=False,
                    ai_data_failed=False,
                    s3_import_failed=False,
                    analyze_failed=False,
                    s3_export_failed=False,
                    has_regression=False,
                    has_improvement=False,
                ),
                test_block,
            )

        # Execute steps in sequence
        logger.info("Starting postprocessing steps")
        self._run_parse_step()
        logger.info(f"After parse step: parse_failed={self.parse_failed}")

        # Abort pipeline if parse failed - no point in running other steps without data
        if self.parse_failed:
            logger.error("Parse step failed - aborting remaining postprocessing steps")
            logger.info("Pipeline aborted due to parse failure")
        else:
            self._run_visualize_step()
            logger.info(f"After visualize step: visualize_failed={self.visualize_failed}")

            self._run_kpi_and_ai_data_steps()
            logger.info(
                f"After KPI/AI steps: artifacts_to_kpis_failed={self.artifacts_to_kpis_failed}, ai_data_failed={self.ai_data_failed}"
            )
            logger.info("All postprocessing steps completed")

        # Compute final status and build result
        final_status = self._compute_final_status()
        result = self._build_result(final_status, test_block)

        # Generate HTML reports
        self._generate_reports(result)

        # Save postprocess status YAML for notifications
        self._save_postprocess_status_yaml(result)

        return result

    def _setup_paths(self) -> None:
        """Resolve and setup all required paths."""
        self.tree_root, self.manifest_path, self.cache_path = _resolve_paths(
            self.config, artifacts_dir=self.artifacts_dir
        )

        self.step_logs_dir = Path(env.ARTIFACT_DIR)
        self.step_logs_dir.mkdir(parents=True, exist_ok=True)

    def _add_step(
        self,
        step_name: str,
        step_data: dict[str, Any] | BaseStepResult,
        log_file: Path | None = None,
    ) -> None:
        """Add a step result to the steps list."""
        # Convert dataclass to dict if needed
        if isinstance(step_data, BaseStepResult):
            step_dict = step_data.to_dict()
        else:
            step_dict = step_data.copy()  # Copy to avoid modifying original

        if log_file:
            # Make log file path relative to artifact root where logs are actually stored
            try:
                artifact_root = Path(env.ARTIFACT_DIR)
                relative_log_path = Path(log_file).relative_to(artifact_root)
                step_dict["log_file"] = str(relative_log_path)
            except ValueError:
                # If log file is not under artifact root, use absolute path
                step_dict["log_file"] = str(log_file)

        self.steps.append({step_name: step_dict})

    def _check_step_result_and_set_failure(self, step_name: str, result: BaseStepResult) -> bool:
        """Check step result status and set appropriate failure flag.

        Args:
            step_name: Name of the step
            result: Step result dataclass object

        Returns:
            True if step failed or warned, False if successful
        """
        status = result.status.value

        # Map step names to their failure flags
        step_failure_map = {
            "artifacts_to_kpis": "artifacts_to_kpis_failed",
            "artifacts_to_ai_data": "ai_data_failed",
            "s3_import": "s3_import_failed",
            "analyse_kpis": "analyze_failed",
            "s3_export": "s3_export_failed",
        }

        failure_attr = step_failure_map.get(step_name)
        if not failure_attr:
            return False

        if status in ("failed", "warning"):
            setattr(self, failure_attr, True)

            if status == "warning":
                warning_msg = (
                    getattr(result, "reason", None)
                    or getattr(result, "message", None)
                    or "unknown warning"
                )
                logger.warning(f"Step '{step_name}' completed with warning: {warning_msg}")
            elif status == "failed":
                error_msg = (
                    getattr(result, "error", None)
                    or getattr(result, "reason", None)
                    or getattr(result, "message", None)
                    or "unknown error"
                )
                # Check for additional details (traceback/context info)
                detail_msg = getattr(result, "detail", None)
                logger.error(f"Step '{step_name}' failed: {error_msg}")
                if detail_msg:
                    logger.error("Additional details:\n%s", detail_msg)

            return True
        elif status == "success":
            # Set flag to False for successful steps
            setattr(self, failure_attr, False)
            return False

        # For other statuses (disabled, etc.), leave flag as None
        return False

    def _get_step(self, step_name: str) -> dict[str, Any]:
        """Get a step result by name."""
        for step in self.steps:
            if step_name in step:
                return step[step_name]
        return {}

    def _any_step_enabled(self) -> bool:
        """Check if any postprocessing step is enabled."""
        return (
            self.config.parse.enabled
            or self.config.visualize.enabled
            or self.config.kpi.enabled
            or self.config.analyze.enabled
        )

    def _has_only_warnings(self) -> bool:
        """Check if all problematic steps are warnings (not actual failures)."""
        has_any_problematic_steps = False
        has_actual_failures = False

        for step_dict in self.steps:
            for _step_name, step_data in step_dict.items():
                step_status = step_data.get("status")
                if step_status in ("failed", "warning"):
                    has_any_problematic_steps = True
                    if step_status == "failed":
                        has_actual_failures = True

        return has_any_problematic_steps and not has_actual_failures

    def _build_result(self, final_status: str, test_block: dict[str, Any]) -> dict[str, Any]:
        """Build the final result dictionary."""
        # Determine success value based on final status and warning-only detection
        if final_status == FINAL_SUCCESS:
            success_value = True
        elif final_status == FINAL_KPI_PIPELINE_FAILED and self._has_only_warnings():
            success_value = "warning"  # Special case for warnings-only
        else:
            success_value = False

        return {
            "final_status": final_status,
            "success": success_value,
            "base_directory": str(Path(env.ARTIFACT_DIR)),
            "test_phase": test_block,
            "steps": self.steps,
        }

    def _run_parse_step(self) -> None:
        """Execute the parse step if enabled."""
        if not self.config.parse.enabled:
            return

        # Create automatic status file path

        status_file = _generate_automatic_status_file_path(self.output_dir, "parse")

        try:
            # Build CLI command
            command = build_parse_command(
                config=self.config,
                tree_root=self.tree_root,
                manifest_path=self.manifest_path,
                status_file=status_file,
                use_cache=not self.config.parse.no_cache,
            )

            # Execute command using generic function
            result, status_data, log_file = _execute_caliper_command(
                command=command,
                step_name="caliper parse",
                status_file=status_file,
                step_logs_dir=self.step_logs_dir,
            )

            if result.returncode == 0 and status_data and status_data.get("success", False):
                record_count = status_data.get("parsed_records", 0)

                # Check if any records were parsed - fail if none found
                if record_count == 0:
                    self.parse_failed = True
                    logger.error("Caliper parse completed but found no records to process")
                    step_result = ParseStepResult(
                        status=StepStatus.FAILED,
                        completed_at=time.time(),
                        plugin_module=status_data.get("plugin_module", "unknown"),
                        record_count=record_count,
                        parse_cache_ref=status_data.get("cache_ref"),
                        reason="No records found - parsing completed successfully but no test data was extracted",
                    )
                else:
                    self.parse_failed = False
                    step_result = ParseStepResult(
                        status=StepStatus.SUCCESS,
                        completed_at=time.time(),
                        plugin_module=status_data.get("plugin_module", "unknown"),
                        record_count=record_count,
                        parse_cache_ref=status_data.get("cache_ref"),
                    )

                self._add_step("parse", step_result, log_file)
            else:
                self.parse_failed = True
                error_msg = (status_data or {}).get(
                    "error", f"Command failed with exit code {result.returncode}"
                )
                traceback_msg = (status_data or {}).get("traceback")
                logger.error("Caliper parse failed: %s", error_msg)
                if traceback_msg:
                    logger.error("Full traceback:\n%s", traceback_msg)
                self._add_step(
                    "parse",
                    ParseStepResult(
                        status=StepStatus.FAILED,
                        completed_at=time.time(),
                        detail=error_msg,
                        exit_code=result.returncode,
                    ),
                    log_file,
                )

        except Exception as e:  # noqa: BLE001
            self.parse_failed = True
            logger.exception("Parse step execution failed")
            self._add_step(
                "parse",
                ParseStepResult(
                    status=StepStatus.FAILED,
                    completed_at=time.time(),
                    detail=f"{str(e)}\n{traceback.format_exc()}",
                ),
                None,  # No log file if we couldn't even start
            )

    def _run_visualize_step(self) -> None:
        """Execute the visualize step if enabled."""
        if not self.config.visualize.enabled:
            return

        status_file = _generate_automatic_status_file_path(self.output_dir, "visualize")

        try:
            output_dir = Path(self.config.visualize.output_dir)
            if not output_dir.is_absolute():
                output_dir = self.output_dir / output_dir
            output_dir.mkdir(parents=True, exist_ok=True)

            # Build CLI command
            command = build_visualize_command(
                config=self.config,
                tree_root=self.tree_root,
                manifest_path=self.manifest_path,
                status_file=status_file,
                output_dir=output_dir,
                use_cache=not self.config.parse.no_cache,
            )

            # Execute command using generic function
            result, status_data, log_file = _execute_caliper_command(
                command=command,
                step_name="caliper visualize",
                status_file=status_file,
                step_logs_dir=self.step_logs_dir,
            )

            if result.returncode == 0 and status_data and status_data.get("success", False):
                # Get output files and paths from status
                output_files = status_data.get("output_files", [])

                # Convert absolute paths to relative paths from output_dir if needed
                relative_paths = []
                for path in output_files:
                    try:
                        path_obj = Path(path)
                        if path_obj.is_absolute():
                            relative_path = path_obj.relative_to(self.output_dir)
                            relative_paths.append(str(relative_path))
                        else:
                            relative_paths.append(str(path))
                    except ValueError:
                        # If path is not under output_dir, keep as-is
                        relative_paths.append(str(path))

                # Calculate relative output_dir path from base_directory
                try:
                    relative_output_dir = str(self.output_dir.relative_to(env.ARTIFACT_DIR))
                except ValueError:
                    # If output_dir is not under ARTIFACT_DIR, use absolute path
                    relative_output_dir = str(self.output_dir)

                self.visualize_failed = False
                self._add_step(
                    "visualize",
                    VisualizeStepResult(
                        status=StepStatus.SUCCESS,
                        completed_at=time.time(),
                        plugin_module=status_data.get("plugin_module", "unknown"),
                        output_files=relative_paths,
                        output_dir=relative_output_dir,
                        generated_files=status_data.get("generated_files", len(relative_paths)),
                    ),
                    log_file,
                )
            else:
                self.visualize_failed = True
                error_msg = (status_data or {}).get(
                    "error", f"Command failed with exit code {result.returncode}"
                )
                traceback_msg = (status_data or {}).get("traceback")
                logger.error("Caliper visualize failed: %s", error_msg)
                if traceback_msg:
                    logger.error("Full traceback:\n%s", traceback_msg)
                self._add_step(
                    "visualize",
                    VisualizeStepResult(
                        status=StepStatus.FAILED,
                        completed_at=time.time(),
                        detail=error_msg,
                        exit_code=result.returncode,
                    ),
                    log_file,
                )

        except Exception as e:  # noqa: BLE001
            self.visualize_failed = True
            logger.exception("Visualize step execution failed")
            self._add_step(
                "visualize",
                VisualizeStepResult(
                    status=StepStatus.FAILED,
                    completed_at=time.time(),
                    detail=f"{str(e)}\n{traceback.format_exc()}",
                ),
                None,  # No log file if we couldn't even start
            )

    def _run_kpi_and_ai_data_steps(self) -> None:
        """Execute KPI generation, CSV export, KPI export, and AI evaluation steps."""
        if not self.config.kpi.enabled:
            return

        # Setup output directory and module string with focused error handling
        try:
            # Determine output directory for KPI/AI data steps - use base artifact directory
            logger.info(f"KPI steps using base artifact directory: {self.output_dir}")
            self.output_dir.mkdir(parents=True, exist_ok=True)

            # Resolve plugin module string (this is just string manipulation, not engine access)
            mod_str = self.config.plugin_module or "unknown"
        except Exception as e:
            completion_time = time.time()
            logger.error(f"Failed to setup KPI/AI eval operations: {e}")
            # Only mark all steps as failed if basic setup fails
            for step_name in [
                "artifacts_to_kpis",
                "dashboard_csv",
                "artifacts_to_ai_data",
                "s3_import",
                "analyse_kpis",
                "s3_export",
            ]:
                self._add_step(
                    step_name,
                    BaseStepResult(
                        status=StepStatus.FAILED,
                        completed_at=completion_time,
                        reason=f"Setup failed: {e}",
                    ),
                )
            self.artifacts_to_kpis_failed = True
            self.ai_data_failed = True
            self.s3_import_failed = True
            self.analyze_failed = True
            self.s3_export_failed = True
            return

        # Run each step independently - each has its own error handling
        # KPI JSON generation
        self._run_artifacts_to_kpis_step(mod_str)

        # Generate per-run metrics.json + parameters.json from kpis.json
        self._run_kpis_to_metrics_step()

        # KPI CSV export
        self._run_dashboard_csv_step()

        # AI evaluation export
        self._run_artifacts_to_ai_data_step(mod_str)

        # S3 import (historical data)
        self._run_s3_import_step()

        # Analyze KPIs (current vs historical) - moved before S3 export
        self._run_analyse_kpis_step(mod_str)

        # S3 export
        self._run_s3_export_step()

    def _run_artifacts_to_kpis_step(self, mod_str: str) -> None:
        """Execute the KPI generation step."""
        if not self.config.kpi.artifacts_to_kpis.enabled:
            self._add_step(
                "artifacts_to_kpis",
                KpiGenerateStepResult(
                    status=StepStatus.DISABLED,
                    completed_at=time.time(),
                    reason="kpi.artifacts_to_kpis disabled",
                    html_file=None,
                ),
            )
            return

        result = _run_artifacts_to_kpis(
            self.config,
            self.output_dir,
            mod_str,
            self.tree_root,
            self.manifest_path,
            self.step_logs_dir,
        )
        log_file = result.log_file
        self._add_step("artifacts_to_kpis", result, log_file)
        self._check_step_result_and_set_failure("artifacts_to_kpis", result)

    def _run_kpis_to_metrics_step(self) -> None:
        """Generate per-run metrics.json + parameters.json from kpis.json.

        Runs automatically after kpis.json generation succeeds. Uses
        ``caliper kpi kpis-to-mlflow`` via fork/exec like all other steps.

        Note: This step is optional and only runs if artifacts_to_kpis step was executed successfully.
        """
        # Check if artifacts_to_kpis step exists and was successful
        kpi_step = self._get_step("artifacts_to_kpis")
        if not kpi_step or kpi_step.get("status") != "success":
            # artifacts_to_kpis step was not run or failed - skip MLflow metrics generation
            return

        # Check if artifacts_to_kpis is enabled in config
        if (
            not hasattr(self.config.kpi, "artifacts_to_kpis")
            or not self.config.kpi.artifacts_to_kpis.enabled
        ):
            # artifacts_to_kpis disabled - skip MLflow metrics generation
            return

        kpis_json_path = self.output_dir / self.config.kpi.artifacts_to_kpis.output

        # Check if the KPI JSON file actually exists
        if not kpis_json_path.exists():
            # No KPI JSON file available - skip MLflow metrics generation
            return

        status_file = _generate_automatic_status_file_path(self.output_dir, "kpis_to_mlflow")

        command = build_kpis_to_mlflow_command(
            tree_root=self.tree_root,
            status_file=status_file,
            input_file=kpis_json_path,
        )

        result, status_data, log_file = _execute_caliper_command(
            command=command,
            step_name="caliper kpi kpis-to-mlflow",
            status_file=status_file,
            step_logs_dir=self.step_logs_dir,
        )

        # Create a dict with all necessary fields since we need dynamic fields for MLflow
        step_result_dict = {
            "status": StepStatus.SUCCESS if status_data.get("success") else StepStatus.FAILED,
            "completed_at": time.time(),
        }
        if status_data.get("success"):
            step_result_dict["tests_processed"] = status_data.get("tests_processed", 0)
            step_result_dict["total_tests"] = status_data.get("total_tests", 0)

        # Convert enum to string for proper serialization
        step_result_dict["status"] = step_result_dict["status"].value
        self._add_step("kpis_to_mlflow", step_result_dict, log_file)

        if result.returncode != 0 or not status_data.get("success"):
            error = status_data.get("error", f"exit code {result.returncode}")
            logger.error("kpis-to-mlflow step failed: %s", error)

    def _run_dashboard_csv_step(self) -> None:
        """Execute the dashboard CSV export step."""
        if not self.config.kpi.dashboard_csv.enabled:
            self._add_step(
                "dashboard_csv",
                CsvExportStepResult(
                    status=StepStatus.DISABLED,
                    completed_at=time.time(),
                    reason="kpi.dashboard_csv disabled",
                ),
            )
            return

        result = _run_dashboard_csv(
            self.config,
            self.output_dir,
            self.tree_root,
            self.manifest_path,
            self.step_logs_dir,
        )
        log_file = result.log_file
        self._add_step("dashboard_csv", result, log_file)
        if result.status == StepStatus.FAILED:
            # CSV export failure doesn't affect overall status - it's supplementary
            logger.warning("KPI CSV export failed but continuing execution")

    def _run_artifacts_to_ai_data_step(self, mod_str: str) -> None:
        """Execute the AI evaluation export step."""
        if not self.config.kpi.artifacts_to_ai_data.enabled:
            self._add_step(
                "artifacts_to_ai_data",
                AiDataStepResult(
                    status=StepStatus.DISABLED,
                    completed_at=time.time(),
                    reason="kpi.artifacts_to_ai_data disabled",
                ),
            )
            return

        try:
            result = _run_artifacts_to_ai_data(
                self.config,
                self.output_dir,
                mod_str,
                self.tree_root,
                self.manifest_path,
                self.step_logs_dir,
            )
            log_file = result.log_file
            self._add_step("artifacts_to_ai_data", result, log_file)

            logger.info("AI eval export result:")
            logger.info(json.dumps(result.to_dict(), indent=2, default=str))

            # Check if the result indicates failure or warning
            self._check_step_result_and_set_failure("artifacts_to_ai_data", result)

        except Exception as e:
            logger.exception("AI eval export failed")
            step_result = AiDataStepResult(
                status=StepStatus.FAILED,
                completed_at=time.time(),
                error=str(e),
            )
            self._add_step("artifacts_to_ai_data", step_result, None)
            self._check_step_result_and_set_failure("artifacts_to_ai_data", step_result)

    def _run_s3_import_step(self) -> None:
        """Execute the S3 import step."""
        if not self.config.s3.import_.enabled:
            self._add_step(
                "s3_import",
                S3StepResult(
                    status=StepStatus.DISABLED,
                    completed_at=time.time(),
                    reason="s3_import disabled",
                ),
            )
            return

        try:
            # Create temporary status file for subprocess communication
            status_file = _generate_automatic_status_file_path(self.output_dir, "s3_import")

            # Build CLI command
            command = build_s3_import_command(
                config=self.config,
                status_file=status_file,
                output_dir=self.output_dir,
            )

            # Execute command using generic function
            result, status_data, log_file = _execute_caliper_command(
                command=command,
                step_name="caliper s3 import",
                status_file=status_file,
                step_logs_dir=self.step_logs_dir,
            )

            # Convert to expected format
            if status_data.get("success"):
                # Get the actual import directory (where files were downloaded)
                import_dir = self.output_dir / self.config.s3.import_.output_dir
                step_result = S3StepResult(
                    status=StepStatus.SUCCESS,
                    completed_at=time.time(),
                    output_dir=_make_path_relative_to_base(import_dir, env.ARTIFACT_DIR),
                    file_count=status_data.get("file_count", 0),
                    reason=status_data.get("warning"),
                    imported_path=status_data.get("imported_path"),
                )
            else:
                step_result = S3StepResult(
                    status=StepStatus.FAILED,
                    completed_at=time.time(),
                    error=status_data.get("error", "Unknown error"),
                    imported_path=status_data.get("imported_path"),
                )

            self._add_step("s3_import", step_result, log_file)

            logger.info("S3 import result:")
            logger.info(json.dumps(step_result.to_dict(), indent=2, default=str))

            self._check_step_result_and_set_failure("s3_import", step_result)

        except Exception as e:
            logger.exception("S3 import failed")
            step_result = S3StepResult(
                status=StepStatus.FAILED,
                completed_at=time.time(),
                error=str(e),
            )
            self._add_step("s3_import", step_result, None)
            self._check_step_result_and_set_failure("s3_import", step_result)

    def _run_analyse_kpis_step(self, plugin_module: str) -> None:
        """Execute the KPI analysis step."""
        if not self.config.analyze.enabled:
            self._add_step(
                "analyse_kpis",
                KpiAnalysisStepResult(
                    status=StepStatus.DISABLED,
                    completed_at=time.time(),
                    reason="analyze disabled",
                    html_file=None,
                ),
            )
            return

        # Get current KPI file path from config
        current_kpis_file = Path(self.config.analyze.current_kpis)
        if not current_kpis_file.is_absolute():
            current_kpis_path = self.output_dir / current_kpis_file
        else:
            current_kpis_path = current_kpis_file

        # Check if current KPI file exists
        if not current_kpis_path.exists():
            self._add_step(
                "analyse_kpis",
                KpiAnalysisStepResult(
                    status=StepStatus.FAILED,
                    completed_at=time.time(),
                    error=f"Current KPI file not found: {current_kpis_path}",
                    html_file=None,
                ),
            )
            self.analyze_failed = True
            return

        status = run_analyse_kpis(
            postprocess_config=self.config,
            output_dir=self.output_dir,
            plugin_module=plugin_module,
            base_dir=self.tree_root,
            manifest_path=self.manifest_path,
            current_kpis_file=current_kpis_path,
            step_logs_dir=self.step_logs_dir,
        )

        # Convert typed status to KpiAnalysisStepResult
        result = KpiAnalysisStepResult(
            status=status.status,
            completed_at=status.completed_at,
            success=status.success,
            exit_code=status.exit_code,
            output_file=status.output_file,
            error=status.error,
            message=status.message,
            regressions_detected=status.regressions_detected,
            regression_count=status.regression_count,
            total_kpis=status.total_kpis,
            log_file=status.log_file,
            html_file=status.html_file,
        )

        self._add_step("analyse_kpis", result, result.log_file)

        logger.info("KPI analysis result:")
        logger.info(json.dumps(result.to_dict(), indent=2, default=str))

        # Handle regression policy - this is now handled in run_analyse_kpis
        if status.regressions_detected:
            logger.info("Regression detected!")
            if self.config.analyze.fail_on_regression:
                logger.info("fail_on_regression is set")
            else:
                logger.info("fail_on_regression is not set")

        if not self.config.analyze.fail_on_regression:
            logger.info("analyse_kpis warning ignored: fail_on_regression is not set")
        else:
            self._check_step_result_and_set_failure("analyse_kpis", result)

    def _run_s3_export_step(self) -> None:
        """Execute the S3 export step."""
        if not self.config.s3.export.enabled:
            self._add_step(
                "s3_export",
                S3StepResult(
                    status=StepStatus.DISABLED,
                    completed_at=time.time(),
                    reason="s3_export disabled",
                ),
            )
            return

        try:
            # Collect file paths from previous steps
            kpis_file = None
            csv_file = None
            ai_data_dir = None
            analysis_file = None

            # Get KPI JSON file from artifacts_to_kpis step
            artifacts_to_kpis_step = self._get_step("artifacts_to_kpis")
            if (
                artifacts_to_kpis_step
                and artifacts_to_kpis_step.get("status") == "success"
                and artifacts_to_kpis_step.get("output_file")
            ):
                kpis_file = self.output_dir / self.config.kpi.artifacts_to_kpis.output

            # Get CSV file from dashboard_csv step
            dashboard_csv_step = self._get_step("dashboard_csv")
            if (
                dashboard_csv_step
                and dashboard_csv_step.get("status") == "success"
                and dashboard_csv_step.get("output_file")
            ):
                csv_output = self.config.kpi.dashboard_csv.output
                csv_file = self.output_dir / csv_output

            # Get AI data directory from artifacts_to_ai_data step
            ai_data_step = self._get_step("artifacts_to_ai_data")

            logger.info(f"AI data step found:\n{json.dumps(ai_data_step, indent=2)}")
            if (
                ai_data_step
                and ai_data_step.get("status") == "success"
                and ai_data_step.get("ai_data_dir")
            ):
                ai_data_dir = env.ARTIFACT_DIR / ai_data_step["ai_data_dir"]
                logger.info(f"AI data directory set to: {ai_data_dir}")
            else:
                logger.warning(
                    f"AI data directory not found - step status: {ai_data_step.get('status') if ai_data_step else 'step not found'}, ai_data_dir: {ai_data_step.get('ai_data_dir') if ai_data_step else 'N/A'}"
                )

            # Get analysis file from analyse_kpis step
            analyze_step = self._get_step("analyse_kpis")
            if (
                analyze_step
                and analyze_step.get("status") in ("success", "warning")
                and analyze_step.get("output_file")
            ):
                analysis_file = env.ARTIFACT_DIR / analyze_step["output_file"]

            # Create temporary status file for subprocess communication
            status_file = _generate_automatic_status_file_path(self.output_dir, "s3_export")

            # Build CLI command
            command = build_s3_export_command(
                config=self.config,
                status_file=status_file,
                kpis_file=kpis_file,
                csv_file=csv_file,
                ai_data_dir=ai_data_dir,
                analysis_file=analysis_file,
            )

            # Execute command using generic function
            result, status_data, log_file = _execute_caliper_command(
                command=command,
                step_name="caliper s3 export",
                status_file=status_file,
                step_logs_dir=self.step_logs_dir,
            )

            # Convert to expected format
            if status_data.get("success"):
                step_result = S3StepResult(
                    status=StepStatus.SUCCESS,
                    completed_at=time.time(),
                    exported_path=status_data.get("exported_path", ""),
                    uploaded_files=status_data.get("uploaded_files", 0),
                    total_files=status_data.get("total_files", 0),
                )

                # Format status for better readability
                uploaded_files = step_result.uploaded_files or 0
                total_files = step_result.total_files or 0
                s3_path = step_result.exported_path or ""

                logger.info(
                    f"S3 export completed successfully: "
                    f"{uploaded_files}/{total_files} files uploaded to {s3_path}"
                )
            else:
                step_result = S3StepResult(
                    status=StepStatus.FAILED,
                    completed_at=time.time(),
                    error=status_data.get("error", "Unknown error"),
                )
                self._check_step_result_and_set_failure("s3_export", step_result)

            self._add_step("s3_export", step_result, log_file)

        except Exception as e:
            error_msg = f"S3 export step failed with exception: {type(e).__name__}: {str(e)}"

            # Log programming errors more prominently
            if isinstance(e, AttributeError | NameError | TypeError):
                logger.error(f"CRITICAL PROGRAMMING ERROR in S3 export: {error_msg}")
                logger.exception("Full traceback for programming error:")
            else:
                logger.error(error_msg)
                logger.exception("Full traceback:")

            step_result = S3StepResult(
                status=StepStatus.FAILED,
                completed_at=time.time(),
                error=error_msg,
                detail=type(e).__name__,
            )
            self._add_step("s3_export", step_result, None)
            self._check_step_result_and_set_failure("s3_export", step_result)

    def _compute_final_status(self) -> str:
        """Compute the final postprocessing status."""
        # Debug logging to identify what's causing failures
        logger.info("Computing final status with failure flags:")
        logger.info(f"  test_outcome.phase: {self.test_outcome.phase}")
        logger.info(f"  parse_failed: {self.parse_failed}")
        logger.info(f"  visualize_failed: {self.visualize_failed}")
        logger.info(f"  artifacts_to_kpis_failed: {self.artifacts_to_kpis_failed}")
        logger.info(f"  ai_data_failed: {self.ai_data_failed}")
        logger.info(f"  s3_import_failed: {self.s3_import_failed}")
        logger.info(f"  analyze_failed: {self.analyze_failed}")
        logger.info(f"  s3_export_failed: {self.s3_export_failed}")

        final_status = compute_final_postprocess_status(
            test_outcome=self.test_outcome,
            parse_failed=self.parse_failed,
            visualize_failed=self.visualize_failed,
            artifacts_to_kpis_failed=self.artifacts_to_kpis_failed,
            ai_data_failed=self.ai_data_failed,
            s3_import_failed=self.s3_import_failed,
            analyze_failed=self.analyze_failed,
            s3_export_failed=self.s3_export_failed,
            has_regression=False,
            has_improvement=False,
        )

        logger.info(f"Computed final status: {final_status}")
        return final_status

    def _generate_reports(self, result: dict[str, Any]) -> None:
        """Generate HTML reports."""

        # Import here to avoid circular imports
        from projects.core.library.postprocess import generate_postprocess_status_report
        from projects.core.library.reports_index import generate_caliper_reports_index

        try:
            generate_caliper_reports_index(result, self.output_dir, "reports_index.html")
        except Exception as e:
            logger.warning("Failed to generate reports index: %s", e)

        try:
            generate_postprocess_status_report(result, self.output_dir, "postprocess_status.html")
        except Exception as e:
            logger.warning("Failed to generate postprocessing status report: %s", e)

    def _save_postprocess_status_yaml(self, result: dict[str, Any]) -> None:
        """Save postprocess status as YAML for GitHub notifications."""
        try:
            from projects.caliper.public import (
                PostprocessStatus,
                save_postprocess_status_yaml,
            )

            output_dir = self.output_dir / "status_files"
            output_dir.mkdir(parents=True, exist_ok=True)
            status_file = output_dir / POSTPROCESS_STATUS_FILENAME

            # Convert to typed status object
            status = PostprocessStatus.from_orchestration_result(result)

            # Save using typed YAML function
            save_postprocess_status_yaml(status, status_file)

            logger.info(f"Saved postprocess status to {status_file}")

        except Exception as e:
            logger.warning(f"Failed to save postprocess status YAML: {e}")


def run_postprocess_from_orchestration_config(
    postprocess_config_raw: dict[str, Any] | None,
    *,
    artifacts_dir: Path,
    output_dir: Path | None = None,
    test_outcome: TestPhaseOutcome | None = None,
) -> dict[str, Any]:
    """
    Run enabled parse / visualize steps and compute ``final_status``.

    KPI and analyze sections only emit stub ``steps`` entries (never failures).

    Parse/visualize use ``artifacts_dir`` and ``output_dir``.
    """
    orchestrator = CaliperPostprocessOrchestrator(
        postprocess_config_raw,
        artifacts_dir=artifacts_dir,
        output_dir=output_dir or env.ARTIFACT_DIR,
        test_outcome=test_outcome,
    )

    return orchestrator.run()
