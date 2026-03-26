#!/usr/bin/env python3
"""
LLM-D Project CI Operations

"""

import sys
import subprocess
import time
from pathlib import Path
import types

import click

import test_llmd, prepare_llmd

@click.group()
@click.pass_context
def cli(ctx):
    """LLM-D Project CI Operations for FORGE."""
    ctx.ensure_object(types.SimpleNamespace)


@cli.command()
@click.pass_context
def prepare(ctx):
    """Prepare phase - Set up environment and dependencies."""
    exit_code = prepare_llmd.prepare()
    sys.exit(exit_code)


@cli.command()
@click.pass_context
def test(ctx):
    """Test phase - Execute the main testing logic."""
    exit_code = test_llmd.test()
    sys.exit(exit_code)


@cli.command()
@click.pass_context
def pre_cleanup(ctx):
    """Cleanup phase - Clean up resources and finalize."""
    exit_code = prepare_llmd.cleanup()
    sys.exit(exit_code)


if __name__ == "__main__":
    test_llmd.init()
    cli()
