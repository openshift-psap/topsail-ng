"""Image registry for vLLM container images.

Single Responsibility: Map (accelerator, vendor) → container image URL.
"""

from pathlib import Path
from typing import Dict, Optional

import yaml


def extract_version_from_image(image_url: str) -> str:
    """Extract version tag from container image URL.

    Examples:
        quay.io/aipcc/rhaiis/cuda-ubi9:3.4.0-ea.1 → 3.4.0-ea.1
        vllm/vllm-openai:v0.14.1 → v0.14.1
        image-without-tag → "latest"
    """
    if ":" in image_url:
        return image_url.split(":")[-1]
    return "latest"


class ImageRegistry:
    """
    vLLM image selection based on accelerator and vendor.

    Loads configuration from config/images.yaml and provides
    lookup for appropriate container images.
    """

    _config: Optional[Dict] = None
    _config_path: Optional[Path] = None

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> None:
        """Load image configuration from YAML file."""
        if config_path is None:
            # Default to config/images.yaml relative to this file
            config_path = Path(__file__).parent.parent / "config" / "images.yaml"
        else:
            config_path = Path(config_path)

        if not config_path.exists():
            raise FileNotFoundError(f"Image config not found: {config_path}")

        with open(config_path) as f:
            cls._config = yaml.safe_load(f)
        cls._config_path = config_path

    @classmethod
    def get_vllm_image(
        cls,
        accelerator: str,
        vendor: Optional[str] = None,
    ) -> str:
        """
        Get vLLM image for accelerator and vendor.

        Args:
            accelerator: "nvidia" or "amd"
            vendor: "redhat" or "upstream" (defaults to config default_vendor)

        Returns:
            Container image URL

        Raises:
            ValueError: If accelerator or vendor not found in config
        """
        if cls._config is None:
            cls.load()

        vendor = vendor or cls._config.get("default_vendor", "redhat")

        vllm_images = cls._config.get("vllm", {})
        if accelerator not in vllm_images:
            available = list(vllm_images.keys())
            raise ValueError(
                f"Unknown accelerator '{accelerator}'. Available: {available}"
            )

        accelerator_images = vllm_images[accelerator]
        if vendor not in accelerator_images:
            available = list(accelerator_images.keys())
            raise ValueError(
                f"Unknown vendor '{vendor}' for accelerator '{accelerator}'. "
                f"Available: {available}"
            )

        return accelerator_images[vendor]

    @classmethod
    def get_default_vendor(cls) -> str:
        """Get the default vendor from config."""
        if cls._config is None:
            cls.load()
        return cls._config.get("default_vendor", "redhat")

    @classmethod
    def list_accelerators(cls) -> list:
        """List available accelerator types."""
        if cls._config is None:
            cls.load()
        return list(cls._config.get("vllm", {}).keys())

    @classmethod
    def list_vendors(cls, accelerator: str) -> list:
        """List available vendors for an accelerator."""
        if cls._config is None:
            cls.load()
        return list(cls._config.get("vllm", {}).get(accelerator, {}).keys())
