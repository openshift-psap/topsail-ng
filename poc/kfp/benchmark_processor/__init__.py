"""Benchmark Processor Module.

OOP-style implementation for post-processing GuideLLM benchmark results
and uploading to MLflow.

Usage:
    from benchmark_processor import BenchmarkProcessor, BenchmarkConfig, MLflowConfig

    config = BenchmarkConfig(
        model_name="Qwen/Qwen3-0.6B",
        accelerator="H200",
        version="RHOAI-3.2",
        tp=4,
        run_uuid="abc123",
    )

    mlflow_config = MLflowConfig(
        tracking_arn="arn:aws:sagemaker:us-east-1:...",
    )

    processor = BenchmarkProcessor(config, mlflow_config)
    result = processor.process(benchmark_json)
"""

from .config import BenchmarkConfig, MLflowConfig
from .metrics import MetricsExtractor
from .csv_generator import CSVGenerator
from .mlflow_uploader import MLflowUploader
from .processor import BenchmarkProcessor

__all__ = [
    "BenchmarkConfig",
    "MLflowConfig",
    "MetricsExtractor",
    "CSVGenerator",
    "MLflowUploader",
    "BenchmarkProcessor",
]
