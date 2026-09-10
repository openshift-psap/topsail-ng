from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def render_graph_deployment(
    *,
    config_dir: str | Path,
    namespace: str,
    model_name: str,
    model_slug: str,
    deployment_profile: dict[str, Any],
    model_cache: dict[str, Any],
    dynamo_config: dict[str, Any],
) -> dict[str, Any]:
    """Render a DynamoGraphDeployment manifest from config inputs."""
    template_path = Path(config_dir) / "manifests" / "dynamo-graph-deployment.yaml"
    manifest = _load_yaml(template_path)

    manifest["metadata"]["name"] = f"dynamo-{model_slug}"
    manifest["metadata"]["namespace"] = namespace
    manifest["metadata"].setdefault("labels", {})
    manifest["metadata"]["labels"].update({
        "app.kubernetes.io/managed-by": "forge",
        "forge.openshift.io/project": "dynamo",
    })

    p = deployment_profile
    backend = p.get("backend_framework", "vllm")
    manifest["spec"]["backendFramework"] = backend

    pvc_name = model_cache.get("pvc_name", "model-cache")
    manifest["spec"]["pvcs"] = [{"name": pvc_name, "create": False}]

    vllm_args = _build_vllm_args(p.get("vllm_args", []))
    if p.get("kv_transfer_config"):
        vllm_args.extend(["--kv-transfer-config", f"'{p['kv_transfer_config']}'"])

    hf_home = p.get("hf_home", "/opt/models")
    base_env = [
        {"name": "SERVED_MODEL_NAME", "value": model_name},
        {"name": "MODEL_PATH", "value": model_name},
        {"name": "HF_HOME", "value": hf_home},
    ]
    if p.get("kvbm_cpu_cache_gb"):
        base_env.append({"name": "DYN_KVBM_CPU_CACHE_GB", "value": str(p["kvbm_cpu_cache_gb"])})
    for key, value in p.get("env", {}).items():
        base_env.append({"name": key, "value": str(value)})

    ctx = _BuildCtx(
        profile=p, backend=backend, vllm_args=vllm_args, base_env=base_env,
        hf_home=hf_home, model_name=model_name, pvc_name=pvc_name,
        runtime_image=p["runtime_image"], frontend_image=p["frontend_image"],
        tp=p.get("tensor_parallelism", 1),
        kv_block_size=p.get("kv_block_size", "16"),
        router_mode=p.get("router_mode", "direct"),
        use_dra=p.get("use_dra_resources", False),
        dra_name=p.get("dra_resource_name", "dra.llm-d.io/gpu-nic-pair"),
    )

    serving_mode = p.get("serving_mode", "aggregated")
    services = _build_routing_service(ctx)
    if serving_mode == "disaggregated":
        services["VllmPrefillWorker"] = _build_worker(ctx, mode="prefill",
            replicas=p.get("prefill_replicas", 1))
        services["VllmDecodeWorker"] = _build_worker(ctx, mode="decode",
            replicas=p.get("decode_replicas", 2))
    else:
        services["VllmWorker"] = _build_worker(ctx, mode="aggregated",
            replicas=p.get("replicas", 1))

    manifest["spec"]["services"] = services
    return manifest


