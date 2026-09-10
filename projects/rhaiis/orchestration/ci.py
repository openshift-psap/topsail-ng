#!/usr/bin/env python3

import base64
import json
import logging
import os
import types
from pathlib import Path

import click
import prepare_rhaiis
import test_rhaiis

from projects.core.agentic.config_review import trigger_config_review_for_ci
from projects.core.agentic.on_failure import agent_review_on_failure
from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.dsl.utils.k8s import oc
from projects.core.library import ci as ci_lib
from projects.core.library import env, vault
from projects.core.library.export import caliper_export_entrypoint
from projects.rhaiis.orchestration import runtime_config

logger = logging.getLogger(__name__)


def _check_pipeline_failure_and_notify() -> None:
    """Detect early pipeline failures (e.g. image pull errors) and send a Slack alert.

    Runs in the post-cleanup finally step. Checks whether prior steps
    produced FAILURE artifacts or the test step was skipped entirely.
    """
    try:
        base_dir_env = os.environ.get("ARTIFACT_BASE_DIR", "")
        if not base_dir_env:
            return
        base_dir = Path(base_dir_env)
        if not base_dir.is_dir():
            return

        test_dir = base_dir / "03__test"
        test_ran = test_dir.exists()

        # Skip steps already handled by do_test's exception handler
        failure_files = sorted(f for f in base_dir.glob("*/FAILURE") if f.parent.name != "03__test")

        if not failure_files and test_ran:
            return

        errors = []
        for f in failure_files:
            step_name = f.parent.name
            content = f.read_text().strip()
            summary = content[:300] if content else "unknown error"
            errors.append(f"[{step_name}] {summary}")

        if not test_ran and not errors:
            errors.append(
                "Test step was skipped — likely an earlier pipeline step failed (e.g. image pull timeout)"
            )

        error_text = "\n".join(errors)

        from projects.core.library import config as _cfg
        from projects.rhaiis.postprocess.regression import send_failure_notification

        model_key = _cfg.project.get_config("tests.rhaiis.model_key", "unknown")
        try:
            model_cfg = runtime_config.get_model(model_key)
            model_name = model_cfg.get("hf_model_id", model_key)
        except Exception:
            model_name = model_key

        accelerator = runtime_config.get_accelerator()
        gpu_type = runtime_config.get_gpu_type(accelerator) or accelerator
        cluster_tag = _cfg.project.get_config("rhaiis.cluster_tag", "")
        accelerator_key = f"{gpu_type}_{cluster_tag}".upper() if cluster_tag else gpu_type.upper()

        send_failure_notification(
            error=error_text,
            model=model_name,
            accelerator=accelerator_key,
            job_id=os.environ.get("FJOB_NAME", ""),
            slack_user=_cfg.project.get_config("tests.rhaiis.slack_user", ""),
            notification_vault="psap-forge-notifications",
            version=_cfg.project.get_config("tests.rhaiis.version", ""),
            cluster=cluster_tag,
        )
    except Exception:
        logger.warning("Failed to check/send pipeline failure notification", exc_info=True)


def list_vaults() -> list[str]:
    test_rhaiis.init()
    return runtime_config.get_vaults()


def resolve_hardware_request(hardware_spec: dict) -> dict:
    test_rhaiis.init()

    if hardware_spec.get("gpuType"):
        return hardware_spec

    # No hardware section in the FournosJob → CPU/no-hardware job, don't add GPU resources.
    # GPU jobs submitted via fournos_launcher always have both gpuCount and gpuType set
    # (enforced by submit.py pair validation), so they always hit the gpuType check above.
    if not hardware_spec:
        return {}

    accelerator = runtime_config.get_accelerator()
    if accelerator == "cpu":
        return {}

    from projects.core.library import config as _cfg

    model_key = runtime_config.get_test_model_key()
    model = runtime_config.get_model(model_key)
    engine = runtime_config.get_engine()
    engine_defaults = _cfg.project.get_config(f"rhaiis.engines.{engine}.args") or {}
    ea = runtime_config.merge_engine_args(engine_defaults, model, {}, engine)
    tp_size = int(ea.get("tensor-parallel-size") or ea.get("tp-size") or ea.get("tp_size") or 1)

    gpu_type = runtime_config.get_gpu_type(accelerator)

    if not gpu_type:
        return {}

    hardware_spec["gpuCount"] = tp_size
    hardware_spec["gpuType"] = gpu_type

    return hardware_spec


