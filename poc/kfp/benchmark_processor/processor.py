"""Main benchmark processor - orchestrates metrics extraction, CSV generation, and MLflow upload."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from .config import BenchmarkConfig, MLflowConfig, ProcessingResult
from .metrics import MetricsExtractor
from .csv_generator import CSVGenerator
from .mlflow_uploader import MLflowUploader

logger = logging.getLogger(__name__)


class BenchmarkProcessor:
    """Orchestrates benchmark result processing.

    This is the main entry point for processing GuideLLM benchmark results.
    It coordinates:
    - Metrics extraction
    - CSV generation
    - MLflow upload

    Example:
        config = BenchmarkConfig(
            model_name="Qwen/Qwen3-0.6B",
            accelerator="H200",
            version="RHOAI-3.2",
            tp=4,
            run_uuid="abc123",
        )
        mlflow_config = MLflowConfig()

        processor = BenchmarkProcessor(config, mlflow_config)
        result = processor.process(benchmark_json_string)

        if result.success:
            print(f"Uploaded to MLflow: {result.mlflow_run_id}")
    """

    def __init__(
        self,
        config: BenchmarkConfig,
        mlflow_config: Optional[MLflowConfig] = None,
        output_dir: Optional[Path] = None,
    ):
        """Initialize processor.

        Args:
            config: Benchmark configuration
            mlflow_config: MLflow configuration (None to skip MLflow upload)
            output_dir: Directory for output files (default: /tmp)
        """
        self.config = config
        self.mlflow_config = mlflow_config
        self.output_dir = output_dir or Path("/tmp")

        # Initialize components
        self.metrics_extractor = MetricsExtractor()
        self.csv_generator = CSVGenerator(config)
        self.mlflow_uploader = (
            MLflowUploader(config, mlflow_config) if mlflow_config else None
        )

    def process(self, benchmark_json: str) -> ProcessingResult:
        """Process benchmark results.

        Args:
            benchmark_json: Raw JSON string from GuideLLM

        Returns:
            ProcessingResult with success status and details
        """
        logger.info("=" * 60)
        logger.info("Starting benchmark processing")
        logger.info("=" * 60)
        logger.info(f"Model: {self.config.model_name}")
        logger.info(f"Accelerator: {self.config.accelerator}")
        logger.info(f"Run UUID: {self.config.run_uuid}")

        # Parse JSON
        try:
            data = json.loads(benchmark_json)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse benchmark JSON: {e}")
            return ProcessingResult(
                success=False,
                error_message=f"JSON parse error: {e}",
            )

        # Extract metrics
        metrics_list = self.metrics_extractor.extract_all(data)
        if not metrics_list:
            logger.warning("No benchmarks found in results")
            return ProcessingResult(
                success=False,
                error_message="No benchmarks found in results",
            )

        logger.info(f"Extracted metrics from {len(metrics_list)} benchmarks")

        # Generate CSV
        csv_path = self.csv_generator.generate(
            metrics_list,
            self.output_dir / "benchmark_results.csv",
        )
        logger.info(f"Generated CSV: {csv_path}")

        # Upload to MLflow
        mlflow_run_id = None
        if self.mlflow_uploader:
            guidellm_args = data.get("args", {})
            mlflow_run_id = self.mlflow_uploader.upload(
                metrics_list=metrics_list,
                guidellm_args=guidellm_args,
                csv_path=csv_path,
            )

            if mlflow_run_id:
                logger.info(f"Uploaded to MLflow: {mlflow_run_id}")
            else:
                logger.warning("MLflow upload failed")

        # Calculate total metrics
        total_metrics = sum(len(m.to_dict()) for m in metrics_list)

        return ProcessingResult(
            success=True,
            mlflow_run_id=mlflow_run_id,
            csv_path=str(csv_path),
            metrics_count=total_metrics,
            benchmarks_count=len(metrics_list),
        )

    def process_file(self, json_path: Path) -> ProcessingResult:
        """Process benchmark results from a file.

        Args:
            json_path: Path to GuideLLM JSON output file

        Returns:
            ProcessingResult with success status and details
        """
        with open(json_path) as f:
            return self.process(f.read())
