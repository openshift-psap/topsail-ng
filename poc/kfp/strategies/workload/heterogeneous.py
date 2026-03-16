"""Heterogeneous workload strategy."""

from typing import List
from .base import WorkloadStrategy
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core.experiment import Experiment


class HeterogeneousWorkload(WorkloadStrategy):
    """
    Heterogeneous workload with mixed token distributions.

    Simulates real-world traffic with varying request sizes.
    """

    @property
    def name(self) -> str:
        return "heterogeneous"

    def get_guidellm_args(self, experiment: Experiment) -> List[str]:
        return [
            "--data", "emulated",
            "--data-args", "prompt_tokens_mean=2000,prompt_tokens_stdev=1000,output_tokens_mean=500,output_tokens_stdev=200",
            "--rate-type", experiment.guidellm_rate_type,
            "--rate", experiment.guidellm_rate,
            "--max-seconds", str(experiment.guidellm_max_seconds),
        ]
