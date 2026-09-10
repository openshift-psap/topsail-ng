from __future__ import annotations

import base64
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from projects.core.library import config as core_config
from projects.core.library import env
from projects.llm_d.orchestration import ci as llmd_ci
from projects.llm_d.orchestration import runtime_config, test_phase
from projects.llm_d.orchestration.render_inference_service import (
    render_inference_service_from_parts,
)
from projects.rhoai.library import deploy as rhoai_deploy

PROJECT_ORCHESTRATION_DIR = Path(__file__).resolve().parents[1] / "orchestration"


@pytest.fixture(autouse=True)
def _reset_project_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    env.init()
    core_config.project = None
    yield
    core_config.project = None


def _init_project_config() -> None:
    core_config.init(PROJECT_ORCHESTRATION_DIR, apply_cluster_config=False)


def test_deployment_presets_resolve_deployments() -> None:
    _init_project_config()

    core_config.project.apply_preset("deployment-approximate-prefix-cache")
    assert runtime_config.get_deployment_profile_name() == "approximate-prefix-cache"

    core_config.project.apply_preset("deployment-precise-prefix-cache")
    assert runtime_config.get_deployment_profile_name() == "precise-prefix-cache"

    core_config.project.apply_preset("deployment-distributed-default")
    assert runtime_config.get_deployment_profile_name() == "distributed-default"


def test_release_deployment_profiles_have_expected_shape() -> None:
    _init_project_config()

    core_config.project.set_config("runtime.deployment_profile", "release-approximate-prefix-cache")
    approximate = runtime_config.get_deployment_profile()
    core_config.project.set_config("runtime.deployment_profile", "release-precise-prefix-cache")
    precise = runtime_config.get_deployment_profile()
    core_config.project.set_config("runtime.deployment_profile", "release-distributed-default")
    distributed = runtime_config.get_deployment_profile()

    for profile in (approximate, precise, distributed):
        assert profile["replicas"] == 4
        assert profile["tensor_parallelism"] == 2

    assert isinstance(approximate["scheduler"], dict)
    assert isinstance(precise["scheduler"], dict)
    assert distributed["scheduler"] == {}


def test_active_profile_inference_service_name_matches_rendered_name() -> None:
    _init_project_config()
    core_config.project.set_config("runtime.deployment_profile", "precise-prefix-cache")

    assert runtime_config.get_inference_service_name() == "llm-d-precise-prefix-cache"


@pytest.mark.parametrize(
    ("preset", "expected_deployment"),
    [
        ("smoke", "approximate-prefix-cache"),
        ("smoke-precise", "precise-prefix-cache"),
        ("smoke-default-scheduler", "distributed-default"),
    ],
)
def test_smoke_presets_inherit_deployment_modes(preset: str, expected_deployment: str) -> None:
    _init_project_config()
    core_config.project.apply_preset(preset)
    assert runtime_config.get_deployment_profile_name() == expected_deployment
    assert runtime_config.get_model_name() == "Qwen/Qwen3-0.6B"
    # Only the base "smoke" preset enables benchmarking via runtime.benchmark_key: short
    if preset == "smoke":
        assert runtime_config.get_benchmark_config() is not None
    else:
        assert runtime_config.get_benchmark_config() is None


def test_benchmark_workloads_are_available() -> None:
    _init_project_config()

    short = core_config.project.get_config("workloads.benchmarks.short", print=False)
    concurrent = core_config.project.get_config(
        "workloads.benchmarks.concurrent-1k-1k", print=False
    )
    heavy = core_config.project.get_config("workloads.benchmarks.heavy-heterogeneous", print=False)
    multi_turn = core_config.project.get_config("workloads.benchmarks.multi-turn", print=False)

    for benchmark in (short, concurrent, heavy):
        assert benchmark["timeout_seconds"] == 3600
    assert multi_turn["timeout_seconds"] == 7200

    assert concurrent["benchconf"] == "llm-d/concurrent-1k-1k"
    assert heavy["benchconf"] == "llm-d/concurrent-heavy-heterogeneous"
    assert multi_turn["args"]["rate"] == [32, 64, 128, 256, 512]
    assert "turns=5" in multi_turn["args"]["data"]
    assert "prefix_count={2*rate}" in multi_turn["args"]["data"]
    assert multi_turn["args"]["max_requests"] == "{10*rate}"


