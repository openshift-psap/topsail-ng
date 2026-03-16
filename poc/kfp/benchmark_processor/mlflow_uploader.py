"""MLflow uploader for benchmark results."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import BenchmarkConfig, MLflowConfig
from .metrics import BenchmarkMetrics

logger = logging.getLogger(__name__)


class MLflowUploader:
    """Uploads benchmark metrics and artifacts to MLflow."""

    def __init__(self, config: BenchmarkConfig, mlflow_config: MLflowConfig):
        self.config = config
        self.mlflow_config = mlflow_config
        self._mlflow = None
        self._client = None

    def _init_mlflow(self):
        """Initialize MLflow connection."""
        if self._mlflow is not None:
            return

        try:
            import mlflow
            import sagemaker_mlflow

            self._mlflow = mlflow
            logger.info(f"MLflow version: {mlflow.__version__}")
        except ImportError as e:
            raise ImportError(
                "MLflow and sagemaker-mlflow are required. "
                "Install with: pip install mlflow sagemaker-mlflow"
            ) from e

        # Connect to SageMaker MLflow
        logger.info(f"Connecting to MLflow: {self.mlflow_config.tracking_arn}")
        self._mlflow.set_tracking_uri(self.mlflow_config.tracking_arn)

        # Set experiment
        experiment_name = self.config.experiment_name
        logger.info(f"Setting experiment: {experiment_name}")
        self._mlflow.set_experiment(experiment_name)

        self._client = self._mlflow.tracking.MlflowClient()

    def _log_tags(self):
        """Log MLflow tags."""
        self._mlflow.set_tag("run_uuid", self.config.run_uuid)
        self._mlflow.set_tag("pipeline_run_uuid", self.config.run_uuid)
        self._mlflow.set_tag("model", self.config.model_name)
        self._mlflow.set_tag("accelerator", self.config.accelerator)
        self._mlflow.set_tag("version", self.config.version)
        self._mlflow.set_tag("rate_type", "concurrent")

    def _log_params(self, guidellm_args: Dict[str, Any] = None):
        """Log MLflow parameters."""
        params = {
            "model": self.config.model_name,
            "tp": self.config.tp,
            "accelerator": self.config.accelerator,
            "version": self.config.version,
            "run_uuid": self.config.run_uuid,
            "rates": self.config.guidellm_rate,
            "prompt_tokens": self.config.prompt_tokens,
            "output_tokens": self.config.output_tokens,
        }

        if guidellm_args:
            if guidellm_args.get("backend_type"):
                params["backend_type"] = guidellm_args["backend_type"]
            if guidellm_args.get("target"):
                params["target"] = guidellm_args["target"]
            if guidellm_args.get("max_seconds"):
                params["max_seconds"] = guidellm_args["max_seconds"]

        self._mlflow.log_params(params)
        logger.info(f"Logged {len(params)} parameters")

    def _log_metrics(self, metrics_list: List[BenchmarkMetrics]) -> int:
        """Log metrics from all benchmarks.

        Args:
            metrics_list: List of BenchmarkMetrics

        Returns:
            Total number of metrics logged
        """
        total_metrics = 0

        for metrics in metrics_list:
            metrics_dict = metrics.to_dict()
            concurrency = metrics.concurrency

            for key, value in metrics_dict.items():
                if isinstance(value, (int, float)) and value is not None:
                    self._mlflow.log_metric(key, value, step=concurrency)

            count = len(metrics_dict)
            total_metrics += count
            logger.info(f"Logged {count} metrics for concurrency={concurrency}")

        return total_metrics

    def _log_artifacts(self, csv_path: Optional[Path] = None, json_path: Optional[Path] = None):
        """Log artifact files to MLflow."""
        if csv_path and csv_path.exists() and self.mlflow_config.upload_csv:
            self._client.log_artifact(
                self._mlflow.active_run().info.run_id,
                str(csv_path),
                "results"
            )
            logger.info(f"Uploaded CSV: {csv_path.name}")

        if json_path and json_path.exists() and self.mlflow_config.upload_json:
            import gzip
            gz_path = Path(f"{json_path}.gz")
            with open(json_path, "rb") as f_in:
                with gzip.open(gz_path, "wb") as f_out:
                    f_out.write(f_in.read())
            self._client.log_artifact(
                self._mlflow.active_run().info.run_id,
                str(gz_path),
                "results"
            )
            logger.info(f"Uploaded JSON (gzipped): {gz_path.name}")

    def upload(
        self,
        metrics_list: List[BenchmarkMetrics],
        guidellm_args: Dict[str, Any] = None,
        csv_path: Optional[Path] = None,
        json_path: Optional[Path] = None,
    ) -> Optional[str]:
        """Upload benchmark results to MLflow.

        Args:
            metrics_list: List of BenchmarkMetrics from each concurrency level
            guidellm_args: Optional GuideLLM args dict for additional parameters
            csv_path: Optional path to CSV file to upload
            json_path: Optional path to JSON file to upload

        Returns:
            MLflow run ID if successful, None otherwise
        """
        try:
            self._init_mlflow()

            with self._mlflow.start_run(run_name=self.config.run_uuid) as run:
                # Log tags and parameters
                self._log_tags()
                self._log_params(guidellm_args)

                # Log metrics
                total_metrics = self._log_metrics(metrics_list)

                # Log artifacts
                self._log_artifacts(csv_path, json_path)

                run_id = run.info.run_id
                logger.info(f"MLflow upload complete: {run_id}")
                logger.info(f"  Experiment: {self.config.experiment_name}")
                logger.info(f"  Benchmarks: {len(metrics_list)}")
                logger.info(f"  Total metrics: {total_metrics}")

                return run_id

        except Exception as e:
            logger.error(f"MLflow upload failed: {e}")
            import traceback
            traceback.print_exc()
            return None

    def get_experiment_url(self) -> str:
        """Get URL to MLflow experiment."""
        return f"https://mlflow.sagemaker.{self.mlflow_config.region}.app.aws/"
