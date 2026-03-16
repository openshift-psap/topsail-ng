"""Declarative scenario generation.

Supports two modes:
1. Per-model matrix expansion (new): Each model defines its own matrix sweep
2. Top-level matrix expansion (legacy): Global matrix across models/workloads/clusters

New YAML format (per-model matrix):
```yaml
models:
  openai/gpt-oss-120b:
    deploy:
      name: gpt-oss-120b
      num_gpus: 4
    matrix:
      workloads: [balanced, short]
      routing: [direct]
      tensor-parallel-size: [1, 2, 4]
```
"""

from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Iterator, Dict, Any, List, Optional
import yaml
import uuid
import sys
import os
import re

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.experiment import Experiment
from core.types import RoutingMode, WorkloadType
from core.scenario_run import ScenarioRun


@dataclass
class ScenarioConfig:
    """Parsed scenario configuration."""
    name: str
    description: str
    # Common defaults
    common: Dict[str, Any] = field(default_factory=dict)
    # Workload definitions
    workloads: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Routing definitions
    routing: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Models with per-model matrix
    models: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Explicit run list (optional)
    runs: List[Dict[str, Any]] = field(default_factory=list)
    # Legacy: top-level matrix
    matrix: Dict[str, List[str]] = field(default_factory=dict)
    # Legacy: defaults
    defaults: Dict[str, Any] = field(default_factory=dict)
    # Legacy: scenarios list
    scenarios: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ExpandedScenario:
    """A single expanded scenario from matrix."""
    model_id: str           # HuggingFace ID
    model_short: str        # Short name for display
    workload: str           # balanced, short, etc.
    routing: str            # direct, prefix-estimation, etc.
    tensor_parallel: int    # TP size
    runtime_args: Dict[str, Any]  # Merged runtime args
    workload_config: Dict[str, Any]  # Workload settings
    routing_config: Dict[str, Any]   # Routing settings
    deploy_config: Dict[str, Any]    # Deployment settings

    @property
    def scenario_id(self) -> str:
        """Generate deterministic scenario ID."""
        return f"{self.model_short}_{self.workload}_{self.routing}_tp{self.tensor_parallel}"


