from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from projects.core.dsl import entrypoint, execute_tasks, shell, task
from projects.core.dsl.utils.k8s import oc

logger = logging.getLogger(__name__)


@entrypoint
def run(
    *,
    chart_repo: str | None = None,
    chart_name: str = "dynamo-platform",
    chart_version: str = "1.2.1",
    chart_path: str | None = None,
    release_name: str = "dynamo",
    namespace: str = "dynamo-system",
    values_override: dict[str, Any] | None = None,
    wait_timeout_seconds: int = 300,
    artifact_dir: Path | None = None,
) -> int:
    execute_tasks(locals())
    return 0


@task
def ensure_helm_repo(args, ctx):
    """Add the Dynamo Helm repository if using a remote chart."""
    if args.chart_path:
        logger.info("Using local chart path: %s, skipping repo add", args.chart_path)
        ctx.chart_ref = args.chart_path
        return

    if not args.chart_repo:
        raise ValueError("Either chart_repo or chart_path must be provided")

    shell.run(
        f"helm repo add dynamo {args.chart_repo} --force-update",
        check=True,
    )
    shell.run("helm repo update dynamo", check=True)
    ctx.chart_ref = f"dynamo/{args.chart_name}"


@task
def create_namespace(args, ctx):
    """Ensure the Dynamo platform namespace exists."""
    oc("create", "namespace", args.namespace, check=False)
    logger.info("Namespace %s ensured", args.namespace)


@task
def install_or_upgrade_chart(args, ctx):
    """Helm install/upgrade the Dynamo platform chart."""
    cmd_parts = [
        "helm", "upgrade", "--install",
        args.release_name,
        ctx.chart_ref,
        f"--namespace={args.namespace}",
        f"--timeout={args.wait_timeout_seconds}s",
        "--wait",
        f"--version={args.chart_version}",
    ]

    if args.values_override:
        import tempfile
        values_file = Path(tempfile.mkdtemp()) / "values-override.yaml"
        import yaml
        with values_file.open("w") as fh:
            yaml.dump(args.values_override, fh)
        cmd_parts.extend(["-f", str(values_file)])

    cmd = " ".join(cmd_parts)
    logger.info("Running: %s", cmd)

    result = shell.run(cmd, check=True)

    if args.artifact_dir:
        output_file = args.artifact_dir / "artifacts" / "helm-install-output.txt"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(result.stdout or "", encoding="utf-8")

    return f"Helm chart {ctx.chart_ref} installed as {args.release_name}"


@task
def wait_for_operator(args, ctx):
    """Wait for the Dynamo operator deployment to be available."""
    shell.run(
        f"oc rollout status deployment/dynamo-operator "
        f"-n {args.namespace} --timeout={args.wait_timeout_seconds}s",
        check=True,
    )
    logger.info("Dynamo operator is ready in namespace %s", args.namespace)
