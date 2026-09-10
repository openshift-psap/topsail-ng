from __future__ import annotations

import logging
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from projects.caliper.engine.constants import METADATA_FILE
from projects.cluster.toolbox.capture_prometheus.main import run as capture_prometheus
from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME
from projects.core.dsl import shell
from projects.core.dsl.utils import slugify_identifier
from projects.core.dsl.utils.k8s import oc
from projects.core.library import config, env
from projects.core.library.postprocess import run_and_postprocess, write_test_labels
from projects.core.library.run import SignalInterrupt
from projects.core.orchestration.utils.k8s import ensure_namespace
from projects.guidellm.toolbox.run_guidellm_benchmark import build_guidellm_args
from projects.guidellm.toolbox.run_guidellm_benchmark import main as run_guidellm_benchmark_command
from projects.guidellm.toolbox.run_smoke_request import main as run_smoke_request_command
from projects.kserve.toolbox.capture_llmisvc_state import main as capture_llmisvc_state
from projects.kserve.toolbox.deploy_llmisvc import main as deploy_llmisvc
from projects.kserve.toolbox.wait_kserve_ready import main as wait_kserve_ready
from projects.llm_d.orchestration import runtime_config
from projects.llm_d.orchestration.prepare_phase import prepare_model_cache
from projects.llm_d.orchestration.render_inference_service import (
    render_inference_service_from_parts,
)
from projects.llm_d.orchestration.utils import write_yaml
from projects.llm_d.toolbox.cleanup_test_resources import main as cleanup_test_resources_command

logger = logging.getLogger(__name__)


def _delete_resources_by_type(resource_type: str, namespace: str, description: str) -> None:
    """Delete all resources of a given type in the namespace.

    Args:
        resource_type: Kubernetes resource type (e.g., 'llminferenceservice', 'workload')
        namespace: Target namespace
        description: Human-readable description for logging
    """
    logger.info("Deleting all %s in namespace %s", description, namespace)

    result = oc(
        "get",
        resource_type,
        "-n",
        namespace,
        "--no-headers",
        "-o",
        "name",
        check=False,
    )

    if result.returncode == 0 and result.stdout.strip():
        resource_names = result.stdout.strip().split("\n")
        logger.info(
            "Found %d %s to delete: %s",
            len(resource_names),
            description,
            ", ".join(resource_names),
        )

        # Delete all found resources
        for resource_name in resource_names:
            logger.info("Deleting %s", resource_name)
            oc("delete", resource_name, "-n", namespace, check=False)

        logger.info("Successfully deleted all existing %s", description)
    else:
        logger.info("No existing %s found in namespace %s", description, namespace)


def cleanup_existing_resources(namespace: str) -> None:
    """Delete all existing LLMInferenceServices and Kueue workloads if configured.

    Args:
        namespace: Target namespace for cleanup
    """
    delete_all_on_start = config.project.get_config("runtime.kserve.delete_all_on_start", False)
    reuse_existing = config.project.get_config("runtime.kserve.reuse_existing", False)

    if not delete_all_on_start:
        return

    if reuse_existing:
        logger.info("Skipping cleanup - reuse_existing is enabled")
        return

    logger.info("Starting cleanup of existing resources in namespace %s", namespace)

    try:
        # Delete LLMInferenceServices
        _delete_resources_by_type("llminferenceservice", namespace, "LLMInferenceServices")

        # Also delete Kueue workloads if Kueue is enabled
        enable_kueue = config.project.get_config("runtime.kueue.enabled", False)
        if enable_kueue:
            _delete_resources_by_type("workload", namespace, "Kueue workloads")

    except Exception as e:
        logger.warning("Failed to delete existing resources: %s, continuing with test", e)


