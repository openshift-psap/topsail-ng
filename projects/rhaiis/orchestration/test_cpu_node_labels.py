#!/usr/bin/env python3
"""Unit tests for CPU node label helpers."""

from __future__ import annotations

import sys

from projects.rhaiis.orchestration.manifests import build_inferenceservice
from projects.rhaiis.toolbox.diagnose_cpu_cluster.node_labels import (
    LABEL_CPU_AMX,
    LABEL_CPU_AVX512,
    LABEL_CPU_BENCHMARK,
    LABEL_CPU_MANAGER_STATIC,
    LABEL_CPU_VLLM_CAPABLE,
    compute_node_labels,
    count_benchmark_eligible_nodes,
    find_managed_labels_on_node,
    is_worker_node,
    parse_cpu_cores,
    parse_cpu_flags,
    select_benchmark_tier,
)


def test_parse_cpu_flags() -> None:
    line = "flags : fpu vme de pse tsc msr pae mce cx8 apic sep mca cmov pat pse36 clflush mmx fxsr sse sse2 ss ht syscall nx pdpe1gb rdtscp lm constant_tsc rep_good nopl xtopology nonstop_tsc cpuid tsc_known_freq pni pclmulqdq ssse3 fma cx16 pcid sse4_1 sse4_2 x2apic movbe popcnt tsc_deadline_timer aes xsave avx f16c rdrand hypervisor lahf_lm abm 3dnowprefetch cpuid_fault invpcid_single ssbd ibrs ibpb stibp ibrs_enhanced fsgsbase tsc_adjust bmi1 avx2 smep bmi2 erms invpcid avx512f avx512dq rdseed adx smap avx512ifma clflushopt clwb avx512cd sha_ni avx512bw avx512vl xsaveopt xsavec xgetbv1 xsaves avx512vbmi umip pku ospke avx512_vbmi2 gfni vaes vpclmulqdq avx512_vnni avx512_bitalg avx512_vpopcntdq rdpid movdiri movdir64b fsrm avx512_vp2intersect md_clear flush_l1d arch_capabilities"
    flags = parse_cpu_flags(line)
    assert flags["avx2"] is True
    assert flags["avx512"] is True
    assert flags["amx"] is False
    print("  parse_cpu_flags  OK")


def test_parse_cpu_cores() -> None:
    assert parse_cpu_cores("16") == 16.0
    assert parse_cpu_cores("23500m") == 23.5
    print("  parse_cpu_cores  OK")


def test_is_worker_node() -> None:
    assert is_worker_node({"node-role.kubernetes.io/worker": ""}) is True
    assert is_worker_node({"node-role.kubernetes.io/control-plane": ""}) is False
    assert is_worker_node({"node-role.kubernetes.io/master": ""}) is False
    assert is_worker_node({"node-role.kubernetes.io/infra": ""}) is False
    print("  is_worker_node  OK")


def test_compute_node_labels() -> None:
    labels = compute_node_labels(
        avx2=True,
        avx512=True,
        amx=False,
        cpu_manager_static=True,
        allocatable_cpu_cores=16,
        min_benchmark_cpu=8,
    )
    assert labels[LABEL_CPU_VLLM_CAPABLE] == "true"
    assert labels[LABEL_CPU_AVX512] == "true"
    assert LABEL_CPU_AMX not in labels
    assert labels[LABEL_CPU_MANAGER_STATIC] == "true"
    assert labels[LABEL_CPU_BENCHMARK] == "true"
    print("  compute_node_labels (full)  OK")

    sparse = compute_node_labels(
        avx2=False,
        avx512=False,
        amx=False,
        cpu_manager_static=False,
        allocatable_cpu_cores=32,
        min_benchmark_cpu=8,
    )
    assert sparse == {}
    print("  compute_node_labels (empty)  OK")


def test_count_benchmark_eligible_nodes() -> None:
    nodes = ["worker-1", "worker-2", "worker-3"]
    node_labels = {
        "worker-1": {LABEL_CPU_BENCHMARK: "true"},
        "worker-2": {},
        "worker-3": {},
    }
    node_features = {
        # worker-1 is labeled and its features compute as eligible → counted
        "worker-1": {"avx2": True, "avx512": False, "amx": False, "cpu_manager_static": False},
        # worker-2 is capable but unlabeled → must be rejected
        "worker-2": {"avx2": True, "avx512": False, "amx": False, "cpu_manager_static": False},
        "worker-3": {"avx2": False, "avx512": False, "amx": False, "cpu_manager_static": False},
    }
    node_allocatable_cpu = {"worker-1": 16.0, "worker-2": 16.0, "worker-3": 32.0}
    assert (
        count_benchmark_eligible_nodes(
            nodes=nodes,
            node_labels=node_labels,
            node_features=node_features,
            node_allocatable_cpu=node_allocatable_cpu,
            min_benchmark_cpu=8,
        )
        == 1  # only worker-1: labeled + features eligible; worker-2 unlabeled → rejected
    )
    print("  count_benchmark_eligible_nodes  OK")


