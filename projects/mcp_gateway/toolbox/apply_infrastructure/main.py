"""
Apply MCP Gateway infrastructure for mock servers.

Generates and applies HTTPRoute + DestinationRule + MCPServerRegistration
for 1..N mock servers, then waits for all registrations to become Ready.

This is MCP Gateway-specific logic — it creates CRDs that only exist in
the MCP Gateway product (MCPServerRegistration, etc).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from projects.agentic_tools.mcp.toolbox.deploy_mock_servers.main import MOCK_MCP_LABEL
from projects.core.dsl import always, entrypoint, execute_tasks, retry, shell, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger(__name__)

DEFAULT_API_GROUP = "mcp.kuadrant.io"


@entrypoint
def run(
    *,
    namespace: str,
    count: int,
    name_prefix: str = "mock-server",
    gateway_namespace: str = "gateway-system",
    gateway_name: str = "mcp-gateway",
    api_group: str = DEFAULT_API_GROUP,
) -> int:
    """Apply MCP Gateway infrastructure and wait for registrations."""
    execute_tasks(locals())
    return 0


@task
def setup_directories(args, ctx):
    """Create command source and artifact directories"""

    shell.mkdir("src")
    shell.mkdir("artifacts")

    return "Prepared src and artifacts directories"


@task
def apply_manifests(args, ctx):
    """Generate and apply HTTPRoute + DestinationRule + MCPServerRegistration."""
    logger.info(
        "Applying infrastructure for %d server(s) (api_group=%s)...",
        args.count,
        args.api_group,
    )

    crd_version = _detect_crd_storage_version(f"mcpserverregistrations.{args.api_group}")

    all_manifests = []
    for i in range(1, args.count + 1):
        server_name = f"{args.name_prefix}-{i}"
        hostname = f"server{i}.mcp.local"
        tool_prefix = f"server{i}_"

        manifest = _generate_infrastructure_manifest(
            server_name=server_name,
            hostname=hostname,
            tool_prefix=tool_prefix,
            namespace=args.namespace,
            gateway_namespace=args.gateway_namespace,
            gateway_name=args.gateway_name,
            api_group=args.api_group,
            crd_version=crd_version,
        )
        all_manifests.append(manifest)

    combined_yaml = "---\n".join(all_manifests)

    src_dir = args.artifact_dir / "src"

    infra_manifest = src_dir / "infrastructure.yaml"
    infra_manifest.write_text(combined_yaml, encoding="utf-8")

    oc("apply", "-f", infra_manifest, log_stdout=False)

    return f"Infrastructure applied for {args.count} server(s)"


@retry(attempts=60, delay=15, backoff=1.0)
@task
def wait_for_registrations(args, ctx):
    """Wait until all N MCPServerRegistrations report Ready."""
    ready_count = _count_ready_registrations(namespace=args.namespace, api_group=args.api_group)

    if ready_count >= args.count:
        return f"All {args.count} registrations are Ready"

    return False, f"registrations ready: {ready_count}/{args.count}"


@always
@task
def capture_artifacts(args, ctx):
    """Capture infrastructure resource state for post-mortem diagnostics."""
    artifacts_dir = args.artifact_dir / "artifacts"

    _capture_to_file(
        artifacts_dir / "mcpserverregistrations.yaml",
        "get",
        f"mcpserverregistrations.{args.api_group}",
        "-n",
        args.namespace,
        "-l",
        MOCK_MCP_LABEL,
        "-o",
        "yaml",
    )
    _capture_to_file(
        artifacts_dir / "httproutes.yaml",
        "get",
        "httproute",
        "-n",
        args.namespace,
        "-l",
        MOCK_MCP_LABEL,
        "-o",
        "yaml",
    )
    _capture_to_file(
        artifacts_dir / "destinationrules.yaml",
        "get",
        "destinationrule",
        "-n",
        "istio-system",
        "-l",
        MOCK_MCP_LABEL,
        "-o",
        "yaml",
    )
    _capture_to_file(
        artifacts_dir / "events.txt",
        "get",
        "events",
        "-n",
        args.namespace,
        "--sort-by=.lastTimestamp",
    )

    return "Captured infrastructure state"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_crd_storage_version(crd_name: str, default: str = "v1alpha1") -> str:
    """Return the storage-served apiVersion of an installed CRD, e.g. "v1".

    Reads it directly off the CRD object on the cluster rather than assuming
    a fixed version, since the mcp-gateway chart has changed the served
    version for these CRDs across releases.
    """
    result = oc(
        "get",
        "crd",
        crd_name,
        "--ignore-not-found",
        "-o",
        "jsonpath={.spec.versions[?(@.storage==true)].name}",
        check=False,
        log_stdout=False,
    )
    version = (result.stdout or "").strip()
    if not version:
        logger.warning(
            "Could not detect storage version for CRD %s, defaulting to %s", crd_name, default
        )
        return default
    return version


def _count_ready_registrations(*, namespace: str, api_group: str = DEFAULT_API_GROUP) -> int:
    """Count MCPServerRegistrations with Ready=True condition."""
    result = oc(
        "get",
        f"mcpserverregistrations.{api_group}",
        "-n",
        namespace,
        "-l",
        MOCK_MCP_LABEL,
        "-o",
        'jsonpath={range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\\n"}{end}',
        check=False,
        log_stdout=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return 0
    return sum(1 for line in result.stdout.strip().split("\n") if line.strip() == "True")


def _generate_infrastructure_manifest(
    *,
    server_name: str,
    hostname: str,
    tool_prefix: str,
    namespace: str,
    gateway_namespace: str,
    gateway_name: str,
    api_group: str = DEFAULT_API_GROUP,
    crd_version: str = "v1alpha1",
) -> str:
    """Generate YAML manifest for HTTPRoute + DestinationRule + MCPServerRegistration."""
    labels = {
        "forge.openshift.io/component": "mock-mcp",
        "forge.openshift.io/project": "mcp_gateway",
    }

    httproute = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {
            "name": f"{server_name}-route",
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "parentRefs": [
                {
                    "name": gateway_name,
                    "namespace": gateway_namespace,
                }
            ],
            "hostnames": [hostname],
            "rules": [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": "/mcp"}}],
                    "backendRefs": [{"name": server_name, "port": 8080}],
                }
            ],
        },
    }

    destination_rule = {
        "apiVersion": "networking.istio.io/v1",
        "kind": "DestinationRule",
        "metadata": {
            "name": f"{server_name}-mtls-disable",
            "namespace": "istio-system",
            "labels": labels,
        },
        "spec": {
            "host": f"{server_name}.{namespace}.svc.cluster.local",
            "trafficPolicy": {
                "tls": {"mode": "DISABLE"},
            },
        },
    }

    prefix_field = "prefix" if api_group == "mcp.kuadrant.io" else "toolPrefix"

    mcp_registration = {
        "apiVersion": f"{api_group}/{crd_version}",
        "kind": "MCPServerRegistration",
        "metadata": {
            "name": server_name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            prefix_field: tool_prefix,
            "targetRef": {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "name": f"{server_name}-route",
            },
        },
    }

    return yaml.safe_dump_all(
        [httproute, destination_rule, mcp_registration],
        sort_keys=False,
    )


def _capture_to_file(path: Path, *oc_args: str) -> None:
    """Best-effort capture of oc output to a file."""
    oc(*oc_args, check=False, log_stdout=False, stdout_dest=path)
