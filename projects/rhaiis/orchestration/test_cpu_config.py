#!/usr/bin/env python3
"""Validation tests for CPU accelerator configuration.

Run with:
    PYTHONPATH=$PWD python projects/rhaiis/orchestration/test_cpu_config.py
"""

from __future__ import annotations

import sys

from projects.rhaiis.orchestration import runtime_config
from projects.rhaiis.orchestration.manifests import _build_resources


def _set(key: str, value) -> None:
    from projects.core.library import config

    config.project.set_config(key, value)


def test_vanilla_image_selection() -> None:
    _set("rhaiis.accelerator", "cpu")
    _set("rhaiis.cpu_flavor", "vanilla")
    image = runtime_config.get_serving_image("cpu")
    assert "vllm-openai-cpu" in image or "vanilla" in image, f"Expected vanilla image, got: {image}"
    print(f"  vanilla image: {image}  OK")


def test_rhaiis_image_selection() -> None:
    _set("rhaiis.accelerator", "cpu")
    _set("rhaiis.cpu_flavor", "rhaiis")
    image = runtime_config.get_serving_image("cpu")
    assert "rhaii" in image or "rhel9" in image, f"Expected RHAIIS image, got: {image}"
    print(f"  rhaiis image:  {image}  OK")


def test_ld_preload_only_on_rhaiis() -> None:
    _set("rhaiis.accelerator", "cpu")

    _set("rhaiis.cpu_flavor", "rhaiis")
    model = {"hf_model_id": "test/model"}
    env = runtime_config.merge_env_vars("cpu", model)
    assert "LD_PRELOAD" in env, "Expected LD_PRELOAD in rhaiis env vars"

    _set("rhaiis.cpu_flavor", "vanilla")
    env = runtime_config.merge_env_vars("cpu", model)
    assert "LD_PRELOAD" not in env, "LD_PRELOAD must not appear for vanilla flavor"
    print("  LD_PRELOAD flavor isolation  OK")


def test_tinyllama_max_model_len() -> None:
    _set("rhaiis.accelerator", "cpu")
    engine_args = runtime_config.get_engine_args("vllm")
    model = runtime_config.get_model("tinyllama-cpu")
    workload = runtime_config.get_workload("cpu-smoke")
    merged = runtime_config.merge_engine_args(engine_args, model, workload, "vllm")
    assert merged.get("max-model-len") == 2048, (
        f"tinyllama-cpu max-model-len should be 2048, got {merged.get('max-model-len')}"
    )
    print(f"  tinyllama max-model-len={merged['max-model-len']}  OK")


def test_memory_request_for_cpu() -> None:
    assert runtime_config.memory_request_for_cpu("8") == "32Gi"
    assert runtime_config.memory_request_for_cpu("16") == "64Gi"
    assert runtime_config.memory_request_for_cpu("32") == "128Gi"
    print("  memory_request_for_cpu  OK")


def test_cpu_engine_args_exclude_gpu_defaults() -> None:
    _set("rhaiis.accelerator", "cpu")
    engine_args = runtime_config.get_engine_args("vllm")
    assert "gpu-memory-utilization" not in engine_args, (
        f"GPU-only args must not bleed into CPU engine args: {engine_args}"
    )
    assert "trust-remote-code" not in engine_args, (
        f"trust-remote-code must come from model config only: {engine_args}"
    )

    from projects.core.library import config

    gpu_engine_args = dict(config.project.get_config("rhaiis.engines.vllm.args"))
    gpu_engine_args["tensor-parallel-size"] = 2
    _set("rhaiis.engines.vllm.args", gpu_engine_args)
    engine_args = runtime_config.get_engine_args("vllm")
    assert engine_args.get("tensor-parallel-size") == 2, (
        f"CLI tensor-parallel override should apply, got {engine_args}"
    )
    assert "gpu-memory-utilization" not in engine_args, (
        f"GPU-only args must not bleed into CPU engine args: {engine_args}"
    )
    print(f"  cpu engine args isolation  OK  {engine_args}")


def test_cpu_build_resources() -> None:
    resources = _build_resources(
        accelerator="cpu",
        gpu_count=0,
        cpu_request="16",
        memory_request="64Gi",
        engine="vllm",
    )
    limits = resources.get("limits", {})
    requests = resources.get("requests", {})
    assert limits.get("cpu") == "16", f"cpu limit wrong: {limits}"
    assert limits.get("memory") == "64Gi", f"memory limit wrong: {limits}"
    assert requests.get("cpu") == "16", f"cpu request wrong: {requests}"
    assert requests.get("memory") == "64Gi", f"memory request wrong: {requests}"
    assert "nvidia.com/gpu" not in limits, "GPU key must not appear in CPU resources"
    assert "amd.com/gpu" not in limits, "GPU key must not appear in CPU resources"
    print(f"  _build_resources limits={limits} requests={requests}  OK")


if __name__ == "__main__":
    runtime_config.init()
    failures: list[str] = []
    tests = [
        test_vanilla_image_selection,
        test_rhaiis_image_selection,
        test_ld_preload_only_on_rhaiis,
        test_tinyllama_max_model_len,
        test_memory_request_for_cpu,
        test_cpu_engine_args_exclude_gpu_defaults,
        test_cpu_build_resources,
    ]
    for t in tests:
        print(f"Running {t.__name__}…")
        try:
            t()
        except Exception as exc:
            print(f"  FAIL: {exc}")
            failures.append(t.__name__)

    if failures:
        print(f"\nFAILED: {failures}")
        sys.exit(1)
    print("\nAll CPU config tests passed.")
