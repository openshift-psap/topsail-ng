from __future__ import annotations

import logging

from projects.core.library import config, env
from projects.core.library.postprocess import run_and_postprocess
from projects.rhaiis.orchestration import runtime_config
from projects.rhaiis.orchestration.test_phase import _run_test

logger = logging.getLogger(__name__)

DEFAULT_MODEL_KEYS = ["tinyllama-cpu"]
DEFAULT_CPU_REQUESTS = ["8", "16", "32"]
DEFAULT_WORKLOAD_KEYS = ["cpu-chat-baseline"]


def run(
    *,
    model_keys: list[str] | None = None,
    cpu_requests: list[str] | None = None,
    workload_keys: list[str] | None = None,
    namespace: str,
    continue_on_error: bool = False,
) -> int:
    return run_and_postprocess(
        do_test,
        model_keys=model_keys,
        cpu_requests=cpu_requests,
        workload_keys=workload_keys,
        namespace=namespace,
        continue_on_error=continue_on_error,
    )


def do_test(
    *,
    model_keys: list[str] | None = None,
    cpu_requests: list[str] | None = None,
    workload_keys: list[str] | None = None,
    namespace: str,
    continue_on_error: bool = False,
) -> int:
    config.project.set_config("rhaiis.accelerator", "cpu")

    resolved_models = model_keys or DEFAULT_MODEL_KEYS
    resolved_cpu_requests = cpu_requests or DEFAULT_CPU_REQUESTS
    resolved_workloads = workload_keys or DEFAULT_WORKLOAD_KEYS

    from projects.core.library import ci
    from projects.rhaiis.orchestration.notifications import send_pipeline_failure_alert

    total = len(resolved_models) * len(resolved_cpu_requests) * len(resolved_workloads)
    current = 0
    failed = 0
    failed_labels: list[str] = []
    first_failed_model: str | None = None
    first_failed_workload: str | None = None

    def _record_cell_failure(
        *, label: str, model_key: str, workload_key: str, error: Exception
    ) -> None:
        nonlocal failed, first_failed_model, first_failed_workload
        failed += 1
        failed_labels.append(label)
        if first_failed_model is None:
            first_failed_model = model_key
            first_failed_workload = workload_key
        ci.add_notification_file(
            f"concurrent-load-{label}",
            f"Concurrent load cell {label} failed: {error}",
        )

    def _send_matrix_failure_alert() -> None:
        if not failed_labels or first_failed_model is None or first_failed_workload is None:
            return
        summary = f"{failed}/{total} concurrent load cells failed: {', '.join(failed_labels)}"
        send_pipeline_failure_alert(
            RuntimeError(summary),
            model_key=first_failed_model,
            workload_keys=[first_failed_workload],
        )

    for model_key in resolved_models:
        for cpu_request in resolved_cpu_requests:
            for workload_key in resolved_workloads:
                current += 1
                label = f"{model_key}_{cpu_request}cpu_{workload_key}"
                logger.info("[%d/%d] Running cell: %s", current, total, label)

                with env.NextArtifactDir(label):
                    try:
                        ret = _run_test(
                            model_key=model_key,
                            workload_keys=[workload_key],
                            namespace=namespace,
                            deploy_cfg_overrides={
                                "cpu_request": cpu_request,
                                "memory_request": runtime_config.memory_request_for_cpu(
                                    cpu_request
                                ),
                            },
                        )
                        if ret != 0:
                            _record_cell_failure(
                                label=label,
                                model_key=model_key,
                                workload_key=workload_key,
                                error=RuntimeError(
                                    f"Concurrent load cell {label} failed (exit code {ret})"
                                ),
                            )
                            if not continue_on_error:
                                _send_matrix_failure_alert()
                                return 1
                    except Exception as exc:
                        logger.error("Cell %s failed", label, exc_info=True)
                        _record_cell_failure(
                            label=label,
                            model_key=model_key,
                            workload_key=workload_key,
                            error=exc,
                        )
                        if not continue_on_error:
                            _send_matrix_failure_alert()
                            raise

    if failed:
        summary = f"{failed}/{total} concurrent load cells failed: {', '.join(failed_labels)}"
        logger.error(summary)
        ci.add_notification_file("concurrent-load-summary", summary)
        _send_matrix_failure_alert()
        return 1

    return 0
