"""
Run an aiperf benchmark as a K8s Job against a Dynamo endpoint.

Deploys aiperf in a pod (pip install at runtime), writes results to a PVC,
polls for completion, extracts the JSON summary to the local artifact dir.
Follows the same Job+PVC pattern as the guidellm toolbox.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from projects.core.dsl import entrypoint, execute_tasks, retry, task
from projects.core.dsl.utils import write_json, write_text
from projects.core.dsl.utils.k8s import oc, oc_apply, oc_get_json

logger = logging.getLogger(__name__)

AIPERF_VERSION = "0.7.0"
AIPERF_IMAGE = "python:3.12-slim"


@entrypoint
def run(
    *,
    endpoint_url: str,
    model_name: str,
    name: str = "aiperf-benchmark",
    namespace: str = "",
    pvc_name: str = "forge-dynamo-results",
    pvc_size: str = "1Gi",
    timeout: int = 7200,
    artifact_dir: Path | None = None,
    # Dataset
    dataset_url: str = "https://raw.githubusercontent.com/kvcache-ai/Mooncake/refs/heads/main/FAST25-release/traces/conversation_trace.jsonl",
    dataset_type: str = "mooncake_trace",
    dataset_cap: int | None = 2000,
    # Endpoint
    endpoint_type: str = "chat",
    endpoint_path: str = "/v1/chat/completions",
    streaming: bool = True,
    # Schedule
    fixed_schedule: bool = True,
    fixed_schedule_auto_offset: bool = True,
    # Limits
    synthesis_max_isl: int | None = 131072,
    # aiperf options
    tokenizer: str | None = None,
) -> int:
    execute_tasks(locals())
    return 0


@task
def validate_parameters(args, ctx):
    """Validate and resolve namespace."""
    if not args.namespace:
        result = oc("project", "-q", check=False)
        if result.returncode == 0:
            ctx.namespace = result.stdout.strip()
        else:
            raise RuntimeError("Could not auto-detect namespace")
    else:
        ctx.namespace = args.namespace

    ctx.job_name = args.name
    ctx.pvc_name = args.pvc_name
    ctx.results_subpath = f"aiperf-{ctx.job_name}"
    return f"Namespace: {ctx.namespace}, Job: {ctx.job_name}"


@task
def cleanup_previous(args, ctx):
    """Delete previous aiperf job if exists."""
    _best_effort_delete("aiperf job", "delete", "job", ctx.job_name,
                        "-n", ctx.namespace, "--ignore-not-found=true")
    _best_effort_delete("aiperf copy pod", "delete", "pod", f"{ctx.job_name}-copy",
                        "-n", ctx.namespace, "--ignore-not-found=true")


@task
def ensure_results_pvc(args, ctx):
    """Ensure results PVC exists."""
    existing = oc("get", "pvc", ctx.pvc_name, "-n", ctx.namespace,
                  "--ignore-not-found", "-oname", check=False)
    if existing.stdout.strip():
        logger.info("Results PVC %s already exists", ctx.pvc_name)
        return

    oc_apply(
        args.artifact_dir / "src" / "aiperf-pvc.yaml",
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": ctx.pvc_name,
                "namespace": ctx.namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "forge",
                    "forge.openshift.io/project": "dynamo",
                },
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": args.pvc_size}},
            },
        },
    )
    logger.info("Created results PVC %s", ctx.pvc_name)


@task
def create_aiperf_job(args, ctx):
    """Render and apply the aiperf benchmark Job."""
    (args.artifact_dir / "src").mkdir(parents=True, exist_ok=True)

    script = _build_aiperf_script(args, ctx)
    tokenizer = args.tokenizer or args.model_name

    job_manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": ctx.job_name,
            "namespace": ctx.namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "forge",
                "forge.openshift.io/project": "dynamo",
            },
        },
        "spec": {
            "backoffLimit": 0,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "volumes": [
                        {"name": "results", "persistentVolumeClaim": {"claimName": ctx.pvc_name}},
                    ],
                    "containers": [{
                        "name": "aiperf",
                        "image": AIPERF_IMAGE,
                        "command": ["/bin/bash", "-c", script],
                        "volumeMounts": [
                            {"name": "results", "mountPath": "/results"},
                        ],
                        "env": [
                            {"name": "AIPERF_HTTP_SSL_VERIFY", "value": "false"},
                        ],
                        "resources": {
                            "requests": {"cpu": "2", "memory": "4Gi"},
                            "limits": {"cpu": "4", "memory": "8Gi"},
                        },
                    }],
                },
            },
        },
    }

    oc_apply(args.artifact_dir / "src" / "aiperf-job.yaml", job_manifest)
    logger.info("Created aiperf job %s", ctx.job_name)


def _build_aiperf_script(args, ctx) -> str:
    """Build the shell script that runs inside the Job pod."""
    tokenizer = args.tokenizer or args.model_name
    aiperf_args = [
        f"--model {args.model_name}",
        f"--url {args.endpoint_url}",
        f"--endpoint-type {args.endpoint_type}",
        f"--endpoint {args.endpoint_path}",
        "--input-file /tmp/dataset.jsonl",
        f"--custom-dataset-type {args.dataset_type}",
        f"--tokenizer {tokenizer}",
        f"--artifact-dir /results/{ctx.results_subpath}",
        "--ui none",
    ]
    if args.streaming:
        aiperf_args.append("--streaming")
    if args.fixed_schedule:
        aiperf_args.append("--fixed-schedule")
    if args.fixed_schedule_auto_offset:
        aiperf_args.append("--fixed-schedule-auto-offset")
    if args.synthesis_max_isl is not None:
        aiperf_args.append(f"--synthesis-max-isl {args.synthesis_max_isl}")

    cap_code = ""
    if args.dataset_cap:
        cap_code = (
            f"lines = open('/tmp/dataset_full.jsonl').readlines()[:{args.dataset_cap}]\n"
            "open('/tmp/dataset.jsonl', 'w').writelines(lines)\n"
            f"print(f'Capped to {{len(lines)}} entries')"
        )
        dl_target = "/tmp/dataset_full.jsonl"
    else:
        dl_target = "/tmp/dataset.jsonl"
        cap_code = ""

    return f"""set -e
