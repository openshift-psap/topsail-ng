#!/usr/bin/env python3
"""Diagnose OpenShift cluster suitability for CPU vLLM benchmarking.

Run before deploying to verify node resources, CPU instruction sets,
NUMA topology, CPU manager policy, and KServe CRD availability.

Optionally apply or remove rhaiis.io/* node labels for CPU scheduling
(see --apply-labels and --remove-labels).
"""

from __future__ import annotations

import json

from projects.core.dsl import (
    entrypoint,
    execute_tasks,
    shell,
    task,
)
from projects.core.dsl.utils.k8s import oc, oc_resource_exists
from projects.rhaiis.toolbox.diagnose_cpu_cluster.node_labels import (
    DEFAULT_BENCHMARK_NODE_SELECTOR,
    LABEL_CPU_BENCHMARK,
    compute_node_labels,
    count_benchmark_eligible_nodes,
    find_managed_labels_on_node,
    is_worker_node,
    parse_cpu_cores,
    parse_cpu_flags,
    select_benchmark_tier,
)


def _resolve_configured_cpu_images(
    rhaiis_cpu_image: str | None,
    vanilla_cpu_image: str | None,
) -> tuple[str, str]:
    """Load CPU serving images from project config when not passed explicitly."""
    if rhaiis_cpu_image and vanilla_cpu_image:
        return rhaiis_cpu_image, vanilla_cpu_image

    from pathlib import Path

    from projects.core.library import config, env

    config_dir = Path(env.FORGE_HOME) / "projects" / "rhaiis" / "orchestration"
    config.init(config_dir)
    return (
        rhaiis_cpu_image or config.project.get_config("rhaiis.images.cpu"),
        vanilla_cpu_image or config.project.get_config("rhaiis.images.cpu-vanilla"),
    )


@entrypoint
def run(
    *,
    apply_labels: bool = False,
    remove_labels: bool = False,
    dry_run: bool = False,
    strict: bool = False,
    min_benchmark_cpu: float = 8,
    workers_only: bool = True,
    rhaiis_cpu_image: str | None = None,
    vanilla_cpu_image: str | None = None,
):
    """Diagnose OpenShift cluster suitability for CPU vLLM benchmarking.

    Args:
        apply_labels: When True, apply rhaiis.io/* labels to worker nodes.
        remove_labels: When True, remove rhaiis.io/* CPU labels from worker nodes.
        dry_run: With apply_labels or remove_labels, print oc commands without running them.
        strict: When True, fail on missing KServe CRDs or zero benchmark-eligible nodes.
        min_benchmark_cpu: Minimum allocatable CPU cores for cpu-benchmark label.
        workers_only: Skip control-plane nodes for checks and labeling.
        rhaiis_cpu_image: Optional RHAIIS CPU image override for display.
        vanilla_cpu_image: Optional vanilla CPU image override for display.
    """
    if apply_labels and remove_labels:
        raise ValueError("Cannot use --apply-labels and --remove-labels together")
    return execute_tasks(locals())


def _skip_diagnose_checks(args) -> bool:
    """Skip slow oc debug checks when only removing labels."""
    return args.remove_labels and not args.apply_labels


def _remove_node_label_keys(node: str, keys: list[str], *, dry_run: bool) -> None:
    """Remove rhaiis.io label keys from a node."""
    if not keys:
        return
    remove_args = [f"{key}-" for key in sorted(keys)]
    cmd = f"oc label node {node} {' '.join(remove_args)}"
    if dry_run:
        print(f"  [dry-run] {cmd}")
        return
    result = oc("label", "node", node, *remove_args, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to remove labels from node {node}: {result.stderr or result.stdout}"
        )
    print(f"  {node}: removed {', '.join(sorted(keys))}")


@task
def show_node_resources(args, context):
    nodes_result = shell.run("oc get nodes -o json", check=False)
    context.nodes = []
    context.node_allocatable_cpu: dict[str, float] = {}
    context.node_labels: dict[str, dict[str, str]] = {}

    if nodes_result.returncode != 0:
        print(nodes_result.stderr or nodes_result.stdout)
        raise RuntimeError("Failed to list cluster nodes")

    payload = json.loads(nodes_result.stdout)
    rows = []
    for item in payload.get("items", []):
        name = item["metadata"]["name"]
        labels = item.get("metadata", {}).get("labels", {})
        if args.workers_only and not is_worker_node(labels):
            continue

        status = item.get("status", {})
        alloc = status.get("allocatable", {})
        cpu = alloc.get("cpu", "?")
        mem = alloc.get("memory", "?")
        ready = next(
            (
                c.get("type")
                for c in status.get("conditions", [])
                if c.get("type") == "Ready" and c.get("status") == "True"
            ),
            "NotReady",
        )
        rows.append(f"{name}\t{cpu}\t{mem}\t{ready}")
        context.nodes.append(name)
        context.node_labels[name] = labels
        if cpu != "?":
            context.node_allocatable_cpu[name] = parse_cpu_cores(cpu)

    print("NAME\tCPU\tMEM\tSTATUS")
    print("\n".join(rows))
    if args.strict and not context.nodes:
        raise RuntimeError("No eligible worker nodes found (control-plane/infra excluded)")
    return f"Node resources listed ({len(context.nodes)} worker node(s))"