def test_benchmark_resolution_applies_workload_defaults_and_per_benchmark_overrides() -> None:
    _init_project_config()

    core_config.project.set_config("runtime.benchmark_key", "concurrent-1k-1k")
    concurrent = runtime_config.get_benchmark_config()
    assert concurrent is not None
    assert concurrent["job_name"] == "guidellm-benchmark"
    assert concurrent["image"] == "ghcr.io/vllm-project/guidellm:v0.5.4"
    assert concurrent["pvc_size"] == "1Gi"
    assert concurrent["timeout_seconds"] == 3600

    core_config.project.set_config("runtime.benchmark_key", "multi-turn")
    multi_turn = runtime_config.get_benchmark_config()
    assert multi_turn is not None
    assert multi_turn["job_name"] == "guidellm-benchmark"
    assert multi_turn["image"] == "ghcr.io/vllm-project/guidellm:v0.5.4"
    assert multi_turn["pvc_size"] == "1Gi"
    assert multi_turn["timeout_seconds"] == 7200


def test_guidellm_benchmark_uses_original_model_name_as_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_project_config()
    core_config.project.set_config("runtime.model_name", "openai/gpt-oss-120b")
    core_config.project.set_config("runtime.deployment_profile", "release-distributed-default")
    core_config.project.set_config("runtime.benchmark_key", "concurrent-1k-1k")
    core_config.project.set_config("prom.capture.enabled", False)

    captured: dict[str, object] = {}

    def _fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    mock_config_path = Path("/mock/benchconf/config.yaml")
    monkeypatch.setattr(test_phase.run_guidellm_benchmark_command, "run", _fake_run)
    monkeypatch.setattr(
        test_phase.benchconf_lib, "resolve_config_path", lambda ref: mock_config_path
    )
    monkeypatch.setattr(test_phase.benchconf_lib, "_is_enabled", lambda: True)
    test_phase.run_guidellm_benchmark(endpoint_url="https://example.test/llm-d")

    assert captured["timeout"] == 3600
    assert captured["config_path"] == mock_config_path
    guidellm_args = captured["guidellm_args"]
    assert isinstance(guidellm_args, list)
    assert "--processor=openai/gpt-oss-120b" in guidellm_args


def test_release_preset_expands_benchmark_list_and_merges_workload_args() -> None:
    _init_project_config()

    core_config.project.apply_preset("cpt-release-testing-gpt-oss-120b")

    assert (
        core_config.project.get_config(
            "caliper.export.backend.mlflow.config.experiment", print=False
        )
        == "cpt-llm-d"
    )
    assert (
        core_config.project.get_config("cpt.kpi.labels.test_harness", print=False)
        == "rhoai-release"
    )

    assert core_config.project.get_config("runtime.deployment_profile", print=False) == [
        "release-distributed-default",
        "release-precise-prefix-cache",
        "release-approximate-prefix-cache",
    ]
    assert runtime_config.get_model_cache_config()["pvc"]["size"] == "300Gi"
    assert runtime_config.get_benchmark_keys() == [
        "concurrent-1k-1k",
        "heavy-heterogeneous",
        "multi-turn",
    ]

    run_specs = runtime_config.get_run_specs()
    for run_spec in run_specs:
        with runtime_config.activate_run_spec(run_spec):
            benchmark = runtime_config.get_benchmark_config()
            assert benchmark["args"]["request_type"] == "text_completions"


def test_gpt_release_preset_produces_deployment_workload_matrix() -> None:
    _init_project_config()

    core_config.project.apply_preset("cpt-release-testing-gpt-oss-120b")

    run_specs = runtime_config.get_run_specs()

    assert len(run_specs) == 9
    assert {spec.benchmark_key for spec in run_specs} == {
        "concurrent-1k-1k",
        "heavy-heterogeneous",
        "multi-turn",
    }
    assert all(spec.model_name == "openai/gpt-oss-120b" for spec in run_specs)
    assert {spec.deployment_profile_name for spec in run_specs} == {
        "release-distributed-default",
        "release-precise-prefix-cache",
        "release-approximate-prefix-cache",
    }


