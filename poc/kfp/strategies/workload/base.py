"""Workload strategy interface."""

from abc import ABC, abstractmethod
from typing import List
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core.experiment import Experiment


class WorkloadStrategy(ABC):
    """
    Abstract base for workload generation strategies.

    Each strategy defines how GuideLLM generates load.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy identifier."""
        pass

    @abstractmethod
    def get_guidellm_args(self, experiment: Experiment) -> List[str]:
        """Return GuideLLM command line arguments."""
        pass