@task
def check_cpu_instruction_sets(args, context):
    if _skip_diagnose_checks(args):
        context.node_features = {}
        return "CPU instruction set checks skipped (remove-labels only)"
    context.node_features: dict[str, dict] = {}
    for node in context.nodes:
        flags_result = shell.run(
            f"oc debug node/{node} -- chroot /host sh -c "
            "'grep -m1 flags /proc/cpuinfo 2>/dev/null'",
            check=False,
        )
        if flags_result.returncode != 0:
            print(
                f"  {node}: WARNING — oc debug failed "
                f"(rc={flags_result.returncode}); "
                f"CPU flags not detected. "
                f"{(flags_result.stderr or flags_result.stdout).strip()}"
            )
            context.node_features[node] = {"avx2": False, "avx512": False, "amx": False}
            continue
        stdout = flags_result.stdout
        flags_line = next(
            (line for line in stdout.splitlines() if "flags" in line),
            "",
        )
        features = parse_cpu_flags(flags_line)
        context.node_features[node] = features
        avx2 = "YES" if features["avx2"] else "no"
        avx512 = "YES" if features["avx512"] else "no"
        amx = "YES" if features["amx"] else "no"
        print(f"  {node}: AVX2={avx2}  AVX-512={avx512}  AMX={amx}")
    return f"CPU instruction sets checked for {len(context.nodes)} node(s)"


@task
def check_numa_topology(args, context):
    if _skip_diagnose_checks(args):
        return "NUMA topology checks skipped (remove-labels only)"
    for node in context.nodes:
        numa_result = shell.run(
            f"oc debug node/{node} -- chroot /host numactl --hardware",
            check=False,
        )
        available_line = next(
            (line for line in numa_result.stdout.splitlines() if line.startswith("available:")),
            "unknown",
        )
        print(f"  {node}: {available_line}")
    return f"NUMA topology checked for {len(context.nodes)} node(s)"


@task
def check_cpu_manager_policy(args, context):
    if _skip_diagnose_checks(args):
        return "CPU manager policy checks skipped (remove-labels only)"
    for node in context.nodes:
        state_result = shell.run(
            f"oc debug node/{node} -- chroot /host cat /var/lib/kubelet/cpu_manager_state",
            check=False,
        )
        policy = "unknown"
        if state_result.returncode != 0:
            print(
                f"  {node}: WARNING — oc debug failed "
                f"(rc={state_result.returncode}); "
                f"CPU manager policy not detected. "
                f"{(state_result.stderr or state_result.stdout).strip()}"
            )
        else:
            try:
                stdout = state_result.stdout
                start = stdout.find("{")
                end = stdout.rfind("}") + 1
                if start >= 0 and end > start:
                    policy = json.loads(stdout[start:end]).get("policyName", "unknown")
            except (json.JSONDecodeError, AttributeError, ValueError):
                print(
                    f"  {node}: WARNING — failed to parse cpu_manager_state; "
                    "policy reported as unknown"
                )
        static = policy == "static"
        context.node_features.setdefault(node, {})["cpu_manager_static"] = static
        print(f"  {node}: cpuManagerPolicy={policy}")
    return f"CPU manager policy checked for {len(context.nodes)} node(s)"


@task
def check_kserve_crds(args, context):
    if _skip_diagnose_checks(args):
        return "KServe CRD checks skipped (remove-labels only)"
    crds = [
        "inferenceservices.serving.kserve.io",
        "servingruntimes.serving.kserve.io",
    ]
    missing = []
    for crd in crds:
        status = "INSTALLED" if oc_resource_exists("crd", crd) else "MISSING"
        if status == "MISSING":
            missing.append(crd)
        print(f"  {crd}: {status}")
    if missing:
        context.missing_crds = missing
        if args.strict:
            raise RuntimeError(f"Missing required KServe CRDs: {', '.join(missing)}")
    return f"KServe CRDs: {len(crds) - len(missing)}/{len(crds)} installed"


@task
def validate_benchmark_scheduling(args, context):
    if _skip_diagnose_checks(args) or not args.strict:
        return "Benchmark scheduling validation skipped"

    eligible = count_benchmark_eligible_nodes(
        nodes=context.nodes,
        node_labels=context.node_labels,
        node_features=context.node_features,
        node_allocatable_cpu=context.node_allocatable_cpu,
        min_benchmark_cpu=args.min_benchmark_cpu,
    )
    print(
        f"  Benchmark-eligible worker nodes: {eligible}/{len(context.nodes)} "
        f"(selector {DEFAULT_BENCHMARK_NODE_SELECTOR})"
    )
    if eligible == 0:
        raise RuntimeError(
            f"No worker nodes match benchmark selector {DEFAULT_BENCHMARK_NODE_SELECTOR!r}. "
            f"Nodes need AVX2 and >={args.min_benchmark_cpu:g} allocatable CPU cores. "
            "Run with --apply-labels after fixing nodes, or lower --min-benchmark-cpu."
        )
    return f"Benchmark scheduling OK ({eligible} eligible node(s))"


