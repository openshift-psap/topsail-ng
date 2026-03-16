"""Protocol definitions for dependency injection."""

from typing import Protocol, List, Optional, TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from .cluster import Cluster


@dataclass
class CheckResult:
    """Result of a validation check."""
    name: str
    passed: bool
    message: str
    details: Optional[str] = None


class ClusterProvider(Protocol):
    """
    Protocol for cluster configuration providers.

    Dependency Inversion: High-level modules depend on this abstraction.
    Open/Closed: New providers can be added without modifying existing code.
    """

    def get(self, cluster_id: str) -> "Cluster":
        """Get cluster by ID. Raises KeyError if not found."""
        ...

    def list_all(self) -> List["Cluster"]:
        """List all available clusters."""
        ...

    def exists(self, cluster_id: str) -> bool:
        """Check if cluster exists."""
        ...

    def get_default(self) -> "Cluster":
        """Get the default cluster."""
        ...


class ClusterValidator(Protocol):
    """Protocol for cluster validation operations."""

    def validate(self, cluster: "Cluster") -> List[CheckResult]:
        """Run all validation checks on a cluster."""
        ...

    def check_connectivity(self, cluster: "Cluster") -> CheckResult:
        """Check if cluster is reachable."""
        ...


class ClusterOperations(Protocol):
    """Protocol for cluster management operations."""

    def create_secret(self, cluster: "Cluster") -> bool:
        """Create kubeconfig secret in KFP namespace."""
        ...

    def update_status(self, cluster_id: str, verified: bool) -> None:
        """Update cluster verification status."""
        ...
