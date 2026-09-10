"""GuideLLM benchmark parsers for Caliper plugin."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import jsonpath_ng
import yaml

from projects.caliper.engine.model import (
    ParseResult,
    TestBaseNode,
    UnifiedResultRecord,
)
from projects.guidellm.postprocess.guidellm.dashboard import (
    canonical_json,
    normalize_product_version,
)

from .models import GuideLLMBenchmark, GuideLLMConfiguration

logger = logging.getLogger(__name__)


def _labels_from_node(node: TestBaseNode) -> dict[str, Any]:
    """Extract labels from a test node."""
    raw = node.test_labels
    inner = raw.get("labels")
    if isinstance(inner, dict):
        return dict(inner)
    if isinstance(raw, dict):
        return dict(raw)
    return {"facet": "default"}


def _kpi_labels_from_node(node: TestBaseNode) -> dict[str, Any]:
    """Extract kpi_labels from a test node."""
    raw = node.test_labels
    kpi_labels = raw.get("kpi_labels")
    if isinstance(kpi_labels, dict):
        return dict(kpi_labels)
    return {}


def extract_field_by_jsonpath(data: dict[str, Any], jsonpath: str, default: Any = None) -> Any:
    """
    Extract a field from nested dictionary using JSONPath notation.

    Args:
        data: The dictionary to extract from
        jsonpath: JSONPath like "spec.model.name" or "spec.template.containers[0].name"
        default: Default value if field not found

    Returns:
        The extracted value or default if not found

    Examples:
        extract_field_by_jsonpath(yaml_data, "spec.model.name")
        extract_field_by_jsonpath(yaml_data, "spec.template.containers[0].name")
    """
    try:
        return jsonpath_ng.parse(jsonpath).find(data)[0].value
    except IndexError:
        return default


def parse_product_version_from_annotation(
    annotation_value: str, prefix: str = "RHOAI-"
) -> str | None:
    """
    Parse product version from annotation value.

    Args:
        annotation_value: Raw annotation like "v3-5-0-ea-2-kserve-anything" or "v2-1-0"
        prefix: Version prefix to use (default: "RHOAI-")

    Returns:
        Cleaned version like "RHOAI-3.5.0-EA.2" or "RHOAI-2.1.0" or None if parsing fails

    Examples:
        parse_product_version_from_annotation("v3-5-0-ea-2-kserve-config-llm-decode-template")
        # Returns: "RHOAI-3.5.0-EA.2"
        parse_product_version_from_annotation("v2-1-0-kserve-something", "CUSTOM-")
        # Returns: "CUSTOM-2.1.0"
    """
    if not annotation_value:
        return None

    # Remove any kserve suffix (anything starting with -kserve-)
    version_part = annotation_value
    if "-kserve-" in annotation_value:
        kserve_index = annotation_value.find("-kserve-")
        version_part = annotation_value[:kserve_index]

    # Transform version strings: v3-5-0-ea-2 -> {prefix}3.5.0-EA.2 or v2-1-0 -> {prefix}2.1.0
    if not version_part.startswith("v"):
        return None

    # Remove the 'v' prefix
    version_without_v = version_part[1:]

    if "-ea-" in version_without_v:
        # Split on -ea- to handle the pre-release part separately
        base_version, ea_part = version_without_v.split("-ea-", 1)
        # Replace hyphens with dots in base version (3-5-0 -> 3.5.0)
        base_version = base_version.replace("-", ".")
        # Reconstruct with specified prefix and uppercase EA
        cleaned_version = f"{prefix}{base_version}-EA.{ea_part}"
        return cleaned_version
    else:
        # Regular version without -ea- (2-1-0 -> {prefix}2.1.0)
        base_version = version_without_v.replace("-", ".")
        cleaned_version = f"{prefix}{base_version}"
        return cleaned_version


class GuideLLMParser:
    """Parser for GuideLLM benchmark JSON artifacts."""

    @staticmethod
    def _is_benchmarks_artifact(path: Path) -> bool:
        return path.name == "benchmarks.json" or (
            path.name.startswith("benchmarks-rate-") and path.suffix == ".json"
        )

    @staticmethod
    def _is_llmisvc_artifact(path: Path) -> bool:
        """Check if path is an LLMInferenceService YAML artifact."""
        return path.name in [
            "llminferenceservice.yaml",
            "llminferenceservice.yml",
            "llminferenceservice.json",
        ]

    @staticmethod
    def _is_config_artifact(path: Path) -> bool:
        """Check if path is a config.yaml artifact."""
        return path.name == "config.yaml"

    @staticmethod
    def _is_node_gpu_mapping_artifact(path: Path) -> bool:
        """Check if path is a node_gpu_mapping.yaml artifact."""
        return path.name == "node_gpu_mapping.yaml"

    def extract_fields_from_llmisvc(self, file_path: Path) -> dict[str, Any]:
        """
        Extract multiple fields from LLMInferenceService YAML file.

        Args:
            file_path: Path to llminferenceservice.yaml file

        Returns:
            Dictionary with extracted fields (product_version, deployment_profile)
        """
        result = {}
        try:
            yaml_data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
            if not isinstance(yaml_data, dict):
                return result

            # Extract product version from any annotation starting with v[number]
            annotations = extract_field_by_jsonpath(yaml_data, "status.annotations", {})
            if isinstance(annotations, dict):
                for annotation_key, annotation_value in annotations.items():
                    if (
                        isinstance(annotation_value, str)
                        and annotation_value.startswith("v")
                        and len(annotation_value) > 1
                    ):
                        # Check if the second character is a digit
                        if annotation_value[1].isdigit():
                            product_version = parse_product_version_from_annotation(
                                annotation_value
                            )
                            if product_version:
                                normalized = normalize_product_version(product_version)
                                result["product_version"] = normalized
                                logger.debug(
                                    f"Extracted product_version '{product_version}' (normalized to '{normalized}') from annotation '{annotation_key}' in {file_path}"
                                )
                                break  # Use the first matching version annotation found

            # Extract deployment profile from forge annotation
            deployment_profile = extract_field_by_jsonpath(
                yaml_data, 'metadata.annotations["forge.openshift.io/deployment-profile"]'
            )
            if deployment_profile:
                result["deployment_profile"] = deployment_profile
                logger.debug(
                    f"Extracted deployment_profile '{deployment_profile}' from {file_path}"
                )

            replicas = extract_field_by_jsonpath(yaml_data, "spec.replicas")
            if replicas is not None:
                result["replicas"] = replicas

            tensor_parallel_size = extract_field_by_jsonpath(yaml_data, "spec.parallelism.tensor")
            if tensor_parallel_size is not None:
                result["tensor_parallel_size"] = tensor_parallel_size

            router_config = extract_field_by_jsonpath(yaml_data, "spec.router.scheduler")
            if router_config is not None:
                result["router_config"] = canonical_json(router_config)

            serving_container = extract_field_by_jsonpath(
                yaml_data, "spec.template.containers[0]", {}
            )
            if isinstance(serving_container, dict):
                image = serving_container.get("image")
                if image:
                    result["image_tag"] = image
                for env_var in serving_container.get("env", []):
                    if env_var.get("name") == "VLLM_ADDITIONAL_ARGS":
                        result["runtime_args"] = env_var.get("value", "")
                        break

        except Exception as e:
            logger.warning(f"Failed to extract fields from {file_path}: {e}")

        return result

    def extract_fields_from_config(self, file_path: Path) -> dict[str, str]:
        """
        Extract multiple fields from config.yaml file.

        Args:
            file_path: Path to config.yaml file

        Returns:
            Dictionary with extracted fields (cluster, benchmark_key)
        """
        result = {}
        try:
            yaml_data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
            if not isinstance(yaml_data, dict):
                return result

            # Extract cluster name
            cluster = extract_field_by_jsonpath(yaml_data, "ci_job.cluster")
            if cluster:
                result["cluster"] = cluster
                logger.info(f"Extracted cluster '{cluster}' from {file_path}")

            # Extract benchmark key
            benchmark_key = extract_field_by_jsonpath(yaml_data, "runtime.benchmark_key")
            if benchmark_key:
                result["benchmark_key"] = benchmark_key
                logger.info(f"Extracted benchmark_key '{benchmark_key}' from {file_path}")

        except Exception as e:
            logger.warning(f"Failed to extract fields from {file_path}: {e}")

        return result

    def extract_fields_from_node_gpu_mapping(self, file_path: Path) -> dict[str, str]:
        """
        Extract GPU type from node_gpu_mapping.yaml file.

        Args:
            file_path: Path to node_gpu_mapping.yaml file

        Returns:
            Dictionary with extracted GPU type field

        Example file structure:
            node_gpu_mapping:
              psap-fire-athena-bnfx9-worker-gpu-h200-66lrw: NVIDIA-H200
        """
        result = {}
        try:
            yaml_data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
            if not isinstance(yaml_data, dict):
                return result

            # Extract GPU types from node_gpu_mapping
            node_gpu_mapping = yaml_data.get("node_gpu_mapping", {})
            if isinstance(node_gpu_mapping, dict) and node_gpu_mapping:
                # Get all unique GPU types from the mapping
                gpu_types = list(set(node_gpu_mapping.values()))

                if gpu_types:
                    # Concatenate all GPU types with comma separator
                    result["gpu_type"] = ",".join(sorted(gpu_types))
                    logger.info(f"Extracted gpu_type '{result['gpu_type']}' from {file_path}")
                else:
                    logger.warning(f"Empty node_gpu_mapping found in {file_path}")

        except Exception as e:
            logger.warning(f"Failed to extract GPU type from {file_path}: {e}")

        return result

    def parse_benchmarks_json(
        self, file_path: Path
    ) -> tuple[list[GuideLLMBenchmark], GuideLLMConfiguration | None, list[str]]:
        """
        Parse a GuideLLM benchmarks.json file.

        Returns:
            Tuple of (benchmarks list, configuration, warnings list)
        """
        warnings: list[str] = []
        benchmarks: list[GuideLLMBenchmark] = []
        configuration: GuideLLMConfiguration | None = None

        try:
            json_data = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(json_data, dict):
                warnings.append(f"{file_path}: benchmarks.json must be a JSON object")
                return [], None, warnings

            # Parse configuration from top-level fields
            args = json_data.get("args")
            metadata = json_data.get("metadata")
            if args or metadata:
                configuration = GuideLLMConfiguration(args=args, metadata=metadata)

            # Parse each benchmark in the JSON
            for benchmark_data in json_data.get("benchmarks", []):
                try:
                    benchmark = self._parse_single_benchmark(benchmark_data)
                    benchmarks.append(benchmark)
                except Exception as e:
                    warnings.append(f"Failed to parse benchmark in {file_path}: {e}")
                    logger.warning(f"Failed to parse benchmark data: {e}")
                    continue

            logger.debug(f"Parsed {len(benchmarks)} GuideLLM benchmarks from {file_path}")
            return benchmarks, configuration, warnings

        except json.JSONDecodeError as e:
            warnings.append(f"Malformed JSON {file_path}: {e}")
            return [], None, warnings
        except Exception as e:
            warnings.append(f"Failed to parse GuideLLM JSON {file_path}: {e}")
            return [], None, warnings

    def _parse_single_benchmark(self, benchmark_data: dict[str, Any]) -> GuideLLMBenchmark:
        """Parse a single benchmark entry from the JSON data."""
        # Extract strategy and concurrency info with fallback logic
        scheduler = benchmark_data.get("scheduler", {})
        config = benchmark_data.get("config", {})
        strategy_info = config.get("strategy", {})
        strategy = strategy_info.get("type_", "unknown")

        # Extract concurrency (streams) with multiple fallback paths
        intended_concurrency = self._extract_concurrency(strategy_info, scheduler)

        # Extract timing info
        state = scheduler.get("state", {})
        start_time = state.get("start_time", 0)
        end_time = state.get("end_time", 0)
        duration = end_time - start_time if end_time > start_time else 60.0

        # Extract metrics
        metrics = benchmark_data.get("metrics", {})

        # Helper function to safely extract metric values
        def get_metric_value(
            metric_name: str, stat_type: str = "median", default: float = 0.0
        ) -> float:
            metric_data = metrics.get(metric_name, {}).get("successful", {})
            if stat_type in ["p99", "p95", "p90", "p75", "p50", "p25", "p10"]:
                percentiles = metric_data.get("percentiles", {})
                return float(percentiles.get(stat_type, default))
            else:
                return float(metric_data.get(stat_type, default))

        # Extract latency metrics (request_latency is already in seconds from guidellm)
        request_latency_median = get_metric_value("request_latency", "median")
        request_latency_p95 = get_metric_value("request_latency", "p95")

        # Extract TTFT percentiles
        ttft_median = get_metric_value("time_to_first_token_ms", "median") / 1000.0
        ttft_p10 = get_metric_value("time_to_first_token_ms", "p10") / 1000.0
        ttft_p25 = get_metric_value("time_to_first_token_ms", "p25") / 1000.0
        ttft_p50 = get_metric_value("time_to_first_token_ms", "p50") / 1000.0
        ttft_p75 = get_metric_value("time_to_first_token_ms", "p75") / 1000.0
        ttft_p90 = get_metric_value("time_to_first_token_ms", "p90") / 1000.0
        ttft_p95 = get_metric_value("time_to_first_token_ms", "p95") / 1000.0

        # Extract ITL percentiles
        itl_median = get_metric_value("inter_token_latency_ms", "median") / 1000.0
        itl_p10 = get_metric_value("inter_token_latency_ms", "p10") / 1000.0
        itl_p25 = get_metric_value("inter_token_latency_ms", "p25") / 1000.0
        itl_p50 = get_metric_value("inter_token_latency_ms", "p50") / 1000.0
        itl_p75 = get_metric_value("inter_token_latency_ms", "p75") / 1000.0
        itl_p90 = get_metric_value("inter_token_latency_ms", "p90") / 1000.0
        itl_p95 = get_metric_value("inter_token_latency_ms", "p95") / 1000.0

        # Extract TPOT percentiles
        tpot_median = get_metric_value("time_per_output_token_ms", "median") / 1000.0
        tpot_p95 = get_metric_value("time_per_output_token_ms", "p95") / 1000.0

        # Extract throughput metrics
        request_rate = get_metric_value("requests_per_second", "mean")
        input_tokens_per_second = get_metric_value("input_tokens_per_second", "mean")
        output_tokens_per_second = get_metric_value("output_tokens_per_second", "mean")
        total_tokens_per_second = input_tokens_per_second + output_tokens_per_second

        # Extract output token percentiles
        output_tokens_per_second_p10 = get_metric_value("output_tokens_per_second", "p10")
        output_tokens_per_second_p25 = get_metric_value("output_tokens_per_second", "p25")
        output_tokens_per_second_p50 = get_metric_value("output_tokens_per_second", "p50")
        output_tokens_per_second_p75 = get_metric_value("output_tokens_per_second", "p75")
        output_tokens_per_second_p90 = get_metric_value("output_tokens_per_second", "p90")

        # Calculate requests completed and tokens per request
        completed_requests = int(request_rate * duration) if request_rate > 0 else 0
        input_tokens_per_request = (
            (input_tokens_per_second / request_rate) if request_rate > 0 else 0.0
        )
        output_tokens_per_request = (
            (output_tokens_per_second / request_rate) if request_rate > 0 else 0.0
        )
        total_tokens_per_request = (
            (total_tokens_per_second / request_rate) if request_rate > 0 else 0.0
        )

        # Extract P99 values
        ttft_p99 = get_metric_value("time_to_first_token_ms", "p99") / 1000.0
        itl_p99 = get_metric_value("inter_token_latency_ms", "p99") / 1000.0
        tpot_p99 = get_metric_value("time_per_output_token_ms", "p99") / 1000.0

        # Create GuideLLMBenchmark object
        return GuideLLMBenchmark(
            strategy=strategy,
            duration=duration,
            warmup_time=0.0,  # Not available in JSON format
            cooldown_time=0.0,  # Not available in JSON format
            # Request metrics
            request_rate=request_rate,
            request_concurrency=intended_concurrency,
            intended_concurrency=int(intended_concurrency),
            completed_requests=completed_requests,
            failed_requests=0,  # Could extract from unsuccessful metrics if needed
            # Token metrics per request
            input_tokens_per_request=input_tokens_per_request,
            output_tokens_per_request=output_tokens_per_request,
            total_tokens_per_request=total_tokens_per_request,
            # Latency metrics (already in seconds)
            request_latency_median=request_latency_median,
            request_latency_p95=request_latency_p95,
            ttft_median=ttft_median,
            ttft_p10=ttft_p10,
            ttft_p25=ttft_p25,
            ttft_p50=ttft_p50,
            ttft_p75=ttft_p75,
            ttft_p90=ttft_p90,
            ttft_p95=ttft_p95,
            ttft_p99=ttft_p99,
            itl_median=itl_median,
            itl_p10=itl_p10,
            itl_p25=itl_p25,
            itl_p50=itl_p50,
            itl_p75=itl_p75,
            itl_p90=itl_p90,
            itl_p95=itl_p95,
            itl_p99=itl_p99,
            tpot_median=tpot_median,
            tpot_p95=tpot_p95,
            tpot_p99=tpot_p99,
            # Throughput metrics
            tokens_per_second=total_tokens_per_second,
            input_tokens_per_second=input_tokens_per_second,
            output_tokens_per_second=output_tokens_per_second,
            # Output token percentiles
            output_tokens_per_second_p10=output_tokens_per_second_p10,
            output_tokens_per_second_p25=output_tokens_per_second_p25,
            output_tokens_per_second_p50=output_tokens_per_second_p50,
            output_tokens_per_second_p75=output_tokens_per_second_p75,
            output_tokens_per_second_p90=output_tokens_per_second_p90,
        )

    def _extract_concurrency(
        self, strategy_info: dict[str, Any], scheduler: dict[str, Any]
    ) -> float:
        """Extract concurrency (streams) from strategy or scheduler info."""
        # Try multiple paths for concurrency extraction
        try:
            # First try: config.strategy.streams
            concurrency = float(strategy_info.get("streams", 0))
            if concurrency > 0:
                return concurrency
        except (ValueError, TypeError):
            pass

        try:
            # Second try: scheduler.strategy.streams
            sched_strategy = scheduler.get("strategy", {})
            streams = sched_strategy.get("streams")
            if streams and streams > 0:
                return float(streams)
        except (ValueError, TypeError):
            pass

        logger.warning(
            "Could not find concurrency 'streams' for benchmark. Using default value 1.0"
        )
        return 1.0

    def _group_benchmarks_by_test(
        self, benchmarks: list[GuideLLMBenchmark]
    ) -> dict[str, list[GuideLLMBenchmark]]:
        """
        Group benchmarks by test characteristics, excluding rate.

        Args:
            benchmarks: List of parsed benchmarks

        Returns:
            Dictionary mapping group keys to lists of benchmarks
        """
        groups = {}

        for benchmark in benchmarks:
            # Create a simple group key based only on strategy
            # This ensures all rate variations are grouped together into a single performance curve
            group_key = benchmark.strategy

            if group_key not in groups:
                groups[group_key] = []
            groups[group_key].append(benchmark)

        return groups

    def _create_aggregated_metrics(
        self, benchmark_group: list[GuideLLMBenchmark]
    ) -> dict[str, Any]:
        """
        Create aggregated metrics with performance curves from a group of benchmarks.

        Args:
            benchmark_group: List of benchmarks representing the same test at different rates

        Returns:
            Dictionary containing aggregated metrics with performance curves
        """
        if not benchmark_group:
            return {}

        # Sort benchmarks by intended concurrency for consistent curve ordering
        sorted_benchmarks = sorted(benchmark_group, key=lambda b: b.intended_concurrency)

        # Use the first benchmark for static characteristics
        representative = sorted_benchmarks[0]

        # Create base metrics from representative benchmark
        metrics = {
            "strategy": representative.strategy,
            "duration": representative.duration,
            "request_concurrency": representative.request_concurrency,
            "warmup_time": representative.warmup_time,
            "cooldown_time": representative.cooldown_time,
        }

        # Create performance curves across all rates
        request_rates = []
        curves = {
            "tokens_per_second": [],
            "input_tokens_per_second": [],
            "output_tokens_per_second": [],
            "ttft_median": [],
            "ttft_p95": [],
            "ttft_p99": [],
            "itl_median": [],
            "itl_p95": [],
            "itl_p99": [],
            "tpot_median": [],
            "tpot_p95": [],
            "tpot_p99": [],
            "request_latency_median": [],
            "request_latency_p95": [],
            "completed_requests": [],
            "failed_requests": [],
            "request_concurrency": [],  # Effective concurrency to curves
            "intended_concurrency": [],  # Intended concurrency to curves
        }

        for benchmark in sorted_benchmarks:
            request_rates.append(benchmark.request_rate)
            curves["tokens_per_second"].append(benchmark.tokens_per_second)
            curves["input_tokens_per_second"].append(benchmark.input_tokens_per_second)
            curves["output_tokens_per_second"].append(benchmark.output_tokens_per_second)
            curves["ttft_median"].append(benchmark.ttft_median)
            curves["ttft_p95"].append(benchmark.ttft_p95)
            curves["ttft_p99"].append(benchmark.ttft_p99)
            curves["itl_median"].append(benchmark.itl_median)
            curves["itl_p95"].append(benchmark.itl_p95)
            curves["itl_p99"].append(benchmark.itl_p99)
            curves["tpot_median"].append(benchmark.tpot_median)
            curves["tpot_p95"].append(benchmark.tpot_p95)
            curves["tpot_p99"].append(benchmark.tpot_p99)
            curves["request_latency_median"].append(benchmark.request_latency_median)
            curves["request_latency_p95"].append(benchmark.request_latency_p95)
            curves["completed_requests"].append(benchmark.completed_requests)
            curves["failed_requests"].append(benchmark.failed_requests)
            curves["request_concurrency"].append(
                benchmark.request_concurrency
            )  # Add effective concurrency per rate point
            curves["intended_concurrency"].append(
                benchmark.intended_concurrency
            )  # Add intended concurrency per rate point

        # Add request_rate and performance curves to metrics
        metrics["request_rate"] = request_rates
        metrics["performance_curves"] = curves

        # Also add summary statistics from the representative benchmark
        metrics.update(
            {
                "input_tokens_per_request": representative.input_tokens_per_request,
                "output_tokens_per_request": representative.output_tokens_per_request,
                "total_tokens_per_request": representative.total_tokens_per_request,
            }
        )

        return metrics

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        """
        Parse test nodes containing GuideLLM benchmarks.json files.

        Args:
            nodes: List of test nodes to parse

        Returns:
            ParseResult with one unified record per node and warnings
        """
        records: list[UnifiedResultRecord] = []
        warnings: list[str] = []

        for node in nodes:
            logger.info(f"Parsing '{node.test_path}' ...")
            # Look for the legacy single-file artifact and the newer per-rate artifacts.
            benchmarks_files = [p for p in node.artifact_paths if self._is_benchmarks_artifact(p)]
            benchmarks_files.sort(key=lambda path: path.name)

            # Also look for LLMInferenceService YAML files and config.yaml files for system information
            llmisvc_files = [p for p in node.artifact_paths if self._is_llmisvc_artifact(p)]
            llmisvc_files.sort(key=lambda path: "__capture_llmisvc_state" not in str(path))
            config_files = [p for p in node.artifact_paths if self._is_config_artifact(p)]
            gpu_mapping_files = [
                p for p in node.artifact_paths if self._is_node_gpu_mapping_artifact(p)
            ]

            if not benchmarks_files:
                # No benchmark result JSON found for this node, create empty record
                labels = _labels_from_node(node)
                records.append(
                    UnifiedResultRecord(
                        test_base_path=str(node.test_path),
                        distinguishing_labels=labels,
                        metrics={"no_benchmarks_found": True},
                        run_identity={"guidellm": True},
                        parse_notes=["No benchmark result JSON file found"],
                    )
                )
                continue

            # Collect all benchmarks from all files for this node
            node_benchmarks = []
            node_config = None

            for benchmarks_file in benchmarks_files:
                benchmarks, config, file_warnings = self.parse_benchmarks_json(benchmarks_file)
                warnings.extend(file_warnings)
                node_benchmarks.extend(benchmarks)
                if config and not node_config:
                    node_config = config

            if node_benchmarks:
                # Create aggregated metrics with performance curves for this node
                labels = _labels_from_node(node)
                node_kpi_labels = _kpi_labels_from_node(node)

                # Start with base labels from node
                distinguishing_labels = {**labels, **node_kpi_labels}

                metrics = self._create_aggregated_metrics(node_benchmarks)
                if node_config:
                    metrics["configuration"] = node_config.to_dict()

                # Extract fields from LLMInferenceService YAML if available
                for llmisvc_file in llmisvc_files:
                    llmisvc_fields = self.extract_fields_from_llmisvc(llmisvc_file)
                    for field_name, field_value in llmisvc_fields.items():
                        if field_value and field_name not in metrics:
                            metrics[field_name] = field_value

                # Extract fields from config.yaml if available
                for config_file in config_files:
                    config_fields = self.extract_fields_from_config(config_file)
                    for field_name, field_value in config_fields.items():
                        if field_value and field_name not in metrics:
                            metrics[field_name] = field_value

                # Extract GPU type from node_gpu_mapping.yaml if available
                for gpu_mapping_file in gpu_mapping_files:
                    gpu_fields = self.extract_fields_from_node_gpu_mapping(gpu_mapping_file)
                    for field_name, field_value in gpu_fields.items():
                        if field_value and field_name not in metrics:
                            metrics[field_name] = field_value

                # Merge extracted artifact fields into distinguishing_labels
                # This creates a single unified label set for both KPI computation and visualization
                extracted_label_fields = [
                    "gpu_type",
                    "product_version",
                    "deployment_profile",
                    "cluster",
                    "benchmark_key",
                ]

                for field_name in extracted_label_fields:
                    if field_name in metrics:
                        distinguishing_labels[field_name] = metrics[field_name]
                        logger.debug(
                            f"Merged {field_name}='{metrics[field_name]}' into distinguishing_labels"
                        )

                # For backwards compatibility, also store the merged labels as kpi_labels in metrics
                # This ensures existing KPI computation code continues to work
                metrics["kpi_labels"] = dict(distinguishing_labels)

                records.append(
                    UnifiedResultRecord(
                        test_base_path=str(node.test_path),
                        distinguishing_labels=distinguishing_labels,
                        metrics=metrics,
                        run_identity={"guidellm": True},
                        parse_notes=[],
                    )
                )

        logger.debug(f"GuideLLM parser created {len(records)} unified result records")
        return ParseResult(records=records, warnings=warnings)