def test_llama_release_preset_produces_deployment_workload_matrix() -> None:
    _init_project_config()

    core_config.project.apply_preset("cpt-release-testing-llama-33-70b")

    assert (
        core_config.project.get_config(
            "caliper.export.backend.mlflow.config.experiment", print=False
        )
        == "cpt-llm-d"
    )
    assert (
        core_config.project.get_config("cpt.kpi.labels.test_harness", print=False)
        == "rhoai-release"
    )

    run_specs = runtime_config.get_run_specs()
    assert len(run_specs) == 9
    assert {spec.deployment_profile_name for spec in run_specs} == {
        "release-distributed-default",
        "release-precise-prefix-cache",
        "release-approximate-prefix-cache",
    }
    assert {spec.benchmark_key for spec in run_specs} == {
        "concurrent-1k-1k",
        "heavy-heterogeneous",
        "multi-turn",
    }
    assert {spec.namespace for spec in run_specs} == {"forge-llm-d"}


def test_test_matrix_continues_after_a_failed_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    _init_project_config()
    run_specs = [
        SimpleNamespace(
            deployment_profile_name="distributed-default",
            benchmark_key="multi-turn",
            artifact_dirname="failed-entry",
        ),
        SimpleNamespace(
            deployment_profile_name="precise-prefix-cache",
            benchmark_key="multi-turn",
            artifact_dirname="next-entry",
        ),
    ]
    calls: list[str] = []

    monkeypatch.setattr(test_phase.runtime_config, "get_run_specs", lambda: run_specs)
    monkeypatch.setattr(
        test_phase.runtime_config,
        "activate_run_spec",
        lambda _run_spec: nullcontext(),
    )

    def _test_entry() -> int:
        calls.append("run")
        if len(calls) == 1:
            raise RuntimeError("expected test failure")
        return 0

    monkeypatch.setattr(test_phase, "do_test", _test_entry)

    assert test_phase.run_all_tests(stop_on_error=False) == 1
    assert calls == ["run", "run"]


def test_precise_profile_preserves_kv_events_json_for_the_serving_eval() -> None:
    _init_project_config()
    core_config.project.set_config("runtime.deployment_profile", "release-precise-prefix-cache")

    profile = runtime_config.get_deployment_profile()
    args = profile["vllm_extra"]["args"]
    assert args["kv_events_config"].startswith('\'{"enable_kv_cache_events"')


