#!/usr/bin/env python3
"""
Batch scenario execution for topsail-ng benchmarks.

Usage:
    # New per-model matrix format
    python scenario_runner.py run --scenario config/scenarios.yaml
    python scenario_runner.py run --scenario config/scenarios.yaml --dry-run
    python scenario_runner.py run --scenario config/scenarios.yaml --filter model_id=openai/gpt-oss-120b
    python scenario_runner.py list --scenario config/scenarios.yaml

    # Legacy format (still supported)
    python scenario_runner.py run-legacy --scenario config/scenarios/rhaiis-3.2.3.yaml
"""

import argparse
import json
from pathlib import Path
from typing import List, Optional
import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.scenario_run import ScenarioRun
from core.platform import PlatformConfig
from orchestration.batch_orchestrator import BatchOrchestrator
from orchestration.run_organization import model_id_to_kfp_experiment
from scenario_generator.generator import ScenarioGenerator

CONFIG_DIR = Path(__file__).parent / "config"

# Load platform config (single source of truth for KFP host, etc.)
_platform_config = PlatformConfig.load(CONFIG_DIR / "platform.yaml")


def run_batch(
    scenario_path: Path,
    filter_str: Optional[str] = None,
    dry_run: bool = False,
    s3_bucket: str = "sagemaker-us-east-1-194365112018",
    cluster_override: Optional[str] = None,
    deployment_mode: Optional[str] = None,
    vllm_image: Optional[str] = None,
):
    """
    Run scenarios using the new BatchOrchestrator.

    Supports per-model matrix expansion with full resiliency.

    Args:
        scenario_path: Path to scenarios.yaml
        filter_str: Filter scenarios (e.g., "model_id=openai/gpt-oss-120b")
        dry_run: If True, don't actually submit to KFP
        s3_bucket: S3 bucket for artifacts
        cluster_override: Override target_cluster from YAML (CLI takes precedence)
        deployment_mode: Override deployment mode (rhoai/rhaiis)
        vllm_image: Override vLLM container image
    """
    orchestrator = BatchOrchestrator(
        config_path=scenario_path,
        s3_bucket=s3_bucket,
        dry_run=dry_run,
        cluster_override=cluster_override,
        deployment_mode=deployment_mode,
        vllm_image=vllm_image,
    )

    # Initialize batch
    batch = orchestrator.initialize_batch()

    # Apply filter if specified
    if filter_str:
        key, value = filter_str.split("=", 1)
        orchestrator.scenarios = [
            s for s in orchestrator.scenarios
            if getattr(s, key, None) == value
        ]
        # Renumber scenarios after filtering
        for i, scenario in enumerate(orchestrator.scenarios, 1):
            scenario.sequence_num = i
        batch.total_scenarios = len(orchestrator.scenarios)

    # Define submit function (integrates with existing benchmark_api)
    def submit_to_kfp(scenario: ScenarioRun) -> Optional[str]:
        """Submit scenario to KFP pipeline."""
        if dry_run:
            return f"dry-run-{scenario.scenario_uuid[:8]}"

        try:
            # Import here to avoid dependency issues
            from benchmark_api import submit_kfp_pipeline, create_workload

            # Create Kueue workload
            workload_name = f"{scenario.model_short}-{scenario.scenario_uuid[:8]}"

            # Get cluster config
            cluster_config = scenario.config.get("cluster_config", {})
            accelerator = cluster_config.get("gpu_type", "H200")
            namespace = cluster_config.get("namespace", "llm-d-bench")
            kubeconfig_secret = cluster_config.get("kubeconfig_secret", "h200-kubeconfig")

            if not create_workload(workload_name, accelerator, scenario.tensor_parallel):
                return None

            # Build pipeline args from scenario config
            runtime_args = scenario.config.get("runtime_args", {})
            workload_config = scenario.config.get("workload_config", {})

            # Convert runtime_args dict to comma-separated string
            vllm_args_list = []
            for k, v in runtime_args.items():
                if isinstance(v, bool):
                    if v:
                        vllm_args_list.append(f"--{k}")
                else:
                    vllm_args_list.append(f"--{k}={v}")
            vllm_args = ",".join(vllm_args_list)

            # Build guidellm_data from workload config
            input_tokens = workload_config.get("input_tokens", 1024)
            output_tokens = workload_config.get("output_tokens", 1024)
            guidellm_data = f"prompt_tokens={input_tokens},output_tokens={output_tokens}"

            # Submit to KFP (using existing API signature)
            run_id = submit_kfp_pipeline(
                workload_name=workload_name,
                model_name=scenario.model_id,
                tp=scenario.tensor_parallel,
                target_kubeconfig_secret=kubeconfig_secret,
                namespace=namespace,
                routing_mode=scenario.routing,
                vllm_args=vllm_args,
                guidellm_data=guidellm_data,
                guidellm_rate="1,50,100,200",
                guidellm_max_seconds="180",
                mlflow_enabled="true",
                accelerator=accelerator,
                version="RHOAI-3.2",
                kfp_run_name=scenario.scenario_id,  # e.g., qwen3-0-6b_balanced_direct_tp1
                batch_id=scenario.batch_id,
                scenario_id=scenario.scenario_id,
            )

            return run_id

        except ImportError:
            print("  WARNING: benchmark_api not available, skipping KFP submission")
            return f"mock-{scenario.scenario_uuid[:8]}"
        except Exception as e:
            print(f"  ERROR: {e}")
            return None

    # Execute batch (summary is printed by orchestrator)
    batch = orchestrator.execute(submit_fn=submit_to_kfp)

    # Return exit code based on results
    if batch.failed_count > 0:
        return 1
    return 0


