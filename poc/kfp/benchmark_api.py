#!/usr/bin/env python3
"""
KFP Benchmark API - Manages Kueue Workloads + Kubeflow Pipeline submission.

Architecture:
1. Create hollow Kueue Workload CR (no pods scheduled)
2. Kueue admits and assigns a flavor (cluster)
3. API submits KFP Pipeline Run with target cluster kubeconfig
4. On completion, delete Workload CR to release quota

Usage:
    python benchmark_api.py setup           # Setup kubeconfig secrets
    python benchmark_api.py rhoai --model "Qwen/Qwen3-0.6B" --gpu-type H200 --tp 4
    python benchmark_api.py status          # Check pipeline runs
    python benchmark_api.py quota           # Show GPU usage
    python benchmark_api.py cleanup --name <workload-name>
"""

import subprocess
import json
import time
import uuid
import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.cluster import Cluster
from core.platform import PlatformConfig
from core.images import ImageRegistry, extract_version_from_image
from registry.cluster_registry import ClusterRegistry
from strategies.deployment.rhaiis import RHAIISDeploymentStrategy

# Configuration paths
CONFIG_DIR = Path(__file__).parent / "config"

# Load platform config (single source of truth)
_platform_config = PlatformConfig.load(CONFIG_DIR / "platform.yaml")

# Load cluster registry
_cluster_registry = ClusterRegistry(CONFIG_DIR / "clusters.yaml", _platform_config)

# Derived config from platform
MGMT_KUBECONFIG = _platform_config.mgmt_kubeconfig or os.environ.get("MGMT_KUBECONFIG", "")
BENCHMARK_NAMESPACE = _platform_config.mgmt_namespace or "benchmark-system"
KFP_NAMESPACE = _platform_config.kfp.namespace or "kubeflow"
KFP_HOST = _platform_config.kfp.host or os.environ.get("KFP_HOST", "")

# KFP Token - can be set via env var or will be created from SA
KFP_TOKEN = os.environ.get("KFP_TOKEN", None)