@task
def apply_node_labels(args, context):
    if not args.apply_labels:
        print("  (skipped — pass --apply-labels to write rhaiis.io/* node labels)")
        return "Node labeling skipped (diagnose-only mode)"

    tier = select_benchmark_tier(
        node_features=context.node_features,
        node_allocatable_cpu=context.node_allocatable_cpu,
        min_benchmark_cpu=args.min_benchmark_cpu,
    )
    print(f"  Benchmark tier selected: {tier.upper()} (preference: AMX > AVX-512 > AVX2)")

    planned: list[tuple[str, dict[str, str]]] = []
    for node in context.nodes:
        features = context.node_features.get(node, {})
        labels = compute_node_labels(
            avx2=features.get("avx2", False),
            avx512=features.get("avx512", False),
            amx=features.get("amx", False),
            cpu_manager_static=features.get("cpu_manager_static", False),
            allocatable_cpu_cores=context.node_allocatable_cpu.get(node, 0),
            min_benchmark_cpu=args.min_benchmark_cpu,
            benchmark_tier=tier,
        )
        planned.append((node, labels))

    if not planned:
        raise RuntimeError("No worker nodes found to label")

    for node, labels in planned:
        # Reconcile: remove managed keys absent from newly computed labels.
        current_managed = find_managed_labels_on_node(context.node_labels.get(node, {}))
        keys_to_remove = sorted(k for k in current_managed if k not in labels)
        for key in keys_to_remove:
            context.node_labels[node].pop(key, None)

        _remove_node_label_keys(node, keys_to_remove, dry_run=args.dry_run)

        if not labels:
            print(f"  {node}: no rhaiis labels (missing AVX2 or insufficient CPU)")
            continue

        label_args = " ".join(f"{key}={value}" for key, value in sorted(labels.items()))
        cmd = f"oc label node {node} {label_args} --overwrite"
        if args.dry_run:
            print(f"  [dry-run] {cmd}")
            continue
        result = oc(
            "label",
            "node",
            node,
            *[f"{key}={value}" for key, value in sorted(labels.items())],
            "--overwrite",
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to label node {node}: {result.stderr or result.stdout}")
        print(f"  {node}: {', '.join(sorted(labels.keys()))}")

    mode = "dry-run" if args.dry_run else "applied"
    benchmark_nodes = sum(1 for _, labels in planned if labels.get(LABEL_CPU_BENCHMARK) == "true")
    print(
        f"\n  Benchmark selector {DEFAULT_BENCHMARK_NODE_SELECTOR} matches "
        f"{benchmark_nodes}/{len(planned)} worker node(s)"
    )
    return f"Node labels {mode} on {len(planned)} worker node(s)"


@task
def remove_node_labels(args, context):
    if not args.remove_labels:
        print("  (skipped — pass --remove-labels to clear rhaiis.io/* node labels)")
        return "Node label removal skipped"

    if not context.nodes:
        raise RuntimeError("No worker nodes found to clean labels from")

    removed_nodes = 0
    for node in context.nodes:
        keys = find_managed_labels_on_node(context.node_labels.get(node, {}))
        if not keys:
            print(f"  {node}: no rhaiis.io CPU labels present")
            continue
        remove_args = [f"{key}-" for key in sorted(keys)]
        cmd = f"oc label node {node} {' '.join(remove_args)}"
        if args.dry_run:
            print(f"  [dry-run] {cmd}")
            removed_nodes += 1
            continue
        result = oc("label", "node", node, *remove_args, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to remove labels from node {node}: {result.stderr or result.stdout}"
            )
        print(f"  {node}: removed {', '.join(sorted(keys))}")
        removed_nodes += 1

    mode = "dry-run" if args.dry_run else "removed"
    return f"Node labels {mode} on {removed_nodes}/{len(context.nodes)} worker node(s)"


@task
def show_cpu_images(args, context):
    if _skip_diagnose_checks(args):
        return "CPU image references skipped (remove-labels only)"
    rhaiis_image, vanilla_image = _resolve_configured_cpu_images(
        args.rhaiis_cpu_image,
        args.vanilla_cpu_image,
    )
    print(f"  RHAIIS:  {rhaiis_image}")
    print(f"  Vanilla: {vanilla_image}")
    print("  (listed for reference only — does not verify registry access or pull-secret validity)")
    print(f"  Deploy nodeSelector default for CPU presets: {DEFAULT_BENCHMARK_NODE_SELECTOR}")
    return "vLLM CPU image references listed"


if __name__ == "__main__":
    run.main()
