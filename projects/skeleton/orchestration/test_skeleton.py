import json
import logging
import pathlib
import signal
import time

import yaml

from projects.core.library import config, env, vault
from projects.core.library.postprocess import run_and_postprocess, write_test_labels
from projects.skeleton.toolbox.cluster_info.main import run as cluster_info

logger = logging.getLogger(__name__)


def seed_skeleton_caliper_artifacts() -> pathlib.Path:
    """
    Create minimal Caliper inputs under the FORGE artifact root:

    * ``__caliper_test_metadata__.yaml`` + ``metrics.json`` per scenario (required by the skeleton plugin).
    """
    demo_dir = env.ARTIFACT_DIR
    FAKE_DATA = (
        ("smoke", 120.5, 8.2),
        ("load", 87.0, 22.1),
    )
    for scenario, throughput, latency_ms in FAKE_DATA:
        d = demo_dir / scenario
        d.mkdir(parents=True, exist_ok=True)
        write_test_labels(d, {"scenario": scenario}, dump_config=False)
        (d / "metrics.json").write_text(
            json.dumps({"throughput": throughput, "latency_ms": latency_ms}),
            encoding="utf-8",
        )

    logger.info("Seeded Caliper demo tree under %s", demo_dir)
    return demo_dir


def _signal_handler_sigint(sig, frame):
    """Sample SIGINT signal handler for skeleton project."""
    env.reset_artifact_dir()
    # Sample handler - does nothing else


def _signal_handler_sigterm(sig, frame):
    """Sample SIGTERM signal handler for skeleton project."""
    env.reset_artifact_dir()
    # Sample handler - does nothing else


def _setup_sample_signal_handlers():
    """Set up sample signal handlers for demonstration."""
    try:
        signal.signal(signal.SIGINT, _signal_handler_sigint)
        signal.signal(signal.SIGTERM, _signal_handler_sigterm)
        logger.debug("Sample signal handlers installed")
    except Exception as e:
        logger.warning(f"Failed to set up sample signal handlers: {e}")


def test():
    """Main test function that wraps do_test() with outcome postprocessing."""
    return run_and_postprocess(do_test)


def skeleton_take_time():
    # Get test duration configuration
    test_duration = config.project.get_config("skeleton.test.duration_seconds")
    logger.info(f"Test duration: {test_duration} seconds")

    start_time = time.time()
    test_iteration = 0

    logger.info(f"Starting {test_duration}s test loop...")

    # Run timed test loop
    while time.time() - start_time < test_duration:
        test_iteration += 1
        elapsed = time.time() - start_time
        remaining = test_duration - elapsed

        logger.info(
            f"Test iteration {test_iteration} - Elapsed: {elapsed:.1f}s, Remaining: {remaining:.1f}s"
        )

        # Simulate some test work with explicit waiting message
        wait_time = min(30.0, remaining)
        logger.info(f"⏳ Waiting {wait_time:.1f}s before next iteration...")
        time.sleep(wait_time)

    elapsed_total = time.time() - start_time
    logger.info(f"✅ Completed {test_iteration} test iterations in {elapsed_total:.1f}s")


def do_test():
    logger.info("=== Skeleton Project Test Phase ===")

    if config.project.get_config("skeleton.deep_testing"):
        logger.info("Running the (fake) deep testing ...")
    else:
        logger.info("Running the (fake) light testing ...")

    client_id = vault.get_vault_content_path("psap-forge-notifications", "topsail-bot.clientid")
    if not client_id:
        logger.error("`client_id` secret not available.")
    else:
        logger.info(f"`client_id` secret available. Size: {client_id.stat().st_size}b")
        del client_id

    skeleton_config = config.project.get_config("skeleton", print=False)

    yaml_cfg = yaml.dump(
        {"skeleton": skeleton_config},
        indent=4,
        default_flow_style=False,
        sort_keys=False,
    )
    logger.info("")
    logger.info(f"Fake test configuration:\n{yaml_cfg}")

    skeleton_take_time()

    with env.NextArtifactDir("skeleton_seed_data_for_caliper_postprocessing"):
        seed_skeleton_caliper_artifacts()

    if not config.project.get_config("skeleton.collect_cluster_info"):
        logger.info("⚠️ Cluster information gathering not enabled. Returning early.")
        return 0

    # Demonstrate calling a toolbox from orchestration
    logger.info("Running cluster information toolbox...")

    result = cluster_info(output_format="text")

    if not result:
        logger.warning("⚠️ Cluster information gathering didn't work")
        return 1

    cluster_nodes_dest = getattr(result, "cluster_nodes_dest", None)
    if not cluster_nodes_dest:
        logger.warning("⚠️ Cluster information gathering didn't generate the cluster node file")
        return 1

    logger.info("✅ Cluster information gathering completed successfully")
    logger.info(f"Check {cluster_nodes_dest.parent} directory for detailed cluster information.")

    return 0


def resolve_hardware_request(hardware_spec: dict):
    """
    Resolve hardware requirements for FournosJob based on skeleton project configuration.

    This is a stub implementation. Update spec.hardware based on project configuration.

    Args:
        hardware_spec: The current spec.hardware dict from the FournosJob. This object should be updated.

    """
    logger.info("Hardware resolution: stub implementation - no changes made")

    # Stub implementation - could be extended to:
    # - Read hardware config from project configuration
    # - Set hardware requirements based on workload needs
    # - Handle different hardware profiles (GPU, CPU, memory requirements)
    # - Example: return {"gpu": {"type": "nvidia-tesla-v100", "count": 1}, "memory": "32Gi"}

    # hardware_spec["gpuType"] = "h200"
    # hardware_spec["gpuCount"] = 4

    return hardware_spec
