"""Factory for routing strategies."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core.types import RoutingMode
from .base import RoutingStrategy
from .direct import DirectRouting
from .prefix_estimation import PrefixEstimationRouting
from .prefix_precise import PrefixPreciseRouting


_STRATEGIES = {
    RoutingMode.DIRECT: DirectRouting,
    RoutingMode.PREFIX_ESTIMATION: PrefixEstimationRouting,
    RoutingMode.PREFIX_PRECISE: PrefixPreciseRouting,
}


def get_routing_strategy(mode: RoutingMode) -> RoutingStrategy:
    """Get routing strategy for the given mode."""
    strategy_cls = _STRATEGIES.get(mode)
    if not strategy_cls:
        raise ValueError(f"Unknown routing mode: {mode}")
    return strategy_cls()
