"""Tests for DynamoGraphDeployment manifest rendering."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from projects.dynamo.orchestration.render_graph_deployment import render_graph_deployment


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory with the manifest template."""
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()

    template = {
        "apiVersion": "nvidia.com/v1alpha1",
        "kind": "DynamoGraphDeployment",
        "metadata": {
            "name": "",
            "namespace": "",
            "labels": {
                "app.kubernetes.io/managed-by": "forge",
                "forge.openshift.io/project": "dynamo",
            },
        },
        "spec": {
            "backendFramework": "vllm",
            "pvcs": [{"name": "model-cache", "create": False}],
            "services": {},
        },
    }

    with (manifests_dir / "dynamo-graph-deployment.yaml").open("w") as fh:
        yaml.dump(template, fh)

    return tmp_path


@pytest.fixture
def base_profile() -> dict:
    return {
        "backend_framework": "vllm",
        "replicas": 2,
        "tensor_parallelism": 1,
        "serving_mode": "aggregated",
        "runtime_image": "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.2.1",
        "frontend_image": "nvcr.io/nvidia/ai-dynamo/dynamo-frontend:1.2.1",
        "vllm_args": ["--gpu-memory-utilization=0.90", "--enable-prefix-caching"],
        "env": {"DYN_STORE_KV": "mem"},
    }


@pytest.fixture
def model_cache() -> dict:
    return {"enabled": True, "pvc": {"name_prefix": "dynamo-model"}}


@pytest.fixture
def dynamo_config() -> dict:
    return {"helm": {"namespace": "dynamo-system"}}


def test_aggregated_manifest_has_correct_structure(
    config_dir, base_profile, model_cache, dynamo_config
):
    manifest = render_graph_deployment(
        config_dir=config_dir,
        namespace="test-ns",
        model_name="Qwen/Qwen3-0.6B",
        model_slug="qwen-qwen3-0-6b",
        deployment_profile=base_profile,
        model_cache=model_cache,
        dynamo_config=dynamo_config,
    )

    assert manifest["apiVersion"] == "nvidia.com/v1alpha1"
    assert manifest["kind"] == "DynamoGraphDeployment"
    assert manifest["metadata"]["name"] == "dynamo-qwen-qwen3-0-6b"
    assert manifest["metadata"]["namespace"] == "test-ns"
    assert manifest["spec"]["backendFramework"] == "vllm"

    services = manifest["spec"]["services"]
    assert "Epp" in services
    assert "VllmWorker" in services
    assert services["VllmWorker"]["replicas"] == 2
    assert services["VllmWorker"]["componentType"] == "worker"


def test_disaggregated_manifest_has_prefill_and_decode(
    config_dir, base_profile, model_cache, dynamo_config
):
    base_profile["serving_mode"] = "disaggregated"
    base_profile["prefill_replicas"] = 1
    base_profile["decode_replicas"] = 3

    manifest = render_graph_deployment(
        config_dir=config_dir,
        namespace="test-ns",
        model_name="Qwen/Qwen3-0.6B",
        model_slug="qwen-qwen3-0-6b",
        deployment_profile=base_profile,
        model_cache=model_cache,
        dynamo_config=dynamo_config,
    )

    services = manifest["spec"]["services"]
    assert "Epp" in services
    assert "VllmPrefillWorker" in services
    assert "VllmDecodeWorker" in services
    assert "VllmWorker" not in services

    assert services["VllmPrefillWorker"]["replicas"] == 1
    assert services["VllmDecodeWorker"]["replicas"] == 3


def test_epp_service_has_correct_config(
    config_dir, base_profile, model_cache, dynamo_config
):
    manifest = render_graph_deployment(
        config_dir=config_dir,
        namespace="test-ns",
        model_name="Qwen/Qwen3-0.6B",
        model_slug="qwen-qwen3-0-6b",
        deployment_profile=base_profile,
        model_cache=model_cache,
        dynamo_config=dynamo_config,
    )

    epp = manifest["spec"]["services"]["Epp"]
    assert epp["componentType"] == "epp"
    assert epp["replicas"] == 1
    assert "eppConfig" in epp

    env_vars = {
        e["name"]: e["value"]
        for e in epp["extraPodSpec"]["mainContainer"]["env"]
    }
    assert env_vars["DYN_MODEL_NAME"] == "Qwen/Qwen3-0.6B"
    assert env_vars["DYN_DECODE_FALLBACK"] == "true"


def test_gpu_resources_match_tensor_parallelism(
    config_dir, base_profile, model_cache, dynamo_config
):
    base_profile["tensor_parallelism"] = 4

    manifest = render_graph_deployment(
        config_dir=config_dir,
        namespace="test-ns",
        model_name="meta-llama/Llama-3-70B",
        model_slug="meta-llama-llama-3-70b",
        deployment_profile=base_profile,
        model_cache=model_cache,
        dynamo_config=dynamo_config,
    )

    worker = manifest["spec"]["services"]["VllmWorker"]
    assert worker["resources"]["limits"]["gpu"] == "4"
    assert worker["resources"]["requests"]["gpu"] == "4"


def test_forge_labels_present(config_dir, base_profile, model_cache, dynamo_config):
    manifest = render_graph_deployment(
        config_dir=config_dir,
        namespace="test-ns",
        model_name="Qwen/Qwen3-0.6B",
        model_slug="qwen-qwen3-0-6b",
        deployment_profile=base_profile,
        model_cache=model_cache,
        dynamo_config=dynamo_config,
    )

    labels = manifest["metadata"]["labels"]
    assert labels["app.kubernetes.io/managed-by"] == "forge"
    assert labels["forge.openshift.io/project"] == "dynamo"
