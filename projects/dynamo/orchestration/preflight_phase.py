from __future__ import annotations

import logging

from projects.core.dsl.utils.k8s import oc_resource_exists
from projects.dynamo.orchestration import runtime_config

logger = logging.getLogger(__name__)


def run() -> int:
    """Validate that required Dynamo CRDs exist in the cluster."""
    logger.info("Starting Dynamo preflight checks")

    dynamo_config = runtime_config.get_dynamo_config()
    required_crds = dynamo_config["required_crds"]

    missing_crds = []

    for crd_name in required_crds:
        logger.info(f"Checking for CRD: {crd_name}")
        if not oc_resource_exists("crd", crd_name):
            missing_crds.append(crd_name)
            logger.error(f"Required CRD not found: {crd_name}")
        else:
            logger.info(f"CRD found: {crd_name}")

    if missing_crds:
        logger.error(
            f"Preflight check failed - missing {len(missing_crds)} required CRDs: "
            f"{', '.join(missing_crds)}"
        )
        return 1

    logger.info("Preflight checks completed successfully - all required Dynamo CRDs are available")
    return 0
