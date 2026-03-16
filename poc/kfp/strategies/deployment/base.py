"""Deployment strategy protocol.

Defines the interface for deployment strategies (SOLID: Interface Segregation).
"""

from typing import Protocol, Dict, Any


class DeploymentStrategy(Protocol):
    """
    Protocol for deployment strategies.

    Single Responsibility: Define interface for generating deployment manifests.
    Open/Closed: New deployment modes implement this protocol.
    Liskov Substitution: All implementations are interchangeable.
    """

    def generate_manifests(
        self,
        model_id: str,
        deployment_name: str,
        namespace: str,
        tensor_parallel: int,
        runtime_args: Dict[str, Any],
        **kwargs,
    ) -> str:
        """
        Generate K8s manifests for deployment.

        Args:
            model_id: HuggingFace model identifier
            deployment_name: K8s resource name
            namespace: Target namespace
            tensor_parallel: Number of GPUs
            runtime_args: vLLM runtime arguments
            **kwargs: Strategy-specific arguments

        Returns:
            YAML string with K8s manifests
        """
        ...

    def get_endpoint_url(self, deployment_name: str, namespace: str) -> str:
        """
        Get the endpoint URL pattern for the deployment.

        Args:
            deployment_name: K8s resource name
            namespace: Target namespace

        Returns:
            Internal cluster URL for the inference endpoint
        """
        ...

    def supports_routing(self) -> bool:
        """
        Whether this strategy supports EPP routing modes.

        Returns:
            True if routing modes other than 'direct' are supported
        """
        ...

    def get_resource_kind(self) -> str:
        """
        Get the primary K8s resource kind for this deployment.

        Returns:
            Resource kind (e.g., "LLMInferenceService", "InferenceService")
        """
        ...
