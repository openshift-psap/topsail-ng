"""
Utilities for the GuideLL-M benchmark toolbox module.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from projects.core.dsl import template

logger = logging.getLogger(__name__)

_CONFIG_FILE_PATH = "/tmp/guidellm-config.yaml"


@dataclass(frozen=True)
class GuideLLMRun:
    rate: str | None
    label: str
    args: list[str]


def _sanitize_rate_label(rate: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", rate).strip("._-")
    return sanitized or "rate"


def _format_expression_value(value: float | int) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _evaluate_rate_expression(expression: str, rate: str) -> str:
    rate_value = float(rate)
    normalized = expression.strip()

    if normalized == "rate":
        return _format_expression_value(rate_value)

    left_multiply = re.fullmatch(r"(\d+)\s*\*\s*rate", normalized)
    if left_multiply:
        return _format_expression_value(int(left_multiply.group(1)) * rate_value)

    right_multiply = re.fullmatch(r"rate\s*\*\s*(\d+)", normalized)
    if right_multiply:
        return _format_expression_value(rate_value * int(right_multiply.group(1)))

    raise ValueError(
        f"Unsupported rate expression: {expression}. "
        "Supported forms are 'rate', 'N*rate', and 'rate*N'."
    )


def _substitute_rate_expressions(value: str, rate: str) -> str:
    return re.sub(
        r"\{([^{}]+)\}",
        lambda match: _evaluate_rate_expression(match.group(1), rate),
        value,
    )


def _has_rate_expressions(guidellm_args: list[str]) -> bool:
    return any(re.search(r"\{[^{}]*\brate\b[^{}]*\}", arg) for arg in guidellm_args)


def expand_guidellm_runs(guidellm_args: list[str]) -> list[GuideLLMRun]:
    has_rate_expressions = _has_rate_expressions(guidellm_args)
    rate_arg = next((arg for arg in guidellm_args if arg.startswith("--rate=")), None)
    if not rate_arg:
        if has_rate_expressions:
            raise ValueError(
                "Rate-based expressions require a '--rate=' argument. "
                "Found '{rate}'-style placeholders without any rate values."
            )
        return [GuideLLMRun(rate=None, label="default", args=list(guidellm_args))]
    if not has_rate_expressions:
        return [GuideLLMRun(rate=None, label="default", args=list(guidellm_args))]

    rate_values = [value.strip() for value in rate_arg.split("=", 1)[1].split(",") if value.strip()]
    if not rate_values:
        raise ValueError(
            "Argument '--rate' must include at least one non-empty value "
            "when rate-based expressions are used."
        )

    runs: list[GuideLLMRun] = []
    for rate in rate_values:
        run_args: list[str] = []
        for arg in guidellm_args:
            if arg.startswith("--rate="):
                run_args.append(f"--rate={rate}")
                continue
            run_args.append(_substitute_rate_expressions(arg, rate))

        runs.append(
            GuideLLMRun(
                rate=rate,
                label=f"rate-{_sanitize_rate_label(rate)}",
                args=run_args,
            )
        )

    return runs


def build_guidellm_args(benchmark: dict[str, object]) -> list[str]:
    guidellm_args: list[str] = []
    benchmark_args = benchmark.get("args", {})
    if benchmark_args:
        for key, value in benchmark_args.items():
            cli_key = key.replace("_", "-")
            if isinstance(value, list):
                rendered_value = ",".join(str(item) for item in value)
            else:
                rendered_value = str(value)
            guidellm_args.append(f"--{cli_key}={rendered_value}")

    if "rate" in benchmark and "rate" not in benchmark_args:
        guidellm_args.append(f"--rate={benchmark['rate']}")

    if not any(arg.startswith("--outputs=") for arg in guidellm_args):
        guidellm_args.append(f"--outputs={benchmark.get('outputs', 'json')}")

    return guidellm_args


def _build_config_heredoc(config_content: str) -> str:
    """Build a shell heredoc that writes a GuideLLM config YAML to a file."""
    return f"cat > {_CONFIG_FILE_PATH} <<'__CONFIG_EOF__'\n{config_content}__CONFIG_EOF__"


def _build_multi_run_script(
    *,
    endpoint_url: str,
    runs: list[GuideLLMRun],
    config_content: str | None = None,
) -> str:
    lines = ["set -euo pipefail", "mkdir -p /results"]
    if config_content:
        lines.append(_build_config_heredoc(config_content))
    for run in runs:
        lines.append("rm -f /results/benchmarks.json")
        run_args = list(run.args)
        if config_content:
            run_args.append(f"--config={_CONFIG_FILE_PATH}")
        command = [
            "/opt/app-root/bin/guidellm",
            "benchmark",
            "run",
            f"--target={endpoint_url}",
            *run_args,
        ]
        lines.append(shlex.join(command))
        output_path = shlex.quote(f"/results/benchmarks-{run.label}.json")
        lines.append(
            f"test -f /results/benchmarks.json && mv /results/benchmarks.json {output_path}"
        )

    return "\n".join(lines)


def render_guidellm_pvc_from_parts(
    *,
    namespace: str,
    name: str,
    pvc_size: str,
    pvc_storage_class: str | None = None,
    owner_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render a GuideLL-M PVC manifest from individual components.

    Args:
        namespace: Target namespace
        name: Name of the benchmark job and PVC
        pvc_size: Size of the PVC
        pvc_storage_class: Optional storage class name for the PVC
        owner_reference: Optional owner reference to set (e.g., for job ownership)

    Returns:
        PVC manifest as dict
    """
    rendered_yaml = template.render_template(
        "guidellm_pvc.yaml.j2",
        {
            "namespace": namespace,
            "name": name,
            "pvc_size": pvc_size,
            "pvc_storage_class": pvc_storage_class,
        },
    )
    manifest = yaml.safe_load(rendered_yaml)

    # Add owner reference if provided
    if owner_reference:
        manifest["metadata"]["ownerReferences"] = [owner_reference]

    return manifest


