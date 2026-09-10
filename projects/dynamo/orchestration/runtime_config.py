from __future__ import annotations

import ast
import copy
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any

import yaml

from projects.core.dsl.utils import slugify_identifier, truncate_k8s_name
from projects.core.library import config, env, run

logger = logging.getLogger(__name__)
RUNTIME_DIR = Path(__file__).resolve().parent
PROJECT_DIR = RUNTIME_DIR.parent
ORCHESTRATION_DIR = PROJECT_DIR / "orchestration"
CONFIG_DIR = ORCHESTRATION_DIR


@dataclass(frozen=True)
class RunSpec:
    model_name: str
    model_slug: str
    deployment_profile_name: str
    deployment_profile_slug: str
    namespace: str
    artifact_dirname: str
    namespace_is_managed: bool


def init() -> Path:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    env.init()
    run.init()
    if config.project is None:
        config.init(CONFIG_DIR)
    ensure_artifact_directories(env.ARTIFACT_DIR)
    return env.ARTIFACT_DIR


def ensure_artifact_directories(artifact_dir: Path) -> None:
    for relative in ("src", "artifacts", "artifacts/results"):
        (artifact_dir / relative).mkdir(parents=True, exist_ok=True)


def _get_runtime_value(key: str) -> Any:
    return config.project.get_config(f"runtime.{key}", None)


def _normalize_string_or_list(value: Any, field_name: str) -> list[str]:
    if value in (None, ""):
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or list of strings, got {type(value)}")

    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except (ValueError, SyntaxError):
            items = [item.strip() for item in value[1:-1].split(",")]
            return [item for item in items if item]

    return [value] if value else []


def _deep_merge(base: Any, override: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)

    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def get_config_dir() -> Path:
    return ORCHESTRATION_DIR


def get_job_name() -> str:
    job_name = _get_runtime_value("job_name")
    if job_name:
        return job_name

    preset_name = config.project.get_config("runtime.selected_preset")
    return f"local-{preset_name}"


def get_platform_config() -> dict[str, Any]:
    return normalize_platform_config(copy.deepcopy(config.project.get_config("platform")))


def get_dynamo_config() -> dict[str, Any]:
    """Get the Dynamo-specific platform configuration (Helm, operator, infra)."""
    platform = get_platform_config()
    return copy.deepcopy(platform["dynamo"])


def _derive_base_namespace() -> str:
    platform_data = get_platform_config()
    namespace_override = _get_runtime_value("namespace_override")
    namespace_config = platform_data["cluster"]["namespace"]
    default_namespace = namespace_config.get("name")

    if namespace_override:
        return namespace_override
    if default_namespace:
        return default_namespace

    return derive_namespace(
        get_job_name(),
        namespace_config["prefix"],
        namespace_config["max_length"],
    )


def get_namespace() -> str:
    return _derive_base_namespace()


_namespace_is_managed_override: bool | None = None


def get_namespace_is_managed() -> bool:
    if _namespace_is_managed_override is not None:
        return _namespace_is_managed_override

    namespace_override = _get_runtime_value("namespace_override")
    platform_data = get_platform_config()
    default_namespace = platform_data["cluster"]["namespace"].get("name")

    return namespace_override is None and default_namespace is None


def get_model_name() -> str:
    model_names = _normalize_string_or_list(_get_runtime_value("model_name"), "runtime.model_name")
    if len(model_names) != 1:
        raise ValueError(
            f"Expected exactly one runtime.model_name in the active dynamo run, got {model_names}"
        )
    return model_names[0]


def get_model_slug(model_name: str | None = None) -> str:
    return slugify_identifier(model_name or get_model_name(), max_length=32)


def get_model_uri(model_name: str | None = None) -> str:
    name = model_name or get_model_name()
    if name.startswith(("hf://", "oci://", "pvc://", "pvc+hf://")):
        return name
    return f"hf://{name}"


def get_served_model_name(model_name: str | None = None) -> str:
    return model_name or get_model_name()


def get_model_cache_config() -> dict[str, Any]:
    return copy.deepcopy(config.project.get_config("model_cache"))


def get_benchmark_tool() -> str | None:
    return _get_runtime_value("benchmark_tool")


def get_benchmark_key() -> str | None:
    return _get_runtime_value("benchmark_key")


def get_benchmark_keys() -> list[str]:
    return _normalize_string_or_list(_get_runtime_value("benchmark_key"), "runtime.benchmark_key")


def get_benchmark_job_names() -> list[str]:
    """Job names to clean up, based on benchmark_tool + benchmark_key."""
    tool = get_benchmark_tool()
    key = get_benchmark_key()
    if not tool or not key:
        return []

    if tool == "guidellm":
        bench = config.project.get_config(f"workloads.guidellm_benchmarks.{key}", {})
        name = bench.get("job_name", "guidellm-benchmark")
        return [name] if name else []
    elif tool == "aiperf":
        from projects.core.dsl.utils import slugify_identifier
        return [f"aiperf-{slugify_identifier(key, max_length=32)}"]
    return []


