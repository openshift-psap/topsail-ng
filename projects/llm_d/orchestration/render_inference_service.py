from __future__ import annotations

import copy
import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml

from projects.core.dsl.utils import slugify_identifier, truncate_k8s_name
from projects.core.library import config

logger = logging.getLogger(__name__)


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _is_efa_enabled() -> bool:
    """Check if EFA is enabled for PD deployments."""
    return config.project.get_config("deployments.pd.efa.enabled", default_value=False)


def _load_efa_config(config_dir: str | Path) -> dict[str, Any]:
    """Load EFA configuration from manifest file."""
    efa_config = config.project.get_config("deployments.pd.efa", default_value={})
    manifest_path = Path(config_dir) / efa_config["manifest"]

    if not manifest_path.exists():
        raise FileNotFoundError(f"EFA manifest not found at {manifest_path}")

    efa_manifest = _load_yaml(manifest_path)

    # Set the image from config
    efa_manifest["init_container"]["image"] = efa_config["image"]

    return efa_manifest


def _apply_efa_configuration(pod_template: dict[str, Any], efa_config: dict[str, Any]) -> None:
    """Apply EFA configuration to a pod template.

    Args:
        pod_template: Pod template to modify in-place
        efa_config: EFA configuration from manifest
    """
    logger.info("Applying EFA configuration to pod template")
    # Add init container
    if "initContainers" not in pod_template:
        pod_template["initContainers"] = []
    pod_template["initContainers"].append(copy.deepcopy(efa_config["init_container"]))

    # Add volumes
    if "volumes" not in pod_template:
        pod_template["volumes"] = []
    pod_template["volumes"].extend(copy.deepcopy(efa_config["volumes"]))

    # Add volume mounts to main container
    main_container = pod_template["containers"][0]
    if "volumeMounts" not in main_container:
        main_container["volumeMounts"] = []
    main_container["volumeMounts"].extend(copy.deepcopy(efa_config["volume_mounts"]))

    # Add EFA environment variables to main container
    if "env" in efa_config:
        if "env" not in main_container:
            main_container["env"] = []
        efa_env_vars = copy.deepcopy(efa_config["env"])
        main_container["env"].extend(efa_env_vars)
        logger.info(f"Applied EFA environment variables: {efa_env_vars}")
    else:
        logger.info("No EFA environment variables found in config")

    # Add security context for EFA
    main_container["securityContext"] = {"capabilities": {"add": ["IPC_LOCK"]}}
    logger.info("Applied EFA security context with IPC_LOCK capability")


