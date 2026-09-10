#!/usr/bin/env python3
"""
Remove the full MCP Gateway platform stack from an OpenShift cluster.
Reverses the install_platform steps in reverse order.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from projects.core.dsl import always, entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import best_effort_oc, oc, oc_resource_exists
from projects.mcp_gateway.toolbox.platform_helpers import (
    best_effort_cmd,
    find_step,
    has_step,
    wait_for_crd_deletion,
)

logger = logging.getLogger(__name__)


@entrypoint
def run(
    *,
    platform_config: dict[str, Any],
    scheduling_node_selector: dict[str, str] | None = None,
) -> int:
    """
    Remove the full MCP Gateway platform stack in reverse order.

    Args:
        platform_config: Platform configuration dict from infrastructure.yaml
        scheduling_node_selector: Labels to remove from worker nodes after cleanup
    """
    execute_tasks(locals())
    return 0


@task
def resolve_config(args, ctx):
    """Resolve configuration values from platform_config"""
    config = args.platform_config
    kustomize_base_raw = config.get("kustomize_base")
    ctx.kustomize_base = (
        Path(kustomize_base_raw).expanduser().resolve() if kustomize_base_raw else None
    )
    ctx.mcp_gateway_namespace = config.get("mcp_gateway_namespace", "mcp-system")
    ctx.gateway_namespace = config.get("gateway_namespace", "gateway-system")
    ctx.ctrl = config.get("mcp_gateway_controller", {})
    ctx.steps = config.get("steps", [])
    ctx.node_selector = args.scheduling_node_selector or {}

    return f"Cleanup config resolved: {len(ctx.steps)} steps"


@task
def uninstall_mcp_gateway_helm(args, ctx):
    """Uninstall MCP Gateway Helm releases"""
    if not has_step(ctx.steps, "mcp-gateway-instance"):
        return "No helm step, skipping"

    best_effort_cmd("helm", "uninstall", "mcp-gateway", "--namespace", ctx.mcp_gateway_namespace)
    best_effort_cmd(
        "helm", "uninstall", "mcp-gateway-ingress", "--namespace", ctx.gateway_namespace
    )
    return "Helm releases uninstalled"


@task
def delete_kuadrant_cr(args, ctx):
    """Delete the Kuadrant CR (Connectivity Link instance)"""
    step = find_step(ctx.steps, "connectivity-link-instance")
    if not step:
        return "No connectivity-link-instance step, skipping"

    ns = step.get("namespace", ctx.mcp_gateway_namespace)
    best_effort_oc(
        "delete", "kuadrant", "kuadrant", "-n", ns, "--ignore-not-found=true", "--timeout=120s"
    )
    return f"Kuadrant CR deleted from {ns}"


@task
def delete_mcp_gateway_controller(args, ctx):
    """Remove MCP Gateway CRDs (strip finalizers from remaining CRs first)

    Verifies each CRD is fully gone before proceeding to prevent
    race conditions where a Terminating CRD interferes with reinstall.
    """
    if not has_step(ctx.steps, "mcp-gateway-controller"):
        return "No mcp-gateway-controller step, skipping"

    crds = [
        "mcpgatewayextensions.mcp.kuadrant.io",
        "mcpserverregistrations.mcp.kuadrant.io",
        "mcpvirtualservers.mcp.kuadrant.io",
        "mcpgatewayextensions.mcp.kagenti.com",
        "mcpserverregistrations.mcp.kagenti.com",
    ]
    deleted = []
    for crd in crds:
        if not oc_resource_exists("crd", crd):
            continue

        _remove_finalizers_for_crd(crd)
        best_effort_oc("delete", "crd", crd, "--timeout=60s")

        if not wait_for_crd_deletion(crd, timeout=120):
            logger.warning(
                "CRD %s still terminating, force-removing finalizers from the CRD itself",
                crd,
            )
            oc(
                "patch",
                "crd",
                crd,
                "--type=merge",
                "-p",
                '{"metadata":{"finalizers":null}}',
                check=False,
            )
            if not wait_for_crd_deletion(crd, timeout=60):
                raise RuntimeError(
                    f"CRD {crd} could not be fully removed after stripping finalizers. "
                    "Manual intervention required."
                )

        deleted.append(crd)

    if deleted:
        return f"Deleted CRDs: {', '.join(deleted)}"
    return "No MCP Gateway CRDs found to delete"


@task
def delete_connectivity_link_operator(args, ctx):
    """Remove Connectivity Link operator kustomize resources"""
    step = find_step(ctx.steps, "connectivity-link-operator")
    if not step:
        return "No connectivity-link-operator step, skipping"

    _delete_kustomize_step(step, ctx)
    return "Connectivity Link operator removed"


@task
def delete_service_mesh_instance(args, ctx):
    """Remove Service Mesh instance kustomize resources"""
    step = find_step(ctx.steps, "service-mesh-instance")
    if not step:
        return "No service-mesh-instance step, skipping"

    _delete_kustomize_step(step, ctx)
    return "Service Mesh instance removed"


@task
def delete_service_mesh_operator(args, ctx):
    """Remove Service Mesh operator kustomize resources"""
    step = find_step(ctx.steps, "service-mesh-operator")
    if not step:
        return "No service-mesh-operator step, skipping"

    _delete_kustomize_step(step, ctx)
    return "Service Mesh operator removed"


@task
def delete_namespaces(args, ctx):
    """Remove platform namespaces and wait for them to fully terminate"""
    from projects.mcp_gateway.toolbox.platform_helpers import wait_for_namespace_termination

    namespaces = [
        ctx.gateway_namespace,
        ctx.mcp_gateway_namespace,
        "istio-system",
        "istio-cni",
        "kuadrant-system",
    ]
    to_delete = [ns for ns in namespaces if oc_resource_exists("namespace", ns)]

    if not to_delete:
        return "No platform namespaces to delete"

    for ns in to_delete:
        best_effort_oc("delete", "namespace", ns, "--wait=false")

    wait_for_namespace_termination(
        to_delete,
        timeout=300,
        force_remove_finalizers=True,
    )

    return f"Deleted namespaces: {', '.join(to_delete)}"


@always
@task
def unlabel_worker_nodes(args, ctx):
    """Remove scheduling node_selector labels from worker nodes."""
    selector = ctx.node_selector
    if not selector:
        return "No node_selector configured, skipping"

    label_sel = ",".join(f"{k}={v}" for k, v in selector.items())
    result = oc("get", "nodes", "-l", label_sel, "-o", "name", check=False, log_stdout=False)
    nodes = [line.rsplit("/", 1)[-1] for line in result.stdout.splitlines() if line.strip()]
    for node in nodes:
        oc("label", "node", node, *[f"{k}-" for k in selector], check=False)
        logger.info("Removed scheduling labels from node %s", node)
    return f"Removed scheduling labels from {len(nodes)} node(s)"


@always
@task
def capture_cleanup_state(args, ctx):
    """Capture remaining platform state after cleanup for diagnostics"""
    artifacts_dir = args.artifact_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    oc(
        "get",
        "csv",
        "-A",
        "-o",
        "wide",
        check=False,
        log_stdout=False,
        stdout_dest=artifacts_dir / "remaining-csvs.txt",
    )

    oc(
        "get",
        "namespaces",
        "-o",
        "wide",
        check=False,
        log_stdout=False,
        stdout_dest=artifacts_dir / "remaining-namespaces.txt",
    )

    return "Captured post-cleanup state"


# --- Helper functions ---


def _delete_kustomize_step(step: dict, ctx) -> None:
    kustomize_base = getattr(ctx, "kustomize_base", None)
    if not kustomize_base:
        logger.warning("No kustomize_base set, skipping kustomize cleanup for %s", step["name"])
        return

    kustomize_path = kustomize_base / step["path"]
    if not kustomize_path.exists():
        logger.warning("Kustomize directory not found, skipping: %s", kustomize_path)
        return

    best_effort_oc("delete", "-k", str(kustomize_path), "--ignore-not-found=true")


def _remove_finalizers_for_crd(crd_name: str) -> None:
    """Remove finalizers from all instances of a CRD so deletion doesn't hang."""
    resource_kind = crd_name.split(".")[0]
    result = oc(
        "get",
        resource_kind,
        "--all-namespaces",
        "-o",
        'jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{"\\n"}{end}',
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return

    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if "/" in line:
            ns, name = line.split("/", 1)
            oc(
                "patch",
                resource_kind,
                name,
                "-n",
                ns,
                "--type=merge",
                "-p",
                '{"metadata":{"finalizers":null}}',
                check=False,
            )
        else:
            oc(
                "patch",
                resource_kind,
                line,
                "--type=merge",
                "-p",
                '{"metadata":{"finalizers":null}}',
                check=False,
            )


if __name__ == "__main__":
    run.main()
