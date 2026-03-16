"""
Run organization helpers for KFP and MLflow.

Hybrid Approach:
- KFP: Experiment per model (group runs by model)
- MLflow: Nested runs (batch = parent, scenarios = children)
"""

import re
from typing import Optional, Dict, Any


def model_id_to_kfp_experiment(model_id: str) -> str:
    """
    Convert HuggingFace model ID to KFP experiment name.

    KFP experiment names must be:
    - Lowercase
    - Alphanumeric + hyphens
    - No slashes or special characters

    Examples:
        "openai/gpt-oss-120b" -> "openai-gpt-oss-120b"
        "RedHatAI/Qwen3-235B-A22B-FP8-dynamic" -> "redhatai-qwen3-235b-a22b-fp8-dynamic"
        "Qwen/Qwen3-235B-A22B-Instruct-2507-FP8" -> "qwen-qwen3-235b-a22b-instruct-2507-fp8"
    """
    # Lowercase
    name = model_id.lower()

    # Replace / with -
    name = name.replace("/", "-")

    # Replace any non-alphanumeric (except hyphen) with hyphen
    name = re.sub(r'[^a-z0-9-]', '-', name)

    # Collapse multiple hyphens
    name = re.sub(r'-+', '-', name)

    # Remove leading/trailing hyphens
    name = name.strip('-')

    # KFP experiment names have length limits
    if len(name) > 63:
        name = name[:63].rstrip('-')

    return name


def scenario_to_kfp_run_name(
    workload: str,
    routing: str,
    tensor_parallel: int,
    batch_id: Optional[str] = None,
) -> str:
    """
    Generate KFP run name for a scenario.

    Format: {workload}_{routing}_tp{tp}
    Optionally append batch_id for uniqueness.

    Examples:
        "balanced_direct_tp4"
        "short_direct_tp2_batch-20260314-143022"
    """
    name = f"{workload}_{routing}_tp{tensor_parallel}"

    if batch_id:
        name = f"{name}_{batch_id}"

    return name


def scenario_to_mlflow_run_name(
    workload: str,
    routing: str,
    tensor_parallel: int,
    model_short: Optional[str] = None,
) -> str:
    """
    Generate MLflow run name for a scenario.

    For nested runs under a batch parent:
    Format: {model_short}_{workload}_{routing}_tp{tp}

    Examples:
        "gpt-oss-120b_balanced_direct_tp4"
        "qwen3-235b-fp8_short_direct_tp2"
    """
    if model_short:
        return f"{model_short}_{workload}_{routing}_tp{tensor_parallel}"
    return f"{workload}_{routing}_tp{tensor_parallel}"


def create_mlflow_tags(
    batch_id: str,
    batch_uuid: str,
    scenario_id: str,
    scenario_uuid: str,
    model_id: str,
    workload: str,
    routing: str,
    tensor_parallel: int,
    **extra_tags,
) -> Dict[str, str]:
    """
    Create MLflow tags for a scenario run.

    Tags enable filtering and searching across runs.
    """
    tags = {
        # Correlation IDs
        "batch_id": batch_id,
        "batch_uuid": batch_uuid,
        "scenario_id": scenario_id,
        "scenario_uuid": scenario_uuid,

        # Configuration
        "model_id": model_id,
        "workload": workload,
        "routing": routing,
        "tensor_parallel": str(tensor_parallel),

        # Useful for MLflow UI filtering
        "mlflow.runName": scenario_id,
    }

    # Add any extra tags
    for key, value in extra_tags.items():
        tags[key] = str(value)

    return tags