def test_ci_init_uses_framework_project_args_preset_and_keeps_var_overrides() -> None:
    variable_overrides_path = env.ARTIFACT_DIR / "000__ci_metadata" / "variable_overrides.yaml"
    variable_overrides_path.parent.mkdir(parents=True, exist_ok=True)
    variable_overrides_path.write_text(
        yaml.safe_dump(
            {
                "project.args": ["cpt-release-testing-gpt-oss-120b"],
                "runtime.benchmark_key": "multi-turn",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    llmd_ci.init()

    assert runtime_config.get_model_name() == "openai/gpt-oss-120b"
    assert core_config.project.get_config("runtime.deployment_profile", print=False) == [
        "release-distributed-default",
        "release-precise-prefix-cache",
        "release-approximate-prefix-cache",
    ]
    assert runtime_config.get_benchmark_keys() == ["multi-turn"]


def test_list_vaults_only_includes_rhoai_custom_catalog_vaults_for_custom_catalog_runs() -> None:
    _init_project_config()

    core_config.project.set_config("platform.rhoai.custom_catalog.enabled", False)
    assert "psap-rhoai-rc" not in llmd_ci.list_vaults()
    assert "psap-forge-staging-image-pull" not in llmd_ci.list_vaults()

    core_config.project.set_config("platform.rhoai.custom_catalog.enabled", True)
    assert "psap-rhoai-rc" in llmd_ci.list_vaults()
    assert "psap-forge-staging-image-pull" in llmd_ci.list_vaults()


def test_prepare_phase_adds_rhoai_custom_catalog_vaults_only_for_custom_catalog_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_project_config()

    calls: list[dict[str, object]] = []

    def _fake_init(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(llmd_ci.vault, "init", _fake_init)

    core_config.project.set_config("platform.rhoai.custom_catalog.enabled", False)
    llmd_ci.init_vaults_for_phase("prepare")

    core_config.project.set_config("platform.rhoai.custom_catalog.enabled", True)
    llmd_ci.init_vaults_for_phase("prepare")

    assert "psap-rhoai-rc" not in calls[0]["mandatory_vaults"]
    assert "psap-forge-staging-image-pull" not in calls[0]["mandatory_vaults"]
    assert "psap-rhoai-rc" in calls[1]["mandatory_vaults"]
    assert "psap-forge-staging-image-pull" in calls[1]["mandatory_vaults"]


def test_prepare_rhoai_operator_runs_registry_setup_before_custom_catalog_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_project_config()

    platform = {
        "operators": {
            "rhcl-operator": {
                "package": "rhcl-operator",
                "namespace": "openshift-operators",
                "source": "redhat-operators",
                "channel": "stable",
            },
            "rhods-operator": {
                "package": "rhods-operator",
                "namespace": "redhat-ods-operator",
                "source": "redhat-operators",
                "channel": "stable-3.x",
            },
        },
        "rhoai": {
            "custom_catalog": {
                "enabled": True,
                "name": "rhoai-catalog-dev",
                "namespace": "openshift-marketplace",
                "image": "quay.io/rhoai/rhoai-fbc-fragment@sha256:test",
                "display_name": "Red Hat OpenShift AI",
                "publisher": "RHOAI Development Catalog",
                "pull_secret": {
                    "vault": {
                        "name": "psap-rhoai-rc",
                        "content": "rhoai_rc.secret",
                    }
                },
                "staging_pull_secret": {
                    "vault": {
                        "name": "psap-forge-staging-image-pull",
                        "content": ".dockerconfigjson",
                    }
                },
            },
            "namespace": "redhat-ods-applications",
            "datasciencecluster_name": "default-dsc",
            "components": ["kserve"],
            "required_crds_before_dsc": ["datascienceclusters.datasciencecluster.opendatahub.io"],
            "required_crds_after_dsc": ["llminferenceservices.serving.kserve.io"],
        },
    }

    calls: list[object] = []

    monkeypatch.setattr(
        rhoai_deploy, "prepare_rhcl_operator", lambda platform: calls.append("rhcl")
    )
    monkeypatch.setattr(
        rhoai_deploy,
        "prepare_rhoai_pull_secret",
        lambda custom_catalog: calls.append(("pull", custom_catalog.image)),
    )
    monkeypatch.setattr(
        rhoai_deploy,
        "deploy_rhoai_custom_catalog",
        lambda custom_catalog: calls.append(("catalog", custom_catalog.publisher)),
    )
    monkeypatch.setattr(
        rhoai_deploy,
        "ensure_operator_subscription",
        lambda operator_spec: calls.append(("sub", operator_spec["package"])),
    )
    monkeypatch.setattr(
        rhoai_deploy,
        "ensure_required_crds_before_dsc",
        lambda rhoai: calls.append("crds"),
    )

    rhoai_deploy.prepare_rhoai_operator(
        platform=platform,
        rhoai=platform["rhoai"],
        icsp_applier=lambda: calls.append("icsp"),
    )

    assert calls == [
        "rhcl",
        ("pull", "quay.io/rhoai/rhoai-fbc-fragment@sha256:test"),
        "icsp",
        ("catalog", "RHOAI Development Catalog"),
        ("sub", "rhods-operator"),
        "crds",
    ]


def test_custom_catalog_pull_secret_path_uses_explicit_vault_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _fake_get_vault_content_path(vault_name: str, content_name: str):
        captured["vault_name"] = vault_name
        captured["content_name"] = content_name
        return Path("/tmp/rhoai_rc.secret")

    monkeypatch.setattr(rhoai_deploy.vault, "get_vault_content_path", _fake_get_vault_content_path)

    path = rhoai_deploy.custom_catalog_pull_secret_path(
        {
            "pull_secret": {
                "vault": {
                    "name": "psap-rhoai-rc",
                    "content": "rhoai_rc.secret",
                }
            },
        }
    )

    assert path == Path("/tmp/rhoai_rc.secret")
    assert captured == {
        "vault_name": "psap-rhoai-rc",
        "content_name": "rhoai_rc.secret",
    }


def test_wait_for_rhoai_pull_secret_ready_treats_empty_mcp_status_as_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_outputs = iter(["", "True"])
    clock = {"value": 0.0}

    def _fake_monotonic() -> float:
        value = clock["value"]
        clock["value"] += 0.1
        return value

    def _fake_sleep(_seconds: float) -> None:
        clock["value"] += 0.1

    def _fake_oc_get_json(*args, **kwargs):
        payload = {
            "auths": {
                "quay.io": {"auth": "token-a"},
                "registry.stage.redhat.io": {"auth": "token-b"},
            }
        }
        return {
            "data": {
                ".dockerconfigjson": base64.b64encode(json.dumps(payload).encode("utf-8")).decode(
                    "utf-8"
                )
            }
        }

    def _fake_oc(*args, **kwargs):
        if args[:2] == ("get", "mcp"):
            return type("Result", (), {"stdout": next(mcp_outputs), "returncode": 0})()
        raise AssertionError(f"Unexpected oc call: {args}")

    monkeypatch.setattr(rhoai_deploy, "oc_get_json", _fake_oc_get_json)
    monkeypatch.setattr(rhoai_deploy, "oc", _fake_oc)
    monkeypatch.setattr(rhoai_deploy.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(rhoai_deploy.time, "sleep", _fake_sleep)

    rhoai_deploy.wait_for_rhoai_pull_secret_ready(timeout_seconds=1, poll_interval_seconds=0)


def test_registries_present_accepts_parent_registry_auth_entries() -> None:
    pull_secret = {
        "auths": {
            "quay.io": {"auth": "token-a"},
            "registry.stage.redhat.io": {"auth": "token-b"},
        }
    }

    assert rhoai_deploy._registries_present(
        pull_secret,
        (
            "quay.io/rhoai",
            "registry.stage.redhat.io/rhaii",
            "registry.stage.redhat.io/rhaii-early-access",
        ),
    )


def test_prepare_rhoai_pull_secret_merges_dockerconfigjson_registry_auths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _init_project_config()

    rc_pull_secret_path = tmp_path / "rhoai_rc.secret"
    rc_pull_secret_path.write_text(
        "rhoai+rhoai_external_readonly_bot:token-c",
        encoding="utf-8",
    )

    staging_pull_secret_path = tmp_path / ".dockerconfigjson"
    staging_pull_secret_path.write_text(
        json.dumps(
            {
                "auths": {
                    "registry.stage.redhat.io/rhaii": {"auth": "token-a"},
                    "registry.stage.redhat.io/rhaii-early-access": {"auth": "token-b"},
                }
            }
        ),
        encoding="utf-8",
    )

    current_secret = {
        "data": {
            ".dockerconfigjson": base64.b64encode(json.dumps({"auths": {}}).encode("utf-8")).decode(
                "utf-8"
            )
        }
    }
    merged_payload: dict[str, object] = {}

    monkeypatch.setattr(
        rhoai_deploy.vault,
        "get_vault_content_path",
        lambda vault_name, content_name: (
            rc_pull_secret_path
            if (vault_name, content_name) == ("psap-rhoai-rc", "rhoai_rc.secret")
            else staging_pull_secret_path
        ),
    )
    monkeypatch.setattr(rhoai_deploy, "oc_get_json", lambda *args, **kwargs: current_secret)

    def _fake_oc(*args, **kwargs):
        if args[:2] == ("set", "data"):
            from_file = next((arg for arg in args if str(arg).startswith("--from-file=")), "")
            payload_path = Path(from_file.rsplit("=", 1)[1])
            merged_payload.update(json.loads(payload_path.read_text(encoding="utf-8")))
            return type("Result", (), {"stdout": "", "returncode": 0})()
        if args[:2] == ("registry", "login"):
            registry = next(
                arg.split("=", 1)[1] for arg in args if str(arg).startswith("--registry=")
            )
            auth_basic = next(
                arg.split("=", 1)[1] for arg in args if str(arg).startswith("--auth-basic=")
            )
            temp_path = Path(
                next(
                    arg
                    for arg in args
                    if not str(arg).startswith("--") and arg != "registry" and arg != "login"
                )
            )
            current = json.loads(temp_path.read_text(encoding="utf-8"))
            current.setdefault("auths", {})[registry] = {"auth": auth_basic}
            temp_path.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
            return type("Result", (), {"stdout": "", "returncode": 0})()
        raise AssertionError(f"Unexpected oc call: {args}")

    monkeypatch.setattr(rhoai_deploy, "oc", _fake_oc)
    monkeypatch.setattr(rhoai_deploy, "wait_for_rhoai_pull_secret_ready", lambda **kwargs: None)

    rhoai_deploy.prepare_rhoai_pull_secret(
        rhoai_deploy.RhoaiCustomCatalogConfig.model_validate(
            {
                "enabled": True,
                "name": "rhoai-catalog-dev",
                "namespace": "openshift-marketplace",
                "image": "quay.io/rhoai/rhoai-fbc-fragment@sha256:test",
                "display_name": "Red Hat OpenShift AI",
                "publisher": "RHOAI Development Catalog",
                "pull_secret": {"vault": {"name": "psap-rhoai-rc", "content": "rhoai_rc.secret"}},
                "staging_pull_secret": {
                    "vault": {
                        "name": "psap-forge-staging-image-pull",
                        "content": ".dockerconfigjson",
                    }
                },
            }
        )
    )

    assert (
        merged_payload["auths"]["quay.io/rhoai"]["auth"]
        == "rhoai+rhoai_external_readonly_bot:token-c"
    )
    assert merged_payload["auths"]["registry.stage.redhat.io/rhaii"]["auth"] == "token-a"
    assert (
        merged_payload["auths"]["registry.stage.redhat.io/rhaii-early-access"]["auth"] == "token-b"
    )


def test_model_and_deployment_profile_accept_yaml_list_strings() -> None:
    _init_project_config()

    core_config.project.set_config(
        "runtime.model_name",
        "[openai/gpt-oss-120b, Qwen/Qwen3-0.6B]",
    )
    core_config.project.set_config(
        "runtime.deployment_profile",
        "[distributed-default, precise-prefix-cache]",
    )

    run_specs = runtime_config.get_run_specs()

    assert [(spec.model_name, spec.deployment_profile_name) for spec in run_specs] == [
        ("openai/gpt-oss-120b", "distributed-default"),
        ("openai/gpt-oss-120b", "precise-prefix-cache"),
        ("Qwen/Qwen3-0.6B", "distributed-default"),
        ("Qwen/Qwen3-0.6B", "precise-prefix-cache"),
    ]
    assert run_specs[0].model_slug == "openai-gpt-oss-120b"
    assert run_specs[2].model_slug == "qwen-qwen3-0-6b"
    # With a single benchmark_key (or null), benchmark fields reflect the scalar
    assert all(spec.benchmark_key is not None or spec.benchmark_slug is None for spec in run_specs)


def test_3d_matrix_with_multiple_benchmark_keys() -> None:
    _init_project_config()

    core_config.project.set_config(
        "runtime.model_name",
        ["openai/gpt-oss-120b", "Qwen/Qwen3-0.6B"],
    )
    core_config.project.set_config(
        "runtime.deployment_profile",
        ["distributed-default", "precise-prefix-cache"],
    )
    core_config.project.set_config(
        "runtime.benchmark_key",
        ["concurrent-1k-1k", "multi-turn"],
    )

    run_specs = runtime_config.get_run_specs()

    # 2 models x 2 profiles x 2 benchmarks = 8 specs
    assert len(run_specs) == 8
    assert all(spec.benchmark_key is not None for spec in run_specs)
    assert all(spec.benchmark_slug is not None for spec in run_specs)

    assert run_specs[0].model_name == "openai/gpt-oss-120b"
    assert run_specs[0].deployment_profile_name == "distributed-default"
    assert run_specs[0].benchmark_key == "concurrent-1k-1k"


def test_null_benchmark_key_produces_smoke_only_specs() -> None:
    _init_project_config()

    core_config.project.set_config("runtime.benchmark_key", None)

    run_specs = runtime_config.get_run_specs()

    assert len(run_specs) == 1
    assert run_specs[0].benchmark_key is None
    assert run_specs[0].benchmark_slug is None


def test_single_benchmark_key_backward_compatible() -> None:
    _init_project_config()

    core_config.project.apply_preset("smoke")

    run_specs = runtime_config.get_run_specs()

    assert len(run_specs) == 1
    assert run_specs[0].benchmark_key == "short"
    assert run_specs[0].benchmark_slug == "short"
    assert run_specs[0].artifact_dirname == "llmd__short__approximate-prefix-cache"


def test_activate_run_spec_sets_benchmark_key() -> None:
    _init_project_config()

    core_config.project.set_config(
        "runtime.benchmark_key",
        "[concurrent-1k-1k, multi-turn]",
    )

    run_specs = runtime_config.get_run_specs()

    for run_spec in run_specs:
        with runtime_config.activate_run_spec(run_spec):
            keys = runtime_config.get_benchmark_keys()
            assert keys == [run_spec.benchmark_key]
            assert runtime_config.get_benchmark_config() is not None


def test_benchmark_deployment_overrides() -> None:
    _init_project_config()

    core_config.project.set_config("runtime.benchmark_key", "concurrent-1k-1k")
    core_config.project.config["workloads"]["benchmarks"]["concurrent-1k-1k"][
        "deployment_overrides"
    ] = {"vllm_args": ["--max-model-len=8192"]}

    overrides = runtime_config.get_benchmark_deployment_overrides()
    assert overrides == {"vllm_args": ["--max-model-len=8192"]}


def test_benchmark_deployment_overrides_empty_when_not_set() -> None:
    _init_project_config()

    core_config.project.set_config("runtime.benchmark_key", "concurrent-1k-1k")

    overrides = runtime_config.get_benchmark_deployment_overrides()
    assert overrides == {}


def test_benchmark_deployment_overrides_empty_when_no_benchmark() -> None:
    _init_project_config()

    core_config.project.set_config("runtime.benchmark_key", None)

    overrides = runtime_config.get_benchmark_deployment_overrides()
    assert overrides == {}


def test_render_uses_sanitized_model_name_and_profile_resources() -> None:
    _init_project_config()
    core_config.project.set_config("model_cache.enabled", False)
    core_config.project.set_config("runtime.model_name", "openai/gpt-oss-120b")
    core_config.project.set_config("runtime.deployment_profile", "release-distributed-default")

    manifest = render_inference_service_from_parts(
        config_dir=str(PROJECT_ORCHESTRATION_DIR),
        namespace="forge-llm-d",
        inference_service=runtime_config.get_platform_config()["inference_service"],
        model_name=runtime_config.get_model_name(),
        model_slug=runtime_config.get_model_slug(),
        deployment_profile=runtime_config.get_deployment_profile(),
        model_cache=runtime_config.get_model_cache_config(),
    )

    assert manifest["spec"]["replicas"] == 4
    assert manifest["spec"]["model"]["uri"] == "hf://openai/gpt-oss-120b"
    assert manifest["spec"]["model"]["name"] == "openai-gpt-oss-120b"
    assert manifest["spec"]["template"]["containers"][0]["resources"] == {
        "requests": {"nvidia.com/gpu": "2"},
        "limits": {"nvidia.com/gpu": "2"},
    }
    assert manifest["spec"]["router"]["scheduler"] == {}


def test_render_uses_embedded_scheduler_config() -> None:
    _init_project_config()
    core_config.project.set_config("model_cache.enabled", False)
    core_config.project.set_config("runtime.model_name", "Qwen/Qwen3-0.6B")
    core_config.project.set_config("runtime.deployment_profile", "approximate-prefix-cache")

    manifest = render_inference_service_from_parts(
        config_dir=str(PROJECT_ORCHESTRATION_DIR),
        namespace="forge-llm-d",
        inference_service=runtime_config.get_platform_config()["inference_service"],
        model_name=runtime_config.get_model_name(),
        model_slug=runtime_config.get_model_slug(),
        deployment_profile=runtime_config.get_deployment_profile(),
        model_cache=runtime_config.get_model_cache_config(),
    )

    scheduler = manifest["spec"]["router"]["scheduler"]
    assert scheduler["template"]["containers"][0]["args"][-2] == "--config-text"
    assert "EndpointPickerConfig" in scheduler["template"]["containers"][0]["args"][-1]


def test_render_removes_scheduler_when_deployment_requests_null_scheduler() -> None:
    _init_project_config()
    core_config.project.set_config("model_cache.enabled", False)
    core_config.project.config["deployments"]["profiles"]["no-scheduler"] = {
        "replicas": 1,
        "tensor_parallelism": 1,
        "scheduler": None,
        "vllm_args": [],
    }
    core_config.project.set_config("runtime.model_name", "Qwen/Qwen3-0.6B")
    core_config.project.set_config("runtime.deployment_profile", "no-scheduler")

    manifest = render_inference_service_from_parts(
        config_dir=str(PROJECT_ORCHESTRATION_DIR),
        namespace="forge-llm-d",
        inference_service=runtime_config.get_platform_config()["inference_service"],
        model_name=runtime_config.get_model_name(),
        model_slug=runtime_config.get_model_slug(),
        deployment_profile=runtime_config.get_deployment_profile(),
        model_cache=runtime_config.get_model_cache_config(),
    )

    assert "scheduler" not in manifest["spec"]["router"]


def test_benchmark_job_name_from_activated_spec() -> None:
    _init_project_config()

    core_config.project.apply_preset("cpt-release-testing-gpt-oss-120b")

    for run_spec in runtime_config.get_run_specs():
        with runtime_config.activate_run_spec(run_spec):
            assert runtime_config.get_benchmark_job_name() == "guidellm-benchmark"


def test_smoke_preset_benchmark_behavior() -> None:
    _init_project_config()

    core_config.project.apply_preset("smoke")
    # The smoke preset enables the short benchmark
    assert runtime_config.get_benchmark_keys() == ["short"]

    run_spec = runtime_config.get_run_specs()[0]
    with runtime_config.activate_run_spec(run_spec):
        assert runtime_config.get_benchmark_job_name() == "guidellm-benchmark"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("no", ["no"]),
        ("yes", ["yes"]),
        ("null", ["null"]),
        ("123", ["123"]),
        ("1.5", ["1.5"]),
        ("meta-llama/Llama-3.1-8B-Instruct", ["meta-llama/Llama-3.1-8B-Instruct"]),
        # Properly quoted list strings (valid Python literals):
        (
            "['openai/gpt-oss-120b', \"Qwen/Qwen3-0.6B\"]",
            ["openai/gpt-oss-120b", "Qwen/Qwen3-0.6B"],
        ),
        # Edge cases now handled by ast.literal_eval:
        ("['a', 'b,c']", ["a", "b,c"]),  # Comma inside quotes
        ("[1, 2, 3]", ["1", "2", "3"]),  # Numbers in brackets
        # Actual Python lists (from YAML parsing):
        (["a", "b"], ["a", "b"]),
        ([1, 2, 3], ["1", "2", "3"]),
    ],
)
def test_normalize_string_or_list_treats_scalars_as_literals(raw: str, expected: list[str]) -> None:
    assert runtime_config._normalize_string_or_list(raw, "runtime.test") == expected


def test_render_supports_oci_model_uri() -> None:
    _init_project_config()
    core_config.project.set_config("model_cache.enabled", True)
    core_config.project.set_config(
        "runtime.model_name",
        "oci://registry.redhat.io/rhelai1/modelcar-llama-3-1-8b-instruct-fp8-dynamic:1.5",
    )
    core_config.project.set_config("runtime.deployment_profile", "distributed-default")

    manifest = render_inference_service_from_parts(
        config_dir=str(PROJECT_ORCHESTRATION_DIR),
        namespace="forge-llm-d",
        inference_service=runtime_config.get_platform_config()["inference_service"],
        model_name=runtime_config.get_model_name(),
        model_slug=runtime_config.get_model_slug(),
        deployment_profile=runtime_config.get_deployment_profile(),
        model_cache=runtime_config.get_model_cache_config(),
    )

    # Should use PVC-cached URI when model cache is enabled
    assert manifest["spec"]["model"]["uri"].startswith("pvc://")
    # Model slug should sanitize the full OCI path (truncated to 32 chars)
    assert manifest["spec"]["model"]["name"] == "oci-registry-redhat-io-rhelai1-m"


def test_get_model_uri_detects_scheme() -> None:
    _init_project_config()

    # Plain name → hf:// prefix
    core_config.project.set_config("runtime.model_name", "meta-llama/Llama-3.1-8B")
    assert runtime_config.get_model_uri() == "hf://meta-llama/Llama-3.1-8B"

    # OCI URI → passed through
    core_config.project.set_config("runtime.model_name", "oci://registry.example.com/model:tag")
    assert runtime_config.get_model_uri() == "oci://registry.example.com/model:tag"

    # HF URI → passed through
    core_config.project.set_config("runtime.model_name", "hf://Qwen/Qwen3-0.6B")
    assert runtime_config.get_model_uri() == "hf://Qwen/Qwen3-0.6B"
