#!/usr/bin/env python3
"""
Dynamo Project CLI entrypoint
"""

import logging
import sys
import types
from pathlib import Path

import click

from projects.core.library import config, env, run
from projects.core.library.cli import safe_cli_command
from projects.core.library.postprocess import postprocess_command

logger = logging.getLogger(__name__)


def init():
    """Initialize Dynamo orchestration environment"""
    env.init()
    run.init()
    config.init(Path(__file__).parent)


@click.group()
@click.option(
    "--preset",
    multiple=True,
    help="Apply a preset to the configuration. Pass multiple --preset NAME to apply multiple presets.",
)
@click.pass_context
def main(ctx, preset):
    """Dynamo CLI Operations."""
    ctx.ensure_object(types.SimpleNamespace)
    init()

    if not preset:
        return

    try:
        for preset_name in preset:
            logger.info(f"Applying preset: {preset_name}")
            config.project.apply_preset(preset_name)
    except ValueError as e:
        logger.error(f"Failed to apply preset '{preset_name}': {e}")
        sys.exit(1)


@main.command()
@click.pass_context
@safe_cli_command
def prepare(ctx):
    """Prepare phase - Set up operators and Dynamo platform."""
    from projects.dynamo.orchestration.prepare_sequence import run_prepare_sequence

    exit_code = run_prepare_sequence()
    sys.exit(exit_code)


@main.command()
@click.pass_context
@safe_cli_command
def preflight(ctx):
    """Preflight check - Validate required Dynamo CRDs exist."""
    from projects.dynamo.orchestration.preflight_phase import run as preflight_run

    exit_code = preflight_run()
    sys.exit(exit_code)


@main.command()
@click.pass_context
@safe_cli_command
def test(ctx):
    """Test phase - Deploy DynamoGraphDeployment, smoke test, benchmark."""
    from projects.dynamo.orchestration.test_phase import run as test_run

    exit_code = test_run()
    sys.exit(exit_code)


@main.command()
@click.pass_context
@safe_cli_command
def cleanup(ctx):
    """Cleanup phase - Remove Dynamo test resources."""
    from projects.dynamo.orchestration import runtime_config
    from projects.dynamo.orchestration.cleanup_phase import run as cleanup_run

    for run_spec in runtime_config.get_run_specs():
        with runtime_config.activate_run_spec(run_spec):
            cleanup_run(namespace=run_spec.namespace)
    sys.exit(0)


main.add_command(postprocess_command)


if __name__ == "__main__":
    main()
