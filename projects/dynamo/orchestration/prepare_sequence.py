from __future__ import annotations

from projects.core.library import env
from projects.dynamo.orchestration import prepare_phase, runtime_config


def run_prepare_sequence() -> int:
    prepare_phase.verify_oc_access()
    prepare_phase.verify_cluster_version()
    prepare_phase.prepare_nfd()
    prepare_phase.prepare_gpu_operator()
    prepare_phase.deploy_dynamo_platform()
    prepare_phase.wait_for_dynamo_crds()

    for run_spec in runtime_config.get_run_specs():
        with runtime_config.activate_run_spec(run_spec):
            with env.NextArtifactDir(f"prepare_{run_spec.artifact_dirname}"):
                prepare_phase.ensure_test_namespace()
                prepare_phase.cleanup_previous_run()
                prepare_phase.prepare_model_cache()
                prepare_phase.verify_gpu_nodes()
                prepare_phase.capture_prepare_state()

    return 0
