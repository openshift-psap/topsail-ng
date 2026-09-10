from __future__ import annotations

import json
import logging
import os
import uuid as _uuid_mod
from pathlib import Path

import yaml

from projects.core.library import env
from projects.core.library.postprocess import run_and_postprocess, write_test_labels
from projects.rhaiis.orchestration import runtime_config

logger = logging.getLogger(__name__)

_K8S_NAME_MAX = 63
_warnings: list[str] = []


def _write_manifest(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)


def _guidellm_job_name(prefix: str, workload_key: str, deployment_name: str) -> str:
    """Build a K8s-safe job name: {prefix}-{workload_key}-{model}, trimming model to fit."""
    base = f"{prefix}-{workload_key}-"
    available = _K8S_NAME_MAX - len(base)
    model = deployment_name[:available] if available > 0 else ""
    return f"{base}{model}".rstrip("-")


def run(
    *,
    model_key: str,
    workload_keys: list[str],
    namespace: str,
    deployment_name: str | None = None,
) -> int:
    ret = run_and_postprocess(
        do_test,
        model_key=model_key,
        workload_keys=workload_keys,
        namespace=namespace,
        deployment_name=deployment_name,
    )

    try:
        _sync_postprocessed_dashboard_csv(model_key, workload_keys)
    except Exception:
        logger.exception("Dashboard CSV S3 sync after postprocessing failed")
        ret = 1

    if ret == 0:
        _maybe_send_success_notification(model_key, workload_keys)

    return ret


def do_test(
    *,
    model_key: str,
    workload_keys: list[str],
    namespace: str,
    deployment_name: str | None = None,
) -> int:
    try:
        dir_name = f"test_{model_key}_{'_'.join(workload_keys)}"
        with env.NextArtifactDir(dir_name):
            return _run_test(
                model_key=model_key,
                workload_keys=workload_keys,
                namespace=namespace,
                deployment_name=deployment_name,
            )
    except Exception as exc:
        from projects.rhaiis.orchestration.notifications import send_pipeline_failure_alert

        send_pipeline_failure_alert(exc, model_key=model_key, workload_keys=workload_keys)
        raise