def get_deployment_profile_name() -> str:
    deployment_profiles = _normalize_string_or_list(
        _get_runtime_value("deployment_profile"),
        "runtime.deployment_profile",
    )
    if len(deployment_profiles) != 1:
        raise ValueError(
            "Expected exactly one runtime.deployment_profile in the active dynamo run, "
            f"got {deployment_profiles}"
        )
    return deployment_profiles[0]


def get_deployment_profile() -> dict[str, Any]:
    profile_name = get_deployment_profile_name()
    deployment_config = copy.deepcopy(config.project.get_config("deployments"))
    profile = copy.deepcopy(deployment_config[profile_name])
    defaults = copy.deepcopy(deployment_config.get("defaults", {}))

    return _deep_merge(defaults, profile)


def get_smoke_request() -> dict[str, Any]:
    smoke_request_key = config.project.get_config("runtime.smoke_request_key")
    return copy.deepcopy(config.project.get_config(f"workloads.smoke_requests.{smoke_request_key}"))


def get_run_specs() -> list[RunSpec]:
    model_names = _normalize_string_or_list(_get_runtime_value("model_name"), "runtime.model_name")
    profile_names = _normalize_string_or_list(
        _get_runtime_value("deployment_profile"),
        "runtime.deployment_profile",
    )

    if not model_names:
        raise ValueError("runtime.model_name must be set to a model name or list of model names")
    if not profile_names:
        raise ValueError(
            "runtime.deployment_profile must be set to a deployment profile or list of profiles"
        )

    combinations = list(product(model_names, profile_names))
    base_namespace = _derive_base_namespace()
    namespace_max_length = get_platform_config()["cluster"]["namespace"]["max_length"]
    namespace_is_managed = get_namespace_is_managed()
    run_specs: list[RunSpec] = []

    for model_name, profile_name in combinations:
        model_slug = get_model_slug(model_name)
        profile_slug = slugify_identifier(profile_name, max_length=24)
        if len(combinations) == 1:
            namespace = base_namespace
            artifact_dirname = "dynamo_run"
        else:
            namespace = truncate_k8s_name(
                f"{base_namespace}-{model_slug}-{profile_slug}",
                max_length=namespace_max_length,
            )
            artifact_dirname = f"dynamo_run_{profile_slug}_{model_slug}"

        run_specs.append(
            RunSpec(
                model_name=model_name,
                model_slug=model_slug,
                deployment_profile_name=profile_name,
                deployment_profile_slug=profile_slug,
                namespace=namespace,
                artifact_dirname=artifact_dirname,
                namespace_is_managed=namespace_is_managed,
            )
        )

    return run_specs


@contextmanager
def activate_run_spec(run_spec: RunSpec):
    global _namespace_is_managed_override

    saved = {
        "model_name": _get_runtime_value("model_name"),
        "deployment_profile": _get_runtime_value("deployment_profile"),
        "namespace_override": _get_runtime_value("namespace_override"),
    }
    prev_managed_override = _namespace_is_managed_override

    config.project.set_config("runtime.model_name", run_spec.model_name)
    config.project.set_config("runtime.deployment_profile", run_spec.deployment_profile_name)
    config.project.set_config("runtime.namespace_override", run_spec.namespace)
    _namespace_is_managed_override = run_spec.namespace_is_managed
    try:
        yield
    finally:
        config.project.set_config("runtime.model_name", saved["model_name"])
        config.project.set_config("runtime.deployment_profile", saved["deployment_profile"])
        config.project.set_config("runtime.namespace_override", saved["namespace_override"])
        _namespace_is_managed_override = prev_managed_override


def normalize_platform_config(platform_data: dict[str, Any]) -> dict[str, Any]:
    cluster = platform_data["cluster"]
    if "namespace" not in cluster:
        cluster["namespace"] = {
            "name": cluster.pop("namespace_name", None),
            "prefix": cluster.pop("namespace_prefix"),
            "max_length": cluster.pop("namespace_max_length"),
        }

    operators = platform_data["operators"]
    if isinstance(operators, list):
        platform_data["operators"] = {
            operator_spec["package"]: {
                key: value for key, value in operator_spec.items() if key != "package"
            }
            for operator_spec in operators
        }

    return platform_data


def derive_namespace(job_name: str, prefix: str, max_length: int) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", job_name.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        slug = "run"

    if slug.startswith(f"{prefix}-"):
        namespace = slug
    else:
        namespace = f"{prefix}-{slug}"

    namespace = namespace[:max_length].rstrip("-")
    if not namespace:
        raise ValueError(f"Could not derive a valid namespace from job name: {job_name}")
    return namespace


def version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(number) for number in numbers[:3])
