"""Shared pytest fixtures for fournos_launcher tests."""

import pytest

import projects.core.library.env as env
from projects.core.dsl.script_manager import reset_script_manager


@pytest.fixture(autouse=True)
def _dsl_isolation(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    monkeypatch.setenv("ARTIFACT_DIR", str(artifact))
    monkeypatch.chdir(env.FORGE_HOME)
    env.init()
    reset_script_manager()
    yield
    reset_script_manager()