def list_scenarios(scenario_path: Path):
    """List all scenarios from config file."""
    generator = ScenarioGenerator(config_path=scenario_path)
    generator.load()

    print(generator.summary())
    print()

    # Detailed list
    expanded = generator.expand()
    print(f"{'='*60}")
    print(f"Detailed Scenario List ({len(expanded)} total)")
    print(f"{'='*60}")
    print()

    # Group by model
    by_model = {}
    for s in expanded:
        by_model.setdefault(s.model_id, []).append(s)

    for model_id, scenarios in by_model.items():
        print(f"Model: {model_id}")
        print("-" * 40)
        for s in scenarios:
            print(f"  [{s.scenario_id}]")
            print(f"    Workload: {s.workload}")
            print(f"    Routing:  {s.routing}")
            print(f"    TP:       {s.tensor_parallel}")
            print()


def export_scenarios(scenario_path: Path, output_path: Path):
    """Export expanded scenarios to JSON."""
    generator = ScenarioGenerator(config_path=scenario_path)
    generator.load()
    expanded = generator.expand()

    data = [
        {
            "scenario_id": s.scenario_id,
            "model_id": s.model_id,
            "model_short": s.model_short,
            "workload": s.workload,
            "routing": s.routing,
            "tensor_parallel": s.tensor_parallel,
            "runtime_args": s.runtime_args,
        }
        for s in expanded
    ]

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Exported {len(data)} scenarios to {output_path}")


