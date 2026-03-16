"""Balanced workload strategy."""

from typing import List
from .base import WorkloadStrategy
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core.experiment import Experiment


class BalancedWorkload(WorkloadStrategy):
    """
    Balanced workload with fixed token sizes.

    Generates uniform traffic with consistent prompt/output lengths.
    """

    @property
    def name(self) -> str:
        return "balanced"

    def get_guidellm_args(self, experiment: Experiment) -> List[str]:
        return [
            "--data", experiment.guidellm_data,
            "--rate-type", experiment.guidellm_rate_type,
            "--rate", experiment.guidellm_rate,
            "--max-seconds", str(experiment.guidellm_max_seconds),
        ]