class _BuildCtx:
    """Carries resolved config through the builder functions."""
    __slots__ = (
        "profile", "backend", "vllm_args", "base_env", "hf_home", "model_name",
        "pvc_name", "runtime_image", "frontend_image", "tp", "kv_block_size",
        "router_mode", "use_dra", "dra_name",
    )

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _build_worker(ctx: _BuildCtx, *, mode: str, replicas: int) -> dict[str, Any]:
    disagg_flag = ""
    if mode in ("prefill", "decode"):
        disagg_flag = f" --disaggregation-mode {mode}"

    worker_cmd = (
        f"python3 -m dynamo.{ctx.backend} "
        f"--model $MODEL_PATH --served-model-name $SERVED_MODEL_NAME "
        f"--tensor-parallel-size {ctx.tp} --data-parallel-size 1 "
        + " ".join(ctx.vllm_args)
        + " --kv-events-config '{\"enable_kv_cache_events\":true}'"
        + f" --block-size {ctx.kv_block_size}"
        + disagg_flag
    )

    sidecar_mode = "direct" if ctx.router_mode == "kv" else ctx.router_mode

    return {
        "componentType": "worker",
        "volumeMounts": [{"name": ctx.pvc_name, "mountPoint": ctx.hf_home}],
        "sharedMemory": {"size": "2Gi"},
        "frontendSidecar": {
            "image": ctx.runtime_image,
            "args": ["-m", "dynamo.frontend", "--router-mode", sidecar_mode],
        },
        "extraPodSpec": {
            "mainContainer": {
                "env": copy.deepcopy(ctx.base_env),
                "args": [worker_cmd],
                "command": ["/bin/sh", "-c"],
                "image": ctx.runtime_image,
                "workingDir": f"/workspace/examples/backends/{ctx.backend}",
            },
        },
        "replicas": replicas,
        "resources": _build_gpu_resources(ctx.tp, use_dra=ctx.use_dra, dra_name=ctx.dra_name),
    }


def _build_routing_service(ctx: _BuildCtx) -> dict[str, Any]:
    if ctx.router_mode == "kv":
        router_args = ctx.profile.get("router_args", [])
        args = ["--router-mode", "kv", "--router-reset-states"] + router_args
        cpu = ctx.profile.get("frontend_cpu", "4")
        return {
            "Frontend": {
                "componentType": "frontend",
                "replicas": 1,
                "extraPodSpec": {
                    "mainContainer": {
                        "image": ctx.runtime_image,
                        "workingDir": "/workspace",
                        "command": ["python3", "-m", "dynamo.frontend"],
                        "args": args,
                        "env": [{"name": "HF_HOME", "value": ctx.hf_home}],
                    },
                },
                "resources": {"requests": {"cpu": cpu}, "limits": {"cpu": cpu}},
            },
        }
    return {"Epp": _build_epp_service(ctx)}


def _build_epp_service(ctx: _BuildCtx) -> dict[str, Any]:
    return {
        "componentType": "epp",
        "replicas": 1,
        "extraPodSpec": {
            "mainContainer": {
                "image": ctx.frontend_image,
                "env": [
                    {"name": "DYN_KV_CACHE_BLOCK_SIZE", "value": ctx.kv_block_size},
                    {"name": "DYN_MODEL_NAME", "value": ctx.model_name},
                    {"name": "DYN_DECODE_FALLBACK", "value": "true"},
                ],
            },
        },
        "eppConfig": {
            "config": {
                "plugins": [
                    {"type": "disagg-profile-handler"},
                    {"name": "decode-filter", "type": "label-filter", "parameters": {
                        "label": "nvidia.com/dynamo-sub-component-type",
                        "validValues": ["decode"], "allowsNoLabel": True,
                    }},
                    {"name": "picker", "type": "max-score-picker"},
                    {"name": "dyn-decode", "type": "dyn-decode-scorer"},
                ],
                "schedulingProfiles": [{
                    "name": "decode",
                    "plugins": [
                        {"pluginRef": "decode-filter", "weight": 1},
                        {"pluginRef": "dyn-decode", "weight": 1},
                        {"pluginRef": "picker", "weight": 1},
                    ],
                }],
            },
        },
    }


def _build_gpu_resources(tp: int, *, use_dra: bool, dra_name: str) -> dict[str, Any]:
    v = str(tp)
    if use_dra:
        return {"limits": {"custom": {dra_name: v}}, "requests": {"custom": {dra_name: v}}}
    return {"limits": {"gpu": v}, "requests": {"gpu": v}}


def _build_vllm_args(vllm_args: dict[str, Any] | list[str]) -> list[str]:
    if isinstance(vllm_args, list):
        return [str(arg) for arg in vllm_args]
    rendered = []
    for key, value in vllm_args.items():
        cli_key = key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                rendered.append(f"--{cli_key}")
            continue
        rendered.append(f"--{cli_key}={value}")
    return rendered
