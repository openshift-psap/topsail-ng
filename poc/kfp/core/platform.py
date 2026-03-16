"""Platform-wide configuration."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional
import os
import yaml


@dataclass
class KFPConfig:
    """KFP connection settings."""
    host: str = ""
    namespace: str = "kubeflow"


@dataclass
class MLflowConfig:
    """MLflow settings."""
    tracking_uri: str = ""
    s3_bucket: str = ""


@dataclass
class PlatformConfig:
    """
    Platform-wide configuration.

    Single Responsibility: Load and provide platform settings.
    Loaded from platform.yaml with environment variable fallbacks.
    """

    # Management cluster settings
    mgmt_kubeconfig: str = ""
    mgmt_namespace: str = "benchmark-system"

    # KFP settings
    kfp: KFPConfig = field(default_factory=KFPConfig)

    # MLflow settings
    mlflow: MLflowConfig = field(default_factory=MLflowConfig)

    # Default values
    defaults: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "PlatformConfig":
        """
        Load from YAML file with environment variable fallbacks.

        If config_path is None or doesn't exist, returns config from env vars only.
        """
        if config_path is None or not config_path.exists():
            return cls._from_env()

        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        mgmt = data.get("management", {})
        kfp_data = data.get("kfp", {})
        mlflow_data = data.get("mlflow", {})

        # Environment variables take precedence
        mgmt_kc = os.environ.get("MGMT_KUBECONFIG") or mgmt.get("kubeconfig", "")
        kfp_host = os.environ.get("KFP_HOST") or kfp_data.get("host", "")

        return cls(
            mgmt_kubeconfig=str(Path(mgmt_kc).expanduser()) if mgmt_kc else "",
            mgmt_namespace=mgmt.get("namespace", "benchmark-system"),
            kfp=KFPConfig(
                host=kfp_host,
                namespace=kfp_data.get("namespace", "kubeflow"),
            ),
            mlflow=MLflowConfig(
                tracking_uri=mlflow_data.get("tracking_uri", ""),
                s3_bucket=mlflow_data.get("s3_bucket", ""),
            ),
            defaults=data.get("defaults", {}),
        )

    @classmethod
    def _from_env(cls) -> "PlatformConfig":
        """Create from environment variables only (fallback mode)."""
        mgmt_kc = os.environ.get("MGMT_KUBECONFIG", "")
        kfp_host = os.environ.get("KFP_HOST", "")

        return cls(
            mgmt_kubeconfig=str(Path(mgmt_kc).expanduser()) if mgmt_kc else "",
            kfp=KFPConfig(host=kfp_host),
            defaults={
                "cluster": "h200-cluster",
                "namespace": "llm-d-bench",
            },
        )

    @property
    def default_cluster(self) -> str:
        """Get default cluster ID."""
        return self.defaults.get("cluster", "h200-cluster")

    @property
    def default_namespace(self) -> str:
        """Get default namespace."""
        return self.defaults.get("namespace", "llm-d-bench")