export HOME=/tmp PIP_CACHE_DIR=/tmp/pip-cache
pip install -q --user aiperf=={AIPERF_VERSION} 2>&1 | tail -3
export PATH="/tmp/.local/bin:$PATH"
python3 -c "
import urllib.request
urllib.request.urlretrieve('{args.dataset_url}', '{dl_target}')
{cap_code}
print('Dataset ready')
"
mkdir -p /results/{ctx.results_subpath}
aiperf profile {' '.join(aiperf_args)}
echo "aiperf completed"
"""


@retry(attempts=360, delay=10, backoff=1.0)
@task
def wait_for_completion(args, ctx):
    """Poll until aiperf job completes."""
    active = oc("get", "job", ctx.job_name, "-n", ctx.namespace,
                "-o", "jsonpath={.status.active}", check=False)
    if active.returncode == 0 and active.stdout.strip() == "1":
        logger.info("Job %s still running...", ctx.job_name)
        return False

    succeeded = oc("get", "job", ctx.job_name, "-n", ctx.namespace,
                   "-o", "jsonpath={.status.succeeded}", check=False)
    failed = oc("get", "job", ctx.job_name, "-n", ctx.namespace,
                "-o", "jsonpath={.status.failed}", check=False)

    if succeeded.returncode == 0 and succeeded.stdout.strip() == "1":
        return f"aiperf job {ctx.job_name} completed"

    if failed.returncode == 0 and failed.stdout.strip() == "1":
        _capture_job_state(args.artifact_dir, ctx.namespace, ctx.job_name)
        raise RuntimeError(f"aiperf job {ctx.job_name} failed — check artifacts for logs")

    return False


@task
def capture_job_state(args, ctx):
    """Capture job logs and pod state."""
    _capture_job_state(args.artifact_dir, ctx.namespace, ctx.job_name)


@task
def create_copy_pod(args, ctx):
    """Create a pod to read results from PVC."""
    pod_data = oc_get_json("pods", namespace=ctx.namespace,
                           selector=f"job-name={ctx.job_name}", ignore_not_found=True)
    node_name = None
    if pod_data and pod_data.get("items"):
        node_name = pod_data["items"][0].get("spec", {}).get("nodeName")

    copy_pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"{ctx.job_name}-copy",
            "namespace": ctx.namespace,
        },
        "spec": {
            "restartPolicy": "Never",
            "volumes": [
                {"name": "results", "persistentVolumeClaim": {"claimName": ctx.pvc_name}},
            ],
            "containers": [{
                "name": "copy",
                "image": "busybox",
                "command": ["sleep", "3600"],
                "volumeMounts": [
                    {"name": "results", "mountPath": "/results"},
                ],
            }],
        },
    }
    if node_name:
        copy_pod["spec"]["nodeName"] = node_name

    oc_apply(args.artifact_dir / "src" / "aiperf-copy-pod.yaml", copy_pod)


@retry(attempts=24, delay=5, backoff=1.0)
@task
def wait_copy_pod_ready(args, ctx):
    """Wait for copy pod to be ready."""
    payload = oc_get_json("pod", name=f"{ctx.job_name}-copy", namespace=ctx.namespace)
    conditions = payload.get("status", {}).get("conditions", [])
    if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
        return f"Copy pod ready"
    return False


@task
def extract_results(args, ctx):
    """Extract ALL aiperf output files from PVC via copy pod."""
    results_dir = args.artifact_dir / "artifacts" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    copy_pod = f"{ctx.job_name}-copy"
    remote_base = f"/results/{ctx.results_subpath}"

    # List all files on PVC
    listing = oc("exec", "-n", ctx.namespace, copy_pod,
                 "--", "find", remote_base, "-type", "f", check=False, log_stdout=False)
    if listing.returncode != 0:
        logger.warning("Could not list aiperf results at %s", remote_base)
        return

    remote_files = [l.strip() for l in (listing.stdout or "").split("\n") if l.strip()]
    logger.info("Found %d result files on PVC", len(remote_files))

    for remote_path in remote_files:
        rel = remote_path.replace(remote_base + "/", "", 1)
        local_path = results_dir / rel
        local_path.parent.mkdir(parents=True, exist_ok=True)

        result = oc("exec", "-n", ctx.namespace, copy_pod,
                     "--", "cat", remote_path, check=False, log_stdout=False)
        if result.returncode == 0 and result.stdout:
            write_text(local_path, result.stdout)
            logger.info("  extracted: %s (%d bytes)", rel, len(result.stdout))
        else:
            logger.warning("  failed: %s", rel)

    # Parse summary
    summary_path = results_dir / "profile_export_aiperf.json"
    if summary_path.exists():
        try:
            full = json.loads(summary_path.read_text())
            ctx.aiperf_results = full
            summary = {
                "request_count": _m(full, "request_count"),
                "error_count": _m(full, "error_request_count"),
                "duration_s": round(_m(full, "benchmark_duration") or 0, 1),
                "throughput_rps": round(_m(full, "request_throughput") or 0, 2),
                "output_tps": round(_m(full, "output_token_throughput") or 0, 2),
                "total_tps": round(_m(full, "total_token_throughput") or 0, 2),
                "ttft_avg_ms": round(_m(full, "time_to_first_token") or 0, 2),
                "ttft_p95_ms": round(_m(full, "time_to_first_token", "p95") or 0, 2),
                "itl_avg_ms": round(_m(full, "inter_token_latency") or 0, 2),
                "itl_p95_ms": round(_m(full, "inter_token_latency", "p95") or 0, 2),
                "latency_avg_ms": round(_m(full, "request_latency") or 0, 2),
                "latency_p95_ms": round(_m(full, "request_latency", "p95") or 0, 2),
            }
            write_json(results_dir / "aiperf_summary.json", summary)
            logger.info("=== aiperf Results ===")
            for k, v in summary.items():
                logger.info("  %s: %s", k, v)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to parse aiperf results: %s", e)


@task
def build_mlflow_metadata(args, ctx):
    """Build MLflow-compatible metadata file for caliper-export."""
    results = getattr(ctx, "aiperf_results", None)
    if not results:
        return

    metadata = {
        "parameters": {
            "benchmark_tool": "aiperf",
            "model": args.model_name,
            "endpoint_url": args.endpoint_url,
            "endpoint_type": args.endpoint_type,
            "dataset_type": args.dataset_type,
            "dataset_cap": str(args.dataset_cap or "full"),
            "streaming": str(args.streaming),
            "synthesis_max_isl": str(args.synthesis_max_isl or ""),
        },
        "metrics": {
            "throughput/request_throughput": _m(results, "request_throughput") or 0,
            "tokens/output_token_throughput": _m(results, "output_token_throughput") or 0,
            "tokens/total_token_throughput": _m(results, "total_token_throughput") or 0,
            "latency/request_latency_ms": _m(results, "request_latency") or 0,
            "latency/request_latency_p95_ms": _m(results, "request_latency", "p95") or 0,
            "ttft/time_to_first_token_ms": _m(results, "time_to_first_token") or 0,
            "ttft/time_to_first_token_p95_ms": _m(results, "time_to_first_token", "p95") or 0,
            "itl/inter_token_latency_ms": _m(results, "inter_token_latency") or 0,
            "itl/inter_token_latency_p95_ms": _m(results, "inter_token_latency", "p95") or 0,
            "request_count": _m(results, "request_count") or 0,
            "error_request_count": _m(results, "error_request_count") or 0,
            "total_output_tokens": _m(results, "total_output_tokens") or 0,
        },
        "tags": {
            "benchmark_tool": "aiperf",
            "framework": "dynamo",
        },
        "description": (
            f"aiperf benchmark: {args.model_name}, "
            f"dataset={args.dataset_type} cap={args.dataset_cap}, "
            f"endpoint={args.endpoint_url}"
        ),
    }

    meta_path = args.artifact_dir / "artifacts" / "mlflow_run_metadata.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(meta_path, metadata)
    logger.info("MLflow metadata written to %s", meta_path)


@task
def export_to_mlflow(args, ctx):
    """Push results + metadata to MLflow via Caliper export (if configured)."""
    meta_path = args.artifact_dir / "artifacts" / "mlflow_run_metadata.json"
    results_dir = args.artifact_dir / "artifacts" / "results"

    if not meta_path.exists() or not results_dir.exists():
        logger.info("No metadata or results to export, skipping MLflow push")
        return

    try:
        from projects.caliper.engine.file_export.runner import run_file_export
    except ImportError:
        logger.info("Caliper not available, skipping MLflow export")
        return

    # Read MLflow connection from env (set by vault or manually)
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
    if not tracking_uri:
        logger.info("MLFLOW_TRACKING_URI not set, skipping MLflow export")
        return

    run_metadata = json.loads(meta_path.read_text())
    insecure = os.environ.get("MLFLOW_TRACKING_INSECURE_TLS", "").lower() == "true"
    connection = {}
    if os.environ.get("MLFLOW_TRACKING_USERNAME"):
        connection["username"] = os.environ["MLFLOW_TRACKING_USERNAME"]
        connection["password"] = os.environ.get("MLFLOW_TRACKING_PASSWORD", "")
    if insecure:
        connection["insecure_tls"] = True

    experiment = os.environ.get("MLFLOW_EXPERIMENT", "forge-dynamo")
    run_name = os.environ.get("MLFLOW_RUN_NAME", f"forge-{args.name}")

    logger.info("Exporting to MLflow: %s experiment=%s", tracking_uri, experiment)
    results = run_file_export(
        source=results_dir,
        backends=["mlflow"],
        dry_run=False,
        mlflow_tracking_uri=tracking_uri,
        mlflow_experiment=experiment,
        mlflow_run_id=None,
        mlflow_run_name=run_name,
        mlflow_insecure_tls=insecure,
        mlflow_connection=connection if connection else None,
        mlflow_run_metadata=run_metadata,
        verbose=True,
    )
    for r in results:
        logger.info("MLflow export: %s — %s", r.status, r.detail)
        if r.metadata:
            for k, v in r.metadata.items():
                logger.info("  %s: %s", k, v)


@task
def cleanup_job_resources(args, ctx):
    """Delete job and copy pod, keep PVC for future runs."""
    _best_effort_delete("copy pod", "delete", "pod", f"{ctx.job_name}-copy",
                        "-n", ctx.namespace, "--ignore-not-found=true")
    _best_effort_delete("aiperf job", "delete", "job", ctx.job_name,
                        "-n", ctx.namespace, "--ignore-not-found=true")


def _best_effort_delete(description: str, *oc_args: str) -> None:
    try:
        oc(*oc_args, check=False, timeout_seconds=60)
    except subprocess.TimeoutExpired:
        logger.warning("Timed out deleting %s", description)


def _capture_job_state(artifact_dir: Path, namespace: str, job_name: str) -> None:
    artifacts = artifact_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    result = oc("logs", f"job/{job_name}", "-n", namespace, check=False, log_stdout=False)
    if result.returncode == 0 and result.stdout:
        write_text(artifacts / "aiperf_job.logs", result.stdout)

    result = oc("get", "job", job_name, "-n", namespace, "-oyaml", check=False, log_stdout=False)
    if result.returncode == 0 and result.stdout:
        write_text(artifacts / "aiperf_job.yaml", result.stdout)


def _m(data: dict, key: str, field: str = "avg"):
    v = data.get(key, {})
    if isinstance(v, dict):
        r = v.get(field)
        try:
            return float(r) if r is not None else None
        except (TypeError, ValueError):
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
