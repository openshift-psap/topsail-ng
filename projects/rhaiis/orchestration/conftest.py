"""Pytest configuration for RHAIIS orchestration unit tests."""

from __future__ import annotations

import pytest

from projects.rhaiis.orchestration import runtime_config


def pytest_ignore_collect(collection_path, config):
    """Skip CI entrypoints that match test_*.py but are not pytest tests."""
    return collection_path.name in {"test_rhaiis.py", "test_phase.py"}


@pytest.fixture(scope="module", autouse=True)
def _init_rhaiis_config():
    runtime_config.init()
