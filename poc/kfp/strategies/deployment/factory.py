"""Deployment strategy factory.

Single Responsibility: Create appropriate deployment strategy based on mode.
Open/Closed: New strategies can be added without modifying existing code.
"""

from typing import Union

from strategies.deployment.base import DeploymentStrategy
from strategies.deployment.rhoai import RHOAIDeploymentStrategy
from strategies.deployment.rhaiis import RHAIISDeploymentStrategy


class DeploymentStrategyFactory:
    """Factory for creating deployment strategies."""

    _strategies = {
        "rhoai": RHOAIDeploymentStrategy,
        "rhaiis": RHAIISDeploymentStrategy,
    }

    @classmethod
    def create(cls, deployment_mode: str) -> Union[RHOAIDeploymentStrategy, RHAIISDeploymentStrategy]:
        """
        Create deployment strategy for the given mode.

        Args:
            deployment_mode: "rhoai" or "rhaiis"

        Returns:
            Deployment strategy instance

        Raises:
            ValueError: If deployment mode is unknown
        """
        mode_lower = deployment_mode.lower()
        if mode_lower not in cls._strategies:
            available = list(cls._strategies.keys())
            raise ValueError(
                f"Unknown deployment mode '{deployment_mode}'. Available: {available}"
            )

        return cls._strategies[mode_lower]()

    @classmethod
    def get_available_modes(cls) -> list:
        """List available deployment modes."""
        return list(cls._strategies.keys())

    @classmethod
    def register(cls, mode: str, strategy_class: type) -> None:
        """
        Register a new deployment strategy.

        Allows extension without modifying this class (Open/Closed).

        Args:
            mode: Deployment mode identifier
            strategy_class: Strategy class to instantiate
        """
        cls._strategies[mode.lower()] = strategy_class
