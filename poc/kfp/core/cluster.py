"""Enhanced cluster configuration dataclass."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any


@dataclass
class ClusterCapacity:
    """GPU capacity information for a cluster."""
    gpus: int = 0
    max_tensor_parallel: int = 8


@dataclass
class Cluster:
    """
    Complete cluster configuration.

    Single Responsibility: Data container for cluster configuration.
    Loaded from clusters.yaml, provides all settings needed by pipeline components.
    """

    # Identity
    id: str
    name: str
    description: str = ""

    # Kubeconfig
    kubeconfig_path: Optional[str] = None  # Local path (~/.kube/...)
    kubeconfig_secret: str = ""  # K8s secret name in KFP namespace

    # Target settings
    namespace: str = "llm-d-bench"
    gpu_type: str = "H200"
    accelerator: str = "nvidia"  # "nvidia" or "amd" - used for image selection

    # Kueue
    kueue_queue: str = "benchmark-queue"
    kueue_flavor: Optional[str] = None

    # Connectivity
    api_url: Optional[str] = None

    # Capacity
    capacity: ClusterCapacity = field(default_factory=ClusterCapacity)

    # Status
    enabled: bool = True
    verified_at: Optional[str] = None

    @property
    def kubeconfig_path_resolved(self) -> Optional[Path]:
        """Resolve ~ and environment variables in path."""
        if self.kubeconfig_path:
            return Path(self.kubeconfig_path).expanduser()
        return None

    def get_vllm_image(self, vendor: Optional[str] = None) -> str:
        """
        Get appropriate vLLM image for this cluster's accelerator.

        Args:
            vendor: "redhat" or "upstream" (defaults to registry default)

        Returns:
            Container image URL
        """
        from core.images import ImageRegistry
        return ImageRegistry.get_vllm_image(self.accelerator, vendor)

    def to_pipeline_params(self) -> Dict[str, str]:
        """
        Convert to KFP pipeline parameters.

        These are the cluster-specific values passed to rhoai_benchmark_pipeline_v2.
        """
        return {
            "kubeconfig_secret": self.kubeconfig_secret,
            "namespace": self.namespace,
            "accelerator": self.gpu_type,
            "kueue_queue_name": self.kueue_queue,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "kubeconfig_path": self.kubeconfig_path,
            "kubeconfig_secret": self.kubeconfig_secret,
            "namespace": self.namespace,
            "gpu_type": self.gpu_type,
            "accelerator": self.accelerator,
            "kueue_queue": self.kueue_queue,
            "kueue_flavor": self.kueue_flavor,
            "api_url": self.api_url,
            "capacity": {
                "gpus": self.capacity.gpus,
                "max_tensor_parallel": self.capacity.max_tensor_parallel,
            },
            "enabled": self.enabled,
            "verified_at": self.verified_at,
        }

    @classmethod
    def from_dict(cls, cluster_id: str, data: Dict[str, Any]) -> "Cluster":
        """Create Cluster from dictionary (e.g., YAML config)."""
        capacity_data = data.get("capacity", {})
        capacity = ClusterCapacity(
            gpus=capacity_data.get("gpus", 0),
            max_tensor_parallel=capacity_data.get("max_tensor_parallel", 8),
        )

        # Infer accelerator from gpu_type if not specified
        gpu_type = data.get("gpu_type", "H200")
        accelerator = data.get("accelerator")
        if accelerator is None:
            # Auto-detect: MI300X is AMD, others are NVIDIA
            accelerator = "amd" if "MI300" in gpu_type.upper() else "nvidia"

        return cls(
            id=cluster_id,
            name=data.get("name", cluster_id),
            description=data.get("description", ""),
            kubeconfig_path=data.get("kubeconfig_path"),
            kubeconfig_secret=data.get("kubeconfig_secret", f"{cluster_id}-kubeconfig"),
            namespace=data.get("namespace", "llm-d-bench"),
            gpu_type=gpu_type,
            accelerator=accelerator,
            kueue_queue=data.get("kueue_queue", "benchmark-queue"),
            kueue_flavor=data.get("kueue_flavor"),
            api_url=data.get("api_url"),
            capacity=capacity,
            enabled=data.get("enabled", True),
            verified_at=data.get("verified_at"),
        )