def ensure_kueue_local_queue() -> None:
    """Create LocalQueue when kueue is enabled."""
    enable_kueue = config.project.get_config("runtime.kueue.enabled")
    if not enable_kueue:
        return

    queue_name = config.project.get_config("runtime.kueue.queue_name")
    manifest_path = config.project.get_config("runtime.kueue.local_queue_manifest")
    namespace = runtime_config.get_namespace()

    logger.info("Creating LocalQueue: %s", queue_name)

    # Read and parse the YAML template
    config_dir = runtime_config.get_config_dir()
    template_file = config_dir / manifest_path

    with template_file.open(encoding="utf-8") as f:
        local_queue_manifest = yaml.safe_load(f)

    # Update the fields
    local_queue_manifest["metadata"]["name"] = queue_name
    local_queue_manifest["metadata"]["namespace"] = namespace
    local_queue_manifest["spec"]["clusterQueue"] = queue_name

    # Write manifest to manifests directory and apply
    manifests_dir = env.ARTIFACT_DIR / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = manifests_dir / f"{queue_name}-localqueue.yaml"

    write_yaml(manifest_file, local_queue_manifest)

    # Apply the manifest
    oc("apply", "-f", str(manifest_file))
    logger.info("LocalQueue %s created successfully", queue_name)


def extract_kpi_labels_from_config() -> dict[str, str]:
    """Extract kpi_labels from project configuration.

    Returns:
        Dictionary of kpi_labels for system context
    """
    kpi_labels = {}

    kpi_labels.update(config.project.get_config("cpt.kpi.labels"))

    for k, v in list(kpi_labels.items()):
        if v is None:
            del kpi_labels[k]

    product_version = config.project.get_config("cpt.kpi.labels.product_version")
    if product_version:
        kpi_labels["product_version"] = product_version

    model_name = runtime_config.get_model_name()
    if model_name:
        kpi_labels["model_name"] = model_name

    platform = config.project.get_config("cpt.kpi.labels.platform")
    if product_version:
        kpi_labels["platform"] = platform

    return kpi_labels