def test_find_managed_labels_on_node() -> None:
    present = find_managed_labels_on_node(
        {
            LABEL_CPU_BENCHMARK: "true",
            LABEL_CPU_AVX512: "true",
            "kubernetes.io/hostname": "worker-1",
        }
    )
    assert present == [LABEL_CPU_AVX512, LABEL_CPU_BENCHMARK]
    assert find_managed_labels_on_node({"kubernetes.io/hostname": "worker-1"}) == []
    print("  find_managed_labels_on_node  OK")


def test_select_benchmark_tier() -> None:
    amx_features = {"avx2": True, "avx512": True, "amx": True}
    avx512_features = {"avx2": True, "avx512": True, "amx": False}
    avx2_features = {"avx2": True, "avx512": False, "amx": False}
    cpu = {"node-a": 16.0, "node-b": 16.0, "node-c": 16.0}

    # AMX wins when present
    assert select_benchmark_tier({"node-a": amx_features, "node-b": avx512_features}, cpu) == "amx"
    # AVX-512 wins when no AMX
    assert (
        select_benchmark_tier({"node-a": avx512_features, "node-b": avx2_features}, cpu) == "avx512"
    )
    # AVX2 is the fallback
    assert select_benchmark_tier({"node-a": avx2_features}, cpu) == "avx2"
    # Nodes below min_benchmark_cpu are excluded from tier selection
    assert (
        select_benchmark_tier(
            {"node-a": amx_features, "node-b": avx2_features},
            {"node-a": 4.0, "node-b": 16.0},
            min_benchmark_cpu=8,
        )
        == "avx2"
    )
    print("  select_benchmark_tier  OK")


def test_compute_node_labels_with_tier() -> None:
    amx_node = dict(
        avx2=True, avx512=True, amx=True, cpu_manager_static=False, allocatable_cpu_cores=16
    )
    avx512_node = dict(
        avx2=True, avx512=True, amx=False, cpu_manager_static=False, allocatable_cpu_cores=16
    )
    avx2_node = dict(
        avx2=True, avx512=False, amx=False, cpu_manager_static=False, allocatable_cpu_cores=16
    )

    # AMX tier: only AMX nodes get cpu-benchmark
    assert compute_node_labels(**amx_node, benchmark_tier="amx").get(LABEL_CPU_BENCHMARK) == "true"
    assert LABEL_CPU_BENCHMARK not in compute_node_labels(**avx512_node, benchmark_tier="amx")
    assert LABEL_CPU_BENCHMARK not in compute_node_labels(**avx2_node, benchmark_tier="amx")

    # AVX-512 tier: only avx512 (non-AMX) nodes get cpu-benchmark
    assert LABEL_CPU_BENCHMARK not in compute_node_labels(**amx_node, benchmark_tier="avx512")
    assert (
        compute_node_labels(**avx512_node, benchmark_tier="avx512").get(LABEL_CPU_BENCHMARK)
        == "true"
    )
    assert LABEL_CPU_BENCHMARK not in compute_node_labels(**avx2_node, benchmark_tier="avx512")

    # None (default): all eligible nodes get cpu-benchmark
    assert compute_node_labels(**amx_node).get(LABEL_CPU_BENCHMARK) == "true"
    assert compute_node_labels(**avx512_node).get(LABEL_CPU_BENCHMARK) == "true"
    assert compute_node_labels(**avx2_node).get(LABEL_CPU_BENCHMARK) == "true"
    print("  compute_node_labels (tier-filtered)  OK")


def test_build_inferenceservice_node_selector() -> None:
    manifest = build_inferenceservice(
        deployment_name="tinyllama",
        namespace="test-ns",
        engine="vllm",
        engine_port=8080,
        accelerator="cpu",
        gpu_count=1,
        replicas=1,
        cpu_request="16",
        memory_request="64Gi",
        storage_source="hf",
        storage_pvc="model-pvc",
        model_id="TinyLlama/TinyLlama-1.1B-Chat-v1.0",
        node_selector={"rhaiis.io/cpu-benchmark": "true"},
    )
    assert manifest["spec"]["predictor"]["nodeSelector"] == {
        "rhaiis.io/cpu-benchmark": "true",
    }
    print("  build_inferenceservice nodeSelector  OK")


if __name__ == "__main__":
    failures: list[str] = []
    tests = [
        test_parse_cpu_flags,
        test_parse_cpu_cores,
        test_is_worker_node,
        test_compute_node_labels,
        test_compute_node_labels_with_tier,
        test_select_benchmark_tier,
        test_count_benchmark_eligible_nodes,
        test_find_managed_labels_on_node,
        test_build_inferenceservice_node_selector,
    ]
    for test in tests:
        print(f"Running {test.__name__}…")
        try:
            test()
        except Exception as exc:
            print(f"  FAIL: {exc}")
            failures.append(test.__name__)

    if failures:
        print(f"\nFAILED: {failures}")
        sys.exit(1)
    print("\nAll CPU node label tests passed.")
