"""Configuration dataclasses for benchmark processing."""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class BenchmarkConfig:
    """Configuration for benchmark run metadata."""

    model_name: str
    accelerator: str
    version: str
    tp: int
    run_uuid: str
    guidellm_data: str = "prompt_tokens=1000,output_tokens=1000"
    guidellm_rate: str = "1,10"

    @property
    def experiment_name(self) -> str:
        """Generate MLflow experiment name: {model}-{accelerator}."""
        return f"{self.model_name.replace('/', '-')}-{self.accelerator}"

    @property
    def prompt_tokens(self) -> int:
        """Extract prompt tokens from guidellm_data."""
        import re
        match = re.search(r"prompt_tokens=(\d+)", self.guidellm_data)
        return int(match.group(1)) if match else 0

    @property
    def output_tokens(self) -> int:
        """Extract output tokens from guidellm_data."""
        import re
        match = re.search(r"output_tokens=(\d+)", self.guidellm_data)
        return int(match.group(1)) if match else 0


@dataclass
class MLflowConfig:
    """Configuration for MLflow connection."""

    # AWS SageMaker MLflow ARN (psap-benchmark-runs app)
    tracking_arn: str = "arn:aws:sagemaker:us-east-1:194365112018:mlflow-app/app-6KQLLW4J4ZQV"

    # Whether to upload artifacts
    upload_csv: bool = True
    upload_json: bool = False  # Full JSON can be large

    # AWS region
    region: str = "us-east-1"


@dataclass
class ProcessingResult:
    """Result of benchmark processing."""

    success: bool
    mlflow_run_id: Optional[str] = None
    csv_path: Optional[str] = None
    metrics_count: int = 0
    benchmarks_count: int = 0
    error_message: Optional[str] = None

    def __str__(self) -> str:
        if self.success:
            return f"Success: {self.benchmarks_count} benchmarks, {self.metrics_count} metrics, run_id={self.mlflow_run_id}"
        return f"Failed: {self.error_message}"
