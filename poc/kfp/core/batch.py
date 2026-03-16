"""Batch run container for benchmark orchestration."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any
import uuid
import subprocess


@dataclass
class BatchRun:
    """
    Parent container for a benchmark batch.

    A batch contains multiple scenarios executed in sequence.
    Provides correlation IDs for KFP and MLflow tracking.
    """
    # Identity - human readable + machine correlation
    batch_id: str                    # "batch-20260314-143022" (timestamp-based)
    batch_uuid: str                  # UUID for correlation across systems

    # Configuration snapshot for reproducibility
    config_path: str                 # Path to scenarios.yaml used
    config_snapshot: str             # Full YAML content at execution time
    expanded_scenarios: List[Dict[str, Any]]  # Post-matrix expansion list

    # Version tracking
    git_commit: Optional[str] = None
    git_branch: Optional[str] = None
    vllm_version: Optional[str] = None

    # Timing
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Execution tracking
    total_scenarios: int = 0
    completed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0

    # S3/artifact paths
    artifacts_base_path: str = ""    # s3://bucket/{batch_id}/

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    @classmethod
    def create(cls, config_path: str, config_snapshot: str,
               expanded_scenarios: List[Dict[str, Any]],
               artifacts_bucket: str = "sagemaker-us-east-1-194365112018") -> "BatchRun":
        """
        Factory method to create a new BatchRun with generated IDs.
        """
        now = datetime.utcnow()
        batch_id = f"batch-{now.strftime('%Y%m%d-%H%M%S')}"
        batch_uuid = str(uuid.uuid4())

        # Get git info
        git_commit = cls._get_git_commit()
        git_branch = cls._get_git_branch()

        artifacts_base_path = f"s3://{artifacts_bucket}/psap-benchmark-runs/{batch_id}"

        return cls(
            batch_id=batch_id,
            batch_uuid=batch_uuid,
            config_path=config_path,
            config_snapshot=config_snapshot,
            expanded_scenarios=expanded_scenarios,
            git_commit=git_commit,
            git_branch=git_branch,
            started_at=now,
            total_scenarios=len(expanded_scenarios),
            artifacts_base_path=artifacts_base_path,
        )

    @staticmethod
    def _get_git_commit() -> Optional[str]:
        """Get current git commit hash."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    @staticmethod
    def _get_git_branch() -> Optional[str]:
        """Get current git branch."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    def mark_completed(self) -> None:
        """Mark the batch as completed."""
        self.completed_at = datetime.utcnow()

    @property
    def duration_seconds(self) -> Optional[int]:
        """Calculate batch duration in seconds."""
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds())
        return None

    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage."""
        if self.total_scenarios == 0:
            return 0.0
        return (self.completed_count / self.total_scenarios) * 100

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "batch_id": self.batch_id,
            "batch_uuid": self.batch_uuid,
            "config_path": self.config_path,
            "git_commit": self.git_commit,
            "git_branch": self.git_branch,
            "vllm_version": self.vllm_version,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "total_scenarios": self.total_scenarios,
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "success_rate": self.success_rate,
            "artifacts_base_path": self.artifacts_base_path,
            "metadata": self.metadata,
            "tags": self.tags,
        }

    def summary(self) -> str:
        """Generate human-readable summary."""
        duration = f"{self.duration_seconds}s" if self.duration_seconds else "in progress"
        return f"""
{'='*60}
Batch Summary: {self.batch_id}
{'='*60}
UUID:        {self.batch_uuid}
Git:         {self.git_branch}@{self.git_commit[:8] if self.git_commit else 'unknown'}
Duration:    {duration}

Scenarios:   {self.total_scenarios} total
  ✅ Completed: {self.completed_count}
  ❌ Failed:    {self.failed_count}
  ⏭️  Skipped:   {self.skipped_count}

Success Rate: {self.success_rate:.1f}%

Artifacts:   {self.artifacts_base_path}
{'='*60}
"""