def get_iso_timestamp() -> str:
    """Get current timestamp in ISO format with Z timezone."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def create_test_labels(
    mlflow_destination: dict[str, str] | None = None,
) -> None:
    """Create caliper metadata file with model name, guidellm configuration, and test start time."""

    model_name = runtime_config.get_model_name()
    deployment_profile = runtime_config.get_deployment_profile_name()
    benchmark_keys = runtime_config.get_benchmark_keys()

    labels = {
        "model_name": model_name,
        "deployment_profile": deployment_profile,
    }

    if benchmark_keys:
        labels["guidellm_loadshape"] = benchmark_keys[0]

    # Extract kpi_labels from config
    kpi_labels = extract_kpi_labels_from_config()

    # Create initial timing structure with test start
    timing = {"test": {"start": get_iso_timestamp()}}

    write_test_labels(
        env.ARTIFACT_DIR,
        labels,
        kpi_labels=kpi_labels if kpi_labels else None,
        mlflow_destination=mlflow_destination,
        timing=timing,
    )
    logger.info("Created test labels with start time: %s", labels)

    # Dump config.project to config.yaml
    config_path = env.ARTIFACT_DIR / "config.yaml"
    try:
        with config_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(config.project.config, f, sort_keys=False)
        logger.info("Saved project configuration to: %s", config_path)
    except Exception as e:
        logger.warning("Failed to save project configuration: %s", e)

    # Copy fournos_fjob.yaml if available
    fournos_source = env.ARTIFACT_DIR / CI_METADATA_DIRNAME / "fournos_fjob.yaml"
    if fournos_source.exists():
        fournos_dest = env.ARTIFACT_DIR / "fournos_fjob.yaml"
        try:
            shutil.copy2(fournos_source, fournos_dest)
            logger.info("Copied fournos job file: %s -> %s", fournos_source, fournos_dest)
        except Exception as e:
            logger.warning("Failed to copy fournos job file: %s", e)
    else:
        logger.debug("No fournos job file found at: %s", fournos_source)


def update_test_labels_with_timing(timing_section: str, timing_event: str) -> datetime:
    """Update caliper metadata file with timing information.

    Args:
        timing_section: Section name (e.g., 'benchmark', 'test')
        timing_event: Event name (e.g., 'start', 'end')
    """
    timestamp_dt = datetime.now(UTC)
    timestamp = timestamp_dt.isoformat().replace("+00:00", "Z")

    test_labels_path = env.ARTIFACT_DIR / METADATA_FILE

    if not test_labels_path.exists():
        logging.error("Caliper metadata file not found ...")
        return datetime.now(UTC)

    # Read existing labels
    with test_labels_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Ensure timing section exists
    if "timing" not in data:
        data["timing"] = {}

    if timing_section not in data["timing"]:
        data["timing"][timing_section] = {}

    # Add the timing event
    data["timing"][timing_section][timing_event] = timestamp

    # Write updated labels
    with test_labels_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)

    logger.info(
        "Updated test labels with timing: %s.%s = %s", timing_section, timing_event, timestamp
    )

    return timestamp_dt


def update_test_labels_with_status(success: bool, message: str) -> None:
    """Update caliper metadata file with test execution status and end time.

    Args:
        success: True if test succeeded, False if failed
        message: Status message describing the result
    """
    test_labels_path = env.ARTIFACT_DIR / METADATA_FILE

    # Read existing labels
    if not test_labels_path.exists():
        logging.error("Caliper metadata file not found ...")
        return

    with test_labels_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Add completion information as separate top-level field
    data["completion"] = {
        "success": success,
        "message": message,
    }

    # Add test end timing
    test_end_time = get_iso_timestamp()
    if "timing" not in data:
        data["timing"] = {}
    if "test" not in data["timing"]:
        data["timing"]["test"] = {}

    data["timing"]["test"]["end"] = test_end_time

    # Write updated labels
    with test_labels_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)

    logger.info(
        "Updated test labels with completion status and end time: success=%s, message=%s",
        success,
        message,
    )

    if not success:
        (test_labels_path.parent / "FAILURE.txt").write_text(message)


def run_all_tests(stop_on_error: bool = False) -> int:
    """Run tests for all run specifications without post-processing.

    Args:
        stop_on_error: If True, stop on the first test failure

    Returns:
        Maximum exit code from all tests
    """
    from projects.llm_d.orchestration import runtime_config

    max_exit_code = 0
    for run_spec in runtime_config.get_run_specs():
        with runtime_config.activate_run_spec(run_spec):
            with env.NextArtifactDir(run_spec.artifact_dirname):
                try:
                    exit_code = do_test()
                    max_exit_code = max(max_exit_code, exit_code)

                    if exit_code != 0 and stop_on_error:
                        logger.error(
                            f"Test failed with exit code {exit_code}, stopping due to stop_on_error"
                        )
                        return exit_code
                except Exception as e:
                    logger.exception(f"Test failed with exception: {e}")
                    # Note: Status update already handled by do_test() exception handler
                    max_exit_code = 1
                    if stop_on_error:
                        logger.error("Stopping due to stop_on_error")
                        return 1

    return max_exit_code


def run() -> int:
    """Main test function that wraps do_test() with outcome postprocessing."""

    dry_run = config.project.get_config("runtime.kserve.dry_run", False)
    if dry_run:
        ret = do_test()
        logger.info("Kserve dry-run mode enabled - Skipping caliper post-processing")
        return ret

    return run_and_postprocess(do_test)


def run_finalizers(
    endpoint_url: str | None,
    llmisvc_name: str | None,
    primary_exc: tuple[type[BaseException], BaseException, Any] | None,
    finalizer_exc: tuple[type[BaseException], BaseException, Any] | None,
) -> tuple[type[BaseException], BaseException, Any] | None:
    def _run_finalizer(
        description: str,
        callback,
        **kwargs,
    ):
        try:
            # with MuteStdOut(reason=f"Finalizer: {description}"):
            callback(**kwargs)
        except Exception:
            if primary_exc is None:
                logger.exception("Finalizer failed: %s", description)
                return finalizer_exc or sys.exc_info()
            logger.exception("Ignoring %s failure after primary test failure", description)
        return finalizer_exc

    namespace = runtime_config.get_namespace()
    platform = runtime_config.get_platform_config()
    capture_namespace_events = platform["artifacts"]["capture_namespace_events"]

    # Only capture service state if we have the llmisvc_name
    if llmisvc_name:
        finalizer_exc = _run_finalizer(
            "capturing inference-service state",
            capture_inference_service_state,
            llmisvc_name=llmisvc_name,
        )
    else:
        logging.warning("No llmisvc name received, cannot capture the llmisvc state")

    finalizer_exc = _run_finalizer(
        "writing endpoint URL",
        write_endpoint_url,
        artifact_dir=env.ARTIFACT_DIR,
        endpoint_url=endpoint_url,
    )
    finalizer_exc = _run_finalizer(
        "capturing namespace events",
        capture_namespace_events_after_test,
        artifact_dir=env.ARTIFACT_DIR,
        namespace=namespace,
        capture_namespace_events=capture_namespace_events,
    )

    finalizer_exc = _run_finalizer(
        "cleaning up runtime resources",
        cleanup_test_resources,
        llmisvc_name=llmisvc_name or "llmisvc-name-not-available",
    )

    return primary_exc, finalizer_exc


def do_test() -> int:
    # Load minimal config needed for orchestration flow

    namespace = runtime_config.get_namespace()
    dry_run = config.project.get_config("runtime.kserve.dry_run", False)

    if not dry_run:
        # Ensure namespace exists before starting any deployments
        ensure_namespace(
            namespace, labels=config.project.get_config("platform.cluster.namespace.labels")
        )

        # Ensure LocalQueue exists when kueue is enabled
        ensure_kueue_local_queue()

        # Delete all existing resources if configured
        cleanup_existing_resources(namespace)

    try:
        from projects.caliper.orchestration.export import precreate_mlflow_run_if_configured

        mlflow_destination = precreate_mlflow_run_if_configured()
    except Exception:
        logger.error("MLflow run pre-creation failed; continuing", exc_info=True)
        mlflow_destination = None

    endpoint_url: str | None = None
    primary_exc: tuple[type[BaseException], BaseException, Any] | None = None
    finalizer_exc: tuple[type[BaseException], BaseException, Any] | None = None

    actual_llmisvc_name = "llmisvc-na-not-computed"
    try:
        # Create test labels with actual model and profile information
        create_test_labels(mlflow_destination=mlflow_destination)

        # Generate the LLMInferenceService name before deployment
        # so we have it available even if deployment fails
        from projects.core.dsl.utils import slugify_identifier

        platform = runtime_config.get_platform_config()
        inference_service = platform["inference_service"]
        base_name = inference_service["name"]
        deployment_profile_name = runtime_config.get_deployment_profile_name()
        # Step 1: Build manifest and get actual truncated name
        initial_llmisvc_name = (
            f"{base_name}-{deployment_profile_name}" if deployment_profile_name else base_name
        )
        initial_llmisvc_name = slugify_identifier(initial_llmisvc_name)

        manifest_path, actual_llmisvc_name = build_inference_service_manifest(initial_llmisvc_name)

        # Step 2: Deploy using the actual name from the manifest
        endpoint_url = deploy_inference_service_from_manifest(manifest_path, actual_llmisvc_name)

        if dry_run:
            logging.warning("Running in dry-run mode, skipping the rest of the test steps")
            update_test_labels_with_status(True, "Dry-run completed successfully")
            return 0

        if not endpoint_url:
            raise ValueError("Failed to extract the endpoint_url from the LLMISVC deployment")

        run_smoke_request(endpoint_url=endpoint_url)

        run_guidellm_benchmark(endpoint_url=endpoint_url)
    except Exception as e:
        primary_exc = sys.exc_info()
        update_test_labels_with_status(False, f"Test failed with exception: {str(e)}")
        logger.exception("Test failed with exception")
    except SignalInterrupt as e:
        primary_exc = sys.exc_info()
        update_test_labels_with_status(False, f"Test interrupted: {str(e)}")
        logger.error("Test interrupted")
    finally:
        do_finalizers = config.project.get_config("runtime.run_test_finalizers")
        if primary_exc and isinstance(primary_exc[1], SignalInterrupt):
            logging.warning("Caught a SignalInterrupt, skipping the finalizers")
            do_finalizers = False

        if dry_run:
            do_finalizers = False

        if do_finalizers:
            primary_exc, finalizer_exc = run_finalizers(
                endpoint_url, actual_llmisvc_name, primary_exc, finalizer_exc
            )

    if primary_exc is not None:
        raise primary_exc[1].with_traceback(primary_exc[2])

    if finalizer_exc is not None:
        raise finalizer_exc[1].with_traceback(finalizer_exc[2])

    # Update test labels with success status
    update_test_labels_with_status(True, "Test completed successfully")

    return 0


def _try_reuse_existing_service(
    namespace: str, llmisvc_name: str, gateway: dict[str, str], reuse_existing: bool, dry_run: bool
) -> str | None:
    """Try to reuse an existing LLMInferenceService if enabled and available.

    Args:
        namespace: Target namespace
        llmisvc_name: Name of the LLMInferenceService
        gateway: Gateway configuration
        reuse_existing: Whether to attempt reuse
        dry_run: Whether in dry-run mode

    Returns:
        Endpoint URL if reuse successful, None otherwise
    """
    # Check if we should reuse existing LLMInferenceService
    if reuse_existing and not dry_run:
        logger.info(f"Checking if LLMInferenceService {llmisvc_name} already exists")

        # Check if the service already exists
        try:
            existing_llmisvc = oc(
                "get",
                "llminferenceservice",
                llmisvc_name,
                "-n",
                namespace,
                check=False,
            )

            if existing_llmisvc.returncode == 0:
                logger.info(
                    f"Found existing LLMInferenceService {llmisvc_name}, attempting to reuse"
                )

                # Import and use the toolbox function to extract URL
                # Check if the existing service has a scheduler to determine gateway address
                from projects.core.dsl.utils.k8s import oc_get_json
                from projects.kserve.toolbox.deploy_llmisvc import try_resolve_endpoint_url

                existing_service = oc_get_json(
                    "llminferenceservice", name=llmisvc_name, namespace=namespace
                )
                has_scheduler = (
                    existing_service.get("spec", {}).get("router", {}).get("scheduler") is not None
                )

                # Use None for status_address_name when existing service has no scheduler
                gateway_status_address_name = (
                    gateway["status_address_name"] if has_scheduler else None
                )

                endpoint_url = try_resolve_endpoint_url(
                    namespace=namespace,
                    inference_service_name=llmisvc_name,
                    gateway_status_address_name=gateway_status_address_name,
                )

                if endpoint_url:
                    logger.info(f"Successfully reused existing LLMInferenceService: {endpoint_url}")
                    return endpoint_url
                else:
                    logger.warning(
                        "Existing LLMInferenceService found but no endpoint URL could be resolved, proceeding with new deployment"
                    )
            else:
                logger.info(
                    f"LLMInferenceService {llmisvc_name} does not exist, proceeding with new deployment"
                )

        except Exception as e:
            logger.warning(
                f"Error checking for existing LLMInferenceService: {e}, proceeding with new deployment"
            )

    return None


def build_inference_service_manifest(llmisvc_name: str) -> tuple[Path, str]:
    """Build inference service manifest and return path and actual service name.

    Args:
        llmisvc_name: The initial name for the LLMInferenceService

    Returns:
        Tuple of (manifest_path, actual_llmisvc_name) where actual_llmisvc_name may be truncated
    """
    # Step 1: Build and write inference service manifest
    manifest_path = _build_inference_service_manifest()

    # Step 2: Extract the actual deployed name from the manifest (may be truncated)
    import yaml

    with manifest_path.open(encoding="utf-8") as f:
        manifest = yaml.safe_load(f)

    # Get the actual deployed name from the manifest (may be truncated)
    actual_llmisvc_name = manifest["metadata"]["name"]

    logger.info(
        "Built LLMInferenceService manifest: %s (actual name: %s)",
        manifest_path,
        actual_llmisvc_name,
    )
    return manifest_path, actual_llmisvc_name


def deploy_inference_service_from_manifest(manifest_path: Path, actual_llmisvc_name: str) -> str:
    """Deploy LLMInferenceService from pre-built manifest and return endpoint URL.

    Args:
        manifest_path: Path to the pre-built manifest
        actual_llmisvc_name: The actual name of the LLMInferenceService from the manifest

    Returns:
        Gateway endpoint URL
    """
    logger.info("Starting LLMInferenceService deployment from manifest")

    # Load config where it's consumed
    namespace = runtime_config.get_namespace()
    platform = runtime_config.get_platform_config()
    gateway = platform["gateway"]

    dry_run = config.project.get_config("runtime.kserve.dry_run")
    wait_readiness = config.project.get_config("runtime.kserve.wait_readiness")
    reuse_existing = config.project.get_config("runtime.kserve.reuse_existing", False)

    # Try to reuse existing LLMInferenceService if enabled
    endpoint_url = _try_reuse_existing_service(
        namespace=namespace,
        llmisvc_name=actual_llmisvc_name,
        gateway=gateway,
        reuse_existing=reuse_existing,
        dry_run=dry_run,
    )
    if endpoint_url:
        return endpoint_url

    # Step 1: Ensure model cache is ready (skip in dry-run)
    if not dry_run:
        _prepare_model_cache()
    else:
        logger.info("Skipping model cache preparation - dry-run mode enabled")

    # Step 2: Wait for the serving control plane to settle before creating the service.
    if not dry_run and wait_readiness:
        rhoai_namespace = platform["rhoai"]["namespace"]
        wait_kserve_ready.run(namespace=rhoai_namespace)

    # Step 3: Check manifest for scheduler to determine gateway status address name
    import yaml

    with manifest_path.open(encoding="utf-8") as f:
        manifest = yaml.safe_load(f)

    has_scheduler = (
        "spec" in manifest
        and "router" in manifest["spec"]
        and "scheduler" in manifest["spec"]["router"]
    )

    # Use None for status_address_name when deploying without a scheduler
    gateway_status_address_name = gateway["status_address_name"] if has_scheduler else None

    # Step 4: Deploy the service and wait for endpoint
    logger.info("Deploying LLMInferenceService from manifest: %s", manifest_path)

    # Get scheduling wait configuration
    wait_long_scheduling = config.project.get_config("runtime.kserve.wait_long_scheduling")

    # Get monitoring configuration
    deploy_monitor = config.project.get_config("deployments.defaults.enable_monitors")

    endpoint_url = deploy_llmisvc.run(
        namespace=namespace,
        inference_service_manifest_path=str(manifest_path),
        gateway_status_address_name=gateway_status_address_name,
        dry_run=dry_run,
        wait_long_scheduling=wait_long_scheduling,
        deploy_monitor=deploy_monitor,
    )

    if dry_run:
        logger.info("Dry-run completed: LLMInferenceService manifest prepared: %s", manifest_path)
        return str(manifest_path)

    logger.info("LLMInferenceService deployed successfully, endpoint: %s", endpoint_url)
    return endpoint_url


def _prepare_model_cache() -> None:
    """Ensure model cache PVC is ready for deployment."""

    model_name = runtime_config.get_model_name()
    logger.info("Preparing model cache for model: %s", model_name)

    # Use the same prepare_model_cache function as the prepare phase
    # This includes vault token handling and PVC existence checks
    prepare_model_cache()


def _build_inference_service_manifest() -> Path:
    """Build and write the LLMInferenceService manifest."""

    config_dir = runtime_config.get_config_dir()
    namespace = runtime_config.get_namespace()
    platform = runtime_config.get_platform_config()
    inference_service = platform["inference_service"]
    model_name = runtime_config.get_model_name()
    model_slug = runtime_config.get_model_slug(model_name)
    deployment_profile = runtime_config.get_deployment_profile()
    model_cache = runtime_config.get_model_cache_config()
    workload = runtime_config.get_workload_config()  # Get workload config with vllm_args

    benchmark_overrides = runtime_config.get_benchmark_deployment_overrides()
    if benchmark_overrides:
        deployment_profile = runtime_config.deep_merge(deployment_profile, benchmark_overrides)

    # Build the InferenceService manifest
    deployment_profile_name = runtime_config.get_deployment_profile_name()
    manifest = render_inference_service_from_parts(
        config_dir=config_dir,
        namespace=namespace,
        inference_service=inference_service,
        model_name=model_name,
        model_slug=model_slug,
        deployment_profile=deployment_profile,
        model_cache=model_cache,
        deployment_profile_name=deployment_profile_name,
        workload=workload,
    )

    # Write the manifest to artifacts
    artifacts_dir = env.ARTIFACT_DIR / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = artifacts_dir / "llminferenceservice.yaml"
    write_yaml(manifest_path, manifest)

    logger.info("Built LLMInferenceService manifest: %s", manifest_path)
    return manifest_path


def run_smoke_request(*, endpoint_url: str) -> dict[str, object]:
    # Load config where it's consumed

    namespace = runtime_config.get_namespace()
    platform = runtime_config.get_platform_config()
    smoke = platform["smoke"]
    smoke_request = runtime_config.get_smoke_request()

    return run_smoke_request_command.run(
        namespace=namespace,
        endpoint_url=endpoint_url,
        pod_name=smoke["pod_name"],
        client_image=smoke["client_image"],
        endpoint_path=smoke["endpoint_path"],
        request_timeout_seconds=smoke["request_timeout_seconds"],
        served_model_name=runtime_config.get_served_model_name(),
        prompt=smoke_request["prompt"],
        max_tokens=smoke_request["max_tokens"],
        temperature=smoke_request["temperature"],
    )


def run_guidellm_benchmark(*, endpoint_url: str) -> None:
    namespace = runtime_config.get_namespace()
    benchmark = runtime_config.get_benchmark_config()
    workload = runtime_config.get_workload_config()

    if benchmark is None:
        return

    # Add benchmark start timing
    start_time = update_test_labels_with_timing("benchmark", "start")

    try:
        benchmark_key = runtime_config.get_benchmark_keys()[0]
        guidellm_args = build_guidellm_args(benchmark)
        if not any(arg.startswith("--processor=") for arg in guidellm_args):
            guidellm_args.append(f"--processor={runtime_config.get_model_name()}")

        # Get fs_group from workload config
        fs_group = None
        if workload:
            fs_group = workload.get("fs_group")

        artifact_name = f"benchmark_{slugify_identifier(benchmark_key, max_length=48)}"
        with env.NextArtifactDir(artifact_name):
            run_guidellm_benchmark_command.run(
                endpoint_url=endpoint_url,
                name=benchmark.get("job_name"),
                namespace=namespace,
                image=benchmark.get("image"),
                timeout=benchmark.get("timeout_seconds"),
                pvc_size=benchmark.get("pvc_size"),
                pvc_storage_class=benchmark.get("pvc_storage_class"),
                guidellm_args=guidellm_args,
                fs_group=fs_group,
                use_pvc=benchmark.get("use_pvc"),
            )
    finally:
        # Add benchmark end timing (even if benchmark failed)
        end_time = update_test_labels_with_timing("benchmark", "end")

        # Capture prometheus metrics if enabled
        if config.project.get_config("prom.capture.enabled"):
            capture_prometheus(start_time, end_time)


def capture_inference_service_state(llmisvc_name: str) -> None:
    """Capture inference service state for the given llmisvc name."""
    namespace = runtime_config.get_namespace()

    capture_llmisvc_state.run(
        llmisvc_name=llmisvc_name,
        namespace=namespace,
    )


def write_endpoint_url(*, artifact_dir: Path, endpoint_url: str | None) -> None:
    if not endpoint_url:
        return

    endpoint_file = artifact_dir / "artifacts" / "endpoint.url"
    endpoint_file.parent.mkdir(parents=True, exist_ok=True)
    endpoint_file.write_text(f"{endpoint_url}\n", encoding="utf-8")


def cleanup_test_resources(llmisvc_name: str | None) -> None:
    """Cleanup test resources using the toolbox script

    Args:
        llmisvc_name: The actual LLMInferenceService name that was deployed, or None if deployment failed
    """

    # Skip cleanup when in dry-run mode
    dry_run = config.project.get_config("runtime.kserve.dry_run", False)
    if dry_run:
        logger.info("Skipping cleanup_test_resources - dry-run mode enabled")
        return

    if not llmisvc_name:
        logger.warning("No LLMInferenceService name provided, cleanup may be incomplete")
        return

    namespace = runtime_config.get_namespace()
    platform = runtime_config.get_platform_config()
    smoke = platform["smoke"]

    cleanup_test_resources_command.run(
        namespace=namespace,
        inference_service_name=llmisvc_name,
        smoke_pod_name=smoke["pod_name"],
        benchmark_job_name=runtime_config.get_benchmark_job_name(),
    )


def capture_namespace_events_after_test(
    *,
    artifact_dir: Path,
    namespace: str,
    capture_namespace_events: bool,
) -> None:
    if not capture_namespace_events:
        return

    shell.run(
        f"oc get events -n {namespace} --sort-by=.metadata.creationTimestamp",
        check=False,
        stdout_dest=artifact_dir / "artifacts" / "namespace.events.txt",
    )
