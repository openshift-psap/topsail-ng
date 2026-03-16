"""Base registry abstraction."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TypeVar, Generic, Dict, Any, List
import yaml

T = TypeVar('T')


class BaseRegistry(ABC, Generic[T]):
    """Abstract base for configuration registries."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self._cache: Dict[str, T] = {}
        self._alias_map: Dict[str, str] = {}
        self._load()

    def _load_yaml(self) -> Dict[str, Any]:
        """Load YAML configuration file."""
        with open(self.config_path) as f:
            return yaml.safe_load(f)

    @abstractmethod
    def _load(self) -> None:
        """Load and parse configuration."""
        pass

    @abstractmethod
    def get(self, key: str) -> T:
        """Get item by key or alias."""
        pass

    def list_all(self) -> List[str]:
        """List all available keys."""
        return list(self._cache.keys())

    def exists(self, key: str) -> bool:
        """Check if key or alias exists."""
        return key in self._cache or key in self._alias_map

    def __len__(self) -> int:
        """Return number of items in registry."""
        return len(self._cache)

    def __iter__(self):
        """Iterate over all items."""
        return iter(self._cache.values())
