"""GuideLLM Caliper PostProcessingPlugin (`projects/caliper/postprocess/guidellm`)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from projects.caliper.engine.kpi import KpiCatalogEntry, KpiComputationStatus, KpiRecord
from projects.caliper.engine.model import (
    ParseResult,
    PostProcessingPlugin,
    TestBaseNode,
    UnifiedRunModel,
)
from projects.llm_d.postprocess.llm_d.parsing.kpis import GuideLLMKpiHandler

from .ai_eval import GuideLLMAIEvaluator
from .parsing import GuideLLMParser

logger = logging.getLogger(__name__)


class _PlotRegistry:
    """Lazy-loading plot registry that avoids importing pandas at module load time."""

    def __init__(self):
        self._registry: dict[str, dict[str, Any]] | None = None

    def _ensure_loaded(self) -> dict[str, dict[str, Any]]:
        """Populate registry on first use, keeping pandas out of module load."""
        if self._registry is not None:
            return self._registry

        from .plotting.performance_analysis import (
            generate_comprehensive_performance_report,
            generate_deployment_profile_report,
        )

        self._registry = {
            "report_performance_analysis": {
                "function": generate_comprehensive_performance_report,
                "type": "report",
                "kwargs": {
                    "report_title": "GuideLLM Performance Analysis",
                },
                "description": "comprehensive performance analysis report (recommended)",
            },
            "report_deployment_profile": {
                "function": generate_deployment_profile_report,
                "type": "report",
                "kwargs": {
                    "report_title": "GuideLLM Deployment Profile Analysis",
                },
                "description": "performance analysis comparing different product versions/models under identical test conditions",
            },
        }
        return self._registry

    def __getitem__(self, key):
        return self._ensure_loaded()[key]

    def __contains__(self, key):
        return key in self._ensure_loaded()

    def __iter__(self):
        return iter(self._ensure_loaded())

    def keys(self):
        return self._ensure_loaded().keys()

    def items(self):
        return self._ensure_loaded().items()

    def values(self):
        return self._ensure_loaded().values()

    def get(self, key, default=None):
        return self._ensure_loaded().get(key, default)

    def __setitem__(self, key, value):
        # Ensure registry is loaded, then set the item
        registry = self._ensure_loaded()
        registry[key] = value


PLOT_REGISTRY = _PlotRegistry()


class GuideLLMPlugin(PostProcessingPlugin):
    """
    Parses GuideLLM benchmark artifacts containing ``benchmarks.json`` files.
    """

    def __init__(self):
        self.parser = GuideLLMParser()
        self.kpi_handler = GuideLLMKpiHandler()
        self.ai_evaluator = GuideLLMAIEvaluator()

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        """Parse test nodes using the GuideLLM parser."""
        return self.parser.parse(nodes)

    def get_available_reports(self) -> dict[str, dict[str, str]]:
        """Get a structured dictionary of available reports and plots with their types and descriptions."""
        return {
            name: {
                "type": config["type"],
                "description": config["description"],
            }
            for name, config in PLOT_REGISTRY.items()
        }

    def get_available_reports_by_type(self) -> dict[str, dict[str, str]]:
        """Get reports and plots grouped by type."""
        result = {"reports": {}, "plots": {}}
        for name, config in PLOT_REGISTRY.items():
            type_key = "reports" if config["type"] == "report" else "plots"
            result[type_key][name] = config["description"]
        return result

    def get_reports_only(self) -> dict[str, str]:
        """Get only comprehensive reports (HTML files with multiple plots)."""
        return {
            name: config["description"]
            for name, config in PLOT_REGISTRY.items()
            if config["type"] == "report"
        }

    def get_plots_only(self) -> dict[str, str]:
        """Get only individual plots (single visualizations)."""
        return {
            name: config["description"]
            for name, config in PLOT_REGISTRY.items()
            if config["type"] == "plot"
        }

    @staticmethod
    def register_plot(  # noqa: FBT001
        name: str, function: callable, description: str, type_: str = "plot", **kwargs
    ) -> None:
        """Register a new plot or report generator function.

        Args:
            name: Report name (used with --reports)
            function: Generator function that takes (records, output_dir, **kwargs)
            description: Human-readable description for help text
            type_: Type of visualization ("plot" for single chart, "report" for comprehensive HTML)
            **kwargs: Additional arguments passed to the function

        Example:
            GuideLLMPlugin.register_plot(
                "custom_analysis",
                my_custom_function,
                "My custom analysis",
                type_="plot",
                custom_param="value"
            )
        """
        PLOT_REGISTRY[name] = {
            "function": function,
            "type": type_,
            "description": description,
            "kwargs": kwargs,
        }

    def visualize(
        self,
        model: UnifiedRunModel,
        output_dir: Path,
        report_ids: list[str] | None,
        group_id: str | None,
        visualize_config: dict[str, Any] | None,
    ) -> list[str]:
        """Generate visualization reports for GuideLLM benchmarks."""
        output_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        # Preserve caller-supplied order while removing duplicates
        wanted = list(dict.fromkeys(report_ids or ()))

        # Filter to only GuideLLM records with benchmarks
        guidellm_records = [
            r
            for r in model.unified_result_records
            if r.run_identity.get("guidellm") and not r.metrics.get("no_benchmarks_found")
        ]

        if not guidellm_records:
            return paths

        # Generate reports using the registry
        invalid_reports: list[str] = []
        report_counter = 0

        for report_name in wanted:
            if report_name not in PLOT_REGISTRY:
                logger.warning("Unknown report '%s' requested", report_name)
                invalid_reports.append(report_name)
                continue

            plot_config = PLOT_REGISTRY[report_name]
            function = plot_config["function"]
            kwargs = plot_config.get("kwargs", {}).copy()  # Copy to avoid modifying the registry

            # Add automatic report numbering for report types
            if plot_config["type"] == "report":
                kwargs["report_number"] = report_counter
                report_counter += 1

            try:
                # Call the generator function with records, output_dir, and any additional kwargs
                path = function(guidellm_records, output_dir, **kwargs)
                if path:
                    paths.append(path)
                    logger.info("Generated %s: %s", report_name, path)
                else:
                    logger.warning("Failed to generate %s", report_name)
            except Exception:
                logger.exception("Error generating %s", report_name)
                raise

        # Check for invalid report names and raise error if found
        if invalid_reports:
            available_reports = list(PLOT_REGISTRY.keys())
            raise ValueError(
                f"Invalid report types requested: {sorted(invalid_reports)}. "
                f"Available types in this plugin: {sorted(available_reports)}"
            )

        return paths

    def compute_kpis(self, model: UnifiedRunModel) -> tuple[list[KpiRecord], KpiComputationStatus]:
        """Compute KPI values using dataclasses with status details."""
        return self.kpi_handler.compute_kpis(model)

    def export_dashboard_csv(self, model: UnifiedRunModel, output_path: Path) -> str:
        """Generate dashboard CSV using shared architecture."""
        from projects.guidellm.postprocess.guidellm.csv_dashboard import BASIC_GUIDELLM_FIELDNAMES
        from projects.guidellm.postprocess.guidellm.dashboard import DashboardCsvExporter

        def metadata_row_mapper(labels: dict[str, Any]) -> dict[str, Any]:
            """Extract GuideLLM metadata for CSV row from dashboard KPI labels."""
            accelerator = labels.get("gpu_type") or labels.get("accelerator", "")
            model_id = labels.get("hf_model_id") or labels.get("model_name", "")
            run_model = model_id.replace("/", "-")
            tp = labels.get("tensor_parallel_size", "")

            return {
                "run": "-".join(str(value) for value in (accelerator, run_model, tp) if value),
                "accelerator": accelerator,
                "model": model_id,
                "version": labels.get("version", ""),
                "prompt toks": labels.get("prompt_toks", ""),
                "output toks": labels.get("output_toks", ""),
                "TP": tp,
                "uuid": labels.get("run_uuid", ""),
                "runtime_args": labels.get("runtime_args", ""),
                "guidellm_start_time_ms": labels.get("guidellm_start_time_ms", ""),
                "guidellm_end_time_ms": labels.get("guidellm_end_time_ms", ""),
                "image_tag": labels.get("image_tag", ""),
                "guidellm_version": labels.get("guidellm_version", ""),
                "mlflow_run_id": labels.get("mlflow_run_id", ""),
                "mlflow_experiment_id": labels.get("mlflow_experiment_id", ""),
                "notes": labels.get("notes", ""),
            }

        exporter = DashboardCsvExporter()
        return exporter.export_dashboard_csv(
            model,
            output_path,
            prefix="guidellm",
            fieldnames=BASIC_GUIDELLM_FIELDNAMES,
            metadata_row_mapper=metadata_row_mapper,
        )

    def kpi_catalog(self) -> list[KpiCatalogEntry]:
        """Return catalog of available KPIs for hierarchical formatting."""
        return self.kpi_handler.get_catalog()

    def build_ai_data_payload(self, model: UnifiedRunModel) -> dict[str, Any]:
        """Build AI evaluation payload from the unified model."""
        return self.ai_evaluator.build_payload(model, self)

    def get_ai_data_artifact_files_for_test(self, test_dir: Path) -> list[str]:
        """Return list of artifact files to copy for AI evaluation export from a specific test directory.

        Args:
            test_dir: The specific test directory to search within

        Returns:
            List of relative paths from test_dir to relevant artifact files
        """
        artifact_files = []

        # LLMInferenceService state files - search within this test directory only
        llmisvc_patterns = [
            "*__capture_llmisvc_state/artifacts/llminferenceservice.json",
            "*__capture_llmisvc_state/artifacts/llminferenceservice.deployments.json",
        ]

        for pattern in llmisvc_patterns:
            matches = list(test_dir.glob(pattern))
            if matches:
                logger.debug(
                    f"Found {len(matches)} LLMInferenceService files in {test_dir} for pattern {pattern}"
                )
                for match in matches:
                    relative_path = str(match.relative_to(test_dir))
                    artifact_files.append(relative_path)
                    logger.debug(f"Found LLMInferenceService artifact: {relative_path}")
            else:
                logger.debug(f"No matches found in {test_dir} for pattern: {pattern}")

        # GuideLLM benchmark results - search within this test directory only
        benchmark_pattern = "**/*__run_guidellm_benchmark/artifacts/results/benchmarks*.json"
        benchmark_matches = list(test_dir.glob(benchmark_pattern))

        if benchmark_matches:
            logger.debug(f"Found {len(benchmark_matches)} GuideLLM benchmark files in {test_dir}")
            for match in benchmark_matches:
                relative_path = str(match.relative_to(test_dir))
                artifact_files.append(relative_path)
                logger.debug(f"Found GuideLLM benchmark artifact: {relative_path}")
        else:
            logger.debug(f"No benchmark files found in {test_dir}")

        # Project configuration file - include if present
        config_file = test_dir / "config.yaml"
        if config_file.exists():
            relative_path = str(config_file.relative_to(test_dir))
            artifact_files.append(relative_path)
            logger.info(f"Found project config artifact: {relative_path}")
        else:
            logger.warning(f"No config.yaml found in {test_dir}")

        logger.info(
            f"AI eval export will copy {len(artifact_files)} artifact files from {test_dir}"
        )
        return artifact_files


def get_plugin() -> PostProcessingPlugin:
    """Return the GuideLLM plugin instance."""
    return GuideLLMPlugin()
