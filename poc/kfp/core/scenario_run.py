"""Scenario run tracking for individual benchmark executions."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List
import uuid
import re


@dataclass
class ScenarioRun:
    """
    Individual scenario execution within a batch.

    Tracks execution status, timing, artifacts, and failure information.
    Provides correlation IDs for KFP and MLflow.
    """
    # Identity - deterministic + unique
    scenario_id: str        # Deterministic: "{model_short}_{workload}_{routing}_tp{tp}"
    scenario_uuid: str      # UUID for this specific execution

    # Parent correlation
    batch_id: str           # Link to parent batch
    batch_uuid: str         # UUID of parent batch

    # Scenario configuration
    model_id: str           # HuggingFace model ID (full path)
    model_short: str        # Short name for display
    workload: str           # balanced, short, long-context
    routing: str            # direct, prefix-estimation, etc.
    tensor_parallel: int    # TP size

    # Full configuration for this run
    config: Dict[str, Any] = field(default_factory=dict)

    # Execution status
    status: str = "pending"  # pending | running | completed | failed | skipped | partial
    sequence_num: int = 0    # Position in batch (1-indexed)

    # Timing
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # External system IDs
    kfp_run_id: Optional[str] = None
    kfp_run_url: Optional[str] = None
    mlflow_run_id: Optional[str] = None
    mlflow_run_url: Optional[str] = None

    # Artifact paths
    artifacts_path: str = ""      # s3://bucket/{batch_id}/{scenario_id}/
    guidellm_json_path: Optional[str] = None

    # Failure information
    failure_reason: Optional[str] = None
    failure_message: Optional[str] = None
    failure_artifacts: List[str] = field(default_factory=list)

    # Metrics (populated after successful run)
    metrics: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        batch_id: str,
        batch_uuid: str,
        model_id: str,
        workload: str,
        routing: str,
        tensor_parallel: int,
        config: Dict[str, Any],
        sequence_num: int = 0,
        artifacts_base_path: str = "",
    ) -> "ScenarioRun":
        """
        Factory method to create a ScenarioRun with generated IDs.
        """
        model_short = cls._shorten_model_name(model_id)
        scenario_id = f"{model_short}_{workload}_{routing}_tp{tensor_parallel}"
        scenario_uuid = str(uuid.uuid4())

        artifacts_path = f"{artifacts_base_path}/{scenario_id}" if artifacts_base_path else ""

        return cls(
            scenario_id=scenario_id,
            scenario_uuid=scenario_uuid,
            batch_id=batch_id,
            batch_uuid=batch_uuid,
            model_id=model_id,
            model_short=model_short,
            workload=workload,
            routing=routing,
            tensor_parallel=tensor_parallel,
            config=config,
            sequence_num=sequence_num,
            artifacts_path=artifacts_path,
        )

    @staticmethod
    def _shorten_model_name(model_id: str) -> str:
        """
        Create short model name from HuggingFace ID.

        Examples:
            "openai/gpt-oss-120b" -> "gpt-oss-120b"
            "RedHatAI/Qwen3-235B-A22B-FP8-dynamic" -> "qwen3-235b-fp8"
            "Qwen/Qwen3-235B-A22B-Instruct-2507-FP8" -> "qwen3-235b-fp8"
        """
        # Take last part after /
        name = model_id.split("/")[-1]

        # Lowercase
        name = name.lower()

        # Remove common suffixes for cleaner names
        name = re.sub(r'-instruct.*', '', name)
        name = re.sub(r'-dynamic$', '', name)
        name = re.sub(r'-a\d+b', '', name)  # Remove -A22B etc.

        # Replace non-alphanumeric with dash
        name = re.sub(r'[^a-z0-9]+', '-', name)

        # Remove trailing/leading dashes
        name = name.strip('-')

        # Truncate if too long
        if len(name) > 40:
            name = name[:40].rstrip('-')

        return name

    def mark_running(self) -> None:
        """Mark scenario as running."""
        self.status = "running"
        self.started_at = datetime.utcnow()

    def mark_completed(self, metrics: Optional[Dict[str, Any]] = None) -> None:
        """Mark scenario as successfully completed."""
        self.status = "completed"
        self.completed_at = datetime.utcnow()
        if metrics:
            self.metrics = metrics

    def mark_failed(
        self,
        reason: str,
        message: str,
        artifacts: Optional[List[str]] = None
    ) -> None:
        """Mark scenario as failed with details."""
        self.status = "failed"
        self.completed_at = datetime.utcnow()
        self.failure_reason = reason
        self.failure_message = message
        if artifacts:
            self.failure_artifacts = artifacts

    def mark_skipped(self, reason: str) -> None:
        """Mark scenario as skipped."""
        self.status = "skipped"
        self.failure_reason = reason
        self.failure_message = f"Skipped: {reason}"

    @property
    def duration_seconds(self) -> Optional[int]:
        """Calculate scenario duration in seconds."""
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds())
        return None

    @property
    def is_terminal(self) -> bool:
        """Check if scenario has reached a terminal state."""
        return self.status in ("completed", "failed", "skipped", "partial")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "scenario_id": self.scenario_id,
            "scenario_uuid": self.scenario_uuid,
            "batch_id": self.batch_id,
            "batch_uuid": self.batch_uuid,
            "model_id": self.model_id,
            "model_short": self.model_short,
            "workload": self.workload,
            "routing": self.routing,
            "tensor_parallel": self.tensor_parallel,
            "sequence_num": self.sequence_num,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "kfp_run_id": self.kfp_run_id,
            "kfp_run_url": self.kfp_run_url,
            "mlflow_run_id": self.mlflow_run_id,
            "mlflow_run_url": self.mlflow_run_url,
            "artifacts_path": self.artifacts_path,
            "guidellm_json_path": self.guidellm_json_path,
            "failure_reason": self.failure_reason,
            "failure_message": self.failure_message,
            "failure_artifacts": self.failure_artifacts,
            "metrics": self.metrics,
        }

    def to_mlflow_tags(self) -> Dict[str, str]:
        """Generate MLflow tags for this scenario."""
        return {
            "batch_id": self.batch_id,
            "batch_uuid": self.batch_uuid,
            "scenario_id": self.scenario_id,
            "scenario_uuid": self.scenario_uuid,
            "model_id": self.model_id,
            "workload": self.workload,
            "routing": self.routing,
            "tensor_parallel": str(self.tensor_parallel),
        }

    def log_line(self, total: int) -> str:
        """Generate log line for progress display."""
        status_icons = {
            "pending": "⏳",
            "running": "🔄",
            "completed": "✅",
            "failed": "❌",
            "skipped": "⏭️",
            "partial": "⚠️",
        }
        icon = status_icons.get(self.status, "❓")
        duration = f"({self.duration_seconds}s)" if self.duration_seconds else ""

        return f"[{self.sequence_num}/{total}] {icon} {self.scenario_id} {duration}"
