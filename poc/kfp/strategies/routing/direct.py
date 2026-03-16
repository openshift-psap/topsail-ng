"""Direct routing - KServe SVC only, no EPP."""

from .base import RoutingStrategy
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core.experiment import Experiment


class DirectRouting(RoutingStrategy):
    """
    Direct routing mode - no EPP.

    Traffic goes directly to KServe workload service.
    """

    @property
    def name(self) -> str:
        return "direct"

    def get_router_config(self, experiment: Experiment) -> str:
        """Empty router config for direct mode."""
        return """router:
    route: {}
    gateway: {}
    scheduler: {}"""

    def get_endpoint(self, deployment_name: str, namespace: str) -> str:
        """Direct KServe service endpoint."""
        return f"https://{deployment_name}-kserve-workload-svc.{namespace}.svc.cluster.local:8000"
