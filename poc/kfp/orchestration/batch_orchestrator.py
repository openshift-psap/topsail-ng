"""Batch orchestrator for benchmark scenario execution."""

import json
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Iterator, Callable, Protocol
from itertools import product

import yaml

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.batch import BatchRun
from core.scenario_run import ScenarioRun
from core.types import ScenarioStatus
from core.cluster import Cluster
from core.platform import PlatformConfig
from registry.cluster_registry import ClusterRegistry
from .artifact_collector import ArtifactCollector
from .failure_handler import FailureHandler

# Configuration paths
CONFIG_DIR = Path(__file__).parent.parent / "config"


class BatchOrchestrator:
    """
    Orchestrates batch execution of benchmark scenarios.

    Responsibilities:
    - Load and expand scenario matrix
    - Execute scenarios sequentially with isolation
    - Handle failures gracefully
    - Track progress and collect artifacts
    - Correlate with KFP and MLflow
    """

    def __init__(
        self,
        config_path: Path,
        kubeconfig_path: Optional[str] = None,
        s3_bucket: str = "sagemaker-us-east-1-194365112018",
        dry_run: bool = False,
        cluster_override: Optional[str] = None,
        cluster_registry: Optional[ClusterRegistry] = None,
        deployment_mode: Optional[str] = None,
        vllm_image: Optional[str] = None,
    ):
        self.config_path = config_path
        self.kubeconfig_path = kubeconfig_path
        self.s3_bucket = s3_bucket
        self.dry_run = dry_run
        self.cluster_override = cluster_override  # CLI takes precedence
        self.deployment_mode = deployment_mode  # CLI override for deployment mode
        self.vllm_image = vllm_image  # CLI override for vLLM image

        # Load platform config and cluster registry (DIP)
        self.platform_config = PlatformConfig.load(CONFIG_DIR / "platform.yaml")
        self.cluster_registry = cluster_registry or ClusterRegistry(
            CONFIG_DIR / "clusters.yaml",
            self.platform_config,
        )

        # Initialize components
        self.artifact_collector = ArtifactCollector(
            kubeconfig_path=kubeconfig_path,
            s3_bucket=s3_bucket,
        )
        self.failure_handler = FailureHandler(
            artifact_collector=self.artifact_collector,
            kubeconfig_path=kubeconfig_path,
        )

        # State
        self.batch: Optional[BatchRun] = None
        self.scenarios: List[ScenarioRun] = []
        self.target_cluster: Optional[str] = None
        self.cluster_config: Dict[str, Any] = {}

    def load_config(self) -> Dict[str, Any]:
        """Load scenario configuration from YAML."""
        with open(self.config_path) as f:
            return yaml.safe_load(f)

    def expand_matrix(self, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Expand per-model matrix into individual scenario configs.

        Args:
            config: Full configuration dictionary

        Returns:
            List of expanded scenario configurations
        """
        expanded = []
        common = config.get("common", {})
        common_runtime_args = common.get("runtime_args", {})
        workload_defs = config.get("workloads", {})
        routing_defs = config.get("routing", {})
        models = config.get("models", {})

        for model_id, model_config in models.items():
            matrix = model_config.get("matrix", {})
            if not matrix:
                continue

            # Get matrix dimensions
            workloads = matrix.get("workloads", ["balanced"])
            routings = matrix.get("routing", ["direct"])
            tp_values = matrix.get("tensor-parallel-size", [1])

            # Additional matrix dimensions (extensible)
            extra_dims = {}
            for key, values in matrix.items():
                if key not in ("workloads", "routing", "tensor-parallel-size"):
                    extra_dims[key] = values

            # Expand matrix
            for workload, routing, tp in product(workloads, routings, tp_values):
                # Build runtime_args by merging: common -> model -> matrix override
                runtime_args = dict(common_runtime_args)
                runtime_args.update(model_config.get("runtime_args", {}))
                runtime_args["tensor-parallel-size"] = tp

                # Get workload config
                workload_config = workload_defs.get(workload, {})

                # Apply workload's max-model-len if specified
                if "max-model-len" in workload_config:
                    runtime_args["max-model-len"] = workload_config["max-model-len"]

                # Get routing config
                routing_config = routing_defs.get(routing, {})

                scenario = {
                    "model_id": model_id,
                    "model_short": model_config.get("deploy", {}).get("name", model_id.split("/")[-1]),
                    "workload": workload,
                    "workload_config": workload_config,
                    "routing": routing,
                    "routing_config": routing_config,
                    "tensor_parallel": tp,
                    "num_gpus": tp,  # Always match tensor-parallel-size
                    "runtime_args": runtime_args,
                    "deploy": model_config.get("deploy", {}),
                }

                # Apply CLI overrides if specified
                if self.deployment_mode:
                    scenario["deployment_mode"] = self.deployment_mode
                if self.vllm_image:
                    scenario["vllm_image"] = self.vllm_image

                expanded.append(scenario)

        return expanded

    def _load_cluster_config(self, cluster_id: str) -> Dict[str, Any]:
        """Load cluster configuration from registry."""
        try:
            cluster = self.cluster_registry.get(cluster_id)
            return cluster.to_dict()
        except KeyError:
            # Fallback defaults if cluster not in registry
            return {
                "id": cluster_id,
                "name": cluster_id,
                "kubeconfig_secret": f"{cluster_id}-kubeconfig",
                "namespace": self.platform_config.default_namespace,
                "gpu_type": "H200",
                "kueue_queue": "benchmark-queue",
            }

    def initialize_batch(self) -> BatchRun:
        """Initialize a new batch run."""
        config = self.load_config()
        config_content = self.config_path.read_text()

        # Determine target cluster: CLI override > YAML config > platform default
        self.target_cluster = (
            self.cluster_override
            or config.get("target_cluster")
            or self.platform_config.default_cluster
        )
        self.cluster_config = self._load_cluster_config(self.target_cluster)

        expanded_scenarios = self.expand_matrix(config)

        self.batch = BatchRun.create(
            config_path=str(self.config_path),
            config_snapshot=config_content,
            expanded_scenarios=expanded_scenarios,
            artifacts_bucket=self.s3_bucket,
        )

        # Create ScenarioRun objects
        self.scenarios = []
        for i, scenario_config in enumerate(expanded_scenarios, 1):
            # Add cluster config to each scenario
            scenario_config["cluster"] = self.target_cluster
            scenario_config["cluster_config"] = self.cluster_config

            scenario = ScenarioRun.create(
                batch_id=self.batch.batch_id,
                batch_uuid=self.batch.batch_uuid,
                model_id=scenario_config["model_id"],
                workload=scenario_config["workload"],
                routing=scenario_config["routing"],
                tensor_parallel=scenario_config["tensor_parallel"],
                config=scenario_config,
                sequence_num=i,
                artifacts_base_path=self.batch.artifacts_base_path,
            )
            self.scenarios.append(scenario)

        # S3 uploads disabled - MLflow handles artifact storage
        # Config and execution logs are stored in MLflow with batch_id/scenario_id tags

        return self.batch

    def execute(
        self,
        on_scenario_start: Optional[Callable[[ScenarioRun], None]] = None,
        on_scenario_complete: Optional[Callable[[ScenarioRun], None]] = None,
        submit_fn: Optional[Callable[[ScenarioRun], Optional[str]]] = None,
    ) -> BatchRun:
        """
        Execute all scenarios in the batch.

        Args:
            on_scenario_start: Callback when scenario starts
            on_scenario_complete: Callback when scenario completes (success or failure)
            submit_fn: Function to submit scenario to KFP, returns run_id or None

        Returns:
            Completed BatchRun
        """
        if not self.batch:
            self.initialize_batch()

        print(self._batch_header())

        for scenario in self.scenarios:
            try:
                # Mark as running
                scenario.mark_running()
                if on_scenario_start:
                    on_scenario_start(scenario)

                print(f"\n{scenario.log_line(self.batch.total_scenarios)}")
                print(f"  Model:    {scenario.model_id} (TP={scenario.tensor_parallel})")
                print(f"  Workload: {scenario.workload}")
                print(f"  Routing:  {scenario.routing}")

                if self.dry_run:
                    scenario.mark_completed()
                    self.batch.completed_count += 1
                    print("  [DRY RUN] Would submit to KFP")
                    continue

                # Submit to KFP
                if submit_fn:
                    kfp_run_id = submit_fn(scenario)
                    if kfp_run_id:
                        scenario.kfp_run_id = kfp_run_id
                        scenario.mark_completed()
                        self.batch.completed_count += 1
                        print(f"  ✅ Submitted: {kfp_run_id}")
                    else:
                        # Handle submission failure
                        reason, message, artifacts = self.failure_handler.handle_kfp_failure(
                            error_message="KFP submission returned None",
                            artifacts_path=scenario.artifacts_path,
                        )
                        scenario.mark_failed(reason, message, artifacts)
                        self.batch.failed_count += 1
                        print(f"  ❌ Failed: {message}")
                else:
                    # No submit function - just mark complete for testing
                    scenario.mark_completed()
                    self.batch.completed_count += 1

            except Exception as e:
                # Catch-all for unexpected errors
                scenario.mark_failed(
                    reason="unknown",
                    message=str(e),
                )
                self.batch.failed_count += 1
                print(f"  ❌ Error: {e}")

            finally:
                if on_scenario_complete:
                    on_scenario_complete(scenario)

                # S3 execution log disabled - MLflow handles tracking

        # Finalize batch
        self.batch.mark_completed()
        print(self.batch.summary())

        # Print run IDs for status checking
        self._print_run_ids()

        return self.batch

    def _print_run_ids(self) -> None:
        """Print KFP run IDs for status checking."""
        run_ids = [s.kfp_run_id for s in self.scenarios if s.kfp_run_id]
        if not run_ids:
            return

        print(f"\nKFP Run IDs (for status check):")
        print(f"-" * 40)
        for s in self.scenarios:
            if s.kfp_run_id:
                print(f"  {s.scenario_id}: {s.kfp_run_id}")

        # Print command to check status
        ids_str = ",".join(run_ids)
        print(f"\nCheck status:")
        print(f"  python3 scenario_runner.py status --runs {ids_str}")

    def _batch_header(self) -> str:
        """Generate batch header for display."""
        cluster_name = self.cluster_config.get("name", self.target_cluster)
        gpu_type = self.cluster_config.get("gpu_type", "unknown")
        namespace = self.cluster_config.get("namespace", "default")

        # Build optional lines for CLI overrides
        overrides = []
        if self.deployment_mode:
            overrides.append(f"Mode:       {self.deployment_mode}")
        if self.vllm_image:
            overrides.append(f"vLLM:       {self.vllm_image}")
        overrides_str = "\n".join(overrides)
        if overrides_str:
            overrides_str = overrides_str + "\n"

        return f"""
{'='*60}
Batch: {self.batch.batch_id}
{'='*60}
UUID:       {self.batch.batch_uuid}
Config:     {self.batch.config_path}
Git:        {self.batch.git_branch}@{self.batch.git_commit[:8] if self.batch.git_commit else 'unknown'}
Cluster:    {self.target_cluster} ({cluster_name})
GPU:        {gpu_type}
Namespace:  {namespace}
{overrides_str}Scenarios:  {self.batch.total_scenarios}
Artifacts:  {self.batch.artifacts_base_path}
{'='*60}
"""

    def _save_execution_log(self) -> None:
        """Save current execution state to S3."""
        if self.dry_run:
            return

        scenarios_data = [s.to_dict() for s in self.scenarios]

        self.artifact_collector.save_execution_log(
            batch_id=self.batch.batch_id,
            scenarios=scenarios_data,
            s3_prefix=f"psap-benchmark-runs/{self.batch.batch_id}",
        )

    def list_scenarios(self) -> None:
        """List all scenarios without executing."""
        if not self.batch:
            self.initialize_batch()

        print(self._batch_header())
        print(f"\nExpanded Scenarios ({len(self.scenarios)} total):\n")

        for scenario in self.scenarios:
            print(f"[{scenario.sequence_num}] {scenario.scenario_id}")
            print(f"    Model:    {scenario.model_id}")
            print(f"    Workload: {scenario.workload}")
            print(f"    Routing:  {scenario.routing}")
            print(f"    TP:       {scenario.tensor_parallel}")
            print()

    def get_scenario_by_id(self, scenario_id: str) -> Optional[ScenarioRun]:
        """Get scenario by ID."""
        for scenario in self.scenarios:
            if scenario.scenario_id == scenario_id:
                return scenario
        return None

    def filter_scenarios(self, **filters) -> List[ScenarioRun]:
        """
        Filter scenarios by criteria.

        Examples:
            filter_scenarios(model_id="openai/gpt-oss-120b")
            filter_scenarios(workload="balanced", routing="direct")
        """
        result = self.scenarios
        for key, value in filters.items():
            result = [s for s in result if getattr(s, key, None) == value]
        return result
