from __future__ import annotations

import logging

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger(__name__)


@entrypoint
def run(
    *,
    namespace: str,
    benchmark_job_name: str | None = None,
) -> int:
    execute_tasks(locals())
    return 0


@task
def delete_graph_deployments(args, ctx):
    """Delete all DynamoGraphDeployments in the namespace."""
    oc("delete", "dynamographdeployments", "--all", "-n", args.namespace,
       "--ignore-not-found", check=False)
    logger.info("Deleted DynamoGraphDeployments in %s", args.namespace)


@task
def delete_component_deployments(args, ctx):
    """Delete all DynamoComponentDeployments in the namespace."""
    oc("delete", "dynamocomponentdeployments", "--all", "-n", args.namespace,
       "--ignore-not-found", check=False)
    logger.info("Deleted DynamoComponentDeployments in %s", args.namespace)


@task
def delete_benchmark_resources(args, ctx):
    """Delete benchmark job and associated resources."""
    if not args.benchmark_job_name:
        return

    oc("delete", "job", args.benchmark_job_name, "-n", args.namespace,
       "--ignore-not-found", check=False)
    oc("delete", "pod", "-n", args.namespace, "-l",
       f"job-name={args.benchmark_job_name}", "--ignore-not-found", check=False)
    logger.info("Deleted benchmark resources for %s", args.benchmark_job_name)


@task
def delete_forge_labeled_resources(args, ctx):
    """Delete all Forge-managed resources in the namespace."""
    for kind in ["pods", "services", "configmaps", "jobs"]:
        oc("delete", kind, "-n", args.namespace,
           "-l", "forge.openshift.io/project=dynamo",
           "--ignore-not-found", check=False)
    logger.info("Deleted Forge-labeled resources in %s", args.namespace)
