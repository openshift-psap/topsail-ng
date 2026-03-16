"""Experiment domain object."""

from dataclasses import dataclass, field
from typing import Dict, Any, List
from .types import RoutingMode, WorkloadType


@dataclass
class Experiment:
    """
    Complete experiment specification.

    This is the central domain object that flows through the system.
    Extends the existing BenchmarkConfig pattern from benchmark_processor.
    """
    # Identity
    id: str
    name: str

    # Model configuration
    model_id: str                      # Registry key or HF path
    model_name: str                    # Human-readable name
    hf_model_id: str                   # Full HuggingFace model path

    # Deployment configuration
    routing_mode: RoutingMode = RoutingMode.DIRECT
    tensor_parallel: int = 4
    vllm_args: List[str] = field(default_factory=list)
    replicas: int = 1

    # Gateway configuration (for EPP routing modes)
    gateway_name: str = "openshift-ai-inference"
    gateway_namespace: str = "openshift-ingress"

    # Workload configuration
    workload_type: WorkloadType = WorkloadType.BALANCED
    guidellm_data: str = "prompt_tokens=1000,output_tokens=1000"
    guidellm_rate: str = "1,50,100,200"
    guidellm_rate_type: str = "concurrent"
    guidellm_max_seconds: int = 180

    # Cluster targeting
    cluster_id: str = "h200-cluster"
    namespace: str = "llm-d-bench"
    kubeconfig_secret: str = "h200-kubeconfig"

    # Control flags
    skip_deploy: bool = False
    skip_cleanup: bool = False
    clear_prefix_cache: bool = False  # Reset prefix cache between runs
    mlflow_enabled: bool = True

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    # Version tracking
    accelerator: str = "H200"
    version: str = "RHOAI-3.2"

    def to_pipeline_args(self) -> Dict[str, Any]:
        """Convert to KFP pipeline arguments (matches benchmark_api.py)."""
        return {
            "model_name": self.hf_model_id,
            "namespace": self.namespace,
            "tp": self.tensor_parallel,
            "vllm_args": ",".join(self.vllm_args),
            "routing_mode": self.routing_mode.value,
            "kueue_queue_name": "benchmark-queue",
            "run_uuid": self.id,
            "guidellm_rate": self.guidellm_rate,
            "guidellm_data": self.guidellm_data,
            "guidellm_max_seconds": str(self.guidellm_max_seconds),
            "accelerator": self.accelerator,
            "version": self.version,
            "mlflow_enabled": str(self.mlflow_enabled).lower(),
            "skip_cleanup": self.skip_cleanup,
            "kubeconfig_secret": self.kubeconfig_secret,
            "gateway_name": self.gateway_name,
            "gateway_namespace": self.gateway_namespace,
        }

    def to_benchmark_config(self):
        """Convert to existing BenchmarkConfig for processor compatibility."""
        # Import here to avoid circular dependency
        import sys
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from benchmark_processor import BenchmarkConfig
        return BenchmarkConfig(
            model_name=self.hf_model_id,
            accelerator=self.accelerator,
            version=self.version,
            tp=self.tensor_parallel,
            run_uuid=self.id,
            guidellm_data=self.guidellm_data,
            guidellm_rate=self.guidellm_rate,
        )