class ScenarioGenerator:
    """
    Generate scenarios from declarative configuration.

    Supports:
    - Per-model matrix expansion (model.matrix: workloads × routing × tp)
    - Common defaults merging
    - Runtime args override
    """

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path
        self.config: Optional[ScenarioConfig] = None

    def load(self, path: Optional[Path] = None) -> ScenarioConfig:
        """Load and parse scenario configuration."""
        path = path or self.config_path
        if not path:
            raise ValueError("No config path provided")

        with open(path) as f:
            data = yaml.safe_load(f)

        self.config = ScenarioConfig(
            name=data.get("name", path.stem),
            description=data.get("description", ""),
            common=data.get("common", {}),
            workloads=data.get("workloads", {}),
            routing=data.get("routing", {}),
            models=data.get("models", {}),
            runs=data.get("runs", []),
            # Legacy fields
            matrix=data.get("matrix", {}),
            defaults=data.get("defaults", {}),
            scenarios=data.get("scenarios", []),
        )

        return self.config

    def expand(self) -> List[ExpandedScenario]:
        """
        Expand all matrices into individual scenarios.

        Returns:
            List of ExpandedScenario objects
        """
        if not self.config:
            raise RuntimeError("Must call load() first")

        expanded = []

        # Per-model matrix expansion
        for model_id, model_config in self.config.models.items():
            matrix = model_config.get("matrix", {})
            if not matrix:
                continue

            scenarios = self._expand_model_matrix(model_id, model_config)
            expanded.extend(scenarios)

        # Explicit runs (no matrix expansion)
        for run in self.config.runs:
            scenario = self._create_from_run(run)
            if scenario:
                expanded.append(scenario)

        return expanded

    def _expand_model_matrix(
        self,
        model_id: str,
        model_config: Dict[str, Any]
    ) -> List[ExpandedScenario]:
        """Expand a single model's matrix."""
        matrix = model_config.get("matrix", {})
        deploy_config = model_config.get("deploy", {})

        # Get matrix dimensions
        workloads = matrix.get("workloads", ["balanced"])
        routings = matrix.get("routing", ["direct"])
        tp_values = matrix.get("tensor-parallel-size", [1])

        # Base runtime args: common -> model-specific
        common_runtime = self.config.common.get("runtime_args", {})
        model_runtime = model_config.get("runtime_args", {})

        scenarios = []

        for workload, routing, tp in product(workloads, routings, tp_values):
            # Merge runtime args
            runtime_args = dict(common_runtime)
            runtime_args.update(model_runtime)
            runtime_args["tensor-parallel-size"] = tp

            # Get workload config
            workload_config = self.config.workloads.get(workload, {})

            # Apply workload's max-model-len if specified
            if "max-model-len" in workload_config:
                runtime_args["max-model-len"] = workload_config["max-model-len"]

            # Get routing config
            routing_config = self.config.routing.get(routing, {})

            # Generate short name
            model_short = self._shorten_model_name(model_id)

            # Override num_gpus to match tensor-parallel-size
            scenario_deploy_config = dict(deploy_config)
            scenario_deploy_config["num_gpus"] = tp

            scenario = ExpandedScenario(
                model_id=model_id,
                model_short=model_short,
                workload=workload,
                routing=routing,
                tensor_parallel=tp,
                runtime_args=runtime_args,
                workload_config=workload_config,
                routing_config=routing_config,
                deploy_config=scenario_deploy_config,
            )
            scenarios.append(scenario)

        return scenarios

    def _create_from_run(self, run: Dict[str, Any]) -> Optional[ExpandedScenario]:
        """Create scenario from explicit run definition."""
        model_id = run.get("model")
        if not model_id:
            return None

        model_config = self.config.models.get(model_id, {})
        deploy_config = model_config.get("deploy", {})

        # Merge runtime args
        common_runtime = self.config.common.get("runtime_args", {})
        model_runtime = model_config.get("runtime_args", {})
        run_override = run.get("runtime_args_override", {})

        runtime_args = dict(common_runtime)
        runtime_args.update(model_runtime)
        runtime_args.update(run_override)

        workload = run.get("workload", "balanced")
        routing = run.get("routing", "direct")
        tp = runtime_args.get("tensor-parallel-size", 1)

        return ExpandedScenario(
            model_id=model_id,
            model_short=self._shorten_model_name(model_id),
            workload=workload,
            routing=routing,
            tensor_parallel=tp,
            runtime_args=runtime_args,
            workload_config=self.config.workloads.get(workload, {}),
            routing_config=self.config.routing.get(routing, {}),
            deploy_config=deploy_config,
        )

    @staticmethod
    def _shorten_model_name(model_id: str) -> str:
        """Create short model name from HuggingFace ID."""
        name = model_id.split("/")[-1].lower()
        name = re.sub(r'-instruct.*', '', name)
        name = re.sub(r'-dynamic$', '', name)
        name = re.sub(r'-a\d+b', '', name)
        name = re.sub(r'[^a-z0-9]+', '-', name)
        name = name.strip('-')
        if len(name) > 40:
            name = name[:40].rstrip('-')
        return name

    def to_scenario_runs(
        self,
        batch_id: str,
        batch_uuid: str,
        artifacts_base_path: str = "",
    ) -> List[ScenarioRun]:
        """
        Convert expanded scenarios to ScenarioRun objects.

        Args:
            batch_id: Parent batch ID
            batch_uuid: Parent batch UUID
            artifacts_base_path: S3 base path for artifacts

        Returns:
            List of ScenarioRun objects ready for execution
        """
        expanded = self.expand()
        runs = []

        for i, scenario in enumerate(expanded, 1):
            run = ScenarioRun.create(
                batch_id=batch_id,
                batch_uuid=batch_uuid,
                model_id=scenario.model_id,
                workload=scenario.workload,
                routing=scenario.routing,
                tensor_parallel=scenario.tensor_parallel,
                config={
                    "runtime_args": scenario.runtime_args,
                    "workload_config": scenario.workload_config,
                    "routing_config": scenario.routing_config,
                    "deploy_config": scenario.deploy_config,
                },
                sequence_num=i,
                artifacts_base_path=artifacts_base_path,
            )
            runs.append(run)

        return runs

    def summary(self) -> str:
        """Generate summary of scenarios."""
        if not self.config:
            return "No config loaded"

        expanded = self.expand()
        lines = [
            f"Scenario Config: {self.config.name}",
            f"Description: {self.config.description}",
            f"Models: {len(self.config.models)}",
            f"Expanded Scenarios: {len(expanded)}",
            "",
        ]

        # Group by model
        by_model: Dict[str, List[ExpandedScenario]] = {}
        for s in expanded:
            by_model.setdefault(s.model_id, []).append(s)

        for model_id, scenarios in by_model.items():
            lines.append(f"  {model_id}: {len(scenarios)} scenarios")
            for s in scenarios[:3]:  # Show first 3
                lines.append(f"    - {s.scenario_id}")
            if len(scenarios) > 3:
                lines.append(f"    ... and {len(scenarios) - 3} more")

        return "\n".join(lines)
