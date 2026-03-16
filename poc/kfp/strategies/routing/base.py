"""Routing strategy interface."""

from abc import ABC, abstractmethod
from typing import List
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core.experiment import Experiment


class RoutingStrategy(ABC):
    """
    Abstract base for routing strategies.

    Each strategy generates the router configuration for LLMInferenceService.
    The deployment is always LLMInferenceService - only the router config varies.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy identifier (matches RoutingMode enum value)."""
        pass

    @abstractmethod
    def get_router_config(self, experiment: Experiment) -> str:
        """Generate the router: section of LLMInferenceService YAML."""
        pass

    @abstractmethod
    def get_endpoint(self, deployment_name: str, namespace: str) -> str:
        """Return the correct endpoint URL for this routing mode."""
        pass

    def supports_prefix_cache_reset(self) -> bool:
        """Whether this routing mode benefits from prefix cache reset."""
        return False

    def get_prefix_cache_reset_commands(self, deployment_name: str, namespace: str) -> List[str]:
        """Return commands to reset prefix cache (delete pods to clear cache)."""
        return [
            f"oc delete pod -l model={deployment_name} -n {namespace}",
            f"oc wait --for=condition=ready pod -l model={deployment_name} -n {namespace} --timeout=300s",
        ]