def list_batches(s3_bucket: str = "sagemaker-us-east-1-194365112018", limit: int = 10):
    """List recent batches from S3."""
    import subprocess

    s3_prefix = f"s3://{s3_bucket}/psap-benchmark-runs/"

    # List batch directories
    result = subprocess.run(
        ["aws", "s3", "ls", s3_prefix],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"Error listing batches: {result.stderr}")
        return

    # Parse directory listing
    batches = []
    for line in result.stdout.strip().split("\n"):
        if line.strip() and "PRE" in line:
            # Format: "                           PRE batch-20260314-203436/"
            parts = line.split()
            if len(parts) >= 2:
                batch_id = parts[-1].rstrip("/")
                if batch_id.startswith("batch-"):
                    batches.append(batch_id)

    # Sort by date (newest first) and limit
    batches.sort(reverse=True)
    batches = batches[:limit]

    print(f"{'='*60}")
    print(f"Recent Batches (last {limit})")
    print(f"{'='*60}")
    print()

    for batch_id in batches:
        # Try to get execution log for details
        log_path = f"{s3_prefix}{batch_id}/execution_log.json"
        log_result = subprocess.run(
            ["aws", "s3", "cp", log_path, "-"],
            capture_output=True, text=True
        )

        if log_result.returncode == 0:
            try:
                log_data = json.loads(log_result.stdout)
                scenarios = log_data.get("scenarios", [])
                completed = sum(1 for s in scenarios if s.get("status") == "completed")
                failed = sum(1 for s in scenarios if s.get("status") == "failed")
                total = len(scenarios)
                print(f"  {batch_id}")
                print(f"    Scenarios: {total} (✅ {completed}, ❌ {failed})")
            except json.JSONDecodeError:
                print(f"  {batch_id}")
        else:
            print(f"  {batch_id}")
        print()

    print(f"S3 Path: {s3_prefix}")


def delete_batch(
    batch_id: str,
    s3_bucket: str = "sagemaker-us-east-1-194365112018",
    dry_run: bool = False,
):
    """Delete a batch - terminate KFP runs and clean up."""
    import subprocess

    print(f"{'='*60}")
    print(f"Deleting Batch: {batch_id}")
    print(f"{'='*60}")
    print()

    s3_prefix = f"s3://{s3_bucket}/psap-benchmark-runs/{batch_id}"

    # Get execution log to find KFP run IDs
    log_path = f"{s3_prefix}/execution_log.json"
    result = subprocess.run(
        ["aws", "s3", "cp", log_path, "-"],
        capture_output=True, text=True
    )

    kfp_run_ids = []
    if result.returncode == 0:
        try:
            log_data = json.loads(result.stdout)
            scenarios = log_data.get("scenarios", [])
            for s in scenarios:
                if s.get("kfp_run_id"):
                    kfp_run_ids.append(s["kfp_run_id"])
            print(f"Found {len(kfp_run_ids)} KFP runs to terminate")
        except json.JSONDecodeError:
            print("Could not parse execution log")
    else:
        print(f"No execution log found at {log_path}")

    # Terminate KFP runs
    if kfp_run_ids:
        if dry_run:
            print(f"[DRY RUN] Would terminate {len(kfp_run_ids)} KFP runs")
        else:
            try:
                from kfp.client import Client
                client = Client(
                    host=_platform_config.kfp.host,
                    namespace=_platform_config.kfp.namespace,
                )
                for run_id in kfp_run_ids:
                    try:
                        client.terminate_run(run_id=run_id)
                        client.archive_run(run_id=run_id)
                        print(f"  ✅ Terminated & archived: {run_id}")
                    except Exception as e:
                        print(f"  ❌ Error with {run_id}: {e}")
            except ImportError:
                print("KFP client not available")

    # Delete S3 artifacts (optional - ask user)
    print()
    if dry_run:
        print(f"[DRY RUN] Would delete S3 artifacts at {s3_prefix}")
    else:
        print(f"S3 artifacts preserved at: {s3_prefix}")
        print("To delete: aws s3 rm --recursive {s3_prefix}")

    print()
    print("Done!")