def _run_test(
    *,
    model_key: str,
    workload_keys: list[str],
    namespace: str,
    deployment_name: str | None = None,
    deploy_cfg_overrides: dict | None = None,
) -> int:
    _warnings.clear()
    model_cfg = runtime_config.get_model(model_key)
    accelerator = runtime_config.get_accelerator()
    gpu_type = runtime_config.get_gpu_type(accelerator) or accelerator
    from projects.core.library import config as _cfg

    cluster_tag = _cfg.project.get_config("rhaiis.cluster_tag", "")
    accelerator_key = f"{gpu_type}_{cluster_tag}".upper() if cluster_tag else gpu_type.upper()
    deploy_cfg = runtime_config.get_deploy_config()
    if deploy_cfg_overrides:
        deploy_cfg.update(deploy_cfg_overrides)
    benchmark_cfg = runtime_config.get_benchmark_config()

    if not deployment_name:
        base_name = runtime_config.derive_deployment_name(model_cfg["hf_model_id"])
        fjob = os.environ.get("FJOB_NAME", "")
        suffix = fjob.rsplit("-", 1)[-1] if fjob else ""
        if suffix:
            # KServe hostname: {name}-predictor-{namespace} must be <= 63 chars
            ns = runtime_config.get_namespace()
            max_base = 63 - len(f"-predictor-{ns}") - len(suffix) - 1
            truncated_base = base_name[:max_base].rstrip("-")
            deployment_name = f"{truncated_base}-{suffix}"
        else:
            deployment_name = base_name

    engine = runtime_config.get_engine()
    serving_image = runtime_config.get_serving_image(accelerator, engine)
    engine_defaults = runtime_config.get_engine_args(engine)
    engine_port = runtime_config.get_engine_port(engine)
    first_workload = runtime_config.get_workload(workload_keys[0])
    engine_args = runtime_config.merge_engine_args(
        engine_defaults, model_cfg, first_workload, engine
    )
    env_vars = runtime_config.merge_env_vars(accelerator, model_cfg)

    from projects.core.library import config as _cfg

    version = _cfg.project.get_config("tests.rhaiis.version", "")
    run_uuid = _cfg.project.get_config("tests.rhaiis.run_uuid", "") or str(_uuid_mod.uuid4())
    logger.info("Run UUID for this job: %s", run_uuid)

    import subprocess

    from projects.guidellm.toolbox.run_guidellm_benchmark.main import (
        wait_guidellm_benchmark_task,
    )
    from projects.rhaiis.orchestration.manifests import (
        build_inferenceservice,
        build_servingruntime,
    )
    from projects.rhaiis.toolbox.deploy_kserve_isvc.main import run as deploy_kserve_isvc
    from projects.rhaiis.toolbox.wait_isvc_ready.main import run as wait_isvc_ready

    fjob_name = os.environ.get("FJOB_NAME", "")
    fjob_ns = os.environ.get("FOURNOS_WORKLOAD_NAMESPACE", "psap-automation")
    if fjob_name:
        try:
            mgmt_env = {k: v for k, v in os.environ.items() if k != "KUBECONFIG"}
            subprocess.run(
                [
                    "oc",
                    "annotate",
                    "fournosjob",
                    fjob_name,
                    "-n",
                    fjob_ns,
                    f"rhaiis.run-uuid={run_uuid}",
                    "--overwrite",
                ],
                check=False,
                capture_output=True,
                timeout=10,
                env=mgmt_env,
            )
            logger.info("Annotated FournosJob %s with run-uuid=%s", fjob_name, run_uuid)
        except Exception:
            logger.warning("Failed to annotate FournosJob with UUID", exc_info=True)

    from projects.core.library import config

    profiler_cfg = runtime_config.get_profiler_config()
    profiler_enabled = profiler_cfg.get("enabled", False)
    run_benchmark = config.project.get_config("tests.rhaiis.run_benchmark", True)

    # Standalone analysis only — no deployment needed
    if not run_benchmark and not profiler_enabled:
        logger.info("run_benchmark=false and profiler=false, running standalone analysis only")
        try:
            from projects.rhaiis.orchestration.analysis import run_standalone_analysis

            run_standalone_analysis(
                model_cfg,
                accelerator_key,
                engine_args,
                run_uuid=run_uuid,
                restrict_profiles=workload_keys or None,
            )
        except Exception:
            logger.warning("Standalone analysis failed", exc_info=True)
            _warnings.append("Standalone analysis failed")
        return 1 if _warnings else 0

    benchmark_timeout = benchmark_cfg.get("timeout", 14400)
    wait_guidellm_benchmark_task._retry_config["attempts"] = max(1, benchmark_timeout // 10)

    try:
        from projects.caliper.orchestration.export import precreate_mlflow_run_if_configured

        mlflow_destination = precreate_mlflow_run_if_configured()
    except Exception:
        logger.warning("MLflow run pre-creation failed; continuing", exc_info=True)
        mlflow_destination = None

    try:
        isvc_labels = {
            "opendatahub.io/dashboard": "true",
            "deployment_uuid": run_uuid,
        }
        if profiler_enabled and engine == "vllm":
            isvc_labels["vllm-profiler/enabled"] = "true"
        elif profiler_enabled and engine != "vllm":
            logger.warning("Profiler is only supported with vLLM engine, skipping profiler")
            profiler_enabled = False

        logger.info("Deploying %s to %s/%s", model_cfg["hf_model_id"], namespace, deployment_name)
        ea = engine_args or {}
        gpu_count = int(
            ea.get("tensor-parallel-size") or ea.get("tp-size") or ea.get("tp_size") or 1
        )

        sr_manifest = build_servingruntime(
            deployment_name=deployment_name,
            namespace=namespace,
            model_id=model_cfg["hf_model_id"],
            serving_image=serving_image,
            engine=engine,
            engine_args=engine_args,
            engine_port=engine_port,
            storage_source=deploy_cfg.get("storage_source", "hf"),
            storage_pvc=deploy_cfg.get("storage_pvc", ""),
            gpu_count=gpu_count,
            image_pull_secrets=deploy_cfg.get("image_pull_secrets") or [],
            env_vars=env_vars,
            trtllm_config=runtime_config.get_trtllm_config() if engine == "trtllm" else None,
        )
        isvc_manifest = build_inferenceservice(
            deployment_name=deployment_name,
            namespace=namespace,
            engine=engine,
            engine_port=engine_port,
            accelerator=accelerator,
            gpu_count=gpu_count,
            replicas=deploy_cfg.get("replicas", 1),
            cpu_request=deploy_cfg.get("cpu_request", "4"),
            memory_request=deploy_cfg.get("memory_request", "16Gi"),
            storage_source=deploy_cfg.get("storage_source", "hf"),
            storage_pvc=deploy_cfg.get("storage_pvc", ""),
            model_id=model_cfg["hf_model_id"],
            service_account_name=deploy_cfg.get("service_account_name", ""),
            labels=isvc_labels,
            node_selector=deploy_cfg.get("node_selector") or None,
        )
        sr_file = env.ARTIFACT_DIR / "src" / "servingruntime.yaml"
        isvc_file = env.ARTIFACT_DIR / "src" / "inferenceservice.yaml"
        _write_manifest(sr_manifest, sr_file)
        _write_manifest(isvc_manifest, isvc_file)

        deploy_kserve_isvc(
            namespace=namespace,
            servingruntime_file=str(sr_file),
            inferenceservice_file=str(isvc_file),
        )

        logger.info("Waiting for InferenceService to be ready")
        wait_isvc_ready(
            name=deployment_name,
            namespace=namespace,
            timeout_seconds=deploy_cfg.get("ready_timeout", 3600),
            health_check_timeout=deploy_cfg.get("health_check_timeout", 120),
        )

        endpoint_url = (
            f"http://{deployment_name}-predictor.{namespace}.svc.cluster.local:{engine_port}"
        )

        warmup_enabled = (
            config.project.get_config("tests.rhaiis.warmup", True)
            and run_benchmark
            and not profiler_enabled
        )

        logger.info(
            "Running %d workload(s): %s",
            len(workload_keys),
            workload_keys,
        )

        # Phase 1: warmup or profiler for ALL workloads first
        for wl_key in workload_keys:
            step_kwargs = dict(
                deployment_name=deployment_name,
                namespace=namespace,
                endpoint_url=endpoint_url,
                benchmark_cfg=benchmark_cfg,
                model_cfg=model_cfg,
                workload=runtime_config.get_workload(wl_key),
                workload_key=wl_key,
                benchmark_timeout=benchmark_timeout,
            )
            if profiler_enabled:
                logger.info("Running profiler for workload=%s", wl_key)
                _run_profiler_step(**step_kwargs)
            elif warmup_enabled:
                logger.info("Running warmup for workload=%s", wl_key)
                _run_warmup_step(**step_kwargs)

        if profiler_enabled:
            try:
                _upload_profiler_traces(model_cfg, gpu_type, engine_args, profiler_cfg)
            except Exception:
                logger.exception("Profiler trace upload failed")
                _warnings.append("Profiler trace upload failed")

        # Phase 2: benchmark + post-processing for ALL workloads
        trtllm_cfg = runtime_config.get_trtllm_config() if engine == "trtllm" else None
        for wl_key in workload_keys:
            _run_workload_benchmark(
                model_key=model_key,
                workload_key=wl_key,
                model_cfg=model_cfg,
                accelerator=accelerator,
                accelerator_key=accelerator_key,
                gpu_type=gpu_type,
                serving_image=serving_image,
                engine_args=engine_args,
                benchmark_cfg=benchmark_cfg,
                deployment_name=deployment_name,
                namespace=namespace,
                endpoint_url=endpoint_url,
                benchmark_timeout=benchmark_timeout,
                run_uuid=run_uuid,
                version=version,
                cluster_tag=cluster_tag,
                trtllm_config=trtllm_cfg,
                mlflow_destination=mlflow_destination,
            )

        try:
            first_workload = runtime_config.get_workload(workload_keys[0])
            first_rates = first_workload.get("rates", [1])
            first_max_seconds = first_workload.get("max_seconds", 180)
            _set_mlflow_metadata(
                model_key,
                ",".join(workload_keys),
                model_cfg,
                accelerator,
                serving_image,
                engine_args,
                benchmark_cfg,
                first_rates,
                first_max_seconds,
                namespace,
                deployment_name,
            )
        except Exception:
            logger.warning("Setting MLflow metadata failed; continuing", exc_info=True)
    finally:
        _capture_and_cleanup(deployment_name, namespace)

    try:
        _upload_predictor_log(run_uuid)
    except Exception:
        logger.warning("Predictor log upload failed", exc_info=True)
        _warnings.append("Predictor log upload failed")

    if _warnings:
        logger.warning(
            "Test completed with %d warning(s): %s", len(_warnings), "; ".join(_warnings)
        )
        from projects.rhaiis.orchestration.notifications import send_pipeline_warning

        send_pipeline_warning(
            warnings=list(_warnings),
            model_key=model_key,
            workload_keys=workload_keys,
        )
        _warnings.clear()
        return 1

    return 0


def _run_workload_benchmark(
    *,
    model_key: str,
    workload_key: str,
    model_cfg: dict,
    accelerator: str,
    accelerator_key: str,
    gpu_type: str,
    serving_image: str,
    engine_args: dict,
    benchmark_cfg: dict,
    deployment_name: str,
    namespace: str,
    endpoint_url: str,
    benchmark_timeout: int,
    run_uuid: str,
    version: str,
    cluster_tag: str,
    trtllm_config: dict | None = None,
    mlflow_destination: dict[str, str] | None = None,
) -> None:
    """Run benchmark and post-processing for a single workload.

    Each workload gets its own NextArtifactDir with per-workload test labels so
    the Caliper parser treats them as separate test nodes and produces one
    dashboard CSV row-set per profile.
    """
    logger.info("=== Benchmark %s (UUID: %s) ===", workload_key, run_uuid)

    workload = runtime_config.get_workload(workload_key)
    rates = workload.get("rates", [1])
    max_seconds = workload.get("max_seconds", 180)
    rampup = workload.get("rampup")

    from projects.core.library import config
    from projects.guidellm.toolbox.run_guidellm_benchmark.main import (
        run as run_guidellm_benchmark,
    )

    run_benchmark = config.project.get_config("tests.rhaiis.run_benchmark", True)

    with env.NextArtifactDir(f"benchmark_{workload_key}"):
        _create_test_labels(
            model_key,
            workload_key,
            accelerator,
            engine_args,
            hf_model_id=model_cfg["hf_model_id"],
            version=version,
            serving_image=serving_image,
            cluster_tag=cluster_tag,
            accelerator_chip=gpu_type.upper(),
            run_uuid=run_uuid,
            trtllm_config=trtllm_config,
            mlflow_destination=mlflow_destination,
        )

        if not run_benchmark:
            logger.info("run_benchmark=false, skipping main benchmark")
            try:
                from projects.rhaiis.orchestration.analysis import run_standalone_analysis

                run_standalone_analysis(
                    model_cfg,
                    accelerator_key,
                    engine_args,
                    run_uuid=run_uuid,
                    restrict_profiles=[workload_key],
                )
            except Exception:
                logger.warning("Standalone analysis failed", exc_info=True)
                _warnings.append(f"Standalone analysis failed for {workload_key}")
        else:
            logger.info("Running benchmark at rates=%s for workload=%s", rates, workload_key)

            benchmark_image = benchmark_cfg.get("image", "ghcr.io/vllm-project/guidellm:v0.6.0")

            guidellm_args = runtime_config.build_guidellm_args(
                benchmark_cfg=benchmark_cfg,
                model_id=model_cfg["hf_model_id"],
                data=workload["data"],
                rates=rates,
                max_seconds=max_seconds,
                rampup=rampup,
            )

            run_guidellm_benchmark(
                endpoint_url=f"{endpoint_url}/v1",
                name=_guidellm_job_name("guidellm-bench", workload_key, deployment_name),
                namespace=namespace,
                image=benchmark_image,
                timeout=benchmark_timeout,
                pvc_size=benchmark_cfg.get("pvc_size", "5Gi"),
                guidellm_args=guidellm_args,
                hf_token_secret=benchmark_cfg.get("hf_token_secret", ""),
                fs_group=benchmark_cfg.get("fs_group"),
            )


def _create_test_labels(
    model_key: str,
    workload_key: str,
    accelerator: str,
    engine_args: dict,
    *,
    hf_model_id: str = "",
    version: str = "",
    serving_image: str = "",
    cluster_tag: str = "",
    accelerator_chip: str = "",
    run_uuid: str = "",
    trtllm_config: dict | None = None,
    mlflow_destination: dict[str, str] | None = None,
) -> None:
    _, image_tag = runtime_config.split_image_tag(serving_image) if serving_image else ("", "")
    parts = [f"{k}: {v}" for k, v in engine_args.items()]
    for key, value in (trtllm_config or {}).items():
        formatted_value = (
            json.dumps(value, separators=(",", ":")) if isinstance(value, (dict, list)) else value
        )
        parts.append(f"trtllm.{key}: {formatted_value}")
    runtime_args = "; ".join(parts)
    tp = (
        engine_args.get("tensor-parallel-size")
        or engine_args.get("tp-size")
        or engine_args.get("tp_size")
        or 1
    )
    labels = {
        "model_key": model_key,
        "workload_key": workload_key,
        "accelerator": accelerator_chip or accelerator,
        "tensor_parallel_size": str(tp),
        "hf_model_id": hf_model_id,
        "version": version,
        "image_tag": image_tag,
        "cluster_tag": cluster_tag,
        "runtime_args": runtime_args,
        "run_uuid": run_uuid,
    }

    write_test_labels(env.ARTIFACT_DIR, labels, mlflow_destination=mlflow_destination)
    logger.info("Created test labels: %s", labels)


def _set_mlflow_metadata(
    model_key: str,
    workload_key: str,
    model_cfg: dict,
    accelerator: str,
    serving_image: str,
    engine_args: dict,
    benchmark_cfg: dict,
    rates: list[int],
    max_seconds: int,
    namespace: str,
    deployment_name: str,
) -> None:
    from projects.core.library import config

    image_name, image_tag = runtime_config.split_image_tag(serving_image)
    guidellm_image = benchmark_cfg.get("image", "ghcr.io/vllm-project/guidellm:v0.6.0")
    benchmark_args = benchmark_cfg.get("args", {})
    tp = (
        engine_args.get("tensor-parallel-size")
        or engine_args.get("tp-size")
        or engine_args.get("tp_size")
        or 1
    )

    tags = {
        "project": "rhaiis",
        "model_key": model_key,
        "hf_model_id": model_cfg["hf_model_id"],
        "accelerator": accelerator,
        "tensor_parallel_size": str(tp),
        "serving_image": serving_image,
        "serving_version": image_tag,
        "workload_key": workload_key,
        "rates": ",".join(str(r) for r in rates),
        "max_seconds": str(max_seconds),
        "guidellm_image": guidellm_image,
        "namespace": namespace,
        "deployment_name": deployment_name,
    }
    for key, value in benchmark_args.items():
        tags[f"guidellm_{key}"] = str(value)

    config.project.set_config("caliper.export.backend.mlflow.config.tags", tags)
    logger.info("Set MLflow tags: %s", list(tags.keys()))


def _sync_postprocessed_dashboard_csv(model_key: str, workload_keys: list[str]) -> None:
    """Find the dashboard CSV generated by Caliper dashboard_csv and sync to S3."""
    from pathlib import Path

    from projects.core.library import config

    csv_dashboard_cfg = config.project.get_config("caliper.postprocess.csv_dashboard", {})
    if not csv_dashboard_cfg or not csv_dashboard_cfg.get("enabled", False):
        logger.info("Dashboard CSV S3 sync not enabled")
        return

    # Find the CSV produced by the dashboard_csv step in the postprocessing directory
    postprocess_dirs = sorted(Path(env.ARTIFACT_DIR).glob("*__postprocessing"))
    if not postprocess_dirs:
        logger.info("No postprocessing directory found, skipping dashboard CSV sync")
        return

    dashboard_csv_output = config.project.get_config(
        "caliper.postprocess.kpi.dashboard_csv.output", "dashboard.csv"
    )
    csv_path = postprocess_dirs[-1] / dashboard_csv_output
    if not csv_path.exists():
        logger.info("Dashboard CSV not found at %s, skipping sync", csv_path)
        return

    logger.info("Found postprocessed dashboard CSV: %s", csv_path)

    from projects.rhaiis.postprocess.s3_dashboard import sync_csv_to_s3

    s3_cfg = config.project.get_config("rhaiis.s3", {})
    sync_result = sync_csv_to_s3(
        csv_path,
        s3_bucket=s3_cfg.get("bucket", ""),
        s3_key=csv_dashboard_cfg.get("s3_key", ""),
        vault_name=s3_cfg.get("vault", ""),
        credentials_file=s3_cfg.get("credentials_file", "aws.credentials"),
        dry_run=config.project.get_config("caliper.export.dry_run", False),
    )
    logger.info("Dashboard CSV sync result: %s", sync_result)

    version = config.project.get_config("tests.rhaiis.version", "")
    compare_version = config.project.get_config("tests.rhaiis.compare_version", "")

    if compare_version and version:
        model_cfg = runtime_config.get_model(model_key)
        accelerator = runtime_config.get_accelerator()
        engine = runtime_config.get_engine()
        engine_defaults = runtime_config.get_engine_args(engine)
        first_workload = runtime_config.get_workload(workload_keys[0])
        ea = runtime_config.merge_engine_args(engine_defaults, model_cfg, first_workload, engine)

        from projects.rhaiis.orchestration.analysis import run_regression_check

        run_regression_check(
            csv_path,
            compare_version,
            version,
            model_cfg,
            accelerator,
            run_uuid="",
            engine_args=ea,
        )
        return


def _maybe_send_success_notification(model_key: str, workload_keys: list[str]) -> None:
    """Send a Slack success notification if slack_notify_always is set and no regression check."""
    from projects.core.library import config

    compare_version = config.project.get_config("tests.rhaiis.compare_version", "")
    if compare_version:
        return

    if not config.project.get_config("tests.rhaiis.slack_notify_always", False):
        return

    from projects.rhaiis.postprocess.regression import send_success_notification

    model_cfg = runtime_config.get_model(model_key)
    accelerator = runtime_config.get_accelerator()
    engine = runtime_config.get_engine()
    engine_defaults = runtime_config.get_engine_args(engine)
    first_workload = runtime_config.get_workload(workload_keys[0])
    ea = runtime_config.merge_engine_args(engine_defaults, model_cfg, first_workload, engine)
    tp = ea.get("tensor-parallel-size") or ea.get("tp-size") or ea.get("tp_size") or ""
    dp = ea.get("data-parallel-size") or ea.get("dp-size") or ""
    version = config.project.get_config("tests.rhaiis.version", "")

    send_success_notification(
        model=model_cfg.get("hf_model_id", ""),
        accelerator=accelerator,
        job_id=os.environ.get("FJOB_NAME", ""),
        slack_user=config.project.get_config("tests.rhaiis.slack_user", ""),
        notification_vault="psap-forge-notifications",
        tp=str(tp),
        dp=str(dp),
        version=version,
        workload_keys=workload_keys,
        cluster=config.project.get_config("rhaiis.cluster_tag", ""),
        engine=engine,
    )


def _upload_predictor_log(run_uuid: str) -> None:
    """Upload the captured predictor pod log to S3 as ``logs/{run_uuid}.log``."""
    from pathlib import Path

    from projects.core.library import config
    from projects.rhaiis.postprocess.s3_dashboard import upload_predictor_log_to_s3

    matches = sorted(
        Path(env.ARTIFACT_DIR).glob("*__capture_isvc_state/artifacts/inferenceservice.pods.log")
    )
    log_path = matches[-1] if matches else None
    if not log_path or not log_path.exists():
        logger.info("No predictor pod log found under %s, skipping upload", env.ARTIFACT_DIR)
        return

    s3_cfg = config.project.get_config("rhaiis.s3", {})

    result = upload_predictor_log_to_s3(
        log_path,
        run_uuid=run_uuid,
        s3_bucket=s3_cfg.get("bucket", ""),
        vault_name=s3_cfg.get("vault", ""),
        credentials_file=s3_cfg.get("credentials_file", "aws.credentials"),
        dry_run=config.project.get_config("caliper.export.dry_run", False),
    )
    logger.info("Predictor log upload result: %s", result)


def _run_warmup_step(
    *,
    deployment_name: str,
    namespace: str,
    endpoint_url: str,
    benchmark_cfg: dict,
    model_cfg: dict,
    workload: dict,
    workload_key: str,
    benchmark_timeout: int,
) -> None:
    """Run a short warmup benchmark to prime KV cache and CUDA kernels."""
    from projects.core.library import config
    from projects.guidellm.toolbox.run_guidellm_benchmark.main import (
        run as run_guidellm_benchmark,
    )

    warmup_cfg = config.project.get_config("rhaiis.warmup", {})
    warmup_rate = warmup_cfg.get("rate", 200)
    warmup_max_seconds = warmup_cfg.get("max_seconds", 60)

    guidellm_args = runtime_config.build_guidellm_args(
        benchmark_cfg=benchmark_cfg,
        model_id=model_cfg["hf_model_id"],
        data=workload["data"],
        rates=[warmup_rate],
        max_seconds=warmup_max_seconds,
    )

    logger.info("Running warmup (concurrency=%d, duration=%ds)", warmup_rate, warmup_max_seconds)
    try:
        run_guidellm_benchmark(
            endpoint_url=f"{endpoint_url}/v1",
            name=_guidellm_job_name("guidellm-warmup", workload_key, deployment_name),
            namespace=namespace,
            image=benchmark_cfg.get("image", "ghcr.io/vllm-project/guidellm:v0.6.0"),
            timeout=benchmark_timeout,
            pvc_size=benchmark_cfg.get("pvc_size", "5Gi"),
            guidellm_args=guidellm_args,
            hf_token_secret=benchmark_cfg.get("hf_token_secret", ""),
            fs_group=benchmark_cfg.get("fs_group"),
        )
        logger.info("Warmup completed")
    except Exception:
        logger.warning("Warmup failed; continuing with benchmark", exc_info=True)


def _run_profiler_step(
    *,
    deployment_name: str,
    namespace: str,
    endpoint_url: str,
    benchmark_cfg: dict,
    model_cfg: dict,
    workload: dict,
    workload_key: str,
    benchmark_timeout: int,
) -> None:
    """Run profiler-gated benchmarks: verify prereqs → enable gate → benchmark → disable gate → copy traces."""
    from projects.guidellm.toolbox.run_guidellm_benchmark.main import (
        run as run_guidellm_benchmark,
    )
    from projects.rhaiis.toolbox.copy_profiler_traces.main import run as copy_profiler_traces
    from projects.rhaiis.toolbox.enable_profiler_gate.main import run as enable_profiler_gate
    from projects.rhaiis.toolbox.verify_profiler_prereqs.main import run as verify_profiler_prereqs

    logger.info("Verifying profiler prerequisites")
    verify_profiler_prereqs(namespace=namespace)

    profiler_cfg = runtime_config.get_profiler_config()
    labels = profiler_cfg.get("labels", [])
    if not labels:
        labels = [_derive_profiler_label(workload)]
        logger.info("Auto-generated profiler label from workload: %s", labels[0])

    profiler_max_seconds = profiler_cfg.get("max_seconds", 60)

    for label in labels:
        logger.info("Profiling label=%s", label)

        gate_value = label if isinstance(label, str) else str(label)
        enable_profiler_gate(
            name=deployment_name,
            namespace=namespace,
            gate_value=gate_value,
        )

        profiler_rates = profiler_cfg.get("rates", [1])
        guidellm_args = runtime_config.build_guidellm_args(
            benchmark_cfg=benchmark_cfg,
            model_id=model_cfg["hf_model_id"],
            data=workload["data"],
            rates=profiler_rates,
            max_seconds=profiler_max_seconds,
        )

        try:
            run_guidellm_benchmark(
                endpoint_url=f"{endpoint_url}/v1",
                name=_guidellm_job_name("guidellm-profiler", workload_key, deployment_name),
                namespace=namespace,
                image=benchmark_cfg.get("image", "ghcr.io/vllm-project/guidellm:v0.6.0"),
                timeout=benchmark_timeout,
                pvc_size=benchmark_cfg.get("pvc_size", "5Gi"),
                guidellm_args=guidellm_args,
                hf_token_secret=benchmark_cfg.get("hf_token_secret", ""),
                fs_group=benchmark_cfg.get("fs_group"),
            )
        finally:
            enable_profiler_gate(
                name=deployment_name,
                namespace=namespace,
                disable=True,
            )

    logger.info("Copying profiler traces from pod")
    try:
        copy_profiler_traces(name=deployment_name, namespace=namespace)
    except Exception:
        logger.warning("Failed to copy profiler traces", exc_info=True)


def _derive_profiler_label(workload: dict) -> str:
    """Auto-generate a profiler label like 'isl1000_osl1000' from the workload data string."""
    data = workload.get("data", "")
    params = dict(item.split("=", 1) for item in data.split(",") if "=" in item)
    isl = params.get("prompt_tokens", "0")
    osl = params.get("output_tokens", "0")
    return f"isl{isl}_osl{osl}"


def _infer_profile_labels_from_traces(trace_files: list) -> list[str]:
    """Extract unique profile labels from trace filenames.

    Filenames follow the pattern: trace_rank{R}_pid{P}_run{LABEL}_range{S}-{E}.json
    The {LABEL} portion is the gate value used during profiling (e.g. 'isl1000_osl1000').
    """
    import re

    pattern = re.compile(r"_run(.+?)_range\d+-\d+")
    labels: list[str] = []
    seen: set[str] = set()
    for f in trace_files:
        m = pattern.search(f.name)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            labels.append(m.group(1))
    return labels


def _upload_profiler_traces(
    model_cfg: dict,
    accelerator: str,
    engine_args: dict,
    profiler_cfg: dict,
) -> None:
    from pathlib import Path

    from projects.core.library import config
    from projects.rhaiis.postprocess.s3_dashboard import upload_profiler_traces_to_s3

    trace_files = sorted(
        Path(env.ARTIFACT_DIR).glob("*__copy_profiler_traces/artifacts/traces/trace_*")
    )
    if not trace_files:
        logger.info("No profiler traces to upload")
        return

    traces_dir = trace_files[0].parent
    if len({f.parent for f in trace_files}) > 1:
        traces_dir = Path(env.ARTIFACT_DIR) / "artifacts" / "traces_combined"
        traces_dir.mkdir(parents=True, exist_ok=True)
        for f in trace_files:
            import shutil

            shutil.copy2(f, traces_dir / f.name)
    logger.info("Found %d profiler trace files in %s", len(trace_files), traces_dir)

    version = config.project.get_config("tests.rhaiis.version", "")
    if not version:
        logger.info("No version configured, skipping profiler trace upload")
        return

    profile_labels = profiler_cfg.get("labels", [])
    if not profile_labels:
        profile_labels = _infer_profile_labels_from_traces(trace_files)
        logger.info("Inferred profile labels from trace filenames: %s", profile_labels)

    s3_cfg = config.project.get_config("rhaiis.s3", {})
    result = upload_profiler_traces_to_s3(
        traces_dir,
        model_name=model_cfg.get("hf_model_id", ""),
        accelerator=accelerator,
        tp_size=int(
            engine_args.get("tensor-parallel-size")
            or engine_args.get("tp-size")
            or engine_args.get("tp_size")
            or 1
        ),
        version=version,
        profile_labels=profile_labels,
        s3_bucket=s3_cfg.get("bucket", ""),
        s3_prefix=profiler_cfg.get("s3_prefix", ""),
        vault_name=s3_cfg.get("vault", ""),
        credentials_file=s3_cfg.get("credentials_file", "aws.credentials"),
        dry_run=config.project.get_config("caliper.export.dry_run", False),
    )
    logger.info("Profiler trace upload result: %s", result)


def _capture_and_cleanup(deployment_name: str, namespace: str) -> None:
    from projects.rhaiis.toolbox.capture_isvc_state.main import run as capture_isvc_state

    logger.info("Capturing state")
    try:
        capture_isvc_state(name=deployment_name, namespace=namespace)
    except Exception:
        logger.warning("Capture failed, continuing with cleanup", exc_info=True)

    from projects.rhaiis.toolbox.cleanup_isvc.main import run as cleanup_isvc

    logger.info("Cleaning up")
    try:
        cleanup_isvc(name=deployment_name, namespace=namespace)
    except Exception:
        logger.warning("Cleanup failed", exc_info=True)
