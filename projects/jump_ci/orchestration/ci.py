#!/usr/bin/env python3
"""
Jump CI Project CI Operations

This is the JumpCI CI entrypoint. It's used to run TOPSAIL-ng remotely inside a VPN cluster"""

import sys
import subprocess
import time
import os
from pathlib import Path
import types

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


class JumpCITestRunner:
    """Test runner for the Jump CI project operations."""

    def __init__(self, verbose: bool = False):
        self.project_name = "jump ci"
        self.verbose = verbose

    def log(self, message: str, level: str = "info"):
        """Log message with project prefix."""
        icon = {"info": "ℹ️", "success": "✅", "error": "❌", "warning": "⚠️"}.get(level, "ℹ️")
        click.echo(f"{icon} [{self.project_name}] {message}")

    def lock_cluster(self):
        """
        Prepare phase - Lock cluster for exclusive access
        """
        self.log("Locking cluster for exclusive access...")
        try:
            prepare_jump_ci_mod.lock_cluster()
            self.log("Cluster locked successfully", "success")
        except Exception as e:
            self.log(f"Lock phase failed: {e}", "error")
            return 1

    def unlock_cluster(self):
        """
        Teardown phase - Unlock cluster exclusive access
        """

        self.log("Unlocking the cluster...")
        try:
            prepare_jump_ci_mod.unlock_cluster()
            self.log("Cluster unlocked successfully", "success")
        except Exception as e:
            self.log(f"Unlock cluster phase failed: {e}", "error")
            return 1

    def prepare_jump_ci(self):
        self.log("Starting prepare_jump_ci phase...")

        try:
            prepare_jump_ci_mod.prepare_jump_ci()
            self.log("Jump CI environment prepared", "success")
            return 0

        except Exception as e:
            self.log(f"Prepare_jump_ci phase failed: {e}", "error")
            return 1

    def prepare(self):
        self.log("Starting prepare phase...")

        try:
            ret = run_on_jump_ci("test_ci")
            self.log("Jump CI environment prepared", "success" if not ret else "error")
            return ret

        except Exception as e:
            self.log(f"Prepare phase failed: {e}", "error")
            return 1

    def test(self):
        """
        Test phase - Execute the main testing logic.
        """
        self.log("Starting test phase...")

        try:
            ret = run_on_jump_ci("test_ci")
            self.log("Jump CI environment test completed", "success" if not ret else "error")
            return ret
        except Exception as e:
            self.log(f"Test phase failed: {e}", "error")
            return 1

    def cleanup(self):
        """
        Cleanup phase - Clean up resources and finalize.
        """
        self.log("Starting cleanup phase...")

        try:
            # Clean up any temporary files or connections
            self.log("Cleaning up jump CI resources...")
            ret = run_on_jump_ci("pre_cleanup_ci")
            self.log("Cleanup phase completed!", "success" if not ret else "error")
            return 0
        except Exception as e:
            self.log(f"Cleanup phase failed: {e}", "error")
            return 1


@click.group()
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose output')
@click.pass_context
def cli(ctx, verbose):
    """Jump CI Project CI Operations for TOPSAIL-NG."""
    ctx.ensure_object(types.SimpleNamespace)
    ctx.obj.verbose = verbose
    ctx.obj.runner = JumpCITestRunner(verbose)

@cli.command()
@click.pass_context
def lock_cluster(ctx):
    """Prepare phase - Lock the cluster."""
    runner = ctx.obj.runner
    exit_code = runner.lock_cluster()
    sys.exit(exit_code)

@cli.command()
@click.pass_context
def prepare_jump_ci(ctx):
    """Prepare phase - Prepare the Jump CI remote system."""
    runner = ctx.obj.runner
    exit_code = runner.prepare_jump_ci()
    sys.exit(exit_code)

@cli.command()
@click.pass_context
def unlock_cluster(ctx):
    """Teardown phase - Unlock the ckuster."""
    runner = ctx.obj.runner
    exit_code = runner.unlock_cluster()
    sys.exit(exit_code)

@cli.command()
@click.pass_context
def prepare(ctx):
    """Prepare phase - Trigger the project's prepare method."""
    runner = ctx.obj.runner
    exit_code = runner.prepare()
    sys.exit(exit_code)


@cli.command()
@click.pass_context
def test(ctx):
    """Test phase - Trigger the project's test method."""
    runner = ctx.obj.runner
    exit_code = runner.test()
    sys.exit(exit_code)


@cli.command()
@click.pass_context
def pre_cleanup(ctx):
    """Prepare phase - Pre-clean up resources."""
    runner = ctx.obj.runner
    exit_code = runner.cleanup()
    sys.exit(exit_code)


if __name__ == "__main__":
    cli()
