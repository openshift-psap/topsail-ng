"""Metrics extraction from GuideLLM benchmark results."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkMetrics:
    """Extracted metrics from a single benchmark run."""

    concurrency: int = 0

    # Request stats
    total_requests: Optional[int] = None
    successful_requests: Optional[int] = None
    failed_requests: Optional[int] = None
    error_rate: Optional[float] = None

    # Throughput
    throughput_requests_per_sec: Optional[float] = None
    throughput_output_tokens_per_sec: Optional[float] = None
    total_tokens_per_second: Optional[float] = None

    # Concurrency
    request_concurrency_mean: Optional[float] = None

    # Request latency (seconds)
    latency_mean_sec: Optional[float] = None
    latency_median_sec: Optional[float] = None
    latency_p50_sec: Optional[float] = None
    latency_p90_sec: Optional[float] = None
    latency_p95_sec: Optional[float] = None
    latency_p99_sec: Optional[float] = None

    # TTFT - Time To First Token (milliseconds)
    ttft_mean_ms: Optional[float] = None
    ttft_median_ms: Optional[float] = None
    ttft_p95_ms: Optional[float] = None
    ttft_p99_ms: Optional[float] = None

    # TPOT - Time Per Output Token (milliseconds)
    tpot_mean_ms: Optional[float] = None
    tpot_median_ms: Optional[float] = None
    tpot_p95_ms: Optional[float] = None
    tpot_p99_ms: Optional[float] = None

    # ITL - Inter-Token Latency (milliseconds)
    itl_mean_ms: Optional[float] = None
    itl_median_ms: Optional[float] = None
    itl_p95_ms: Optional[float] = None
    itl_p99_ms: Optional[float] = None

    # Token counts
    total_input_tokens: Optional[int] = None
    total_output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        return {k: v for k, v in self.__dict__.items() if v is not None}

    def __len__(self) -> int:
        """Return count of non-None metrics."""
        return len(self.to_dict())


class MetricsExtractor:
    """Extracts metrics from GuideLLM benchmark JSON output."""

    def __init__(self):
        self._metrics_map = self._build_metrics_map()

    @staticmethod
    def _get_nested(d: Dict[str, Any], *keys, default=None) -> Any:
        """Safely get a nested value from a dictionary."""
        for key in keys:
            if not isinstance(d, dict):
                return default
            d = d.get(key, default)
        return d

    def _build_metrics_map(self) -> Dict[str, tuple]:
        """Build mapping of metric names to JSON paths."""
        return {
            "throughput_requests_per_sec": ("requests_per_second", "successful", "mean"),
            "total_tokens_per_second": ("tokens_per_second", "successful", "mean"),
            "throughput_output_tokens_per_sec": ("output_tokens_per_second", "successful", "mean"),
            "request_concurrency_mean": ("request_concurrency", "successful", "mean"),
            "latency_mean_sec": ("request_latency", "successful", "mean"),
            "latency_median_sec": ("request_latency", "successful", "median"),
            "latency_p50_sec": ("request_latency", "successful", "percentiles", "p50"),
            "latency_p90_sec": ("request_latency", "successful", "percentiles", "p90"),
            "latency_p95_sec": ("request_latency", "successful", "percentiles", "p95"),
            "latency_p99_sec": ("request_latency", "successful", "percentiles", "p99"),
            "ttft_mean_ms": ("time_to_first_token_ms", "successful", "mean"),
            "ttft_median_ms": ("time_to_first_token_ms", "successful", "median"),
            "ttft_p95_ms": ("time_to_first_token_ms", "successful", "percentiles", "p95"),
            "ttft_p99_ms": ("time_to_first_token_ms", "successful", "percentiles", "p99"),
            "tpot_mean_ms": ("time_per_output_token_ms", "successful", "mean"),
            "tpot_median_ms": ("time_per_output_token_ms", "successful", "median"),
            "tpot_p95_ms": ("time_per_output_token_ms", "successful", "percentiles", "p95"),
            "tpot_p99_ms": ("time_per_output_token_ms", "successful", "percentiles", "p99"),
            "itl_mean_ms": ("inter_token_latency_ms", "successful", "mean"),
            "itl_median_ms": ("inter_token_latency_ms", "successful", "median"),
            "itl_p95_ms": ("inter_token_latency_ms", "successful", "percentiles", "p95"),
            "itl_p99_ms": ("inter_token_latency_ms", "successful", "percentiles", "p99"),
            "total_input_tokens": ("prompt_token_count", "successful", "total_sum"),
            "total_output_tokens": ("output_token_count", "successful", "total_sum"),
        }

    def get_concurrency(self, benchmark: Dict[str, Any]) -> int:
        """Extract concurrency (streams) from benchmark config."""
        config = benchmark.get("config") or benchmark.get("args", {})
        try:
            return int(config.get("strategy", {}).get("streams", 0))
        except (KeyError, TypeError, ValueError):
            try:
                return int(config.get("profile", {}).get("streams", [0])[0])
            except (KeyError, TypeError, ValueError, IndexError):
                logger.warning("Could not extract concurrency, using 0")
                return 0

    def extract(self, benchmark: Dict[str, Any]) -> BenchmarkMetrics:
        """Extract metrics from a single benchmark object."""
        metrics = BenchmarkMetrics()

        # Get concurrency
        metrics.concurrency = self.get_concurrency(benchmark)

        # Get metrics and request stats
        all_metrics = benchmark.get("metrics", {})
        scheduler_metrics = benchmark.get("scheduler_metrics", {})
        run_stats = benchmark.get("run_stats", {})
        requests_made = scheduler_metrics.get("requests_made", {}) or run_stats.get("requests_made", {})

        # Extract request stats
        metrics.total_requests = requests_made.get("total")
        metrics.successful_requests = requests_made.get("successful")
        metrics.failed_requests = requests_made.get("errored")

        # Calculate error rate
        if metrics.total_requests and metrics.total_requests > 0:
            failed = metrics.failed_requests or 0
            metrics.error_rate = failed / metrics.total_requests

        # Extract all mapped metrics
        for attr_name, path in self._metrics_map.items():
            value = self._get_nested(all_metrics, *path)
            if value is not None:
                setattr(metrics, attr_name, value)

        # Calculate total tokens
        input_tokens = metrics.total_input_tokens or 0
        output_tokens = metrics.total_output_tokens or 0
        if input_tokens > 0 or output_tokens > 0:
            metrics.total_tokens = input_tokens + output_tokens

        return metrics

    def extract_all(self, guidellm_output: Dict[str, Any]) -> List[BenchmarkMetrics]:
        """Extract metrics from all benchmarks in GuideLLM output."""
        benchmarks = guidellm_output.get("benchmarks", [])
        return [self.extract(b) for b in benchmarks]
