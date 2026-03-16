"""Model registry.

Model keys are HuggingFace IDs directly (e.g., "openai/gpt-oss-120b").
This ensures consistency between registry and actual model download.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any
from .base import BaseRegistry


@dataclass
class Model:
    """
    Model definition from registry.

    The `id` field IS the HuggingFace model ID - no separate hf_model_id needed.
    """
    # Identity - this IS the HuggingFace ID
    id: str  # e.g., "openai/gpt-oss-120b", "RedHatAI/Qwen3-235B-A22B-FP8-dynamic"

    # Display name (optional, defaults to short name from ID)
    name: str

    # Deployment configuration
    deploy_name: str  # Short name for K8s resources
    num_gpus: int = 1
    default_tp: int = 1

    # Runtime arguments (as dict, converted to CLI args)
    runtime_args: Dict[str, Any] = field(default_factory=dict)

    # Legacy: vllm_args as list (for backward compatibility)
    vllm_args: List[str] = field(default_factory=list)

    # Matrix configuration (optional, for per-model sweeps)
    matrix: Dict[str, List[Any]] = field(default_factory=dict)

    # Metadata
    aliases: List[str] = field(default_factory=list)
    supported_workflows: List[str] = field(default_factory=lambda: ["standard"])
    env_vars: Dict[str, str] = field(default_factory=dict)

    @property
    def hf_model_id(self) -> str:
        """HuggingFace model ID (same as id)."""
        return self.id

    @property
    def short_name(self) -> str:
        """Short name derived from HF ID."""
        return self.id.split("/")[-1].lower()

    def get_runtime_args_list(self) -> List[str]:
        """Convert runtime_args dict to CLI argument list."""
        args = []
        for key, value in self.runtime_args.items():
            if isinstance(value, bool):
                if value:
                    args.append(f"--{key}")
            else:
                args.append(f"--{key}={value}")
        # Include legacy vllm_args
        args.extend(self.vllm_args)
        return args


class ModelRegistry(BaseRegistry[Model]):
    """
    Registry for model definitions.

    Models are keyed by HuggingFace ID (e.g., "openai/gpt-oss-120b").
    """

    def _load(self) -> None:
        """Load models from YAML configuration."""
        data = self._load_yaml()
        common = data.get("common", {})
        common_runtime_args = common.get("runtime_args", {})

        for model_id, config in data.get("models", {}).items():
            # model_id IS the HuggingFace ID
            deploy_config = config.get("deploy", {})

            # Merge runtime args: common -> model-specific
            runtime_args = dict(common_runtime_args)
            runtime_args.update(config.get("runtime_args", {}))

            model = Model(
                id=model_id,
                name=deploy_config.get("name", model_id.split("/")[-1]),
                deploy_name=deploy_config.get("name", model_id.split("/")[-1].lower()),
                num_gpus=deploy_config.get("num_gpus", 1),
                default_tp=runtime_args.get("tensor-parallel-size", 1),
                runtime_args=runtime_args,
                vllm_args=config.get("vllm_args", []),
                matrix=config.get("matrix", {}),
                aliases=config.get("aliases", []),
                supported_workflows=config.get("supported_workflows", ["standard"]),
                env_vars=config.get("env_vars", {}),
            )
            self._cache[model_id] = model

            # Register aliases (short names that point to full HF ID)
            for alias in model.aliases:
                self._alias_map[alias] = model_id

            # Auto-register short name as alias
            short_name = model_id.split("/")[-1].lower()
            if short_name not in self._alias_map:
                self._alias_map[short_name] = model_id

    def get(self, key: str) -> Model:
        """
        Get model by HuggingFace ID or alias.

        Args:
            key: HuggingFace ID (e.g., "openai/gpt-oss-120b") or alias

        Returns:
            Model object

        Raises:
            KeyError: If model not found
        """
        # Direct lookup by HF ID
        if key in self._cache:
            return self._cache[key]

        # Lookup by alias
        if key in self._alias_map:
            return self._cache[self._alias_map[key]]

        raise KeyError(f"Model not found: {key}")

    def list_models(self) -> List[str]:
        """List all model HuggingFace IDs."""
        return list(self._cache.keys())

    def list_by_org(self, org: str) -> List[Model]:
        """List models by organization (e.g., 'openai', 'RedHatAI')."""
        return [
            model for model in self._cache.values()
            if model.id.startswith(f"{org}/")
        ]