def check_status(run_ids: str):
    """Check status of KFP runs by ID."""
    from kfp.client import Client

    KFP_HOST = _platform_config.kfp.host

    # Parse run IDs (comma-separated)
    ids = [rid.strip() for rid in run_ids.split(",") if rid.strip()]

    if not ids:
        print("No run IDs provided")
        return

    print(f"{'='*70}")
    print(f"KFP Run Status")
    print(f"{'='*70}")
    print()

    try:
        client = Client(host=KFP_HOST, namespace=_platform_config.kfp.namespace)

        for run_id in ids:
            try:
                run = client.get_run(run_id)
                status = run.state if hasattr(run, 'state') else 'unknown'
                name = run.display_name if hasattr(run, 'display_name') else run_id[:8]

                # Status emoji
                if status == "SUCCEEDED":
                    emoji = "✅"
                elif status == "FAILED":
                    emoji = "❌"
                elif status == "RUNNING":
                    emoji = "🔄"
                else:
                    emoji = "⏳"

                print(f"  {emoji} {name}")
                print(f"     ID:     {run_id}")
                print(f"     Status: {status}")
                if hasattr(run, 'error') and run.error:
                    print(f"     Error:  {run.error[:100]}")
                print()

            except Exception as e:
                print(f"  ❓ {run_id[:8]}")
                print(f"     Error: {e}")
                print()

        # Print links
        print(f"{'='*70}")
        print("KFP UI Links:")
        for run_id in ids:
            print(f"  {KFP_HOST}/#/runs/details/{run_id}")

    except Exception as e:
        print(f"Error connecting to KFP: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Batch scenario execution for topsail-ng benchmarks"
    )
    subparsers = parser.add_subparsers(dest="command")

    # Run command (new per-model matrix)
    run_parser = subparsers.add_parser("run", help="Run scenarios with BatchOrchestrator")
    run_parser.add_argument("--scenario", "-s", required=True, type=Path, help="Scenario YAML file")
    run_parser.add_argument("--filter", "-f", help="Filter scenarios (e.g., model_id=openai/gpt-oss-120b)")
    run_parser.add_argument("--cluster", "-c", help="Target cluster/SUT (overrides YAML config)")
    run_parser.add_argument("--dry-run", action="store_true", help="Print without executing")
    run_parser.add_argument("--s3-bucket", default="sagemaker-us-east-1-194365112018", help="S3 bucket for artifacts")
    run_parser.add_argument("--deployment-mode", "-m", choices=["rhoai", "rhaiis"], help="Deployment mode (default: from scenario config)")
    run_parser.add_argument("--vllm-image", help="Override vLLM container image")

    # List command
    list_parser = subparsers.add_parser("list", help="List all scenarios")
    list_parser.add_argument("--scenario", "-s", required=True, type=Path, help="Scenario YAML file")

    # Export command
    export_parser = subparsers.add_parser("export", help="Export scenarios to JSON")
    export_parser.add_argument("--scenario", "-s", required=True, type=Path, help="Scenario YAML file")
    export_parser.add_argument("--output", "-o", required=True, type=Path, help="Output JSON file")

    # Batches command - list recent batches
    batches_parser = subparsers.add_parser("batches", help="List recent batches from S3")
    batches_parser.add_argument("--s3-bucket", default="sagemaker-us-east-1-194365112018", help="S3 bucket")
    batches_parser.add_argument("--limit", "-n", type=int, default=10, help="Number of batches to show")

    # Delete command - cleanup a batch
    delete_parser = subparsers.add_parser("delete", help="Delete/cleanup a batch")
    delete_parser.add_argument("--batch", "-b", required=True, help="Batch ID to delete")
    delete_parser.add_argument("--s3-bucket", default="sagemaker-us-east-1-194365112018", help="S3 bucket")
    delete_parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted")

    # Status command - check KFP run status
    status_parser = subparsers.add_parser("status", help="Check KFP run status")
    status_parser.add_argument("--runs", "-r", required=True, help="Comma-separated KFP run IDs")

    args = parser.parse_args()

    if args.command == "run":
        exit_code = run_batch(
            args.scenario,
            args.filter,
            args.dry_run,
            args.s3_bucket,
            args.cluster,
            args.deployment_mode,
            args.vllm_image,
        )
        sys.exit(exit_code)
    elif args.command == "list":
        list_scenarios(args.scenario)
    elif args.command == "export":
        export_scenarios(args.scenario, args.output)
    elif args.command == "batches":
        list_batches(args.s3_bucket, args.limit)
    elif args.command == "delete":
        delete_batch(args.batch, args.s3_bucket, args.dry_run)
    elif args.command == "status":
        check_status(args.runs)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
