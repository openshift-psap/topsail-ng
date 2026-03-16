"""CSV generation from benchmark metrics."""

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import BenchmarkConfig
from .metrics import BenchmarkMetrics

logger = logging.getLogger(__name__)


@dataclass
class CSVRow:
    """Single row in the benchmark CSV."""

    run: str
    accelerator: str
    model: str
    version: str
    prompt_toks: int
    output_toks: int
    tp: int
    measured_concurrency: Optional[float]
    intended_concurrency: int
    measured_rps: Optional[float]
    output_tok_per_sec: Optional[float]
    total_tok_per_sec: Optional[float]
    ttft_mean_ms: Optional[float]
    ttft_median_ms: Optional[float]
    ttft_p95_ms: Optional[float]
    ttft_p99_ms: Optional[float]
    tpot_mean_ms: Optional[float]
    tpot_median_ms: Optional[float]
    tpot_p95_ms: Optional[float]
    tpot_p99_ms: Optional[float]
    itl_mean_ms: Optional[float]
    itl_p99_ms: Optional[float]
    latency_mean_sec: Optional[float]
    latency_median_sec: Optional[float]
    latency_p99_sec: Optional[float]
    successful_requests: Optional[int]
    failed_requests: Optional[int]
    error_rate: Optional[float]
    uuid: str

    def to_dict(self) -> dict:
        """Convert to dictionary for CSV writing."""
        return {
            "run": self.run,
            "accelerator": self.accelerator,
            "model": self.model,
            "version": self.version,
            "prompt_toks": self.prompt_toks,
            "output_toks": self.output_toks,
            "TP": self.tp,
            "measured_concurrency": self._fmt(self.measured_concurrency),
            "intended_concurrency": self.intended_concurrency,
            "measured_rps": self._fmt(self.measured_rps),
            "output_tok/sec": self._fmt(self.output_tok_per_sec),
            "total_tok/sec": self._fmt(self.total_tok_per_sec),
            "ttft_mean_ms": self._fmt(self.ttft_mean_ms),
            "ttft_median_ms": self._fmt(self.ttft_median_ms),
            "ttft_p95_ms": self._fmt(self.ttft_p95_ms),
            "ttft_p99_ms": self._fmt(self.ttft_p99_ms),
            "tpot_mean_ms": self._fmt(self.tpot_mean_ms),
            "tpot_median_ms": self._fmt(self.tpot_median_ms),
            "tpot_p95_ms": self._fmt(self.tpot_p95_ms),
            "tpot_p99_ms": self._fmt(self.tpot_p99_ms),
            "itl_mean_ms": self._fmt(self.itl_mean_ms),
            "itl_p99_ms": self._fmt(self.itl_p99_ms),
            "latency_mean_sec": self._fmt(self.latency_mean_sec),
            "latency_median_sec": self._fmt(self.latency_median_sec),
            "latency_p99_sec": self._fmt(self.latency_p99_sec),
            "successful_requests": self.successful_requests or "",
            "failed_requests": self.failed_requests or "",
            "error_rate": self._fmt(self.error_rate),
            "uuid": self.uuid,
        }

    @staticmethod
    def _fmt(value: Optional[float]) -> str:
        """Format float value for CSV."""
        return f"{value:.6f}" if value is not None else ""


class CSVGenerator:
    """Generates CSV files from benchmark metrics."""

    # CSV column order
    FIELDNAMES = [
        "run",
        "accelerator",
        "model",
        "version",
        "prompt_toks",
        "output_toks",
        "TP",
        "measured_concurrency",
        "intended_concurrency",
        "measured_rps",
        "output_tok/sec",
        "total_tok/sec",
        "ttft_mean_ms",
        "ttft_median_ms",
        "ttft_p95_ms",
        "ttft_p99_ms",
        "tpot_mean_ms",
        "tpot_median_ms",
        "tpot_p95_ms",
        "tpot_p99_ms",
        "itl_mean_ms",
        "itl_p99_ms",
        "latency_mean_sec",
        "latency_median_sec",
        "latency_p99_sec",
        "successful_requests",
        "failed_requests",
        "error_rate",
        "uuid",
    ]

    def __init__(self, config: BenchmarkConfig):
        self.config = config

    def metrics_to_row(self, metrics: BenchmarkMetrics) -> CSVRow:
        """Convert BenchmarkMetrics to CSVRow."""
        return CSVRow(
            run=f"{self.config.accelerator}-{self.config.model_name}-{self.config.tp}",
            accelerator=self.config.accelerator,
            model=self.config.model_name,
            version=self.config.version,
            prompt_toks=self.config.prompt_tokens,
            output_toks=self.config.output_tokens,
            tp=self.config.tp,
            measured_concurrency=metrics.request_concurrency_mean,
            intended_concurrency=metrics.concurrency,
            measured_rps=metrics.throughput_requests_per_sec,
            output_tok_per_sec=metrics.throughput_output_tokens_per_sec,
            total_tok_per_sec=metrics.total_tokens_per_second,
            ttft_mean_ms=metrics.ttft_mean_ms,
            ttft_median_ms=metrics.ttft_median_ms,
            ttft_p95_ms=metrics.ttft_p95_ms,
            ttft_p99_ms=metrics.ttft_p99_ms,
            tpot_mean_ms=metrics.tpot_mean_ms,
            tpot_median_ms=metrics.tpot_median_ms,
            tpot_p95_ms=metrics.tpot_p95_ms,
            tpot_p99_ms=metrics.tpot_p99_ms,
            itl_mean_ms=metrics.itl_mean_ms,
            itl_p99_ms=metrics.itl_p99_ms,
            latency_mean_sec=metrics.latency_mean_sec,
            latency_median_sec=metrics.latency_median_sec,
            latency_p99_sec=metrics.latency_p99_sec,
            successful_requests=metrics.successful_requests,
            failed_requests=metrics.failed_requests,
            error_rate=metrics.error_rate,
            uuid=self.config.run_uuid,
        )

    def generate(
        self,
        metrics_list: List[BenchmarkMetrics],
        output_path: Optional[Path] = None,
    ) -> Path:
        """Generate CSV file from list of metrics.

        Args:
            metrics_list: List of BenchmarkMetrics from each concurrency level
            output_path: Output file path (default: /tmp/benchmark_results.csv)

        Returns:
            Path to generated CSV file
        """
        if output_path is None:
            output_path = Path("/tmp/benchmark_results.csv")

        rows = [self.metrics_to_row(m) for m in metrics_list]

        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow(row.to_dict())

        logger.info(f"Generated CSV: {output_path} ({len(rows)} rows)")
        return output_path
