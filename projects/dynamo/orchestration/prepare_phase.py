from __future__ import annotations

import json
import logging
from typing import Any

from projects.cluster.toolbox.cluster_deploy_operator import main as cluster_deploy_operator
from projects.cluster.toolbox.wait_for_crds import main as wait_for_crds_command
from projects.core.dsl.utils.k8s import oc, oc_get_json
from projects.core.library import env
from projects.core.orchestration.utils.k8s import ensure_namespace
from projects.dynamo.orchestration import runtime_config
from projects.dynamo.orchestration.cleanup_phase import run as cleanup_toolbox_run
from projects.dynamo.toolbox.capture_dynamo_state.main import (
    run as capture_dynamo_state_toolbox_run,
)
from projects.dynamo.toolbox.deploy_dynamo_platform.main import (
    run as deploy_dynamo_platform_toolbox_run,
)
from projects.gpu_operator.toolbox.bootstrap_gpu_clusterpolicy import (
    main as bootstrap_gpu_clusterpolicy,
)
from projects.gpu_operator.toolbox.bootstrap_nfd_instance import main as bootstrap_nfd_instance
from projects.kserve.toolbox.prepare_hf_model_cache.main import (
    run as prepare_hf_model_cache_toolbox_run,
)

logger = logging.getLogger(__name__)


def operator_spec_by_package(platform: dict[str, Any], package: str) -> dict[str, Any]:
    operators = platform["operators"]
    if isinstance(operators, dict):
        if package in operators:
            return {"package": package, **operators[package]}
        raise KeyError(f"Unknown operator package in dynamo platform config: {package}")

    for operator_spec in operators:
        if operator_spec["package"] == package:
            return operator_spec
    raise KeyError(f"Unknown operator package in dynamo platform config: {package}")


def verify_oc_access() -> None:
    oc("whoami")


def verify_cluster_version() -> None:
    platform = runtime_config.get_platform_config()
    version_info = oc("version", "-o", "json")
    payload = json.loads(version_info.stdout)

    openshift_version = (
        payload.get("openshiftVersion")
        or payload.get("releaseClientVersion")
        or payload.get("clientVersion", {}).get("gitVersion")
        or payload.get("serverVersion", {}).get("gitVersion")
        or payload.get("serverVersion", {}).get("platform")
    )
    if not openshift_version:
        raise RuntimeError("Could not determine OpenShift version from `oc version -o json`")

    minimum = platform["cluster"]["minimum_openshift_version"]
    if runtime_config.version_tuple(openshift_version) < runtime_config.version_tuple(minimum):
        raise RuntimeError(
            f"Cluster version {openshift_version} is older than the dynamo minimum {minimum}"
        )


def ensure_operator_subscription(operator_spec: dict[str, str]) -> dict[str, object]:
    return cluster_deploy_operator.run(
        package_name=operator_spec["package"],
        target_namespace=operator_spec["namespace"],
        source_name=operator_spec["source"],
        channel=operator_spec["channel"],
        source_namespace=operator_spec.get("source_namespace", "openshift-marketplace"),
        display_name=operator_spec.get("display_name", operator_spec["package"]),
        artifact_dirname_suffix=f"_{operator_spec['package']}",
    )


def prepare_nfd() -> None:
    platform = runtime_config.get_platform_config()
    operator_spec = operator_spec_by_package(platform, "nfd")
    ensure_operator_subscription(operator_spec)
    wait_for_crds_command.run(
        crd_names=[operator_spec["bootstrap_crd"]],
        display_name="NFD bootstrap CRD",
    )
    bootstrap_nfd_instance.run()


def prepare_gpu_operator() -> None:
    platform = runtime_config.get_platform_config()
    operator_spec = operator_spec_by_package(platform, "gpu-operator-certified")
    ensure_operator_subscription(operator_spec)
    wait_for_crds_command.run(
        crd_names=[operator_spec["bootstrap_crd"]],
        display_name="GPU Operator bootstrap CRD",
    )
    bootstrap_gpu_clusterpolicy.run()


def deploy_dynamo_platform() -> None:
    """Deploy the Dynamo platform via Helm (operator + etcd + NATS)."""
    dynamo_config = runtime_config.get_dynamo_config()
    helm_config = dynamo_config["helm"]

    deploy_dynamo_platform_toolbox_run(
        chart_repo=helm_config.get("chart_repo"),
        chart_name=helm_config["chart_name"],
        chart_version=helm_config["chart_version"],
        chart_path=helm_config.get("chart_path"),
        release_name=helm_config["release_name"],
        namespace=helm_config["namespace"],
        values_override=helm_config.get("values_override", {}),
        wait_timeout_seconds=dynamo_config["operator"]["wait_timeout_seconds"],
    )


def wait_for_dynamo_crds() -> None:
    """Wait for Dynamo CRDs to be registered after Helm install."""
    dynamo_config = runtime_config.get_dynamo_config()
    wait_for_crds_command.run(
        crd_names=dynamo_config["required_crds"],
        display_name="Dynamo CRDs",
    )


def ensure_test_namespace() -> None:
    namespace = runtime_config.get_namespace()
    ensure_namespace(
        namespace,
        labels={
            "app.kubernetes.io/managed-by": "forge",
            "forge.openshift.io/project": "dynamo",
        },
    )


def cleanup_previous_run() -> None:
    namespace = runtime_config.get_namespace()
    cleanup_toolbox_run(namespace=namespace)


def prepare_model_cache() -> None:
    model_cache = runtime_config.get_model_cache_config()

    if not model_cache.get("enabled", False):
        logger.info("Model cache disabled")
        return

    model_uri = runtime_config.get_model_uri()

    if model_uri.startswith(("pvc://", "pvc+hf://")):
        logger.info("Skipping cache for PVC-based model: %s", model_uri)
        return

    namespace = runtime_config.get_namespace()
    model_slug = runtime_config.get_model_slug()

    common_args = {
        "namespace": namespace,
        "namespace_is_managed": runtime_config.get_namespace_is_managed(),
        "model_key": model_slug,
        "model_uri": model_uri,
        "pvc_size": model_cache["pvc"]["size"],
        "access_mode": model_cache["pvc"]["access_mode"],
        "storage_class_name": model_cache["pvc"].get("storage_class_name"),
        "pvc_name_prefix": model_cache["pvc"]["name_prefix"],
        "model_directory_name": model_cache["pvc"]["model_directory_name"],
    }

    if model_uri.startswith("hf://"):
        prepare_hf_model_cache_toolbox_run(
            **common_args,
            downloader_image=model_cache["hf"]["downloader_image"],
            hf_token_file_path=None,
        )
    else:
        raise ValueError(f"Unsupported model URI scheme: {model_uri}")


def verify_gpu_nodes() -> None:
    platform = runtime_config.get_platform_config()
    selector = platform["cluster"]["gpu_node_label_selector"]
    data = oc_get_json("nodes", selector=selector, ignore_not_found=True)
    items = data.get("items", []) if data else []
    if not items:
        raise RuntimeError(
            f"No GPU nodes found with selector {selector}. Dynamo requires GPUs."
        )


def capture_prepare_state() -> None:
    artifact_dir = env.ARTIFACT_DIR
    namespace = runtime_config.get_namespace()
    dynamo_config = runtime_config.get_dynamo_config()

    capture_dynamo_state_toolbox_run(
        artifact_dir=artifact_dir,
        namespace=namespace,
        dynamo_namespace=dynamo_config["helm"]["namespace"],
    )