def render_guidellm_job_from_parts(
    *,
    namespace: str,
    name: str,
    image: str,
    endpoint_url: str,
    guidellm_args: list[str],
    timeout_seconds: int,
    hf_token_secret: str = "",
    fs_group: int | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Render a GuideLL-M job manifest from individual components.

    Args:
        namespace: Target namespace
        name: Name of the benchmark job
        image: Container image for GuideLLM
        endpoint_url: Gateway endpoint URL
        guidellm_args: Additional arguments for GuideLLM
        timeout_seconds: Active deadline for the Kubernetes Job
        hf_token_secret: Name of the K8s secret containing HF_TOKEN. If empty, HF_TOKEN is not injected.
        fs_group: If set, adds a pod-level securityContext.fsGroup to ensure
            the PVC is writable by the container. Needed on clusters where the
            CSI driver provisions volumes with root-only permissions.
        config_path: Path to a GuideLLM config YAML file on the local filesystem.
            When set, the file is read and embedded in the container via heredoc,
            and GuideLLM is invoked with ``--config`` pointing to it.

    Returns:
        Job manifest as dict
    """
    config_content = config_path.read_text() if config_path else None
    runs = expand_guidellm_runs(guidellm_args)
    rendered_yaml = template.render_template(
        "guidellm_job.yaml.j2",
        {
            "namespace": namespace,
            "name": name,
            "image": image,
            "hf_token_secret": hf_token_secret,
            "fs_group": fs_group,
        },
    )
    manifest = yaml.safe_load(rendered_yaml)
    manifest["spec"]["activeDeadlineSeconds"] = timeout_seconds
    container = manifest["spec"]["template"]["spec"]["containers"][0]

    # Config file mode requires a shell script to write the file first.
    # Plain single runs can invoke guidellm directly.
    if not config_content and len(runs) == 1 and runs[0].rate is None:
        container["command"] = ["/opt/app-root/bin/guidellm"]
        container["args"] = [
            "benchmark",
            "run",
            f"--target={endpoint_url}",
            *runs[0].args,
        ]
        return manifest

    container["command"] = ["/bin/sh", "-lc"]
    container["args"] = [
        _build_multi_run_script(endpoint_url=endpoint_url, runs=runs, config_content=config_content)
    ]
    return manifest


def render_guidellm_shared_volume_job_from_parts(
    *,
    namespace: str,
    name: str,
    image: str,
    endpoint_url: str,
    guidellm_args: list[str],
    timeout_seconds: int,
    hf_token_secret: str = "",
    fs_group: int | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Render a GuideLL-M job manifest with shared volume (main + sidecar containers).

    Args:
        namespace: Target namespace
        name: Name of the benchmark job
        image: Container image for GuideLLM
        endpoint_url: Gateway endpoint URL
        guidellm_args: Additional arguments for GuideLLM
        timeout_seconds: Active deadline for the Kubernetes Job
        hf_token_secret: Name of the K8s secret containing HF_TOKEN. If empty, HF_TOKEN is not injected.
        fs_group: If set, adds a pod-level securityContext.fsGroup to ensure
            the shared volume is writable by both containers.
        config_path: Path to a GuideLLM config YAML file on the local filesystem.
            When set, the file is read and embedded in the container via heredoc.

    Returns:
        Job manifest as dict with main and sidecar containers
    """
    config_content = config_path.read_text() if config_path else None
    runs = expand_guidellm_runs(guidellm_args)
    rendered_yaml = template.render_template(
        "guidellm_shared_volume_job.yaml.j2",
        {
            "namespace": namespace,
            "name": name,
            "image": image,
            "hf_token_secret": hf_token_secret,
            "fs_group": fs_group,
        },
    )
    manifest = yaml.safe_load(rendered_yaml)
    manifest["spec"]["activeDeadlineSeconds"] = timeout_seconds

    # Build the main container script.
    # Config file mode always uses a shell script to write the file first.
    if not config_content and len(runs) == 1 and runs[0].rate is None:
        main_script_lines = [
            "set -euo pipefail",
            "mkdir -p /results",
            f"/opt/app-root/bin/guidellm benchmark run --target={endpoint_url} {' '.join(runs[0].args)}",
        ]
        main_script = "\n".join(main_script_lines)
    else:
        main_script = _build_multi_run_script(
            endpoint_url=endpoint_url, runs=runs, config_content=config_content
        )
    manifest["spec"]["template"]["spec"]["containers"][0]["command"] = ["/bin/sh", "-c"]
    manifest["spec"]["template"]["spec"]["containers"][0]["args"] = [main_script]

    return manifest


def render_guidellm_copy_pod_from_parts(
    *,
    namespace: str,
    name: str,
    pvc_size: str,
    node_name: str | None = None,
) -> dict[str, Any]:
    """Render a GuideLL-M copy pod manifest from individual components.

    Args:
        namespace: Target namespace
        name: Name of the benchmark job (used for copy pod naming)
        pvc_size: Size of the PVC (not used directly, but kept for interface consistency)
        node_name: Optional node name to pin the pod to

    Returns:
        Pod manifest as dict
    """
    rendered_yaml = template.render_template(
        "guidellm_copy_pod.yaml.j2",
        {
            "namespace": namespace,
            "name": name,
            "node_name": node_name,
        },
    )
    return yaml.safe_load(rendered_yaml)
