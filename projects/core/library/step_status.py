"""Step status enumeration for export operations."""

from __future__ import annotations

from enum import StrEnum


class StepStatus(StrEnum):
    """Status of a step execution."""

    SUCCESS = "success"
    FAILURE = "failure"
    ONGOING = "ongoing"
    UNKNOWN = "unknown"
    WARNING = "warning"
