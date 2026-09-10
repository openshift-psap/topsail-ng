"""Resolve benchmark configurations from the benchconf package.

This module bridges the benchconf package (external benchmark config repository)
with forge's benchmark runner. It resolves a benchconf reference to a local
``Path`` that the toolbox can read and embed in the GuideLLM container.
"""

from __future__ import annotations

import logging
from pathlib import Path

from projects.core.library import config

logger = logging.getLogger(__name__)


def _is_enabled() -> bool:
    """Return whether benchconf resolution is enabled in the project config."""
    return config.project.get_config("benchconf.enabled", True, print=False)


def set_version(repo: str, version: str) -> None:
    """Install a specific version of the benchconf package at runtime.

    Useful during development to pin a branch or commit without rebuilding
    the container image.

    Args:
        repo: Git repository URL (e.g. ``git+https://github.com/openshift-psap/benchconf``).
        version: Git ref to install (branch, tag, or commit SHA).
    """
    import subprocess
    import sys

    spec = f"benchconf @ {repo}@{version}"
    logger.info("Installing benchconf: %s", spec)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", spec])


def resolve_config_path(benchconf_ref: str) -> Path:
    """Resolve a benchconf reference to a local config file path.

    Args:
        benchconf_ref: Reference in ``suite/name`` format
            (e.g. ``"llm-d/concurrent-1k-1k"``).

    Returns:
        Path to the resolved YAML config file on the local filesystem.
    """
    try:
        import benchconf
    except ImportError as exc:
        raise ImportError(
            "benchconf package is required for benchconf-based benchmarks. "
            "Install with: pip install 'benchconf @ git+https://github.com/openshift-psap/benchconf'"
        ) from exc

    parts = benchconf_ref.split("/", 1)
    if len(parts) != 2:
        raise ValueError(
            f"Invalid benchconf reference '{benchconf_ref}'. "
            "Expected format: 'suite/name' (e.g. 'llm-d/concurrent-1k-1k')"
        )

    suite, name = parts
    config_path = benchconf.get_config(suite, name)
    logger.info("Resolved benchconf '%s' to %s", benchconf_ref, config_path)
    return config_path
