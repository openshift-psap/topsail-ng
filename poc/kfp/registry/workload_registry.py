"""Workload registry."""

from dataclasses import dataclass
from pathlib import Path
from .base import BaseRegistry


@dataclass
class Workload:
    """Workload profile from registry."""
    id: str
    name: str
    type: str  # balanced, heterogeneous, multi_turn
    workflow: str = "standard"
    guidellm_data: str = "prompt_tokens=1000,output_tokens=1000"
    guidellm_rate_type: str = "concurrent"
    guidellm_rate: str = "1,50,100,200"
    max_seconds: int = 180
    description: str = ""


class WorkloadRegistry(BaseRegistry[Workload]):
    """Registry for workload profiles."""

    def _load(self) -> None:
        """Load workloads from YAML configuration."""
        data = self._load_yaml()

        for key, config in data.get("workloads", {}).items():
            workload = Workload(
                id=key,
                name=config.get("name", key),
                type=config.get("type", "balanced"),
                workflow=config.get("workflow", "standard"),
                guidellm_data=config.get("guidellm_data", "prompt_tokens=1000,output_tokens=1000"),
                guidellm_rate_type=config.get("guidellm_rate_type", "concurrent"),
                guidellm_rate=config.get("guidellm_rate", "1,50,100,200"),
                max_seconds=config.get("max_seconds", 180),
                description=config.get("description", ""),
            )
            self._cache[key] = workload

    def get(self, key: str) -> Workload:
        """Get workload by key."""
        if key in self._cache:
            return self._cache[key]
        raise KeyError(f"Workload not found: {key}")