def create_kfp_labels(
    batch_id: str,
    scenario_id: str,
    model_id: str,
    workload: str,
    routing: str,
) -> Dict[str, str]:
    """
    Create KFP pipeline run labels.

    Labels enable filtering in KFP UI.
    Note: K8s labels have stricter requirements than MLflow tags.
    """
    # Sanitize for K8s label values (max 63 chars, alphanumeric + -_.)
    def sanitize(value: str, max_len: int = 63) -> str:
        value = re.sub(r'[^a-zA-Z0-9._-]', '-', value)
        value = value[:max_len].rstrip('-_.')
        return value

    return {
        "batch-id": sanitize(batch_id),
        "scenario-id": sanitize(scenario_id),
        "model": sanitize(model_id.split("/")[-1]),  # Just model name, not org
        "workload": sanitize(workload),
        "routing": sanitize(routing),
    }


class MLflowNestedRunManager:
    """
    Helper for managing MLflow nested runs.

    Usage:
        manager = MLflowNestedRunManager(experiment_name="psap-benchmark-runs")
        parent_run_id = manager.start_batch_run(batch_id, batch_uuid)

        for scenario in scenarios:
            with manager.scenario_run(scenario) as run:
                # Log metrics, artifacts
                mlflow.log_metric("throughput", 100)

        manager.end_batch_run()
    """

    def __init__(self, experiment_name: str = "psap-benchmark-runs"):
        self.experiment_name = experiment_name
        self.parent_run_id: Optional[str] = None
        self._mlflow = None

    def _get_mlflow(self):
        """Lazy import mlflow."""
        if self._mlflow is None:
            import mlflow
            self._mlflow = mlflow
        return self._mlflow

    def start_batch_run(
        self,
        batch_id: str,
        batch_uuid: str,
        git_commit: Optional[str] = None,
        config_snapshot: Optional[str] = None,
    ) -> str:
        """
        Start a parent run for the batch.

        Returns:
            Parent run ID
        """
        mlflow = self._get_mlflow()

        mlflow.set_experiment(self.experiment_name)

        run = mlflow.start_run(
            run_name=batch_id,
            tags={
                "batch_id": batch_id,
                "batch_uuid": batch_uuid,
                "run_type": "batch_parent",
            },
        )
        self.parent_run_id = run.info.run_id

        # Log git commit
        if git_commit:
            mlflow.set_tag("git_commit", git_commit)

        # Log config as artifact
        if config_snapshot:
            import tempfile
            import os
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(config_snapshot)
                temp_path = f.name
            mlflow.log_artifact(temp_path, "config")
            os.unlink(temp_path)

        return self.parent_run_id

    def start_scenario_run(
        self,
        scenario_id: str,
        scenario_uuid: str,
        model_id: str,
        workload: str,
        routing: str,
        tensor_parallel: int,
        batch_id: str,
        batch_uuid: str,
    ) -> str:
        """
        Start a nested child run for a scenario.

        Returns:
            Scenario run ID
        """
        mlflow = self._get_mlflow()

        if not self.parent_run_id:
            raise RuntimeError("Must call start_batch_run first")

        tags = create_mlflow_tags(
            batch_id=batch_id,
            batch_uuid=batch_uuid,
            scenario_id=scenario_id,
            scenario_uuid=scenario_uuid,
            model_id=model_id,
            workload=workload,
            routing=routing,
            tensor_parallel=tensor_parallel,
            run_type="scenario",
        )

        # Use model_short + workload + routing as run name
        model_short = model_id.split("/")[-1].lower()
        run_name = scenario_to_mlflow_run_name(
            workload=workload,
            routing=routing,
            tensor_parallel=tensor_parallel,
            model_short=model_short,
        )

        run = mlflow.start_run(
            run_name=run_name,
            nested=True,
            tags=tags,
        )

        return run.info.run_id

    def end_scenario_run(self, status: str = "FINISHED") -> None:
        """End the current scenario run."""
        mlflow = self._get_mlflow()
        mlflow.end_run(status=status)

    def end_batch_run(self, status: str = "FINISHED") -> None:
        """End the parent batch run."""
        mlflow = self._get_mlflow()
        mlflow.end_run(status=status)
        self.parent_run_id = None
