from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_servingruntime(
    *,
    deployment_name: str,
    namespace: str,
    model_id: str,
    serving_image: str,
    engine: str,
    engine_args: dict,
    engine_port: int,
    storage_source: str,
    storage_pvc: str = "",
    gpu_count: int,
    image_pull_secrets: list[str] | None = None,
    env_vars: dict | None = None,
    trtllm_config: dict | None = None,
) -> dict[str, Any]:
    """Build a KServe ServingRuntime manifest dict."""
    env_vars_list = [{"name": k, "value": str(v)} for k, v in (env_vars or {}).items()]

    manifest: dict[str, Any] = {
        "apiVersion": "serving.kserve.io/v1alpha1",
        "kind": "ServingRuntime",
        "metadata": {
            "annotations": {
                "opendatahub.io/template-display-name": f"ServingRuntime for {engine} | RHAIIS",
            },
            "labels": {"opendatahub.io/dashboard": "true"},
            "name": deployment_name,
            "namespace": namespace,
        },
        "spec": {
            "builtInAdapter": {"modelLoadingTimeoutMillis": 300000},
            "multiModel": False,
            "supportedModelFormats": [
                {"autoSelect": True, "name": _model_format_name(engine)},
            ],
        },
    }

    if image_pull_secrets:
        manifest["spec"]["imagePullSecrets"] = [{"name": s} for s in image_pull_secrets]

    if engine == "trtllm":
        container = _build_trtllm_container(
            model_id=model_id,
            serving_image=serving_image,
            engine_args=engine_args,
            engine_port=engine_port,
            env_vars_list=env_vars_list,
            trtllm_config=trtllm_config,
        )
        manifest["spec"]["volumes"] = [
            {"name": "shared-memory", "emptyDir": {"medium": "Memory", "sizeLimit": "128Gi"}},
        ]
    else:
        container = _build_vllm_sglang_container(
            model_id=model_id,
            serving_image=serving_image,
            engine=engine,
            engine_args=engine_args,
            engine_port=engine_port,
            storage_source=storage_source,
            storage_pvc=storage_pvc,
            gpu_count=gpu_count,
            env_vars_list=env_vars_list,
        )
        if gpu_count > 1:
            manifest["spec"]["volumes"] = [
                {"name": "shared-memory", "emptyDir": {"medium": "Memory", "sizeLimit": "64Gi"}},
            ]

    manifest["spec"]["containers"] = [container]
    return manifest


