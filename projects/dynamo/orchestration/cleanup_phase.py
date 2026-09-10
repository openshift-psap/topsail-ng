from __future__ import annotations

import logging

from projects.core.dsl.utils.k8s import oc_resource_exists
from projects.dynamo.toolbox.cleanup_dynamo_resources.main import (
    run as cleanup_dynamo_resources_toolbox_run,
)

logger = logging.getLogger(__name__)


def run(*, namespace: str | None = None) -> int:
    """Delete Dynamo test resources from a namespace."""
    from projects.dynamo.orchestration import runtime_config

    if namespace is None:
        namespace = runtime_config.get_namespace()

    if not oc_resource_exists("namespace", namespace):
        logger.info("Namespace %s does not exist, nothing to clean up", namespace)
        return 0

    benchmark_job_names = runtime_config.get_benchmark_job_names()
    benchmark_name = benchmark_job_names[0] if benchmark_job_names else None

    cleanup_dynamo_resources_toolbox_run(
        namespace=namespace,
        benchmark_job_name=benchmark_name,
    )

    return 0
