from __future__ import annotations

import logging
from pathlib import Path

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger(__name__)


@entrypoint
def run(
    *,
    artifact_dir: Path,
    namespace: str,
    dynamo_namespace: str = "dynamo-system",
    capture_namespace_events: bool = True,
) -> int:
    execute_tasks(locals())
    return 0


@task
def setup_artifacts_directory(args, ctx):
    ctx.artifacts_dir = args.artifact_dir / "artifacts"
    ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return f"Artifacts directory prepared: {ctx.artifacts_dir}"


@task
def capture_dynamo_operator_state(args, ctx):
    """Capture Dynamo operator pod status and logs."""
    dest = ctx.artifacts_dir / "dynamo-operator-pods.txt"
    oc("get", "pods", "-n", args.dynamo_namespace, "-l", "app=dynamo-operator",
       "-o", "wide", check=False, stdout_dest=str(dest))

    log_dest = ctx.artifacts_dir / "dynamo-operator-logs.txt"
    oc("logs", "-n", args.dynamo_namespace, "-l", "app=dynamo-operator",
       "--tail=200", check=False, stdout_dest=str(log_dest))


@task
def capture_dynamo_crds(args, ctx):
    """Capture DynamoGraphDeployment and DynamoComponentDeployment resources."""
    dest = ctx.artifacts_dir / "dynamographdeployments.yaml"
    oc("get", "dynamographdeployments", "-n", args.namespace,
       "-o", "yaml", check=False, stdout_dest=str(dest))

    dest2 = ctx.artifacts_dir / "dynamocomponentdeployments.yaml"
    oc("get", "dynamocomponentdeployments", "-n", args.namespace,
       "-o", "yaml", check=False, stdout_dest=str(dest2))


@task
def capture_infrastructure_state(args, ctx):
    """Capture etcd and NATS pod status."""
    for component in ["etcd", "nats"]:
        dest = ctx.artifacts_dir / f"{component}-pods.txt"
        oc("get", "pods", "-n", args.dynamo_namespace, "-l", f"app={component}",
           "-o", "wide", check=False, stdout_dest=str(dest))


@task
def capture_worker_pods(args, ctx):
    """Capture Dynamo worker and frontend pod status in the test namespace."""
    dest = ctx.artifacts_dir / "dynamo-pods.txt"
    oc("get", "pods", "-n", args.namespace, "-o", "wide",
       check=False, stdout_dest=str(dest))


@task
def capture_namespace_events(args, ctx):
    """Capture namespace events if enabled."""
    if not args.capture_namespace_events:
        return

    dest = ctx.artifacts_dir / "namespace-events.txt"
    oc("get", "events", "-n", args.namespace, "--sort-by=.metadata.creationTimestamp",
       check=False, stdout_dest=str(dest))
