from __future__ import annotations

import logging
import pathlib

from projects.core.library import config, env, run

logger = logging.getLogger(__name__)

CONFIG_DIR = pathlib.Path(__file__).resolve().parent
MEMORY_GIB_PER_CPU_CORE = 4

# Args shared between GPU and CPU vLLM builds; CLI overrides land under engines.<engine>.args.
_CPU_SHARED_ENGINE_ARG_KEYS = frozenset(
    {
        "tensor-parallel-size",
        "data-parallel-size",
    }
)


def memory_request_for_cpu(cpu_request: str) -> str:
    """Derive memory request from CPU request (4 GiB/core, matching CPU presets)."""
    return f"{int(cpu_request) * MEMORY_GIB_PER_CPU_CORE}Gi"


def init() -> None:
    env.init()
    run.init()
    config.init(CONFIG_DIR)


def get_namespace() -> str:
    return config.project.get_config("rhaiis.namespace")


def get_accelerator() -> str:
    return config.project.get_config("rhaiis.accelerator")


def get_cpu_flavor() -> str:
    return config.project.get_config("rhaiis.cpu_flavor", "vanilla")


def get_gpu_type(accelerator: str) -> str | None:
    gpu_types = config.project.get_config("rhaiis.gpu_types", None)
    if gpu_types and accelerator in gpu_types:
        return gpu_types[accelerator]
    return None


def get_deploy_config() -> dict:
    return dict(config.project.get_config("rhaiis.deploy"))


def get_benchmark_config() -> dict:
    return dict(config.project.get_config("benchmarks.guidellm"))


def get_engine() -> str:
    return config.project.get_config("rhaiis.engine", "vllm")


def get_serving_image(accelerator: str, engine: str | None = None) -> str:
    engine = engine or get_engine()
    if accelerator == "cpu":
        flavor = get_cpu_flavor()
        if flavor == "vanilla":
            return config.project.get_config("rhaiis.images.cpu-vanilla")
        return config.project.get_config("rhaiis.images.cpu")
    engine_image_key = f"rhaiis.engines.{engine}.images.{accelerator}"
    try:
        return config.project.get_config(engine_image_key)
    except KeyError:
        logger.debug(
            "Engine-scoped image missing at %s; falling back to rhaiis.images.%s",
            engine_image_key,
            accelerator,
        )
        return config.project.get_config(f"rhaiis.images.{accelerator}")


def get_engine_args(engine: str | None = None) -> dict:
    engine = engine or get_engine()
    if get_accelerator() == "cpu":
        cpu_args = config.project.get_config("rhaiis.vllm_args_cpu", None)
        args = dict(cpu_args) if cpu_args else {}
        # CLI overrides (e.g. --tensor-parallel) land under engines.<engine>.args.
        engine_overrides = config.project.get_config(f"rhaiis.engines.{engine}.args", None)
        if engine_overrides:
            for key in _CPU_SHARED_ENGINE_ARG_KEYS:
                if key in engine_overrides:
                    args[key] = engine_overrides[key]
        return args
    return dict(config.project.get_config(f"rhaiis.engines.{engine}.args", {}))


def get_engine_port(engine: str | None = None) -> int:
    engine = engine or get_engine()
    return int(config.project.get_config(f"rhaiis.engines.{engine}.port", 8080))


def get_trtllm_config() -> dict:
    return dict(config.project.get_config("rhaiis.engines.trtllm.trtllm_config", {}))


def get_model(model_key: str) -> dict:
    return dict(config.project.get_config(f"models.{model_key}"))


def get_workload(workload_key: str) -> dict:
    return dict(config.project.get_config(f"workloads.{workload_key}"))


def get_vaults() -> list[str]:
    return config.project.get_config("vaults")


def get_test_model_key() -> str:
    return config.project.get_config("tests.rhaiis.model_key")


def get_test_workload_key() -> str:
    return config.project.get_config("tests.rhaiis.workload_key")


def get_profiler_config() -> dict:
    return dict(config.project.get_config("rhaiis.profiler", {}) or {})


