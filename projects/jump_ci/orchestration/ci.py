#!/usr/bin/env python3
"""
Jump CI Project CI Operations

This is the JumpCI CI entrypoint. It's used to run TOPSAIL-ng remotely inside a VPN cluster"""

import sys
import subprocess
import time
import os
from pathlib import Path

import click

# Add the testing directory to path for imports
testing_dir = Path(__file__).parent.parent / "testing"
if str(testing_dir) not in sys.path:
    sys.path.insert(0, str(testing_dir))

# Import jump CI testing functionality
try:
    import prepare_jump_ci as prepare_jump_ci_mod
    import utils
    from test import jump_ci as run_on_jump_ci
except ImportError as e:
    raise RuntimeError(f"Jump CI testing functionality not available: {e}")


def log(message: str, level: str = "info"):
    """Log message with project prefix."""
    project_name = "jump ci"
    icon = {"info": "ℹ️", "success": "✅", "error": "❌", "warning": "⚠️"}.get(level, "ℹ️")
    click.echo(f"{icon} [{project_name}] {message}")


@click.group()
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose output')
@click.pass_context
def cli(ctx, verbose):
    """Jump CI Project CI Operations for TOPSAIL-NG."""
    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose

@cli.command()
@click.pass_context
@click.option('--cluster', 'cluster', nargs=1, metavar='KEY', help='Give the name of the cluster to lock', default=None)
def lock_cluster(ctx, cluster):
    """Prepare phase - Lock the cluster."""
    log("Locking cluster for exclusive access...")
    try:

        prepare_jump_ci_mod.lock_cluster(cluster)
        log("Cluster locked successfully", "success")
        sys.exit(0)
    except Exception as e:
        log(f"Lock phase failed: {e}", "error")
        sys.exit(1)

@cli.command()
@click.pass_context
def prepare_jump_ci(ctx):
    """Prepare phase - Prepare the Jump CI remote system."""
    log("Starting prepare_jump_ci phase...")
    try:
        prepare_jump_ci_mod.prepare()
        log("Jump CI environment prepared", "success")
        sys.exit(0)
    except Exception as e:
        log(f"Prepare_jump_ci phase failed: {e}", "error")
        sys.exit(1)

@cli.command()
@click.pass_context
@click.option('--cluster', 'cluster', nargs=1, metavar='KEY', help='Give the name of the cluster to unlock', default=None)
def unlock_cluster(ctx, cluster):
    """Teardown phase - Unlock the cluster."""
    log("Unlocking the cluster...")
    try:
        prepare_jump_ci_mod.unlock_cluster(cluster)
        log("Cluster unlocked successfully", "success")
        sys.exit(0)
    except Exception as e:
        log(f"Unlock cluster phase failed: {e}", "error")
        sys.exit(1)

@cli.command()
@click.pass_context
def prepare(ctx):
    """Prepare phase - Trigger the project's prepare method."""
    log("Starting prepare phase...")
    try:
        ret = run_on_jump_ci("test_ci")
        log("Jump CI environment prepared", "success" if not ret else "error")
        sys.exit(ret)
    except Exception as e:
        log(f"Prepare phase failed: {e}", "error")
        sys.exit(1)

@cli.command()
@click.pass_context
def test(ctx):
    """Test phase - Trigger the project's test method."""
    log("Starting test phase...")
    try:
        ret = run_on_jump_ci("test_ci")
        log("Jump CI environment test completed", "success" if not ret else "error")
        sys.exit(ret)
    except Exception as e:
        log(f"Test phase failed: {e}", "error")
        sys.exit(1)

@cli.command()
@click.pass_context
def pre_cleanup(ctx):
    """Cleanup phase - Pre-clean up resources."""
    log("Starting cleanup phase...")
    try:
        log("Cleaning up jump CI resources...")
        ret = run_on_jump_ci("pre_cleanup_ci")
        log("Cleanup phase completed!", "success" if not ret else "error")
        sys.exit(ret)
    except Exception as e:
        log(f"Cleanup phase failed: {e}", "error")
        sys.exit(1)


if __name__ == "__main__":
    cli()