def _replace_placeholders(value: Any, replacements: dict[str, str]) -> Any:
    """Replace Python-time deployment placeholders while preserving value types."""
    if isinstance(value, dict):
        return {key: _replace_placeholders(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if isinstance(value, str):
        for placeholder, replacement in replacements.items():
            value = value.replace(placeholder, replacement)
    return value


def _create_model_cache_spec(
    model_cache: dict[str, Any],
    source_uri: str,
    source_scheme: str,
    model_slug: str,
    namespace: str,
) -> dict[str, Any] | None:
    """Create model cache specification if caching is enabled and applicable."""
    if not model_cache.get("enabled", False) or source_uri.startswith(("pvc://", "pvc+hf://")):
        return None

    pvc_defaults = model_cache["pvc"]
    pvc_prefix = pvc_defaults["name_prefix"]
    cache_key = hashlib.sha256(source_uri.encode("utf-8")).hexdigest()[:10]
    pvc_name = truncate_k8s_name(
        f"{pvc_prefix}-{slugify_identifier(model_slug, max_length=32)}-{cache_key}"
    )
    model_path = pvc_defaults["model_directory_name"]

    return {
        "source_uri": source_uri,
        "source_scheme": source_scheme,
        "cache_key": cache_key,
        "namespace": namespace,
        "pvc_name": pvc_name,
        "pvc_size": pvc_defaults["size"],
        "access_mode": pvc_defaults["access_mode"],
        "storage_class_name": pvc_defaults.get("storage_class_name"),
        "model_path": model_path,
        "model_uri": f"pvc://{pvc_name}/{model_path}",
        "marker_filename": model_cache["marker_filename"],
        "marker_path": f"/cache/{model_path}/{model_cache['marker_filename']}",
        "download_job_name": truncate_k8s_name(f"{pvc_name}-download"),
        "hf_token_secret_name": model_cache["hf"].get("token_secret_name"),
        "hf_token_secret_key": model_cache["hf"].get("token_secret_key"),
    }


def render_inference_service_from_parts(
    *,
    config_dir: str | Path,
    namespace: str,
    inference_service: dict[str, Any],
    model_name: str,
    model_slug: str,
    deployment_profile: dict[str, Any],
    model_cache: dict[str, Any],
    deployment_profile_name: str | None = None,
    workload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render an llm_d-owned LLMInferenceService manifest from concrete runtime inputs."""
    template_path = Path(config_dir) / inference_service["template"]
    manifest = _load_yaml(template_path)

    # Check if this is a P/D deployment
    is_pd_deployment = "prefill" in deployment_profile

    name = inference_service["name"]
    if deployment_profile_name:
        name = f"{name}-{deployment_profile_name}"
    # Normalize name to be Kubernetes compliant and limit to 25 characters
    name = slugify_identifier(name)
    name = truncate_k8s_name(name, max_length=25)
    manifest["metadata"]["name"] = name
    manifest["metadata"]["namespace"] = namespace
    manifest["metadata"].setdefault("labels", {})
    manifest["metadata"]["labels"].update(
        config.project.get_config("deployments.defaults.labels") or {}
    )

    # Add deployment profile name as annotation for testing
    manifest["metadata"].setdefault("annotations", {})
    if deployment_profile_name:
        manifest["metadata"]["annotations"]["forge.openshift.io/deployment-profile"] = (
            deployment_profile_name
        )

    model_hostpath = config.project.get_config("runtime.model_hostpath", default_value=None)

    if model_name.startswith("oci://"):
        source_uri = model_name
        source_scheme = "oci"
    elif model_name.startswith("hf://"):
        source_uri = model_name
        source_scheme = "hf"
    else:
        source_uri = f"hf://{model_name}"
        source_scheme = "hf"

    if model_hostpath:
        cache_spec = None
    else:
        cache_spec = _create_model_cache_spec(
            model_cache=model_cache,
            source_uri=source_uri,
            source_scheme=source_scheme,
            model_slug=model_slug,
            namespace=namespace,
        )

    # These sentinels are resolved here because VLLM_ADDITIONAL_ARGS is opaque
    # shell input and cannot use controller-time Go-template substitutions.
    rendered_service_name = name
    deployment_profile = _replace_placeholders(
        deployment_profile,
        {
            "__INFERENCE_SERVICE_NAME__": rendered_service_name,
            "__MODEL_NAME__": model_slug,
            "__NAMESPACE__": namespace,
        },
    )
    manifest["metadata"]["annotations"].update(deployment_profile.get("annotations", {}))

    manifest["spec"]["model"]["uri"] = cache_spec["model_uri"] if cache_spec else source_uri
    manifest["spec"]["model"]["name"] = model_slug

    if is_pd_deployment:
        rendered_manifest = _render_pd_deployment(
            manifest, deployment_profile, deployment_profile_name, workload, config_dir
        )
    else:
        rendered_manifest = _render_standard_deployment(
            manifest, deployment_profile, deployment_profile_name, workload
        )

    # Apply Kueue configuration if enabled
    _apply_kueue_configuration(rendered_manifest, deployment_profile)

    # Apply hostpath model configuration if configured
    if model_hostpath:
        _apply_hostpath_model_configuration(rendered_manifest, model_hostpath, source_uri)

    # Apply image pull secrets if configured
    image_pull_secret = config.project.get_config(
        "platform.inference_service.image_pull_secret", default_value=None
    )
    if image_pull_secret:
        _apply_image_pull_secrets(rendered_manifest, image_pull_secret)

    return rendered_manifest


def handle_pd_resources(
    base_resources: dict[str, Any],
    deployment_profile: dict[str, Any],
    is_prefill: bool = False,
) -> None:
    """Handle P/D resource configuration for mutually exclusive networking technologies.

    Supports IB, RoCE, and EFA networking - only one can be enabled at a time.

    Args:
        base_resources: Base resources dict to modify in-place
        deployment_profile: Deployment profile configuration
        is_prefill: Whether this is for a prefill container
    """
    # Get networking configurations - they are mutually exclusive
    ib_config = config.project.get_config("deployments.pd.ib", default_value={})
    roce_config = config.project.get_config("deployments.pd.roce", default_value={})
    efa_config = config.project.get_config("deployments.pd.efa", default_value={})

    # Check which networking technology is enabled
    enabled_configs = []
    if ib_config.get("enabled", False):
        enabled_configs.append("ib")
    if roce_config.get("enabled", False):
        enabled_configs.append("roce")
    if efa_config.get("enabled", False):
        enabled_configs.append("efa")

    # Validate mutual exclusivity
    if len(enabled_configs) > 1:
        raise ValueError(
            f"Multiple networking technologies enabled: {enabled_configs}. "
            "IB, RoCE, and EFA are mutually exclusive."
        )

    if not enabled_configs:
        return  # No networking technology enabled

    networking_type = enabled_configs[0]

    if networking_type == "ib":
        # Add IB resources
        ib_resources = ib_config.get("resources", {})
        resource_dict = _normalize_resources(ib_resources, "IB")
        _apply_resources(base_resources, resource_dict)
        logger.info(f"Applied IB resources: {resource_dict}")

    elif networking_type == "roce":
        # Add RoCE resources
        roce_resources = roce_config.get("resources", {})
        resource_dict = _normalize_resources(roce_resources, "RoCE")
        _apply_resources(base_resources, resource_dict)
        logger.info(f"Applied RoCE resources: {resource_dict}")

    elif networking_type == "efa":
        # Add EFA resources with calculated count (1:4 GPU-to-EFA ratio)
        efa_resources = efa_config.get("resources", {})
        tensor_parallelism = deployment_profile.get("tensor_parallelism", 1)
        efa_count = 4 * tensor_parallelism

        if isinstance(efa_resources, dict):
            resource_dict = efa_resources.copy()
            # Override with calculated count for any EFA resource
            for key in resource_dict:
                if "efa" in key.lower():
                    resource_dict[key] = str(efa_count)
        elif isinstance(efa_resources, str):
            resource_dict = {efa_resources: str(efa_count)}
        else:
            logger.warning(f"Unexpected EFA resources format: {type(efa_resources)}")
            return

        _apply_resources(base_resources, resource_dict)
        logger.info(f"Applied EFA resources: {resource_dict} (TP={tensor_parallelism}, ratio=1:4)")


def _normalize_resources(resources: Any, technology: str) -> dict[str, str]:
    """Normalize resource specification to dictionary format."""
    if isinstance(resources, dict):
        return {k: str(v) for k, v in resources.items()}
    elif isinstance(resources, str):
        return {resources: "1"}
    else:
        logger.warning(f"Unexpected {technology} resources format: {type(resources)}")
        return {}


def _apply_resources(base_resources: dict[str, Any], resource_dict: dict[str, str]) -> None:
    """Apply resource dictionary to both requests and limits."""
    for bound in ("requests", "limits"):
        if bound not in base_resources:
            base_resources[bound] = {}
        base_resources[bound].update(resource_dict)


def _build_serving_resources(deployment_profile: dict[str, Any]) -> dict[str, Any]:
    tensor_parallelism = deployment_profile["tensor_parallelism"]
    profile_resources = deployment_profile.get("resources", {})
    rendered_resources: dict[str, Any] = {}

    for bound in ("requests", "limits"):
        source = profile_resources.get(bound, {})
        rendered_bound = {"nvidia.com/gpu": str(tensor_parallelism)}
        for resource_name in ("cpu", "memory"):
            value = source.get(resource_name)
            if value not in (None, ""):
                rendered_bound[resource_name] = value
        rendered_resources[bound] = rendered_bound

    return rendered_resources


def _build_vllm_args(vllm_args: dict[str, Any] | list[str]) -> list[str]:
    if isinstance(vllm_args, list):
        return [str(arg) for arg in vllm_args]

    rendered_args: list[str] = []
    for key, value in vllm_args.items():
        cli_key = key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                rendered_args.append(f"--{cli_key}")
            continue
        rendered_args.append(f"--{cli_key}={value}")
    return rendered_args


def _has_cli_arg(args: list[str], option_name: str) -> bool:
    prefix = f"--{option_name}="
    bare = f"--{option_name}"
    return any(arg == bare or arg.startswith(prefix) for arg in args)


def _build_vllm_additional_args(
    deployment_profile: dict[str, Any],
    workload: dict[str, Any] | None = None,
) -> str:
    """Build VLLM_ADDITIONAL_ARGS string based on deployment profile configuration.

    Args:
        deployment_profile: The deployment profile configuration
        workload: The workload configuration (merged benchmark config)

    Returns:
        String suitable for VLLM_ADDITIONAL_ARGS environment variable
    """

    vllm_extra = deployment_profile.get("vllm_extra", {})
    vllm_deploy_args = _build_vllm_args(vllm_extra.get("args", {}))

    # Add workload vllm_args if available
    if workload and "vllm_args" in workload:
        workload_vllm_args = _build_vllm_args(workload["vllm_args"])
        deployment_options = {arg.split("=", 1)[0] for arg in vllm_deploy_args}
        vllm_deploy_args.extend(
            arg for arg in workload_vllm_args if arg.split("=", 1)[0] not in deployment_options
        )

    return " ".join(vllm_deploy_args)


def _render_standard_deployment(
    manifest: dict[str, Any],
    deployment_profile: dict[str, Any],
    deployment_profile_name: str | None = None,
    workload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render standard (non-P/D) deployment configuration."""
    # Check if this is intelligent routing (scheduler_manifest exists)
    scheduler = deployment_profile.get("scheduler")
    has_scheduler_manifest = "scheduler_manifest" in deployment_profile

    name = f"llm-d-{deployment_profile_name}"
    name = slugify_identifier(name)
    name = truncate_k8s_name(name, max_length=25)
    manifest["metadata"]["name"] = name
    manifest["spec"]["replicas"] = deployment_profile["replicas"]
    manifest["spec"]["parallelism"] = {"tensor": deployment_profile["tensor_parallelism"]}

    serving_container = manifest["spec"]["template"]["containers"][0]
    serving_container["resources"] = _build_serving_resources(deployment_profile)

    # Set serving container image (deployment profile specific or default)
    serving_image = deployment_profile.get("serving_image")
    if not serving_image:
        # Fall back to defaults vllm_extra.image
        serving_image = config.project.get_config(
            "deployments.defaults.serving_image", default_value=None
        )
    if serving_image:
        serving_container["image"] = serving_image

    vllm_additional_args = _build_vllm_additional_args(deployment_profile, workload)

    # Add environment variable (don't set generic env vars or args)
    if "env" not in serving_container:
        serving_container["env"] = []

    serving_container["env"].append({"name": "VLLM_ADDITIONAL_ARGS", "value": vllm_additional_args})
    serving_container["env"].extend(
        copy.deepcopy(deployment_profile.get("vllm_extra", {}).get("env", []))
    )

    # Configure router/scheduler
    has_scheduler_key = "scheduler" in deployment_profile
    is_simple_deployment = not has_scheduler_key and not has_scheduler_manifest

    if is_simple_deployment:
        # Simple deployments (no scheduler key, no scheduler_manifest) have no router section at all
        manifest["spec"].pop("router", None)
    elif scheduler is None:
        # Some deployments might have router but no scheduler
        manifest["spec"]["router"].pop("scheduler", None)
    else:
        # Configure scheduler for intelligent routing
        manifest["spec"]["router"]["scheduler"] = copy.deepcopy(scheduler)

        # Set router container image (deployment profile specific or default)
        router_image = deployment_profile.get("router_image")
        if not router_image:
            # Fall back to defaults router_image
            router_image = config.project.get_config(
                "deployments.defaults.router_image", default_value=None
            )
        if router_image:
            manifest["spec"]["router"]["scheduler"]["template"]["containers"][0]["image"] = (
                router_image
            )

    return manifest


def _render_pd_deployment(
    manifest: dict[str, Any],
    deployment_profile: dict[str, Any],
    deployment_profile_name: str | None = None,
    workload: dict[str, Any] | None = None,
    config_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Render P/D (Prefill/Decode) deployment configuration."""
    from .runtime_config import get_decode_pod_count, get_prefill_pod_count

    # Set manifest name with deployment profile
    name = f"llm-d-{deployment_profile_name}"
    name = slugify_identifier(name)
    name = truncate_k8s_name(name, max_length=25)
    manifest["metadata"]["name"] = name

    # Configure prefill section
    manifest["spec"]["prefill"] = {
        "replicas": get_prefill_pod_count(),
        "parallelism": {"tensor": deployment_profile["prefill"]["tensor_parallelism"]},
        "template": _build_pd_pod_template(
            deployment_profile,
            deployment_profile_name,
            is_prefill=True,
            workload=workload,
            config_dir=config_dir,
        ),
    }

    # Configure main template (decode)
    manifest["spec"]["replicas"] = get_decode_pod_count()
    manifest["spec"]["parallelism"] = {"tensor": deployment_profile["decode"]["tensor_parallelism"]}
    manifest["spec"]["template"] = _build_pd_pod_template(
        deployment_profile,
        deployment_profile_name,
        is_prefill=False,
        workload=workload,
        config_dir=config_dir,
    )

    # Apply scheduler config (nodeSelector, tolerations, image, …) from deployment profile
    scheduler = deployment_profile.get("scheduler")
    if scheduler is not None:
        manifest["spec"]["router"]["scheduler"] = copy.deepcopy(scheduler)

    return manifest


def _build_pd_pod_template(
    deployment_profile: dict[str, Any],
    deployment_profile_name: str | None = None,
    is_prefill: bool = False,
    workload: dict[str, Any] | None = None,
    config_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build pod template for P/D deployment."""

    # Get P/D extra configuration from deployments config

    pd_vllm_extra = config.project.get_config("deployments.pd.vllm_extra")

    # Build VLLM args with correct tensor parallelism

    if is_prefill and deployment_profile_name:
        # For prefill pods, use prefill tensor parallelism
        from .runtime_config import _extract_value_from_profile_name

        try:
            prefill_tp = _extract_value_from_profile_name(
                deployment_profile_name, "prefill_tensor_parallelism"
            )
            # Create modified profile with prefill tensor parallelism
            prefill_profile = copy.deepcopy(deployment_profile)
            prefill_profile["tensor_parallelism"] = prefill_tp
            base_vllm_args = _build_vllm_additional_args(prefill_profile, workload)
        except ValueError:
            # Fallback to main tensor parallelism if extraction fails
            base_vllm_args = _build_vllm_additional_args(deployment_profile, workload)
    else:
        # For decode pods, use main tensor parallelism
        base_vllm_args = _build_vllm_additional_args(deployment_profile, workload)

    # Add P/D extra args
    pd_extra_args = pd_vllm_extra.get("args", [])

    # Handle kv_transfer_config
    kv_transfer_config = pd_vllm_extra.get("kv_transfer_config", {})
    logger.info(f"Base kv_transfer_config: {kv_transfer_config}")

    # Merge EFA-specific kv_transfer_config if EFA is enabled
    efa_enabled = _is_efa_enabled()
    logger.info(f"EFA enabled: {efa_enabled}, config_dir: {config_dir}")
    if efa_enabled and config_dir is not None:
        efa_pd_config = config.project.get_config("deployments.pd.efa", default_value={})
        efa_vllm_extra = efa_pd_config.get("vllm_extra", {})
        efa_kv_config = efa_vllm_extra.get("kv_transfer_config", {})
        logger.info(f"EFA kv_transfer_config: {efa_kv_config}")

        if efa_kv_config:
            # Simple merge: EFA config adds to base config
            kv_transfer_config = {**kv_transfer_config, **efa_kv_config}
            logger.info(f"Merged kv_transfer_config: {kv_transfer_config}")
        else:
            logger.info("No EFA kv_transfer_config found")

    # Add kv_transfer_config as argument if it exists
    kv_transfer_args = []
    if kv_transfer_config:
        import json

        kv_transfer_json = json.dumps(kv_transfer_config, separators=(",", ":"))
        kv_transfer_args = ["--kv-transfer-config", f"'{kv_transfer_json}'"]

    all_vllm_args = base_vllm_args.split() + pd_extra_args + kv_transfer_args

    vllm_additional_args = " ".join(all_vllm_args)

    # Build base environment variables
    base_env = [
        {"name": "VLLM_ADDITIONAL_ARGS", "value": vllm_additional_args},
    ]

    # Add P/D extra environment variables
    pd_extra_env = pd_vllm_extra.get("env", [])
    all_env = base_env + copy.deepcopy(pd_extra_env)

    # Add ROCE extra environment variables if enabled
    roce_config = config.project.get_config("deployments.pd.roce", default_value={})
    if roce_config.get("enabled", False):
        roce_extra_env = roce_config.get("vllm_extra", {}).get("env", [])
        if roce_extra_env:
            all_env.extend(copy.deepcopy(roce_extra_env))
            logger.info(f"Added ROCE environment variables: {roce_extra_env}")

    # Build base resources with correct tensor parallelism
    effective_profile = deployment_profile
    if is_prefill and deployment_profile_name:
        # For prefill pods, use prefill tensor parallelism
        from .runtime_config import _extract_value_from_profile_name

        try:
            prefill_tp = _extract_value_from_profile_name(
                deployment_profile_name, "prefill_tensor_parallelism"
            )
            # Create modified profile with prefill tensor parallelism
            prefill_profile = copy.deepcopy(deployment_profile)
            prefill_profile["tensor_parallelism"] = prefill_tp
            effective_profile = prefill_profile
            base_resources = _build_serving_resources(prefill_profile)
        except ValueError:
            # Fallback to main tensor parallelism if extraction fails
            base_resources = _build_serving_resources(deployment_profile)
    else:
        # For decode pods, use main tensor parallelism
        base_resources = _build_serving_resources(deployment_profile)

    # Handle P/D extra resources with the effective profile
    handle_pd_resources(base_resources, effective_profile, is_prefill)

    # Build container configuration
    container = {
        "name": "main",
        "resources": base_resources,
        "env": all_env,
    }

    # Set serving container image (deployment profile specific or default)
    serving_image = deployment_profile.get("serving_image")
    if not serving_image:
        # Fall back to defaults serving_image
        serving_image = config.project.get_config(
            "deployments.defaults.serving_image", default_value=None
        )
    if serving_image:
        container["image"] = serving_image

    # Build pod template with anti-affinity for P/D deployments
    component_type = "prefill" if is_prefill else "decode"
    opposite_component = "decode" if is_prefill else "prefill"

    pod_template = {
        "containers": [container],
        "metadata": {"labels": {"app.kubernetes.io/component": component_type}},
    }

    # Add anti-affinity to prevent prefill and decode pods from landing on the same node
    affinity = {
        "podAntiAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "weight": 100,
                    "podAffinityTerm": {
                        "labelSelector": {
                            "matchLabels": {
                                # Anti-affinity between prefill and decode pods
                                "app.kubernetes.io/component": opposite_component
                            }
                        },
                        "topologyKey": "kubernetes.io/hostname",
                    },
                }
            ]
        }
    }

    pod_template["affinity"] = affinity

    # Apply EFA configuration if enabled
    if _is_efa_enabled() and config_dir is not None:
        efa_config = _load_efa_config(config_dir)
        _apply_efa_configuration(pod_template, efa_config)

    # Add shared memory volume if configured
    shmem_size = config.project.get_config("deployments.pd.shmem.size", default_value=None)
    if shmem_size is not None:
        logger.info(f"Adding shared memory volume with size: {shmem_size}")

        # Add shared memory volume
        if "volumes" not in pod_template:
            pod_template["volumes"] = []

        shm_volume = {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": str(shmem_size)}}
        pod_template["volumes"].append(shm_volume)

        # Add volume mount to main container
        main_container = pod_template["containers"][0]
        if "volumeMounts" not in main_container:
            main_container["volumeMounts"] = []

        shm_mount = {"name": "shm", "mountPath": "/dev/shm"}
        main_container["volumeMounts"].append(shm_mount)

        logger.info("Applied shared memory volume and mount")

    return pod_template


def _calculate_total_gpu_usage(deployment_profile: dict[str, Any]) -> int:
    """Calculate the total GPU usage for a deployment profile.

    Args:
        deployment_profile: The deployment profile configuration

    Returns:
        Total number of GPUs required for this deployment
    """
    # Check if this is a P/D deployment (has prefill/decode sections)
    if "prefill" in deployment_profile and "decode" in deployment_profile:
        # P/D deployment: sum up prefill and decode GPU usage
        prefill_gpus = (
            deployment_profile["prefill"]["tensor_parallelism"]
            * deployment_profile["prefill"]["replicas"]
        )
        decode_gpus = (
            deployment_profile["decode"]["tensor_parallelism"]
            * deployment_profile["decode"]["replicas"]
        )
        return prefill_gpus + decode_gpus
    else:
        # Standard deployment: tensor_parallelism * replicas
        tensor_parallelism = deployment_profile.get("tensor_parallelism", 1)
        replicas = deployment_profile.get("replicas", 1)
        return tensor_parallelism * replicas


def _apply_hostpath_model_configuration(
    manifest: dict[str, Any],
    hostpath: str,
    source_uri: str,
) -> None:
    """Configure hostPath model loading with link-model init container.

    This approach:
    - disables the storage initializer
    - adds a link-model init container that creates symlinks from hostpath to /mnt/models
    - mounts the hostPath directory for the init container and main container

    Args:
        manifest: The rendered LLMInferenceService manifest to modify in-place.
        hostpath: Absolute path on the node where the model store lives
                  (e.g. /mnt/nvme/models).
        source_uri: The model URI (e.g. hf://model-name) used to determine model path.
    """
    # Disable storage initializer
    manifest["spec"]["storageInitializer"] = {"enabled": False}

    # Extract model name from URI
    if source_uri.startswith("hf://"):
        model_name = source_uri.removeprefix("hf://")
    else:
        model_name = source_uri

    # Load hostpath configuration from manifest
    config_dir = Path(__file__).parent
    hostpath_manifest_path = config_dir / "manifests" / "hostpath-model-config.yaml"
    hostpath_config = _load_yaml(hostpath_manifest_path)

    # Replace placeholders
    hostpath_config = _replace_placeholders(
        hostpath_config,
        {
            "__MODEL_NAME__": model_name,
            "__HOSTPATH__": hostpath,
        },
    )

    def _configure_pod_template(template: dict[str, Any]) -> None:
        # Add volumes
        template.setdefault("volumes", [])
        template["volumes"].extend(copy.deepcopy(hostpath_config["volumes"]))

        # Add init container
        template.setdefault("initContainers", [])
        template["initContainers"].append(copy.deepcopy(hostpath_config["init_container"]))

        # Add volume mounts to main container
        if not template.get("containers"):
            raise ValueError("No containers found in template for volume mount configuration")
        container = template["containers"][0]
        container.setdefault("volumeMounts", [])
        volume_mounts = hostpath_config.get("volume_mounts", [])
        if not volume_mounts:
            raise ValueError("No volume_mounts found in hostpath_config")
        container["volumeMounts"].extend(copy.deepcopy(volume_mounts))

    # Apply to main (decode) pod template
    _configure_pod_template(manifest["spec"]["template"])

    # Apply to prefill pod template for P/D deployments
    if "prefill" in manifest["spec"]:
        _configure_pod_template(manifest["spec"]["prefill"]["template"])


def _apply_image_pull_secrets(
    manifest: dict[str, Any],
    image_pull_secret: str,
) -> None:
    """Inject a single imagePullSecret into all pod templates: serving, prefill, and scheduler.

    Args:
        manifest: The rendered LLMInferenceService manifest to modify in-place.
        image_pull_secret: Secret name string.
    """
    normalized = [{"name": image_pull_secret}]

    def _inject(template: dict[str, Any]) -> None:
        template.setdefault("imagePullSecrets", [])
        template["imagePullSecrets"].extend(normalized)

    # Serving (decode) pod template
    _inject(manifest["spec"]["template"])

    # Prefill pod template (P/D deployments)
    if "prefill" in manifest["spec"]:
        _inject(manifest["spec"]["prefill"]["template"])

    # Scheduler (router) pod template
    if "router" in manifest["spec"] and "scheduler" in manifest["spec"]["router"]:
        scheduler_template = manifest["spec"]["router"]["scheduler"].setdefault("template", {})
        _inject(scheduler_template)


def _apply_kueue_configuration(
    manifest: dict[str, Any], deployment_profile: dict[str, Any]
) -> None:
    """Apply Kueue annotations and labels to the ISVC manifest.

    Based on the implementation from topsail's test_llmd.py.
    Can be enabled by setting runtime.kserve_use_kueue config.

    Args:
        manifest: The Kubernetes manifest to modify
        deployment_profile: The deployment profile configuration used to calculate GPU usage
    """
    # Check if kueue annotations should be enabled
    enable_kueue = config.project.get_config("runtime.kueue.enabled")

    if not enable_kueue:
        return

    # Calculate total GPU usage from deployment profile
    total_gpus = _calculate_total_gpu_usage(deployment_profile)

    # Check if we should skip Kueue due to high GPU usage
    disable_above_n_gpus = config.project.get_config("runtime.kueue.disable_above_n_gpus")
    if disable_above_n_gpus is not None and total_gpus > disable_above_n_gpus:
        logger.info(f"Skipping Kueue labels: {total_gpus} GPUs > {disable_above_n_gpus} threshold")
        return

    # Configure kueue settings
    queue_name = config.project.get_config("runtime.kueue.queue_name")
    kueue_config = {
        "enabled": True,
        "prefix": "kueue.x-k8s.io/",
        "labels": {"queue-name": queue_name},
        "annotations": {"queue-name": queue_name},
    }

    # Get prefix for kueue labels/annotations
    kueue_prefix = kueue_config.get("prefix", "kueue.x-k8s.io/")

    # Ensure metadata sections exist
    if "metadata" not in manifest:
        manifest["metadata"] = {}
    if "labels" not in manifest["metadata"]:
        manifest["metadata"]["labels"] = {}
    if "annotations" not in manifest["metadata"]:
        manifest["metadata"]["annotations"] = {}

    # Apply Kueue labels
    kueue_labels = kueue_config.get("labels", {})
    for label_key, label_value in kueue_labels.items():
        full_label_key = f"{kueue_prefix}{label_key}"
        manifest["metadata"]["labels"][full_label_key] = label_value

    # Apply Kueue annotations
    kueue_annotations = kueue_config.get("annotations", {})
    for annotation_key, annotation_value in kueue_annotations.items():
        full_annotation_key = f"{kueue_prefix}{annotation_key}"
        manifest["metadata"]["annotations"][full_annotation_key] = annotation_value

    # Apply Kueue annotations to router scheduler if it exists
    if (
        "spec" in manifest
        and "router" in manifest["spec"]
        and "scheduler" in manifest["spec"]["router"]
    ):
        scheduler = manifest["spec"]["router"]["scheduler"]

        # Ensure annotations and labels exist on scheduler
        if "annotations" not in scheduler:
            scheduler["annotations"] = {}
        if "labels" not in scheduler:
            scheduler["labels"] = {}

        # Apply the same Kueue annotations to the scheduler
        for annotation_key, annotation_value in kueue_annotations.items():
            full_annotation_key = f"{kueue_prefix}{annotation_key}"
            scheduler["annotations"][full_annotation_key] = annotation_value

        # Apply the same Kueue labels to the scheduler
        for label_key, label_value in kueue_labels.items():
            full_label_key = f"{kueue_prefix}{label_key}"
            scheduler["labels"][full_label_key] = label_value

    # Calculate pod group total count: 1 scheduler + number of replicas
    replicas = manifest.get("spec", {}).get("replicas", 1)

    # For P/D deployments, we need to account for prefill replicas too
    prefill_replicas = 0
    if "spec" in manifest and "prefill" in manifest["spec"]:
        prefill_replicas = manifest["spec"]["prefill"].get("replicas", 0)

    # Total: main replicas + prefill replicas + (1 scheduler if router exists)
    has_scheduler = (
        "spec" in manifest
        and "router" in manifest["spec"]
        and "scheduler" in manifest["spec"]["router"]
    )

    scheduler_count = 1 if has_scheduler else 0
    pod_group_total_count = replicas + prefill_replicas + scheduler_count

    manifest["metadata"]["annotations"][f"{kueue_prefix}pod-group-total-count"] = str(
        pod_group_total_count
    )

    # Add required pod-group-name label using the ISVC name
    pod_group_name = manifest["metadata"]["name"]
    manifest["metadata"]["labels"][f"{kueue_prefix}pod-group-name"] = pod_group_name

    # Also add required Kueue annotations/labels to scheduler if it exists
    if has_scheduler:
        scheduler = manifest["spec"]["router"]["scheduler"]

        # Ensure annotations and labels exist on scheduler
        if "annotations" not in scheduler:
            scheduler["annotations"] = {}
        if "labels" not in scheduler:
            scheduler["labels"] = {}

        # Add the same pod-group annotations and labels to scheduler
        scheduler["annotations"][f"{kueue_prefix}pod-group-total-count"] = str(
            pod_group_total_count
        )
        scheduler["labels"][f"{kueue_prefix}pod-group-name"] = pod_group_name