_COMMON_ARG_TRANSLATIONS: dict[str, dict[str, str]] = {
    "sglang": {
        "tensor-parallel-size": "tp-size",
        "data-parallel-size": "dp-size",
    },
    "trtllm": {
        "tensor-parallel-size": "tp_size",
        "data-parallel-size": "dp_size",
    },
}


def _translate_args(args: dict, engine: str) -> dict:
    """Translate vLLM-style args to another engine's arg naming.

    Only shared args (TP, DP) are translated; vLLM-specific args are dropped.
    """
    mapping = _COMMON_ARG_TRANSLATIONS.get(engine, {})
    translated = {}
    for key, val in args.items():
        if key in mapping:
            translated[mapping[key]] = val
        # Drop vLLM-only args that have no equivalent
    return translated


def _resolve_boolean_flag_conflicts(args: dict) -> dict:
    """If both "no-<flag>" and "<flag>" are set, drop the earlier one.

    A config override can add one without clearing the other's default.
    Keeping only the later-set key matches argparse's last-flag-wins
    behavior for these paired CLI flags.
    """
    keys_in_order = list(args.keys())
    losers = set()
    for key in keys_in_order:
        if not key.startswith("no-"):
            continue
        positive_key = key[len("no-") :]
        if positive_key not in args:
            continue
        earlier_key = (
            key if keys_in_order.index(key) < keys_in_order.index(positive_key) else positive_key
        )
        losers.add(earlier_key)
    return {key: value for key, value in args.items() if key not in losers}


def merge_engine_args(
    overrides: dict,
    model: dict,
    workload: dict,
    engine: str | None = None,
) -> dict:
    engine = engine or get_engine()
    engine_key = f"{engine}_args"

    # Use engine-specific block if present, otherwise fall back to vllm_args
    # and auto-translate common args when the engine isn't vLLM
    model_args = model.get(engine_key) if engine != "vllm" else None
    if model_args is None:
        base = dict(model.get("vllm_args", {}))
        if engine != "vllm":
            base = _translate_args(base, engine)
    else:
        base = dict(model_args)

    wl_args = workload.get(engine_key) if engine != "vllm" else None
    if wl_args is None:
        wl = dict(workload.get("vllm_args", {}))
        if engine != "vllm":
            wl = _translate_args(wl, engine)
    else:
        wl = dict(wl_args)

    base.update(wl)
    base.update(overrides)
    return _resolve_boolean_flag_conflicts(base)


def merge_env_vars(accelerator: str, model: dict) -> dict:
    base = dict(config.project.get_config("rhaiis.env_vars") or {})
    base.update(model.get("env_vars", {}))
    if accelerator == "cpu":
        for key in ("cpu", f"cpu-{get_cpu_flavor()}"):
            accel_vars = config.project.get_config(f"rhaiis.accelerator_env_vars.{key}") or {}
            base.update(accel_vars)
        return base
    accel_vars = config.project.get_config(f"rhaiis.accelerator_env_vars.{accelerator}") or {}
    base.update(accel_vars)
    return base


def _format_arg_value(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


def build_guidellm_args(
    *,
    benchmark_cfg: dict,
    model_id: str,
    data: str,
    rates: list[int],
    max_seconds: int,
    rampup: int | None = None,
) -> list[str]:
    guidellm_args = []
    for key, value in benchmark_cfg.get("args", {}).items():
        cli_key = key.replace("_", "-")
        guidellm_args.append(f"--{cli_key}={_format_arg_value(value)}")

    guidellm_args.append(f"--model={model_id}")
    guidellm_args.append(f"--data={data}")
    guidellm_args.append(f"--rate={_format_arg_value(rates)}")
    guidellm_args.append(f"--max-seconds={max_seconds}")
    if rampup is not None:
        guidellm_args.append(f"--rampup={rampup}")
    return guidellm_args


def split_image_tag(full_image: str) -> tuple[str, str]:
    if ":" in full_image:
        parts = full_image.rsplit(":", 1)
        return parts[0], parts[1]
    return full_image, "latest"


def derive_deployment_name(hf_model_id: str) -> str:
    parts = hf_model_id.split("/")
    return (parts[1] if len(parts) > 1 else hf_model_id).lower().replace(".", "-")