def oc(args: list, kubeconfig: str = None, input_data: str = None, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run oc command."""
    if kubeconfig is None:
        kubeconfig = MGMT_KUBECONFIG
    cmd = ["oc", "--kubeconfig", kubeconfig] + args
    return subprocess.run(cmd, capture_output=True, text=True, input=input_data, timeout=timeout)


def create_workload(name: str, gpu_type: str, gpu_count: int) -> bool:
    """Create a hollow Kueue Workload CR."""
    workload_yaml = f"""
apiVersion: kueue.x-k8s.io/v1beta2
kind: Workload
metadata:
  name: {name}
  namespace: {BENCHMARK_NAMESPACE}
  labels:
    kueue.x-k8s.io/queue-name: benchmark-queue
    benchmark.topsail.io/managed: "true"
    pipeline-type: kfp
spec:
  queueName: benchmark-queue
  podSets:
    - name: launcher
      count: 1
      template:
        spec:
          nodeSelector:
            gpu-type: {gpu_type}
          containers:
            - name: placeholder
              image: registry.k8s.io/pause:3.9
              resources:
                requests:
                  nvidia.com/gpu: "{gpu_count}"
          restartPolicy: Never
"""
    result = oc(["apply", "-f", "-"], input_data=workload_yaml)
    if result.returncode != 0:
        print(f"Error creating workload: {result.stderr}")
        return False
    print(f"Created workload: {name}")
    return True


def wait_for_admission(name: str, timeout: int = 300) -> Optional[str]:
    """Wait for workload to be admitted and return the assigned flavor."""
    print(f"Waiting for workload '{name}' to be admitted...")
    start = time.time()

    while time.time() - start < timeout:
        result = oc([
            "get", "workload", name, "-n", BENCHMARK_NAMESPACE,
            "-o", "jsonpath={.status.admission.podSetAssignments[0].flavors}"
        ])

        if result.returncode == 0 and result.stdout:
            try:
                flavors = json.loads(result.stdout)
                flavor = flavors.get("nvidia.com/gpu")
                if flavor:
                    print(f"Workload admitted with flavor: {flavor}")
                    return flavor
            except json.JSONDecodeError:
                pass

        time.sleep(2)

    print(f"Timeout waiting for admission")
    return None


def get_cluster_config(cluster_id: str) -> Optional[Dict[str, Any]]:
    """Get cluster configuration by ID or GPU type."""
    try:
        # Try direct lookup by cluster ID
        if _cluster_registry.exists(cluster_id):
            cluster = _cluster_registry.get(cluster_id)
        else:
            # Try lookup by GPU type
            cluster = _cluster_registry.get_by_gpu_type(cluster_id)

        # Return dict for backward compatibility
        return {
            "kubeconfig": str(cluster.kubeconfig_path_resolved) if cluster.kubeconfig_path else "",
            "gpu_type": cluster.gpu_type,
            "namespace": cluster.namespace,
            "secret_name": cluster.kubeconfig_secret,
        }
    except KeyError:
        return None


def delete_workload(name: str) -> bool:
    """Delete workload to release quota."""
    result = oc(["delete", "workload", name, "-n", BENCHMARK_NAMESPACE, "--ignore-not-found"])
    if result.returncode == 0:
        print(f"Deleted workload: {name}")
        return True
    print(f"Error deleting workload: {result.stderr}")
    return False


def cleanup_target_deployment(workload_name: str, cluster: str = None, deployment_type: str = "llminferenceservice") -> bool:
    """Cleanup deployment on target cluster.

    Args:
        workload_name: The workload name (e.g., qwen-qwen3-06b-ea346210)
        cluster: Optional cluster name (e.g., h200-cluster). If not specified, tries all clusters.
        deployment_type: Type of deployment to delete (llminferenceservice or inferenceservice)

    Returns:
        True if cleanup succeeded on at least one cluster.
    """
    # Extract deployment name from workload name
    # Workload: qwen-qwen3-06b-ea346210 -> Deployment: qwen-qwen3-06b-46210 (last 5 chars of uuid)
    parts = workload_name.rsplit("-", 1)
    if len(parts) == 2:
        base_name = parts[0]
        uuid_suffix = parts[1][-5:]  # Last 5 chars
        deployment_name = f"{base_name}-{uuid_suffix}"
    else:
        deployment_name = workload_name

    clusters_to_check = []
    if cluster:
        # Use specified cluster
        if _cluster_registry.exists(cluster):
            clusters_to_check.append(_cluster_registry.get(cluster))
    else:
        # Try all clusters
        clusters_to_check = _cluster_registry.list_enabled()

    success = False
    for cluster_obj in clusters_to_check:
        kubeconfig = str(cluster_obj.kubeconfig_path_resolved) if cluster_obj.kubeconfig_path else ""
        namespace = cluster_obj.namespace
        flavor = cluster_obj.id

        # Check if deployment exists
        result = oc(
            ["get", deployment_type, deployment_name, "-n", namespace],
            kubeconfig=kubeconfig
        )

        if result.returncode == 0:
            # Delete the deployment
            result = oc(
                ["delete", deployment_type, deployment_name, "-n", namespace, "--ignore-not-found"],
                kubeconfig=kubeconfig
            )
            if result.returncode == 0:
                print(f"Deleted {deployment_type}: {deployment_name} (cluster: {flavor})")
                success = True
            else:
                print(f"Error deleting deployment on {flavor}: {result.stderr}")
        else:
            # Try with original workload name as fallback
            result = oc(
                ["delete", deployment_type, workload_name, "-n", namespace, "--ignore-not-found"],
                kubeconfig=kubeconfig
            )
            if "deleted" in result.stdout.lower():
                print(f"Deleted {deployment_type}: {workload_name} (cluster: {flavor})")
                success = True

    if not success:
        print(f"No {deployment_type} found for: {deployment_name}")

    return success


def get_quota_usage() -> str:
    """Get current quota usage."""
    result = oc([
        "get", "clusterqueue", "benchmark-queue",
        "-o", "jsonpath={.status.flavorsUsage}"
    ])
    if result.returncode == 0 and result.stdout:
        try:
            usage = json.loads(result.stdout)
            lines = []
            for flavor in usage:
                name = flavor.get("name", "unknown")
                for res in flavor.get("resources", []):
                    if res.get("name") == "nvidia.com/gpu":
                        total = res.get("total", 0)
                        lines.append(f"  {name}: {total} GPUs in use")
            return "\n".join(lines) if lines else "  No GPUs in use"
        except json.JSONDecodeError:
            return result.stdout
    return "  Unable to fetch quota"


def setup_kubeconfig_secrets():
    """Create kubeconfig secrets in KFP namespace for remote execution."""
    print("Setting up kubeconfig secrets...")

    # Management cluster kubeconfig secret
    result = oc([
        "create", "secret", "generic", "mgmt-kubeconfig",
        "-n", KFP_NAMESPACE,
        f"--from-file=config={MGMT_KUBECONFIG}",
        "--dry-run=client", "-o", "yaml"
    ])
    if result.returncode == 0:
        oc(["apply", "-f", "-"], input_data=result.stdout)
        print("  Created: mgmt-kubeconfig")

    # Target cluster kubeconfig secrets
    for cluster in _cluster_registry.list_enabled():
        kubeconfig_path = cluster.kubeconfig_path_resolved
        if not kubeconfig_path or not kubeconfig_path.exists():
            print(f"  Skipped: {cluster.kubeconfig_secret} (kubeconfig not found)")
            continue

        result = oc([
            "create", "secret", "generic", cluster.kubeconfig_secret,
            "-n", KFP_NAMESPACE,
            f"--from-file=config={kubeconfig_path}",
            "--dry-run=client", "-o", "yaml"
        ])
        if result.returncode == 0:
            oc(["apply", "-f", "-"], input_data=result.stdout)
            print(f"  Created: {cluster.kubeconfig_secret}")


def submit_kfp_pipeline(
    workload_name: str,
    model_name: str,
    tp: int,
    target_kubeconfig_secret: str,
    namespace: str = "llm-d-bench",
    routing_mode: str = "direct",
    vllm_image: str = "quay.io/aipcc/rhaiis/cuda-ubi9:3.3.0-1769597087",
    vllm_args: str = "--max-model-len=8192,--gpu-memory-utilization=0.92,--uvicorn-log-level=debug,--no-enable-prefix-caching",
    guidellm_rate: str = "1,50,100,200",
    guidellm_data: str = "prompt_tokens=1000,output_tokens=1000",
    guidellm_max_seconds: str = "120",
    mlflow_enabled: str = "false",
    accelerator: str = "H200",
    version: str = "RHOAI-3.2",
    skip_deploy: bool = False,
    skip_benchmark: bool = False,
    skip_cleanup: bool = False,
    kfp_run_name: Optional[str] = None,
    batch_id: Optional[str] = None,
    scenario_id: Optional[str] = None,
) -> Optional[str]:
    """Submit RHOAI benchmark pipeline via KFP."""
    try:
        from kfp.client import Client
        from rhoai_benchmark_pipeline_v2 import rhoai_benchmark_pipeline
    except ImportError as e:
        print(f"Error importing KFP: {e}")
        print("Install with: pip install kfp")
        return None

    # Create KFP client - upstream KFP typically doesn't require auth
    print(f"Connecting to KFP at: {KFP_HOST}")

    # For upstream KFP with Argo, try without authentication first
    try:
        client = Client(
            host=KFP_HOST,
            namespace=KFP_NAMESPACE,
        )
    except Exception as e:
        print(f"Connection without auth failed: {e}")
        # Fallback to token auth if needed
        token = KFP_TOKEN
        if not token:
            token_result = oc(["whoami", "-t"])
            if token_result.returncode == 0 and token_result.stdout.strip():
                token = token_result.stdout.strip()
            else:
                token_result = oc(["create", "token", "pipeline-runner", "-n", KFP_NAMESPACE, "--duration=1h"])
                if token_result.returncode != 0:
                    print(f"Error: Could not create SA token: {token_result.stderr}")
                    return None
                token = token_result.stdout.strip()

        client = Client(
            host=KFP_HOST,
            existing_token=token,
            namespace=KFP_NAMESPACE,
        )

    # Create pipeline run name
    if kfp_run_name:
        run_name = kfp_run_name
    else:
        # Fallback: generate from model name
        model_short = model_name.replace("/", "-").replace(".", "").lower()[:20]
        run_name = f"rhoai-{model_short}-{workload_name[:8]}"

    print(f"Submitting pipeline run: {run_name}")

    try:
        run = client.create_run_from_pipeline_func(
            pipeline_func=rhoai_benchmark_pipeline,
            run_name=run_name,
            arguments={
                "model_name": model_name,
                "namespace": namespace,
                "tp": tp,
                "routing_mode": routing_mode,
                "vllm_args": vllm_args,
                "kueue_queue_name": "benchmark-queue",
                "run_uuid": workload_name,
                "guidellm_image": "image-registry.openshift-image-registry.svc:5000/llm-d-bench/guidellm-custom:v0.5.3",
                "guidellm_rate": guidellm_rate,
                "guidellm_data": guidellm_data,
                "guidellm_max_seconds": guidellm_max_seconds,
                "accelerator": accelerator,
                "version": version,
                "mlflow_enabled": mlflow_enabled,
                "skip_cleanup": skip_cleanup,
                "kubeconfig_secret": target_kubeconfig_secret,
                "workload_name": workload_name,  # For Kueue admission tracking
                "batch_id": batch_id or "",
                "scenario_id": scenario_id or "",
            },
        )
        print(f"Pipeline run created: {run.run_id}")
        return run.run_id
    except Exception as e:
        print(f"Error creating pipeline run: {e}")
        return None


def list_pipeline_runs():
    """List KFP pipeline runs."""
    try:
        from kfp.client import Client
    except ImportError:
        print("KFP not installed. Install with: pip install kfp")
        return

    token_result = oc(["whoami", "-t"])
    if token_result.returncode != 0:
        print("Error: Could not get OAuth token. Run 'oc login' first.")
        return
    token = token_result.stdout.strip()

    try:
        client = Client(
            host=KFP_HOST,
            existing_token=token,
            namespace=KFP_NAMESPACE,
        )
        runs = client.list_runs(namespace=KFP_NAMESPACE)
        print(f"Pipeline Runs ({len(runs.runs) if runs.runs else 0}):")
        if runs.runs:
            for run in runs.runs[:10]:  # Last 10 runs
                status = run.state if hasattr(run, 'state') else 'Unknown'
                print(f"  {run.name}: {status}")
        else:
            print("  No runs found")
    except Exception as e:
        print(f"Error listing runs: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="KFP Benchmark API - Multi-cluster benchmark orchestration with Kueue + KFP"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Setup command
    subparsers.add_parser("setup", help="Setup kubeconfig secrets")

    # Status command
    status_parser = subparsers.add_parser("status", help="Check workload/pipeline status")
    status_parser.add_argument("--name", "-n", help="Workload name (optional)")

    # Cleanup command
    cleanup_parser = subparsers.add_parser("cleanup", help="Delete workload and target deployment")
    cleanup_parser.add_argument("--name", "-n", required=True, help="Workload name")
    cleanup_parser.add_argument("--cluster", "-c", help="Target cluster ID (if not specified, tries all clusters)")
    cleanup_parser.add_argument("--deployment-type", "-d", choices=["llminferenceservice", "inferenceservice"],
                               default="llminferenceservice", help="Deployment type (default: llminferenceservice for RHOAI/RHAIIS)")

    # List command
    subparsers.add_parser("list", help="List workloads and pipeline runs")

    # Quota command
    subparsers.add_parser("quota", help="Show quota usage")

    # RHOAI benchmark command
    rhoai_parser = subparsers.add_parser("rhoai", help="Submit RHOAI end-to-end benchmark via KFP")
    rhoai_parser.add_argument("--model", "-m", required=True, help="HuggingFace model name")
    rhoai_parser.add_argument("--gpu-type", "-t", required=True, help="Target GPU type (H200, A100, MI300X)")
    rhoai_parser.add_argument("--tp", type=int, default=4, help="Tensor parallelism (GPU count)")
    rhoai_parser.add_argument("--namespace", "-n", default="llm-d-bench", help="Target namespace")
    rhoai_parser.add_argument("--routing-mode", default="direct", choices=["direct", "prefix-estimation", "prefix-precise"], help="Routing mode")
    rhoai_parser.add_argument("--vllm-image", default="quay.io/aipcc/rhaiis/cuda-ubi9:3.3.0-1769597087", help="vLLM container image")
    rhoai_parser.add_argument("--vllm-args", default="--max-model-len=8192,--gpu-memory-utilization=0.92,--uvicorn-log-level=debug,--no-enable-prefix-caching", help="vLLM args (comma-separated)")
    rhoai_parser.add_argument("--rate", default="1,50,100,200", help="GuideLLM rates (comma-separated)")
    rhoai_parser.add_argument("--data", default="prompt_tokens=1000,output_tokens=1000", help="GuideLLM data spec")
    rhoai_parser.add_argument("--max-seconds", default="120", help="Max seconds per rate")
    rhoai_parser.add_argument("--mlflow-enabled", default="false", help="Enable MLflow upload to AWS SageMaker")
    rhoai_parser.add_argument("--accelerator", default="H200", help="Accelerator tag")
    rhoai_parser.add_argument("--version", default="RHOAI-3.2", help="Version tag")
    rhoai_parser.add_argument("--skip-deploy", action="store_true", help="Skip model deployment")
    rhoai_parser.add_argument("--skip-benchmark", action="store_true", help="Skip benchmark")
    rhoai_parser.add_argument("--skip-cleanup", action="store_true", help="Skip cleanup")
    rhoai_parser.add_argument("--dry-run", action="store_true", help="Print parameters only")

    # Compile command
    compile_parser = subparsers.add_parser("compile", help="Compile pipeline to YAML")
    compile_parser.add_argument("--output", "-o", default="rhoai_benchmark_pipeline.yaml", help="Output file")

    # RHAIIS benchmark command (KServe deployment)
    rhaiis_parser = subparsers.add_parser("rhaiis", help="Submit RHAIIS benchmark via KFP (KServe InferenceService)")
    rhaiis_parser.add_argument("--model", "-m", required=True, help="HuggingFace model name")
    rhaiis_parser.add_argument("--gpu-type", "-t", required=True, help="Target GPU type (H200, A100, MI300X)")
    rhaiis_parser.add_argument("--tp", type=int, default=1, help="Tensor parallelism (GPU count)")
    rhaiis_parser.add_argument("--namespace", "-n", default="llm-d-bench", help="Target namespace")
    rhaiis_parser.add_argument("--vllm-image", help="vLLM container image (default: from images.yaml)")
    rhaiis_parser.add_argument("--vllm-args", default="--max-model-len=8192,--gpu-memory-utilization=0.92", help="vLLM args (comma-separated)")
    rhaiis_parser.add_argument("--rate", default="1,50,100,200", help="GuideLLM rates (comma-separated)")
    rhaiis_parser.add_argument("--data", default="prompt_tokens=1000,output_tokens=1000", help="GuideLLM data spec")
    rhaiis_parser.add_argument("--max-seconds", default="120", help="Max seconds per rate")
    rhaiis_parser.add_argument("--mlflow-enabled", default="false", help="Enable MLflow upload")
    rhaiis_parser.add_argument("--accelerator", help="Accelerator type (nvidia/amd, default: from cluster)")
    rhaiis_parser.add_argument("--version", default="RHAIIS-3.4", help="Version tag")
    rhaiis_parser.add_argument("--skip-deploy", action="store_true", help="Skip model deployment")
    rhaiis_parser.add_argument("--skip-benchmark", action="store_true", help="Skip benchmark")
    rhaiis_parser.add_argument("--skip-cleanup", action="store_true", help="Skip cleanup")
    rhaiis_parser.add_argument("--dry-run", action="store_true", help="Print parameters only")

    args = parser.parse_args()

    if args.command == "setup":
        setup_kubeconfig_secrets()
        print("\nSetup complete!")

    elif args.command == "status":
        print(f"{'='*60}")
        print(f"Workloads (Kueue)")
        print(f"{'='*60}")
        if args.name:
            result = oc(["get", "workload", args.name, "-n", BENCHMARK_NAMESPACE, "-o", "wide"])
        else:
            result = oc(["get", "workload", "-n", BENCHMARK_NAMESPACE, "-o", "wide"])
        print(result.stdout if result.stdout else "No workloads found")

        print(f"\n{'='*60}")
        print(f"Pipeline Runs (KFP)")
        print(f"{'='*60}")
        list_pipeline_runs()

    elif args.command == "cleanup":
        # Delete Kueue workload on management cluster
        delete_workload(args.name)

        # Delete deployment on target cluster
        cluster = getattr(args, 'cluster', None)
        deployment_type = getattr(args, 'deployment_type', 'llminferenceservice')
        cleanup_target_deployment(args.name, cluster, deployment_type)

        print(f"\n{'='*60}")
        print(f"Current Quota Usage")
        print(f"{'='*60}")
        print(get_quota_usage())

    elif args.command == "list":
        print(f"{'='*60}")
        print(f"Workloads (Kueue)")
        print(f"{'='*60}")
        result = oc([
            "get", "workload", "-n", BENCHMARK_NAMESPACE,
            "-o", "custom-columns=NAME:.metadata.name,QUEUE:.spec.queueName,ADMITTED:.status.conditions[?(@.type=='Admitted')].status,FLAVOR:.status.admission.podSetAssignments[0].flavors,TYPE:.metadata.labels.pipeline-type"
        ])
        print(result.stdout if result.stdout else "No workloads")

        print(f"\n{'='*60}")
        print(f"Pipeline Runs (KFP)")
        print(f"{'='*60}")
        list_pipeline_runs()

    elif args.command == "quota":
        print(f"{'='*60}")
        print(f"Quota Usage")
        print(f"{'='*60}")
        print(get_quota_usage())

        print(f"\n{'='*60}")
        print(f"Cluster Queue Details")
        print(f"{'='*60}")
        result = oc(["get", "clusterqueue", "benchmark-queue", "-o", "wide"])
        print(result.stdout)

    elif args.command == "rhoai":
        # Generate unique workload name
        run_id = str(uuid.uuid4())[:8]
        model_short = args.model.replace("/", "-").replace(".", "").lower()[:15]
        workload_name = f"{model_short}-{run_id}"

        # Get cluster by GPU type from registry
        try:
            cluster_obj = _cluster_registry.get_by_gpu_type(args.gpu_type)
            flavor = cluster_obj.id
            cluster = {
                "kubeconfig": str(cluster_obj.kubeconfig_path_resolved) if cluster_obj.kubeconfig_path else "",
                "gpu_type": cluster_obj.gpu_type,
                "namespace": cluster_obj.namespace,
                "secret_name": cluster_obj.kubeconfig_secret,
            }
        except KeyError:
            print(f"Error: No cluster found for GPU type: {args.gpu_type}")
            return 1

        # Auto-extract version from vLLM image if not explicitly set
        if args.version == "RHOAI-3.2":
            version = extract_version_from_image(args.vllm_image)
        else:
            version = args.version

        print(f"\n{'='*60}")
        print(f"RHOAI End-to-End Benchmark (KFP)")
        print(f"{'='*60}")
        print(f"Model:         {args.model}")
        print(f"GPU Type:      {args.gpu_type} ({flavor})")
        print(f"TP:            {args.tp}")
        print(f"Namespace:     {args.namespace}")
        print(f"Routing:       {args.routing_mode}")
        print(f"Rates:         {args.rate}")
        print(f"Version:       {version}")
        print(f"Workload:      {workload_name}")
        print()

        if args.dry_run:
            print("DRY RUN - would create:")
            print(f"  1. Kueue Workload: {workload_name}")
            print(f"  2. KFP Pipeline Run")
            return

        # Step 1: Create Kueue Workload
        print(f"{'='*60}")
        print(f"Step 1: Creating Kueue Workload")
        print(f"{'='*60}")
        if not create_workload(workload_name, args.gpu_type, args.tp):
            return 1

        # Step 2: Submit KFP Pipeline (immediately - admission waiting happens inside pipeline)
        print(f"\n{'='*60}")
        print(f"Step 2: Submitting KFP Pipeline")
        print(f"{'='*60}")
        print("Pipeline will wait for Kueue admission as its first step.")
        print("Run will be immediately visible in KFP UI.")
        print()
        run_id = submit_kfp_pipeline(
            workload_name=workload_name,
            model_name=args.model,
            tp=args.tp,
            target_kubeconfig_secret=cluster["secret_name"],
            namespace=args.namespace,
            routing_mode=args.routing_mode,
            vllm_image=args.vllm_image,
            vllm_args=args.vllm_args,
            guidellm_rate=args.rate,
            guidellm_data=args.data,
            guidellm_max_seconds=args.max_seconds,
            mlflow_enabled=args.mlflow_enabled,
            accelerator=args.accelerator,
            version=version,
            skip_deploy=args.skip_deploy,
            skip_benchmark=args.skip_benchmark,
            skip_cleanup=args.skip_cleanup,
        )

        if not run_id:
            print("Failed to create pipeline run, cleaning up...")
            delete_workload(workload_name)
            return 1

        print(f"\n{'='*60}")
        print(f"Benchmark Submitted Successfully!")
        print(f"{'='*60}")
        print(f"Workload:    {workload_name}")
        print(f"Pipeline:    {run_id}")
        print(f"Status:      Waiting for Kueue admission (visible in KFP UI)")
        print()
        print(f"View in KFP UI:")
        print(f"  {KFP_HOST}/")
        print()
        print(f"Monitor Kueue workload:")
        print(f"  oc get workload {workload_name} -n {BENCHMARK_NAMESPACE} -w")
        print()
        print(f"Manual cleanup (if needed):")
        print(f"  python benchmark_api.py cleanup --name {workload_name}")
        print()

        print(f"{'='*60}")
        print(f"Current Quota Usage")
        print(f"{'='*60}")
        print(get_quota_usage())

    elif args.command == "rhaiis":
        # Generate unique workload name
        run_id = str(uuid.uuid4())[:8]
        model_short = args.model.replace("/", "-").replace(".", "").lower()[:15]
        workload_name = f"{model_short}-{run_id}"

        # Get cluster by GPU type from registry
        try:
            cluster_obj = _cluster_registry.get_by_gpu_type(args.gpu_type)
            flavor = cluster_obj.id
            cluster = {
                "kubeconfig": str(cluster_obj.kubeconfig_path_resolved) if cluster_obj.kubeconfig_path else "",
                "gpu_type": cluster_obj.gpu_type,
                "namespace": cluster_obj.namespace,
                "secret_name": cluster_obj.kubeconfig_secret,
            }
        except KeyError:
            print(f"Error: No cluster found for GPU type: {args.gpu_type}")
            return 1

        # Determine accelerator type (from args or cluster)
        accelerator = args.accelerator or cluster_obj.accelerator or "nvidia"

        # Get vLLM image from ImageRegistry if not specified
        if args.vllm_image:
            vllm_image = args.vllm_image
        else:
            try:
                vllm_image = ImageRegistry.get_vllm_image(accelerator, "redhat")
            except ValueError as e:
                print(f"Error getting vLLM image: {e}")
                return 1

        # Auto-extract version from vLLM image if not explicitly set
        if args.version == "RHAIIS-3.4":
            version = extract_version_from_image(vllm_image)
        else:
            version = args.version

        print(f"\n{'='*60}")
        print(f"RHAIIS End-to-End Benchmark (KFP + KServe)")
        print(f"{'='*60}")
        print(f"Model:         {args.model}")
        print(f"GPU Type:      {args.gpu_type} ({flavor})")
        print(f"TP:            {args.tp}")
        print(f"Namespace:     {args.namespace}")
        print(f"Accelerator:   {accelerator}")
        print(f"vLLM Image:    {vllm_image}")
        print(f"Version:       {version}")
        print(f"Rates:         {args.rate}")
        print(f"Workload:      {workload_name}")
        print(f"Deployment:    KServe InferenceService")
        print()

        if args.dry_run:
            print("DRY RUN - would create:")
            print(f"  1. Kueue Workload: {workload_name}")
            print(f"  2. KFP Pipeline Run (RHAIIS/KServe deployment)")
            # Show sample manifest
            strategy = RHAIISDeploymentStrategy()
            sample_name = strategy.sanitize_name(args.model) + "-" + run_id[:5]
            runtime_args = {}
            for arg in args.vllm_args.split(","):
                if "=" in arg:
                    key, val = arg.split("=", 1)
                    runtime_args[key.lstrip("-")] = val
                else:
                    runtime_args[arg.lstrip("-")] = True
            manifest = strategy.generate_manifests(
                model_id=args.model,
                deployment_name=sample_name,
                namespace=args.namespace,
                tensor_parallel=args.tp,
                runtime_args=runtime_args,
                vllm_image=vllm_image,
                accelerator=accelerator,
            )
            print(f"\n  Sample KServe manifest:")
            print("  " + "\n  ".join(manifest.split("\n")[:30]) + "\n  ...")
            return

        # Step 1: Create Kueue Workload
        print(f"{'='*60}")
        print(f"Step 1: Creating Kueue Workload")
        print(f"{'='*60}")
        if not create_workload(workload_name, args.gpu_type, args.tp):
            return 1

        # Step 2: Submit KFP Pipeline
        print(f"\n{'='*60}")
        print(f"Step 2: Submitting KFP Pipeline (RHAIIS)")
        print(f"{'='*60}")
        print("Pipeline will wait for Kueue admission as its first step.")
        print("Run will be immediately visible in KFP UI.")
        print()
        run_id = submit_kfp_pipeline(
            workload_name=workload_name,
            model_name=args.model,
            tp=args.tp,
            target_kubeconfig_secret=cluster["secret_name"],
            namespace=args.namespace,
            routing_mode="direct",  # RHAIIS always direct (no EPP)
            vllm_image=vllm_image,
            vllm_args=args.vllm_args,
            guidellm_rate=args.rate,
            guidellm_data=args.data,
            guidellm_max_seconds=args.max_seconds,
            mlflow_enabled=args.mlflow_enabled,
            accelerator=accelerator.upper(),
            version=version,
            skip_deploy=args.skip_deploy,
            skip_benchmark=args.skip_benchmark,
            skip_cleanup=args.skip_cleanup,
        )

        if not run_id:
            print("Failed to create pipeline run, cleaning up...")
            delete_workload(workload_name)
            return 1

        print(f"\n{'='*60}")
        print(f"RHAIIS Benchmark Submitted Successfully!")
        print(f"{'='*60}")
        print(f"Workload:    {workload_name}")
        print(f"Pipeline:    {run_id}")
        print(f"Deployment:  KServe InferenceService")
        print(f"Status:      Waiting for Kueue admission (visible in KFP UI)")
        print()
        print(f"View in KFP UI:")
        print(f"  {KFP_HOST}/")
        print()
        print(f"Monitor Kueue workload:")
        print(f"  oc get workload {workload_name} -n {BENCHMARK_NAMESPACE} -w")
        print()
        print(f"Manual cleanup (if needed):")
        print(f"  python benchmark_api.py cleanup --name {workload_name} --deployment-type inferenceservice")
        print()

        print(f"{'='*60}")
        print(f"Current Quota Usage")
        print(f"{'='*60}")
        print(get_quota_usage())

    elif args.command == "compile":
        try:
            from kfp import compiler
            from rhoai_benchmark_pipeline import rhoai_benchmark_pipeline

            compiler.Compiler().compile(
                pipeline_func=rhoai_benchmark_pipeline,
                package_path=args.output
            )
            print(f"Pipeline compiled to: {args.output}")
        except ImportError as e:
            print(f"Error: {e}")
            print("Install kfp with: pip install kfp")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
