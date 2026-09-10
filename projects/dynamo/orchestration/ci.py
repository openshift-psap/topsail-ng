#!/usr/bin/env python3
"""
Dynamo Project CI Operations
"""

import logging
import types
from pathlib import Path

import click

from projects.core.agentic.config_review import trigger_config_review_for_ci
from projects.core.agentic.on_failure import agent_review_on_failure
from projects.core.ci_entrypoint.fournos_resolve import create_fournos_resolve_entrypoint
from projects.core.library import ci as ci_lib
from projects.core.library import config, env, run, vault
from projects.core.library.export import caliper_export_entrypoint
from projects.core.library.replot import caliper_replot_entrypoint
from projects.dynamo.orchestration.cleanup_phase import run as cleanup_toolbox_run
from projects.dynamo.orchestration.preflight_phase import run as preflight_toolbox_run
from projects.dynamo.orchestration.prepare_sequence import run_prepare_sequence
from projects.dynamo.orchestration.test_phase import run as test_toolbox_run

logger = logging.getLogger(__name__)


def init():
    """Initialize Dynamo orchestration environment"""
    env.init()
    run.init()
    config.init(Path(__file__).parent)


def list_vaults() -> list[str]:
    """List all vaults (includes both mandatory and optional)."""
    return vault.phase_vault_list_all()


@click.group(cls=ci_lib.HelpfulGroup)
@click.pass_context
@ci_lib.safe_ci_function
def main(ctx):
    """Dynamo Project CI Operations for FORGE."""
    ctx.ensure_object(types.SimpleNamespace)
    init()

    if ctx.invoked_subcommand == "resolve-fournos-config":
        logger.info("No need to initialize the vaults for the resolve step")
        return

    vault.phase_vault_init(ctx.invoked_subcommand)


@main.command()
@click.pass_context
@ci_lib.safe_ci_command
@agent_review_on_failure
def prepare(ctx) -> int:
    """Prepare phase - Set up environment, operators, and Dynamo platform."""
    return run_prepare_sequence()


@main.command()
@click.pass_context
@ci_lib.safe_ci_command
@agent_review_on_failure
def preflight(ctx) -> int:
    """Preflight check phase - Validate required Dynamo CRDs exist."""
    return preflight_toolbox_run()


@main.command()
@click.pass_context
@ci_lib.safe_ci_command
@agent_review_on_failure
def test(ctx) -> int:
    """Test phase - Deploy DynamoGraphDeployment, run smoke test and benchmarks."""
    trigger_config_review_for_ci(env.BASE_ARTIFACT_DIR, async_mode=True)
    return test_toolbox_run()


@main.command()
@click.pass_context
@ci_lib.safe_ci_command
@agent_review_on_failure
def pre_cleanup(ctx) -> int:
    """Cleanup phase - Clean up Dynamo test resources."""
    from projects.dynamo.orchestration import runtime_config

    for run_spec in runtime_config.get_run_specs():
        with runtime_config.activate_run_spec(run_spec):
            cleanup_toolbox_run(namespace=run_spec.namespace)
    return 0


@main.command()
@click.pass_context
@ci_lib.safe_ci_command
@agent_review_on_failure
def post_cleanup(ctx) -> int:
    """Cleanup phase - Final cleanup of Dynamo resources."""
    from projects.dynamo.orchestration import runtime_config

    for run_spec in runtime_config.get_run_specs():
        with runtime_config.activate_run_spec(run_spec):
            cleanup_toolbox_run(namespace=run_spec.namespace)
    return 0


main.add_command(create_fournos_resolve_entrypoint(vault_list_func=vault.phase_vault_list_all))
main.add_command(caliper_export_entrypoint)
main.add_command(caliper_replot_entrypoint)

if __name__ == "__main__":
    main()
