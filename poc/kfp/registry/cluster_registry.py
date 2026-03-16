"""Cluster registry - single source of truth for cluster configurations."""

from pathlib import Path
from typing import List, Optional
import sys
import os

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .base import BaseRegistry
from core.cluster import Cluster, ClusterCapacity
from core.platform import PlatformConfig


class ClusterRegistry(BaseRegistry[Cluster]):
    """
    Registry for cluster configurations.

    Single Responsibility: Load and provide cluster configurations.
    Implements ClusterProvider protocol for dependency injection.
    """

    def __init__(
        self,
        config_path: Path,
        platform_config: Optional[PlatformConfig] = None,
    ):
        self.platform_config = platform_config
        super().__init__(config_path)

    def _load(self) -> None:
        """Load clusters from YAML configuration."""
        data = self._load_yaml()

        for cluster_id, config in data.get("clusters", {}).items():
            cluster = Cluster.from_dict(cluster_id, config)
            self._cache[cluster_id] = cluster

            # Register aliases (e.g., gpu_type as alias)
            if config.get("gpu_type"):
                gpu_alias = config["gpu_type"].lower()
                if gpu_alias not in self._alias_map:
                    self._alias_map[gpu_alias] = cluster_id

    def get(self, key: str) -> Cluster:
        """
        Get cluster by ID or alias.

        Raises KeyError if not found.
        """
        # Direct lookup
        if key in self._cache:
            return self._cache[key]

        # Alias lookup
        if key in self._alias_map:
            return self._cache[self._alias_map[key]]

        # Case-insensitive lookup
        key_lower = key.lower()
        for cluster_id in self._cache:
            if cluster_id.lower() == key_lower:
                return self._cache[cluster_id]

        raise KeyError(f"Cluster not found: {key}")

    def list_all(self) -> List[Cluster]:
        """List all available clusters."""
        return list(self._cache.values())

    def get_default(self) -> Cluster:
        """
        Get default cluster from platform config.

        Falls back to first enabled cluster if no default specified.
        """
        # Try platform config default
        if self.platform_config:
            default_id = self.platform_config.default_cluster
            if default_id and self.exists(default_id):
                return self.get(default_id)

        # Fallback to first enabled cluster
        for cluster in self._cache.values():
            if cluster.enabled:
                return cluster

        raise ValueError("No enabled clusters found")

    def list_by_gpu_type(self, gpu_type: str) -> List[Cluster]:
        """Filter clusters by GPU type."""
        gpu_type_upper = gpu_type.upper()
        return [
            c for c in self._cache.values()
            if c.gpu_type.upper() == gpu_type_upper
        ]

    def list_enabled(self) -> List[Cluster]:
        """List only enabled clusters."""
        return [c for c in self._cache.values() if c.enabled]

    def get_by_gpu_type(self, gpu_type: str) -> Cluster:
        """
        Get first cluster matching GPU type.

        Useful for backward compatibility with flavor-based lookups.
        """
        clusters = self.list_by_gpu_type(gpu_type)
        if clusters:
            return clusters[0]
        raise KeyError(f"No cluster found for GPU type: {gpu_type}")


# Singleton instance for convenience (optional usage)
_default_registry: Optional[ClusterRegistry] = None


def get_cluster_registry(
    config_path: Optional[Path] = None,
    platform_config: Optional[PlatformConfig] = None,
) -> ClusterRegistry:
    """
    Get or create the default cluster registry.

    Uses singleton pattern for convenience, but fresh instances
    can be created by passing config_path directly.
    """
    global _default_registry

    if config_path is not None:
        # Create fresh instance with specified path
        return ClusterRegistry(config_path, platform_config)

    if _default_registry is None:
        # Create default instance
        default_path = Path(__file__).parent.parent / "config" / "clusters.yaml"
        _default_registry = ClusterRegistry(default_path, platform_config)

    return _default_registry
