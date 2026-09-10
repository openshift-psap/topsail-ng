"""
Shared helpers for MCP Gateway platform install/cleanup toolbox modules.

Avoids duplicating step-lookup and namespace-wait logic across
install_platform and cleanup_platform.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from projects.core.dsl.utils.k8s import oc, oc_resource_exists

logger = logging.getLogger(__name__)

_PLATFORM_REPO_DEFAULT = "https://github.com/Kuadrant/mcp-gateway.git"
_PLATFORM_SUBDIR_DEFAULT = "config/openshift"
_PLATFORM_CLONE_DIR = Path(os.environ.get("FORGE_BASE_DIR", "/tmp")) / "mcp-gw-platform-manifests"

# Upstream mcp-gateway still pins EOL Istio (v1.26-latest). Current
# servicemeshoperator3 / Sail rejects that for new installs.
_DEFAULT_ISTIO_VERSION = "v1.30-latest"
_ISTIO_VERSION_RE = re.compile(r"^(\s*version:\s*)\S+\s*$", re.MULTILINE)


_RESOLVED_REF_MARKER = ".forge-resolved-ref"


def clone_platform_repo(
    *,
    version: str,
    repo_url: str = _PLATFORM_REPO_DEFAULT,
    subdir: str = _PLATFORM_SUBDIR_DEFAULT,
) -> Path:
    """Fetch the platform manifests from the upstream mcp-gateway repo at ``version``.

    ``version`` may be a branch, a tag, or a commit SHA. Each is fetched
    directly by name/SHA (``git fetch --depth 1 origin <version>``), which
    upstream Git hosts support for any ref or reachable commit. If that
    fetch fails (e.g. the ref/commit doesn't exist), this falls back to the
    repo's ``main`` branch so installs can still proceed with a warning.

    Only the ``subdir`` subtree is checked out (sparse checkout) to keep
    this fast.

    The checkout is placed under ``$FORGE_BASE_DIR/mcp-gw-platform-manifests/``
    (defaults to ``/tmp/mcp-gw-platform-manifests/``) so that subsequent
    phases (e.g. cleanup) can reuse it without re-fetching. Call
    :func:`cleanup_platform_clone` at the end of the last phase to remove it.

    Returns the absolute path to the checked-out subdirectory
    (e.g. ``/tmp/mcp-gw-platform-manifests/mcp-gateway/config/openshift``).
    """
    repo_dir = _PLATFORM_CLONE_DIR / "mcp-gateway"
    result_path = repo_dir / subdir

    if result_path.is_dir():
        cached_ref = _get_cached_ref(repo_dir)
        if cached_ref == version:
            logger.info("Platform manifests already fetched at %s, reusing", result_path)
            return result_path
        logger.info(
            "Cached clone ref (%s) differs from requested (%s), re-fetching",
            cached_ref,
            version,
        )
        shutil.rmtree(str(_PLATFORM_CLONE_DIR), ignore_errors=True)

    _PLATFORM_CLONE_DIR.mkdir(parents=True, exist_ok=True)
    repo_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "add", "origin", repo_url],
        check=True,
        timeout=30,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "sparse-checkout", "set", subdir],
        check=True,
        timeout=30,
    )

    fetch_cmd = [
        "git",
        "-C",
        str(repo_dir),
        "fetch",
        "--depth",
        "1",
        "--filter=blob:none",
        "origin",
    ]
    fetched_ref = version
    fetch_result = subprocess.run(
        [*fetch_cmd, version],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if fetch_result.returncode != 0:
        logger.warning(
            "Could not fetch '%s' from %s (%s) — falling back to 'main'",
            version,
            repo_url,
            fetch_result.stderr.strip().splitlines()[-1]
            if fetch_result.stderr
            else "unknown error",
        )
        fetched_ref = "main"
        subprocess.run([*fetch_cmd, "main"], check=True, timeout=120)

    logger.info(
        "Fetched platform manifests from %s (ref=%s, subdir=%s)",
        repo_url,
        fetched_ref,
        subdir,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "checkout", "-q", "FETCH_HEAD"],
        check=True,
        timeout=30,
    )

    if not result_path.is_dir():
        raise FileNotFoundError(f"Expected directory {result_path} not found after checkout")

    (repo_dir / _RESOLVED_REF_MARKER).write_text(version)
    logger.info("Platform manifests available at %s", result_path)
    return result_path


def get_platform_clone_path(subdir: str = _PLATFORM_SUBDIR_DEFAULT) -> Path | None:
    """Return the path to a previously cloned platform checkout, or None."""
    candidate = _PLATFORM_CLONE_DIR / "mcp-gateway" / subdir
    return candidate if candidate.is_dir() else None


def cleanup_platform_clone() -> None:
    """Remove the cloned platform manifests directory."""
    if _PLATFORM_CLONE_DIR.exists():
        shutil.rmtree(str(_PLATFORM_CLONE_DIR), ignore_errors=True)
        logger.info("Cleaned up platform clone at %s", _PLATFORM_CLONE_DIR)


def _get_cached_ref(repo_dir: Path) -> str | None:
    """Return the version string a cached clone was fetched for, or None if unknown."""
    marker = repo_dir / _RESOLVED_REF_MARKER
    try:
        return marker.read_text().strip() or None
    except OSError:
        return None


def detect_mcp_gateway_extension_crd_spec(
    chart_ref: str, version_flag: list[str] | None = None
) -> dict[str, Any]:
    """Inspect the MCPGatewayExtension CRD shipped by the mcp-gateway chart
    that is being installed and return its group, storage apiVersion, and
    supported spec fields.

    Reads the CRD straight from the chart definition via ``helm show
    crds`` (works for both an OCI chart ref + ``--version`` and a local
    chart path), so the generated MCPGatewayExtension custom resource
    always matches exactly the chart version this run is deploying,
    independent of anything already present on the target cluster.

    Returns a dict with keys ``api_group``, ``api_version``, and
    ``has_private_host``.
    """
    result = subprocess.run(
        ["helm", "show", "crds", chart_ref, *(version_flag or [])],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )

    for doc in yaml.safe_load_all(result.stdout):
        if not doc or doc.get("kind") != "CustomResourceDefinition":
            continue
        if "mcpgatewayextension" not in doc["metadata"]["name"]:
            continue

        spec = doc["spec"]
        versions = spec["versions"]
        storage_version = next((v for v in versions if v.get("storage")), versions[-1])
        schema_props = (
            storage_version.get("schema", {})
            .get("openAPIV3Schema", {})
            .get("properties", {})
            .get("spec", {})
            .get("properties", {})
        )
        return {
            "api_group": spec["group"],
            "api_version": storage_version["name"],
            "has_private_host": "privateHost" in schema_props,
        }

    raise RuntimeError(f"No MCPGatewayExtension CRD found in chart {chart_ref}")


def prune_stale_mcp_gateway_extension_crds(expected_group: str) -> list[str]:
    """Remove any MCPGatewayExtension-family CRD (and its CR instances)
    whose API group doesn't match ``expected_group``.

    Different mcp-gateway chart versions have shipped this CRD under
    different API groups (e.g. ``mcp.kagenti.com`` before ``mcp.kuadrant.io``).
    Running this after determining the current chart's group ensures a
    cluster previously used with a different chart version doesn't keep
    stale CRDs/CRs around alongside the ones the current chart defines.

    Returns the list of CRD names that were removed.
    """
    result = oc("get", "crd", "-o", "name", check=False, log_stdout=False)
    stale = [
        line.split("/", 1)[-1]
        for line in result.stdout.splitlines()
        if "mcpgatewayextension" in line and not line.endswith(f".{expected_group}")
    ]

    for crd_name in stale:
        _remove_finalizers_for_crd(crd_name)
        oc("delete", "crd", crd_name, "--ignore-not-found=true", "--timeout=60s", check=False)
        if wait_for_crd_deletion(crd_name, timeout=60):
            logger.info(
                "Removed stale CRD %s (doesn't match current chart group %s)",
                crd_name,
                expected_group,
            )
        else:
            oc(
                "patch",
                "crd",
                crd_name,
                "--type=merge",
                "-p",
                '{"metadata":{"finalizers":null}}',
                check=False,
            )
            wait_for_crd_deletion(crd_name, timeout=30)

    return stale


def _remove_finalizers_for_crd(crd_name: str) -> None:
    """Remove finalizers from all instances of a CRD so its deletion doesn't hang."""
    resource_kind = crd_name.split(".", 1)[0]
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

    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        ns, _, name = line.partition("/")
        patch_args = [
            "patch",
            resource_kind,
            name or ns,
            "--type=merge",
            "-p",
            '{"metadata":{"finalizers":null}}',
        ]
        if name:
            patch_args += ["-n", ns]
        oc(*patch_args, check=False)


def find_step(steps: list[dict], name: str) -> dict | None:
    """Find a step by name in the platform steps list."""
    for step in steps:
        if step["name"] == name:
            return step
    return None


def has_step(steps: list[dict], name: str) -> bool:
    """Check whether a named step exists."""
    return find_step(steps, name) is not None


def wait_for_namespace_termination(
    namespaces: list[str],
    timeout: int = 300,
    force_remove_finalizers: bool = False,
) -> None:
    """Wait until the given namespaces are fully gone.

    Args:
        namespaces: Namespace names to wait for.
        timeout: Maximum seconds to wait.
        force_remove_finalizers: After 60s, strip finalizers from stuck namespaces.
    """
    still_present = [
        ns for ns in namespaces if oc_resource_exists("namespace", ns) and _is_terminating(ns)
    ]
    if not still_present:
        return

    logger.info("Waiting for terminating namespaces: %s", still_present)
    deadline = time.time() + timeout
    start = time.time()

    while still_present and time.time() < deadline:
        time.sleep(10)

        if force_remove_finalizers and (time.time() - start) > 60:
            for ns in still_present:
                _force_remove_namespace_finalizers(ns)

        still_present = [ns for ns in still_present if oc_resource_exists("namespace", ns)]
        if still_present:
            logger.info("Still waiting for: %s", still_present)

    if still_present:
        raise RuntimeError(
            f"Namespaces still terminating after {timeout}s: {still_present}. "
            "Manual intervention may be required."
        )


def wait_for_crd_deletion(
    crd_name: str,
    timeout: int = 120,
) -> bool:
    """Wait until a CRD is fully removed from the cluster.

    Returns True if the CRD was confirmed gone, False if timed out.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not oc_resource_exists("crd", crd_name):
            return True
        time.sleep(5)

    logger.warning("CRD %s still present after %ds", crd_name, timeout)
    return False


def best_effort_cmd(*cmd_args: str) -> None:
    """Run an arbitrary command, swallowing timeout and other errors."""
    try:
        subprocess.run(list(cmd_args), check=False, timeout=120)
    except subprocess.TimeoutExpired:
        logger.warning("Timed out: %s", " ".join(cmd_args))
    except Exception as exc:
        logger.warning("Error: %s: %s", " ".join(cmd_args), exc)


def _is_terminating(namespace: str) -> bool:
    result = oc(
        "get",
        "namespace",
        namespace,
        "-o",
        "jsonpath={.status.phase}",
        check=False,
    )
    return result.returncode == 0 and "Terminating" in result.stdout


def _force_remove_namespace_finalizers(namespace: str) -> None:
    """Strip finalizers from a stuck namespace so it can terminate."""
    import json as json_mod

    result = oc("get", "namespace", namespace, "-o", "json", check=False)
    if result.returncode != 0 or not result.stdout:
        return

    try:
        ns_obj = json_mod.loads(result.stdout)
        if not ns_obj.get("spec", {}).get("finalizers"):
            return
        ns_obj["spec"]["finalizers"] = []
        payload = json_mod.dumps(ns_obj)

        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(payload)
            tmp_path = f.name

        oc(
            "replace",
            "--raw",
            f"/api/v1/namespaces/{namespace}/finalize",
            "-f",
            tmp_path,
            check=False,
        )
        logger.info("Removed finalizers from namespace %s", namespace)
    except Exception as exc:
        logger.warning("Failed to remove finalizers from %s: %s", namespace, exc)


def patch_service_mesh_istio_version(
    kustomize_path: Path,
    version: str = _DEFAULT_ISTIO_VERSION,
) -> list[str]:
    """Rewrite ``spec.version`` in Istio / IstioCNI manifests under *kustomize_path*.

    Upstream mcp-gateway pins an EOL Istio tag that current Sail / OSSM 3
    rejects. Forge patches the cloned manifests before ``oc apply -k``.

    Returns the list of files that were modified.
    """
    if not version:
        return []

    patched: list[str] = []
    for path in sorted(kustomize_path.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        if "sailoperator.io" not in text and "kind: Istio" not in text:
            continue
        if "version:" not in text:
            continue

        new_text, n = _ISTIO_VERSION_RE.subn(rf"\g<1>{version}", text)
        if n == 0 or new_text == text:
            continue

        path.write_text(new_text, encoding="utf-8")
        patched.append(str(path))
        logger.info("Patched Istio version -> %s in %s", version, path)

    if not patched:
        logger.warning(
            "No Istio/IstioCNI version fields patched under %s (wanted %s)",
            kustomize_path,
            version,
        )
    return patched