@click.group()
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx):
    """RHAIIS Project CI Operations for FORGE."""
    ctx.ensure_object(types.SimpleNamespace)
    test_rhaiis.init()

    if ctx.invoked_subcommand != "resolve-fournos-config":
        vault.init(runtime_config.get_vaults())


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def prepare(ctx):
    """Prepare phase - Set up environment and dependencies."""
    return prepare_rhaiis.prepare()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
@agent_review_on_failure
def test(ctx):
    """Test phase - Deploy model, run benchmarks, capture results."""
    trigger_config_review_for_ci(env.BASE_ARTIFACT_DIR, async_mode=True)
    return test_rhaiis.test()


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def pre_cleanup(ctx):
    """Pre-cleanup phase - no-op to avoid cleaning up running resources."""
    return 0


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def post_cleanup(ctx):
    """Post-cleanup phase - Clean up resources after test."""
    cleanup_result = None
    try:
        if runtime_config.get_accelerator() == "cpu":
            from projects.rhaiis.toolbox.diagnose_cpu_cluster.main import (
                run as diagnose_cpu_cluster,
            )

            diagnose_cpu_cluster(remove_labels=True)
            ns = runtime_config.get_namespace()
            oc(
                "delete", "secret", "storage-config", "-n", ns,
                "--ignore-not-found", check=False, log_stdout=False,
            )
    finally:
        _check_pipeline_failure_and_notify()
        cleanup_result = prepare_rhaiis.cleanup()
    return cleanup_result


def _ensure_storage_config_secret(namespace: str) -> None:
    """Create storage-config secret with HF_TOKEN if it does not already exist.

    CPU-only helper: mirrors the secret GPU clusters pre-provision manually.
    """
    result = oc("get", "secret", "storage-config", "-n", namespace, check=False, log_stdout=False)
    if result.returncode == 0:
        logger.info("Secret storage-config already exists in %s", namespace)
        return

    token_path = vault.get_vault_content_path("psap-forge-hf", "hf_token")
    if token_path is None or not token_path.exists():
        logger.warning(
            "psap-forge-hf vault not available — storage-config secret must be "
            "created manually in %s for HuggingFace model downloads to work",
            namespace,
        )
        return

    token_b64 = base64.b64encode(token_path.read_text().strip().encode()).decode()
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "storage-config", "namespace": namespace},
        "type": "Opaque",
        "data": {"HF_TOKEN": token_b64},
    }
    oc("apply", "-f", "-", input_text=json.dumps(manifest), handled_secretly=True)
    logger.info("Created storage-config secret in %s", namespace)


def _verify_model_pvc(namespace: str, pvc_name: str) -> None:
    """Warn when the model-cache PVC is missing (GPU clusters pre-provision it manually)."""
    result = oc("get", "pvc", pvc_name, "-n", namespace, check=False, log_stdout=False)
    if result.returncode == 0:
        logger.info("Model PVC %s exists in %s", pvc_name, namespace)
    else:
        logger.warning(
            "Model PVC %s not found in %s — create it manually before deploying "
            "(see CPU_TESTING.md)",
            pvc_name,
            namespace,
        )


@main.command()
@click.pass_context
@ci_lib.safe_ci_entrypoint
def preflight(ctx) -> int:
    """Preflight check phase - Validate that the cluster is ready for testing."""

    if runtime_config.get_accelerator() == "cpu":
        from projects.rhaiis.toolbox.diagnose_cpu_cluster.main import (
            run as diagnose_cpu_cluster,
        )

        namespace = runtime_config.get_namespace()
        diagnose_cpu_cluster(apply_labels=True)
        diagnose_cpu_cluster(strict=True)

        _ensure_storage_config_secret(namespace)

        deploy_cfg = runtime_config.get_deploy_config()
        pvc_name = deploy_cfg.get("storage_pvc", "")
        if pvc_name:
            _verify_model_pvc(namespace, pvc_name)

    return 0


main.add_command(caliper_export_entrypoint)
main.add_command(
    create_fournos_resolve_entrypoint(
        vault_list_func=list_vaults,
        hardware_resolver_func=resolve_hardware_request,
    )
)

if __name__ == "__main__":
    main()