def build_inferenceservice(
    *,
    deployment_name: str,
    namespace: str,
    engine: str,
    engine_port: int,
    accelerator: str,
    gpu_count: int,
    replicas: int,
    cpu_request: str,
    memory_request: str,
    storage_source: str,
    storage_pvc: str,
    model_id: str,
    service_account_name: str = "",
    labels: dict | None = None,
    node_selector: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a KServe InferenceService manifest dict."""
    annotations: dict[str, str] = {
        "serving.kserve.io/deploymentMode": "RawDeployment",
        "storage.kserve.io/readonly": "false",
        "serving.kserve.io/enable-prometheus-scraping": "true",
        "prometheus.io/scrape": "true",
        "prometheus.io/path": "/metrics",
        "prometheus.io/port": str(engine_port),
    }

    metadata: dict[str, Any] = {
        "annotations": annotations,
        "name": deployment_name,
        "namespace": namespace,
    }

    if labels:
        meta_labels = dict(labels)
        # SGLang now supports Prometheus metrics via --enable-metrics flag (PR #196)
        # Only TRT-LLM needs to be excluded from OpenDataHub monitoring
        if engine == "trtllm":
            meta_labels["monitoring.opendatahub.io/scrape"] = "false"
        metadata["labels"] = meta_labels

    resources = _build_resources(
        accelerator=accelerator,
        gpu_count=gpu_count,
        cpu_request=cpu_request,
        memory_request=memory_request,
        engine=engine,
    )

    model_spec: dict[str, Any] = {
        "modelFormat": {"name": _model_format_name(engine)},
        "runtime": deployment_name,
        "resources": resources,
    }

    if storage_source == "hf" and storage_pvc:
        model_spec["storageUri"] = f"pvc://{storage_pvc}/"
    elif storage_source != "hf":
        model_spec["storageUri"] = f"{storage_source}://{model_id}"

    predictor: dict[str, Any] = {
        "minReplicas": replicas,
        "maxReplicas": replicas,
        "model": model_spec,
    }

    if engine == "trtllm":
        predictor["terminationGracePeriodSeconds"] = 120

    if service_account_name:
        predictor["serviceAccountName"] = service_account_name

    if node_selector:
        predictor["nodeSelector"] = dict(node_selector)

    return {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": metadata,
        "spec": {"predictor": predictor},
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _model_format_name(engine: str) -> str:
    if engine == "sglang":
        return "sglang"
    if engine == "trtllm":
        return "trtllm"
    return "vLLM"


def _build_resources(
    *,
    accelerator: str,
    gpu_count: int,
    cpu_request: str,
    memory_request: str,
    engine: str,
) -> dict[str, Any]:
    resources: dict[str, Any] = {}

    if accelerator == "cpu":
        resources["limits"] = {"cpu": cpu_request, "memory": memory_request}
        resources["requests"] = {"cpu": cpu_request, "memory": memory_request}
        return resources

    gpu_key = "nvidia.com/gpu" if accelerator == "nvidia" else "amd.com/gpu"

    if accelerator in ("nvidia", "amd"):
        limits: dict[str, str] = {gpu_key: str(gpu_count)}
        requests: dict[str, str] = {gpu_key: str(gpu_count)}

        if accelerator == "nvidia" and engine == "trtllm":
            limits["memory"] = memory_request
            limits["ephemeral-storage"] = "50Gi"
            requests["cpu"] = cpu_request
            requests["memory"] = memory_request
            requests["ephemeral-storage"] = "50Gi"

        resources["limits"] = limits
        resources["requests"] = requests

    if not (accelerator == "nvidia" and engine == "trtllm"):
        resources.setdefault("requests", {})
        resources["requests"]["cpu"] = cpu_request
        resources["requests"]["memory"] = memory_request

    return resources


def _build_vllm_sglang_container(
    *,
    model_id: str,
    serving_image: str,
    engine: str,
    engine_args: dict,
    engine_port: int,
    storage_source: str,
    storage_pvc: str = "",
    gpu_count: int,
    env_vars_list: list[dict],
) -> dict[str, Any]:
    if engine == "sglang":
        command = ["sglang", "serve"]
        if storage_source == "hf":
            args = [f"--model-path={model_id}", "--port=8080", "--host=0.0.0.0"]
        else:
            args = ["--model-path=/mnt/models", "--port=8080", "--host=0.0.0.0"]
    else:
        command = ["python3", "-m", "vllm.entrypoints.openai.api_server"]
        if storage_source == "hf":
            args = [f"--model={model_id}", "--port=8080"]
        else:
            args = ["--model=/mnt/models", f"--served-model-name={model_id}", "--port=8080"]

    for key, val in (engine_args or {}).items():
        if isinstance(val, bool):
            if val:
                args.append(f"--{key}")
        else:
            args.append(f"--{key}={val}")

    env: list[dict[str, Any]] = [
        {"name": "USER", "value": engine},
        {"name": "XDG_CACHE_HOME", "value": "/tmp/.cache"},
        {"name": "XDG_CONFIG_HOME", "value": "/tmp/.config"},
    ]

    if gpu_count > 1:
        env.append({"name": "NCCL_DEBUG", "value": "WARN"})

    if storage_source == "hf":
        # When a PVC is mounted at /mnt/models it acts as a persistent HF cache.
        # Without a PVC, KServe won't mount anything there, so fall back to /tmp.
        cache_root = "/mnt/models" if storage_pvc else "/tmp/.cache/huggingface"
        home = "/mnt/models" if storage_pvc else "/tmp"
        env.extend(
            [
                {"name": "HF_HUB_OFFLINE", "value": "0"},
                {"name": "HOME", "value": home},
                {"name": "HF_HOME", "value": cache_root},
            ]
        )
        if engine != "sglang":
            env.append({"name": "VLLM_CACHE_DIR", "value": f"{cache_root}/.cache/vllm"})
        env.extend(
            [
                {"name": "HF_DATASETS_CACHE", "value": f"{cache_root}/.cache/huggingface/datasets"},
                {
                    "name": "HF_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {"name": "storage-config", "key": "HF_TOKEN"},
                    },
                },
            ]
        )
    else:
        env.append({"name": "HOME", "value": "/tmp"})

    env.extend(env_vars_list)

    container: dict[str, Any] = {
        "name": "kserve-container",
        "image": serving_image,
        "command": command,
        "args": args,
        "env": env,
        "ports": [{"containerPort": engine_port, "protocol": "TCP"}],
    }

    if gpu_count > 1:
        container["volumeMounts"] = [{"name": "shared-memory", "mountPath": "/dev/shm"}]

    return container


def _build_trtllm_launch_script(
    model_id: str,
    engine_port: int,
    engine_args: dict,
    trtllm_config: dict | None,
) -> str:
    safe_model = model_id.replace("/", "--")
    parts = [
        "set -ex",
        f'if [ ! -d "/mnt/models/hub/models--{safe_model}/snapshots" ]; then',
        f"  hf download {model_id}",
        "fi",
        'python3 -c "',
        "from flashinfer.norm import get_norm_module",
        "get_norm_module()",
        "print('FlashInfer norm kernel pre-compiled successfully')",
        '"',
    ]

    if trtllm_config:
        import yaml

        config_yaml = yaml.dump(trtllm_config, default_flow_style=False).rstrip()
        parts.extend(["cat > /tmp/trtllm-config.yml << EOF", config_yaml, "EOF"])

    cmd = [f"trtllm-serve {model_id} \\", "  --host 0.0.0.0 \\", f"  --port={engine_port}"]

    for key, val in (engine_args or {}).items():
        if key == "trtllm-config":
            continue
        if isinstance(val, bool):
            if val:
                cmd.append(f"  --{key}")
        else:
            cmd.append(f"  --{key}={val}")

    if trtllm_config:
        cmd.append("  --extra_llm_api_options=/tmp/trtllm-config.yml")

    # Add line continuations between cmd parts (except last)
    cmd_lines = []
    for i, line in enumerate(cmd):
        if i < len(cmd) - 1 and not line.endswith("\\"):
            cmd_lines.append(f"{line} \\")
        else:
            cmd_lines.append(line)

    parts.extend(cmd_lines)
    return "\n".join(parts) + "\n"


def _build_trtllm_container(
    *,
    model_id: str,
    serving_image: str,
    engine_args: dict,
    engine_port: int,
    env_vars_list: list[dict],
    trtllm_config: dict | None,
) -> dict[str, Any]:
    script = _build_trtllm_launch_script(model_id, engine_port, engine_args, trtllm_config)

    env: list[dict[str, Any]] = [
        {"name": "HF_HUB_OFFLINE", "value": "0"},
        {"name": "HF_HOME", "value": "/mnt/models"},
        {
            "name": "HF_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": "storage-config", "key": "HF_TOKEN"}},
        },
        {"name": "TRTLLM_ENABLE_PDL", "value": "1"},
        {"name": "HOME", "value": "/tmp"},
        {"name": "XDG_CACHE_HOME", "value": "/tmp/.cache"},
        {"name": "FLASHINFER_WORKSPACE_BASE", "value": "/mnt/models"},
    ]
    env.extend(env_vars_list)

    return {
        "name": "kserve-container",
        "image": serving_image,
        "command": ["/bin/bash", "-c", script],
        "env": env,
        "securityContext": {"runAsUser": 0},
        "ports": [{"containerPort": engine_port, "protocol": "TCP"}],
        "volumeMounts": [{"name": "shared-memory", "mountPath": "/dev/shm"}],
    }
