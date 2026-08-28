"""Caliper CLI command definitions."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import yaml

from projects.caliper.cli.s3_import import run_s3_import_with_explicit_params
from projects.caliper.engine.ai_eval import run_ai_eval_export
from projects.caliper.engine.constants import METADATA_FILE
from projects.caliper.engine.file_export.artifacts_export_run import run_artifacts_export
from projects.caliper.engine.file_export.artifacts_import_run import run_artifacts_import
from projects.caliper.engine.file_export.censoring import apply_censoring_to_artifacts
from projects.caliper.engine.file_export.mlflow_config import load_mlflow_config_yaml
from projects.caliper.engine.kpi.generate import run_kpi_generate
from projects.caliper.engine.kpi.import_export import (
    import_kpis_snapshot,
)
from projects.caliper.engine.load_plugin import load_plugin
from projects.caliper.engine.parse import run_parse
from projects.caliper.engine.plugin_config import resolve_plugin_module_string
from projects.caliper.engine.visualize import run_visualize
from projects.caliper.public import StatusLevel


def _exit_with_help(ctx: click.Context, msg: str, code: int = 1) -> None:
    """Print error message and help, then exit with error code."""
    click.echo(f"Error: {msg}", err=True)
    click.echo(ctx.get_help(), err=True)
    ctx.exit(code)


def _workspace_cli_options(cmd_func):
    """Common workspace CLI options decorator."""
    cmd_func = click.option(
        "--artifacts-dir",
        "artifacts_dir",
        type=click.Path(path_type=Path, exists=True),
        default=None,
    )(cmd_func)
    cmd_func = click.option(
        "--postprocess-config",
        type=click.Path(path_type=Path, dir_okay=False, exists=True),
        default=None,
    )(cmd_func)
    cmd_func = click.option(
        "--plugin",
        "plugin_module_override",
        metavar="MODULE",
        default=None,
    )(cmd_func)
    return cmd_func


def _label_filter_options(cmd_func):
    """Common include/exclude label filtering options decorator."""
    cmd_func = click.option(
        "--include-label",
        multiple=True,
        help="Include only test directories with matching labels (format: key=value).",
    )(cmd_func)
    cmd_func = click.option(
        "--exclude-label",
        multiple=True,
        help="Exclude test directories with matching labels (format: key=value).",
    )(cmd_func)
    return cmd_func


def _apply_workspace_cli_overrides(
    ctx: click.Context,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
) -> None:
    """Apply CLI overrides to context object."""
    if artifacts_dir:
        ctx.obj["base_dir"] = artifacts_dir
    if postprocess_config:
        ctx.obj["postprocess_config"] = postprocess_config
    if plugin_module_override:
        ctx.obj["plugin_cli"] = plugin_module_override


def _root_obj(ctx: click.Context) -> dict:
    """Get root context object."""
    return ctx.find_root().obj


def _plugin_tuple(ctx: click.Context):
    """Get plugin module and plugin from context."""

    root = _root_obj(ctx)
    try:
        mod, _manifest_path = resolve_plugin_module_string(
            base_dir=root.get("base_dir"),
            postprocess_config=root.get("postprocess_config"),
            cli_plugin=root.get("plugin_cli"),
        )
        if not mod:
            raise RuntimeError("Plugin module not found")
        plugin = load_plugin(mod)
        if not plugin:
            raise RuntimeError(f"Unable to load plugin: {mod}")
    except RuntimeError as e:
        _exit_with_help(ctx, str(e), code=2)
    return mod, plugin


def _parse_label_filters(
    include_label: tuple[str, ...], exclude_label: tuple[str, ...]
) -> tuple[list[dict[str, str]] | None, list[dict[str, str]] | None]:
    """Parse include and exclude label CLI options into lists of filter dictionaries.

    Args:
        include_label: Tuple of include label specs in "key=value" format
        exclude_label: Tuple of exclude label specs in "key=value" format

    Returns:
        Tuple of (list of include_filter_dicts, list of exclude_filter_dicts)
        Each filter dict contains a single key=value pair to support multiple filters on the same key.

    Raises:
        click.BadParameter: If label format is invalid
    """
    from projects.caliper.engine.label_filters import parse_filter_pairs

    include_filters = None
    if include_label:
        try:
            include_filters = parse_filter_pairs(include_label, "include")
        except ValueError as e:
            # Convert to click.BadParameter for CLI error handling
            raise click.BadParameter(str(e)) from e

    exclude_filters = None
    if exclude_label:
        try:
            exclude_filters = parse_filter_pairs(exclude_label, "exclude")
        except ValueError as e:
            # Convert to click.BadParameter for CLI error handling
            raise click.BadParameter(str(e)) from e

    return include_filters, exclude_filters


# Main commands
@click.command("parse")
@_workspace_cli_options
@_label_filter_options
@click.option("--no-cache", is_flag=True, help="Force full parse.")
@click.option(
    "--cache-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Override cache file path.",
)
@click.option(
    "--show-matrix/--no-show-matrix",
    default=True,
    help="Display parameter matrix summary after parsing (default: enabled).",
)
@click.option(
    "--status-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to write status YAML for orchestration (absolute path required).",
)
@click.option(
    "--verbose-parsing",
    is_flag=True,
    default=False,
    help="Enable verbose parsing logs.",
)
@click.pass_context
def parse_cmd(
    ctx: click.Context,
    no_cache: bool,
    cache_dir: Path | None,
    show_matrix: bool,
    include_label: tuple[str, ...],
    exclude_label: tuple[str, ...],
    status_file: Path | None,
    verbose_parsing: bool,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
) -> None:
    """Parse test artifacts into a unified data model."""
    _apply_workspace_cli_overrides(
        ctx,
        artifacts_dir=artifacts_dir,
        postprocess_config=postprocess_config,
        plugin_module_override=plugin_module_override,
    )
    mod, plugin = _plugin_tuple(ctx)
    artifact_root: Path = _root_obj(ctx)["base_dir"]

    # Parse label filters
    include_filter, exclude_filter = _parse_label_filters(include_label, exclude_label)

    status = {"success": False}

    try:
        model = run_parse(
            base_dir=artifact_root,
            plugin_module=mod,
            plugin=plugin,
            use_cache=not no_cache,
            show_parameter_matrix=show_matrix,
            include_label_filter=include_filter,
            exclude_label_filter=exclude_filter,
            verbose_parsing=verbose_parsing,
        )

        # Extract test directories with labels and relative paths
        test_directories = []
        for node in model.test_nodes:
            test_directories.append(
                {
                    "path": str(node.test_path),  # relative to artifact_dir
                    **node.test_labels,  # spread test_labels directly into the object
                }
            )

        # Add excluded directories information
        excluded_summary = {}
        if model.excluded_test_directories:
            # Group excluded dirs by reason for summary
            by_reason = {}
            for excluded in model.excluded_test_directories:
                reason = excluded["reason"]
                by_reason.setdefault(reason, []).append(excluded)

            excluded_summary = {
                "total_excluded": len(model.excluded_test_directories),
                "by_reason": {reason: len(dirs) for reason, dirs in by_reason.items()},
                "excluded_directories": model.excluded_test_directories,
            }

        status.update(
            {
                "success": True,
                "plugin_module": mod,
                "parsed_records": len(model.unified_result_records),
                "test_directories": test_directories,
                "test_directories_count": len(test_directories),
                "excluded_test_directories": excluded_summary,
                "cache_ref": str(model.parse_cache_ref) if model.parse_cache_ref else None,
            }
        )

        click.echo(
            f"Parsed {len(model.unified_result_records)} record(s) from {len(test_directories)} test directories; cache={model.parse_cache_ref}"
        )

        # Show test directories for user reference
        if test_directories:
            click.echo("📁 Test directories found:")
            for test_dir in test_directories:
                click.echo(f"   • {test_dir}")
        else:
            click.echo("⚠️  No test directories found")

        # Show excluded directories summary
        if model.excluded_test_directories:
            excluded_count = len(model.excluded_test_directories)
            click.echo(f"🚫 Excluded {excluded_count} directories:")

            # Group by reason and show counts
            by_reason = {}
            for excluded in model.excluded_test_directories:
                reason = excluded["reason"]
                by_reason.setdefault(reason, []).append(excluded)

            for reason, dirs in by_reason.items():
                reason_label = {
                    "skip": "marked with skip: true",
                    "filter_mismatch": "filtered out by include/exclude rules",
                }.get(reason, reason)

                click.echo(f"   • {len(dirs)} {reason_label}")

                # Show first few examples
                for d in dirs[:2]:
                    click.echo(f"     - {d['path']}: {d['detail']}")
                if len(dirs) > 2:
                    click.echo(f"     ... and {len(dirs) - 2} more")

    except Exception as e:  # noqa: BLE001
        import traceback

        full_traceback = traceback.format_exc()
        status.update(
            {
                "success": False,
                "error": str(e),
                "traceback": full_traceback,
            }
        )
        click.echo(f"parse failed: {e}", err=True)
        click.echo("Full traceback:", err=True)
        click.echo(full_traceback, err=True)
    finally:
        # Write status file if requested
        if status_file:
            try:
                status_file.parent.mkdir(parents=True, exist_ok=True)
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                click.echo(f"Failed to write status file {status_file}: {e}", err=True)

    # Exit with non-zero code on failure
    if not status["success"]:
        sys.exit(2)


@click.command("visualize")
@_workspace_cli_options
@_label_filter_options
@click.option("--reports", default=None, help="Comma-separated report ids.")
@click.option("--report-group", default=None)
@click.option("--visualize-config", type=click.Path(path_type=Path), default=None)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    required=True,
)
@click.option(
    "--status-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to write status YAML for orchestration (absolute path required).",
)
@click.option(
    "--verbose-parsing",
    is_flag=True,
    default=False,
    help="Enable verbose parsing logs.",
)
@click.pass_context
def visualize_cmd(
    ctx: click.Context,
    reports: str | None,
    report_group: str | None,
    visualize_config: Path | None,
    include_label: tuple[str, ...],
    exclude_label: tuple[str, ...],
    output_dir: Path,
    status_file: Path | None,
    verbose_parsing: bool,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
) -> None:
    """Generate visual reports and charts from parsed test data."""
    _apply_workspace_cli_overrides(
        ctx,
        artifacts_dir=artifacts_dir,
        postprocess_config=postprocess_config,
        plugin_module_override=plugin_module_override,
    )
    mod, plugin = _plugin_tuple(ctx)
    artifact_root: Path = _root_obj(ctx)["base_dir"]

    status = {"success": False}

    try:
        paths = run_visualize(
            base_dir=artifact_root,
            plugin_module=mod,
            plugin=plugin,
            output_dir=output_dir,
            reports_csv=reports,
            report_group=report_group,
            visualize_config_path=visualize_config,
            include_pairs=include_label,
            exclude_pairs=exclude_label,
            use_cache=True,
            cache_path=None,
            verbose_parsing=verbose_parsing,
        )

        status.update(
            {
                "success": True,
                "plugin_module": mod,
                "output_files": paths,
                "output_dir": str(output_dir),
                "generated_files": len(paths),
            }
        )

        click.echo("Wrote: " + ", ".join(paths))

    except Exception as e:  # noqa: BLE001
        import traceback

        error_details = {
            "error": str(e),
            "traceback": traceback.format_exc(),
        }

        status.update(
            {
                "success": False,
                **error_details,
            }
        )

        # Print full traceback to stderr for visibility
        click.echo(f"visualize failed: {e}", err=True)
        click.echo("Full traceback:", err=True)
        click.echo(traceback.format_exc(), err=True)
    finally:
        # Write status file if requested
        if status_file:
            try:
                status_file.parent.mkdir(parents=True, exist_ok=True)
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status, f, default_flow_style=False, sort_keys=False)
            except Exception as e:
                click.echo(f"Failed to write status file {status_file}: {e}", err=True)

    # Exit with non-zero code on failure
    if not status["success"]:
        sys.exit(2)


@click.command("list-reports")
@_workspace_cli_options
@click.pass_context
def list_reports_cmd(
    ctx: click.Context,
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
):
    """List available reports supported by the plugin."""
    try:
        _apply_workspace_cli_overrides(
            ctx,
            artifacts_dir=artifacts_dir,
            postprocess_config=postprocess_config,
            plugin_module_override=plugin_module_override,
        )
        mod, plugin = _plugin_tuple(ctx)

        # Get plugin docstring to extract report information
        plugin_doc = plugin.__class__.__doc__ or ""

        # Extract available reports from docstring
        reports = []
        in_reports_section = False

        for line in plugin_doc.split("\n"):
            line = line.strip()
            if "Available visual reports:" in line:
                in_reports_section = True
                continue
            elif in_reports_section and line.startswith("*"):
                # Extract report ID from lines like "* ``report_id`` — description"
                if "``" in line:
                    report_parts = line.split("``")
                    if len(report_parts) >= 3:
                        report_id = report_parts[1]
                        description = report_parts[2].strip(" —").strip()
                        reports.append((report_id, description))
            elif in_reports_section and not line.startswith("*") and line:
                # End of reports section
                break

        if reports:
            click.echo("Available reports:")
            for report_id, description in reports:
                click.echo(f"  * {report_id} — {description}")
        else:
            click.echo("No reports found in plugin documentation.")

    except Exception as e:
        click.echo(f"Failed to list reports: {e}", err=True)
        sys.exit(1)


@click.command("ai-eval-export", hidden=True)
@_workspace_cli_options
@_label_filter_options
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option("--no-cache", is_flag=True, help="Disable parse cache")
@click.option(
    "--status-file", type=click.Path(path_type=Path), help="YAML file to write operation status"
)
@click.option(
    "--verbose-parsing",
    is_flag=True,
    default=False,
    help="Enable verbose parsing logs.",
)
@click.pass_context
def ai_eval_export(
    ctx: click.Context,
    output: Path,
    include_label: tuple[str, ...],
    exclude_label: tuple[str, ...],
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
    no_cache: bool,
    status_file: Path | None,
    verbose_parsing: bool,
) -> None:
    _apply_workspace_cli_overrides(
        ctx,
        artifacts_dir=artifacts_dir,
        postprocess_config=postprocess_config,
        plugin_module_override=plugin_module_override,
    )
    mod, plugin = _plugin_tuple(ctx)
    artifact_root: Path = _root_obj(ctx)["base_dir"]

    # Parse label filters
    include_filter, exclude_filter = _parse_label_filters(include_label, exclude_label)

    status_data = {"success": False}

    try:
        run_ai_eval_export(
            base_dir=artifact_root,
            plugin_module=mod,
            plugin=plugin,
            output=output,
            use_cache=not no_cache,
            include_label_filter=include_filter,
            exclude_label_filter=exclude_filter,
            verbose_parsing=verbose_parsing,
        )
        status_data = {"success": True, "output_file": str(output)}
        click.echo(f"Exported {output}")
    except Exception as e:  # noqa: BLE001
        import traceback

        full_traceback = traceback.format_exc()
        status_data = {"success": False, "error": str(e), "traceback": full_traceback}
        click.echo(f"ai-eval-export failed: {e}", err=True)
        click.echo(f"Full traceback:\n{full_traceback}", err=True)

        if not status_file:
            sys.exit(3)
    finally:
        # Write status file if requested
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)

    # Exit with error code if operation failed
    if not status_data.get("success", False):
        sys.exit(3)


# KPI commands
@click.command("generate")
@_workspace_cli_options
@_label_filter_options
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option(
    "--format",
    "format_type",
    type=click.Choice(["hierarchical", "jsonl"], case_sensitive=False),
    default="hierarchical",
    help="Output format: hierarchical JSON (default) or JSONL",
)
@click.option(
    "--status-file", type=click.Path(path_type=Path), help="YAML file to write operation status"
)
@click.option(
    "--verbose-parsing",
    is_flag=True,
    default=False,
    help="Enable verbose parsing logs.",
)
@click.option(
    "--html",
    is_flag=True,
    default=False,
    help="Generate HTML report in addition to JSON output.",
)
@click.pass_context
def kpi_generate(
    ctx: click.Context,
    output: Path,
    format_type: str,
    include_label: tuple[str, ...],
    exclude_label: tuple[str, ...],
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
    status_file: Path | None,
    verbose_parsing: bool,
    html: bool,
) -> None:
    _apply_workspace_cli_overrides(
        ctx,
        artifacts_dir=artifacts_dir,
        postprocess_config=postprocess_config,
        plugin_module_override=plugin_module_override,
    )
    mod, plugin = _plugin_tuple(ctx)
    artifact_root: Path = _root_obj(ctx)["base_dir"]

    # Parse label filters
    include_filter, exclude_filter = _parse_label_filters(include_label, exclude_label)

    status_data = {"success": False}

    try:
        # First run parse to check test directory count
        model = run_parse(
            base_dir=artifact_root,
            plugin_module=mod,
            plugin=plugin,
            use_cache=True,
            include_label_filter=include_filter,
            exclude_label_filter=exclude_filter,
            verbose_parsing=verbose_parsing,
        )

        # Extract test directories from test nodes
        test_directories = [str(node.directory) for node in model.test_nodes]
        test_dir_count = len(test_directories)

        # Prepare excluded directory summary
        excluded_summary = {}
        if model.excluded_test_directories:
            by_reason = {}
            for excluded in model.excluded_test_directories:
                reason = excluded["reason"]
                by_reason.setdefault(reason, []).append(excluded)

            excluded_summary = {
                "total_excluded": len(model.excluded_test_directories),
                "by_reason": {reason: len(dirs) for reason, dirs in by_reason.items()},
                "excluded_directories": model.excluded_test_directories,
            }

        # Check if any test directories were found
        if test_dir_count == 0:
            status_data = {
                "success": False,
                "message": "No test directories found - nothing to process for KPI generation",
                "test_directories_count": 0,
                "test_directories": [],
                "excluded_test_directories": excluded_summary,
            }
            click.echo("❌ No test directories found - KPI generation failed", err=True)
            click.echo(f"   No {METADATA_FILE} files found in artifact directory", err=True)

            # Show excluded directories if any
            if model.excluded_test_directories:
                excluded_count = len(model.excluded_test_directories)
                click.echo(f"   Found {excluded_count} directories that were excluded:", err=True)
                for reason, count in excluded_summary["by_reason"].items():
                    reason_label = {
                        "skip": "marked with skip: true",
                        "filter_mismatch": "filtered out by include/exclude rules",
                    }.get(reason, reason)
                    click.echo(f"     • {count} {reason_label}", err=True)

            sys.exit(3)
        else:
            # Proceed with KPI generation
            rows, status_details = run_kpi_generate(
                base_dir=artifact_root,
                plugin_module=mod,
                plugin=plugin,
                output=output,
                use_cache=True,
                cache_path=None,
                format_type=format_type,
                include_label_filter=include_filter,
                exclude_label_filter=exclude_filter,
                verbose_parsing=verbose_parsing,
            )

            # Check for KPI generation failure
            if not status_details.get("success", True):
                status_data = {
                    "success": False,
                    "message": status_details.get("message", "KPI generation failed"),
                    "test_directories_count": test_dir_count,
                    "test_directories": test_directories,
                    "excluded_test_directories": excluded_summary,
                    "status_details": status_details,
                }
                click.echo(
                    f"❌ KPI generation failed: {status_details.get('message', 'Unknown error')}",
                    err=True,
                )
                sys.exit(2)

            if not rows:
                status_data = {
                    "success": False,
                    "message": "No KPIs generated",
                    "test_directories_count": test_dir_count,
                    "test_directories": test_directories,
                    "excluded_test_directories": excluded_summary,
                    "status_details": status_details,
                }
                click.echo("❌ No KPIs generated", err=True)
                sys.exit(3)

            status_data = {
                "success": True,
                "output_file": str(output),
                "test_directories_count": test_dir_count,
                "test_directories": test_directories,
                "excluded_test_directories": excluded_summary,
                "status_details": status_details,
            }
            click.echo(f"Generated {output}")
            click.echo(f"📁 Processed {test_dir_count} test directories")

            # Generate HTML report if requested
            if html:
                try:
                    from projects.caliper.engine.kpi.html_generator import (
                        generate_kpi_html_from_file,
                    )

                    html_output = output.with_suffix(".html")
                    generate_kpi_html_from_file(output, html_output)
                    click.echo(f"📄 Generated HTML report: {html_output}")
                    status_data["html_file"] = str(html_output)
                except Exception as e:
                    click.echo(f"⚠️  Failed to generate HTML report: {e}", err=True)
                    status_data["html_generation_error"] = str(e)

            # Log any warnings from KPI generation
            if status_details and status_details.get("warnings"):
                for warning in status_details["warnings"]:
                    click.echo(f"⚠️  Warning: {warning}", err=True)

    except Exception as e:  # noqa: BLE001
        import traceback

        full_traceback = traceback.format_exc()
        status_data = {"success": False, "message": str(e), "traceback": full_traceback}
        click.echo(f"kpi generate failed: {e}", err=True)
        click.echo(f"Full traceback:\n{full_traceback}", err=True)

        sys.exit(3)
    finally:
        # Write status file if requested
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)

    # Exit with error code if operation failed
    if not status_data.get("success", False):
        sys.exit(3)


@click.command("csv-export")
@_workspace_cli_options
@_label_filter_options
@click.option("--output", type=click.Path(path_type=Path), required=True, help="Output CSV file")
@click.option(
    "--status-file", type=click.Path(path_type=Path), help="YAML file to write operation status"
)
@click.pass_context
def kpi_csv_export(
    ctx: click.Context,
    output: Path,
    include_label: tuple[str, ...],
    exclude_label: tuple[str, ...],
    artifacts_dir: Path | None,
    postprocess_config: Path | None,
    plugin_module_override: str | None,
    status_file: Path | None,
) -> None:
    _apply_workspace_cli_overrides(
        ctx,
        artifacts_dir=artifacts_dir,
        postprocess_config=postprocess_config,
        plugin_module_override=plugin_module_override,
    )
    mod, plugin = _plugin_tuple(ctx)
    artifact_root: Path = _root_obj(ctx)["base_dir"]

    # Parse label filters
    include_filter, exclude_filter = _parse_label_filters(include_label, exclude_label)

    status_data = {"success": False}

    try:
        # Parse artifacts to get the unified model for dashboard CSV generation
        from projects.caliper.engine.parse import run_parse

        model = run_parse(
            base_dir=artifact_root,
            plugin_module=mod,
            plugin=plugin,
            use_cache=True,  # Use cache for performance
            show_parameter_matrix=False,  # No need to show matrix for CSV export
            include_label_filter=include_filter,
            exclude_label_filter=exclude_filter,
            verbose_parsing=False,
        )

        # Export to dashboard CSV using the new architecture
        from projects.caliper.engine.kpi.csv_export import export_dashboard_csv

        result_path, kpi_count = export_dashboard_csv(
            plugin=plugin,
            model=model,
            output_path=output,
        )

        status_data = {
            "success": True,
            "output_file": str(result_path),
            "kpi_count": kpi_count,
            "record_count": len(model.unified_result_records),
        }
        click.echo(
            f"Generated dashboard CSV with {kpi_count} KPI rows from {len(model.unified_result_records)} records: {result_path}"
        )
    except Exception as e:  # noqa: BLE001
        import traceback

        full_traceback = traceback.format_exc()
        status_data = {"success": False, "error": str(e), "traceback": full_traceback}
        click.echo(f"kpi csv-export failed: {e}", err=True)
        click.echo(f"Full traceback:\n{full_traceback}", err=True)

        if not status_file:
            sys.exit(3)
    finally:
        # Write status file if requested
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)

    # Exit with error code if operation failed
    if not status_data.get("success", False):
        sys.exit(3)


@click.command("kpis-to-mlflow")
@click.option(
    "--input",
    "input_file",
    type=click.Path(path_type=Path),
    required=True,
    help="Input KPI JSON file (schema v2 hierarchical format)",
)
@click.option(
    "--artifacts-dir",
    "artifacts_dir",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help=f"Root of the artifact tree containing {METADATA_FILE} markers",
)
@click.option(
    "--status-file", type=click.Path(path_type=Path), help="YAML file to write operation status"
)
def kpis_to_mlflow_cmd(
    input_file: Path,
    artifacts_dir: Path,
    status_file: Path | None,
) -> None:
    """Convert kpis.json into per-run metrics.json + parameters.json for MLflow."""
    from projects.caliper.engine.kpi.kpis_to_mlflow import generate_metrics_from_kpis

    status_data: dict = {"success": False}

    try:
        result = generate_metrics_from_kpis(input_file, artifacts_dir)
        # Convert dataclass result to status_data format for YAML file
        status_data = result.to_status_data()

        if result.is_success():
            click.echo(
                f"Generated metrics.json for {result.tests_processed}/{result.total_tests} test(s)"
            )
            if result.partial:
                click.echo(f"Warning: {result.message}")
        elif result.is_skipped():
            click.echo(f"Skipped: {result.reason}")
        else:  # failed
            click.echo(f"kpis-to-mlflow failed: {result.error}", err=True)
    except Exception as e:  # noqa: BLE001
        import traceback

        full_traceback = traceback.format_exc()

        # Create failure result for unexpected exceptions
        from projects.caliper.engine.kpi.report_dataclasses import MlflowConversionResult

        exception_result = MlflowConversionResult(
            status="failed",
            error=str(e),
            tests_processed=0,
            total_tests=0,
        )
        status_data = exception_result.to_status_data(traceback=full_traceback)

        click.echo(f"kpis-to-mlflow failed: {e}", err=True)
        click.echo(f"Full traceback:\n{full_traceback}", err=True)

        if not status_file:
            sys.exit(3)
    finally:
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)

    if not status_data.get("success", False):
        sys.exit(3)


@click.command("import")
@click.option("--snapshot", type=click.Path(path_type=Path), required=True)
@click.pass_context
def kpi_import(ctx: click.Context, snapshot: Path) -> None:
    try:
        import_kpis_snapshot(snapshot_path=snapshot)
    except Exception as e:  # noqa: BLE001
        click.echo(f"kpi import failed: {e}", err=True)
        sys.exit(3)
    click.echo(f"Imported {snapshot}")


@click.command("analyse-kpis")
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    required=True,
    help="Output file for analysis results",
)
@click.option(
    "--current-kpis-file",
    type=click.Path(path_type=Path),
    required=True,
    help="Path to current KPIs JSON file",
)
@click.option(
    "--historical-kpis-dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Directory containing historical KPI files",
)
@click.option(
    "--plugin",
    "plugin_module",
    metavar="MODULE",
    required=True,
    help="Plugin module for KPI definitions and analysis rules",
)
@click.option(
    "--status-file", type=click.Path(path_type=Path), help="YAML file to write operation status"
)
@click.option(
    "--html",
    is_flag=True,
    default=False,
    help="Generate HTML report in addition to JSON output.",
)
def analyse_kpis_cmd(
    output: Path,
    current_kpis_file: Path,
    historical_kpis_dir: Path,
    plugin_module: str,
    status_file: Path | None,
    html: bool,
) -> None:
    """Analyze KPIs for orchestration (fork/exec)."""
    import time

    import yaml

    # Validate paths exist before proceeding
    if not current_kpis_file.exists():
        status_data = {
            "success": False,
            "error": f"Current KPI file not found: {current_kpis_file}",
            "completed_at": time.time(),
        }
        click.echo(f"❌ Current KPI file not found: {current_kpis_file}", err=True)

        # Write status file if requested
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)
        sys.exit(1)

    if not historical_kpis_dir.exists():
        status_data = {
            "success": False,
            "error": f"Historical KPI directory not found: {historical_kpis_dir}",
            "completed_at": time.time(),
        }
        click.echo(f"❌ Historical KPI directory not found: {historical_kpis_dir}", err=True)

        # Write status file if requested
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)
        sys.exit(2)

    # Delegate to engine layer
    from projects.caliper.engine.kpi.analyze import analyze_kpis

    status_data, report = analyze_kpis(
        current_kpis_file=current_kpis_file,
        historical_kpis_dir=historical_kpis_dir,
        output_file=output,
        plugin_module=plugin_module,
    )

    # Display result
    if status_data.success and status_data.status == StatusLevel.WARNING:
        click.echo("⚠️ KPI analysis completed with warning")
    elif status_data.success:
        click.echo("✅ KPI analysis completed with success")
    else:
        error_msg = status_data.error or f"Status: {status_data.status}"
        click.echo(f"❌ KPI analysis failed: {error_msg}", err=True)

    if status_data.message:
        click.echo("> " + status_data.message)

    # Generate HTML report if requested and analysis was successful
    if html and status_data.success:
        try:
            from projects.caliper.engine.kpi.html_generator import (
                generate_regression_html_from_file,
            )

            html_output = output.with_suffix(".html")
            generate_regression_html_from_file(output, html_output)
            click.echo(f"📄 Generated HTML regression report: {html_output}")

            # Store HTML file path in status data for notifications
            status_data.html_file = str(html_output)
        except Exception as e:
            click.echo(f"⚠️  Failed to generate HTML report: {e}", err=True)

    # Write status file if requested
    if status_file:
        try:
            # Convert status object to dict for YAML serialization
            from dataclasses import asdict
            from enum import Enum

            def enum_to_value(obj):
                """Convert enums to their string values recursively."""
                if isinstance(obj, Enum):
                    return obj.value
                elif isinstance(obj, dict):
                    return {k: enum_to_value(v) for k, v in obj.items()}
                elif isinstance(obj, (list, tuple)):
                    return [enum_to_value(item) for item in obj]
                return obj

            status_dict = asdict(status_data)
            # Convert enums to their string values
            status_dict = enum_to_value(status_dict)

            with open(status_file, "w", encoding="utf-8") as f:
                yaml.dump(status_dict, f, default_flow_style=False)
        except Exception as status_err:
            click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
            sys.exit(4)

    # Use exit code directly from analysis result
    exit_code = getattr(status_data, "exit_code", 1)
    sys.exit(exit_code)


@click.command("s3-import")
@click.option("--bucket", required=True, help="S3 bucket name")
@click.option("--prefix", default="", help="S3 object prefix/path")
@click.option(
    "--output-dir", type=click.Path(path_type=Path), required=True, help="Local output directory"
)
@click.option("--include-kpis-json", is_flag=True, default=False, help="Download kpis.json files")
@click.option("--include-kpis-csv", is_flag=True, default=False, help="Download kpis.csv files")
@click.option("--include-ai-data", is_flag=True, default=False, help="Download ai_data directories")
@click.option("--max-downloads", type=int, default=50, help="Maximum number of files to download")
@click.option(
    "--vault", default="psap-forge-aws-s3-export", help="Vault containing AWS credentials"
)
@click.option(
    "--aws-credentials-file", default="aws.credentials", help="Credentials file name within vault"
)
@click.option("-v", "--verbose", is_flag=True, help="Show detailed progress information")
@click.option(
    "--status-file", type=click.Path(path_type=Path), help="YAML file to write operation status"
)
@click.pass_context
def kpi_s3_import(
    ctx: click.Context,
    bucket: str,
    prefix: str,
    output_dir: Path,
    include_kpis_json: bool,
    include_kpis_csv: bool,
    include_ai_data: bool,
    max_downloads: int,
    vault: str,
    aws_credentials_file: str,
    verbose: bool,
    status_file: Path | None,
) -> None:
    """Download historical KPI and analysis data from S3."""
    from projects.core.library import vault as vault_lib

    status_data = {"success": False}

    try:
        # Ensure output directory exists
        output_dir.mkdir(parents=True, exist_ok=True)

        if verbose:
            click.echo("🔍 S3 Import Configuration:")
            click.echo(f"   Bucket: {bucket}")
            click.echo(f"   Prefix: {prefix}")
            click.echo(f"   Output Directory: {output_dir}")

        # Initialize vault system
        vault_lib.init(vaults=[vault] if vault else [])

        # Run S3 import
        result = run_s3_import_with_explicit_params(
            bucket=bucket,
            prefix=prefix,
            output_dir=output_dir,
            vault=vault,
            aws_credentials_file=aws_credentials_file,
            include_kpis_json=include_kpis_json,
            include_kpis_csv=include_kpis_csv,
            include_ai_data=include_ai_data,
            max_downloads=max_downloads,
        )

        # Construct S3 path for status
        s3_path = f"s3://{bucket}"
        if prefix:
            s3_path += f"/{prefix}"

        if result["status"] == "success":
            downloaded_count = result.get("downloaded_files", 0)
            status_data = {
                "success": True,
                "output_dir": str(output_dir),
                "downloaded_files": downloaded_count,
                "file_count": downloaded_count,
                "imported_path": s3_path,
            }
            click.echo("✅ S3 import completed successfully")
            click.echo(f"📄 Downloaded {downloaded_count} files to {output_dir}")
        elif result["status"] == "warning":
            status_data = {
                "success": True,
                "output_dir": str(output_dir),
                "downloaded_files": 0,
                "file_count": 0,
                "warning": result.get("message", "No objects found matching filters"),
                "imported_path": s3_path,
            }
            click.echo(f"⚠️  {result.get('message', 'No objects found matching filters')}")
        else:
            error_msg = result.get("error", "unknown error")
            status_data = {"success": False, "error": error_msg, "imported_path": s3_path}
            click.echo(f"❌ S3 import failed: {error_msg}", err=True)
            if not status_file:
                sys.exit(1)

    except Exception as e:  # noqa: BLE001
        import traceback

        # Construct S3 path for error cases too
        s3_path = f"s3://{bucket}"
        if prefix:
            s3_path += f"/{prefix}"

        full_traceback = traceback.format_exc()
        status_data = {
            "success": False,
            "error": str(e),
            "traceback": full_traceback,
            "imported_path": s3_path,
        }
        click.echo(f"S3 import failed: {e}", err=True)
        click.echo(f"Full traceback:\n{full_traceback}", err=True)

        if not status_file:
            sys.exit(3)
    finally:
        # Write status file if requested
        if status_file:
            try:
                with open(status_file, "w", encoding="utf-8") as f:
                    yaml.dump(status_data, f, default_flow_style=False)
            except Exception as status_err:
                click.echo(f"Failed to write status file {status_file}: {status_err}", err=True)
                sys.exit(4)

    # Exit with error code if operation failed
    if not status_data.get("success", False):
        sys.exit(3)


# Artifacts commands
@click.command("export")
@click.option(
    "--from",
    "from_path",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help="Source path containing artifacts to export.",
)
@click.option("--to", default=None, help="Target URL for upload.")
@click.option("--backend", multiple=True, type=str, help="Repeat: mlflow.")
@click.option(
    "--mlflow-tracking-uri",
    help="MLflow tracking server URI.",
    envvar="MLFLOW_TRACKING_URI",
)
@click.option("--mlflow-experiment", default=None, envvar="MLFLOW_EXPERIMENT_NAME")
@click.option("--mlflow-run-id", default=None, envvar="MLFLOW_RUN_ID")
@click.option("--mlflow-run-name", default=None, envvar="CALIPER_MLFLOW_RUN_NAME")
@click.option("--mlflow-workspace", default=None, help="MLflow workspace name")
@click.option("--mlflow-insecure-tls", is_flag=True)
@click.option(
    "--mlflow-secrets",
    "mlflow_secrets_path",
    type=click.Path(path_type=Path, exists=True),
    default=None,
)
@click.option(
    "--mlflow-config",
    "mlflow_config_path",
    type=click.Path(path_type=Path, exists=True),
    default=None,
)
@click.option("--dry-run", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@click.option(
    "--status-yaml",
    "status_yaml_path",
    type=click.Path(path_type=Path),
    default=None,
)
@click.option("--upload-workers", type=click.IntRange(min=1, max=64), default=10)
@click.pass_context
def artifacts_export(
    ctx: click.Context,
    from_path: Path,
    to: str | None,
    backend: tuple[str, ...],
    mlflow_tracking_uri: str | None,
    mlflow_experiment: str | None,
    mlflow_run_id: str | None,
    mlflow_run_name: str | None,
    mlflow_workspace: str | None,
    mlflow_insecure_tls: bool,
    mlflow_secrets_path: Path | None,
    mlflow_config_path: Path | None,
    dry_run: bool,
    verbose: bool,
    status_yaml_path: Path | None,
    upload_workers: int,
) -> None:
    # Load config from file if provided, CLI args override
    config = {}
    if mlflow_config_path:
        try:
            config = load_mlflow_config_yaml(mlflow_config_path)
        except Exception as e:
            click.echo(f"Failed to load MLflow config: {e}", err=True)
            sys.exit(1)

    # CLI args override config file values
    final_config = {
        "tracking_uri": mlflow_tracking_uri or config.get("tracking_uri"),
        "experiment": mlflow_experiment or config.get("experiment"),
        "run_id": mlflow_run_id or config.get("run_id"),
        "run_name": mlflow_run_name or config.get("run_name"),
        "workspace": mlflow_workspace or config.get("workspace"),
        "insecure_tls": mlflow_insecure_tls or config.get("insecure_tls", False),
        "secrets_path": mlflow_secrets_path or config.get("secrets_path"),
        "upload_workers": upload_workers,
    }

    try:
        exit_code = run_artifacts_export(
            from_path=from_path,
            backend=list(backend) if backend else ["mlflow"],
            mlflow_tracking_uri=final_config["tracking_uri"],
            mlflow_experiment=final_config["experiment"],
            mlflow_run_id=final_config["run_id"],
            mlflow_run_name=final_config["run_name"],
            mlflow_insecure_tls=final_config["insecure_tls"],
            mlflow_secrets_path=Path(final_config["secrets_path"])
            if final_config["secrets_path"]
            else None,
            mlflow_config_data=final_config,
            dry_run=dry_run,
            verbose=verbose,
            status_yaml_path=status_yaml_path,
            upload_workers=upload_workers,
        )
    except Exception as e:  # noqa: BLE001
        import traceback

        full_traceback = traceback.format_exc()
        click.echo(f"artifacts export failed: {e}", err=True)
        click.echo("Full traceback:", err=True)
        click.echo(full_traceback, err=True)
        sys.exit(3)

    # Check return value outside try block to avoid intercepting SystemExit
    if exit_code != 0:
        sys.exit(exit_code)


@click.command("censor")
@click.option(
    "--from",
    "from_path",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help="Source directory containing artifacts to censor.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Output directory for cleaned artifacts. If not specified, censoring is done in-place.",
)
@click.option("--dry-run", is_flag=True, help="Show what would be censored without making changes.")
@click.option("-v", "--verbose", is_flag=True, help="Show detailed censoring information.")
@click.option(
    "--report",
    "report_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write censoring report to this file.",
)
def artifacts_censor(
    from_path: Path,
    output_path: Path | None,
    dry_run: bool,
    verbose: bool,
    report_path: Path | None,
) -> None:
    """Censor sensitive artifacts by removing files containing secrets or sensitive patterns."""
    import json
    import shutil
    from datetime import datetime

    if not from_path.exists():
        click.echo(f"Error: Source path does not exist: {from_path}", err=True)
        sys.exit(1)

    if not from_path.is_dir():
        click.echo(f"Error: Source path is not a directory: {from_path}", err=True)
        sys.exit(1)

    # Collect all artifact files
    all_artifact_paths = [p for p in from_path.rglob("*") if p.is_file()]

    if not all_artifact_paths:
        click.echo(f"No artifacts found in {from_path}")
        return

    click.echo(f"Scanning {len(all_artifact_paths)} artifacts in {from_path}")

    # Determine which files to censor based on output_path
    if output_path:
        # Copy source tree to output directory first, then censor the copy
        if not dry_run:
            try:
                if output_path.exists():
                    import shutil

                    shutil.rmtree(output_path)
                shutil.copytree(from_path, output_path)
                click.echo(f"Copied source tree to {output_path}")
            except Exception as e:
                click.echo(f"Error copying source tree: {e}", err=True)
                sys.exit(1)
        else:
            click.echo(f"Dry run: Would copy source tree to {output_path}")

        # Get corresponding files in the output directory for censoring
        artifact_paths_to_censor = [
            output_path / p.relative_to(from_path) for p in all_artifact_paths
        ]
        censoring_source_path = output_path
    else:
        # Use original files for in-place censoring
        artifact_paths_to_censor = all_artifact_paths
        censoring_source_path = from_path

    # Apply censoring to the target files (copy or original)
    filtered_paths, censoring_results = apply_censoring_to_artifacts(
        artifact_paths_to_censor, censoring_enabled=True, verbose=verbose, dry_run=dry_run
    )

    # Separate results: excluded (fully censored), sanitized (redacted in-place), and clean
    excluded_files = [r.file_path for r in censoring_results if r.censored and not r.sanitized]
    sanitized_files = [r.file_path for r in censoring_results if r.sanitized]
    clean_files = [r.file_path for r in censoring_results if not r.censored]

    # Print summary
    click.echo("\nCensoring Summary:")
    click.echo(f"  Total files scanned: {len(all_artifact_paths)}")
    click.echo(f"  Clean files: {len(clean_files)}")
    click.echo(f"  Sanitized files: {len(sanitized_files)}")
    click.echo(f"  Excluded files: {len(excluded_files)}")

    if (excluded_files or sanitized_files) and verbose:
        if sanitized_files:
            click.echo("\nSanitized files (redacted in-place, kept):")
            for result in censoring_results:
                if result.sanitized:
                    rel_path = result.file_path.relative_to(censoring_source_path)
                    click.echo(f"  - {rel_path}: {result.reason}")
        if excluded_files:
            click.echo("\nExcluded files (to be removed):")
            for result in censoring_results:
                if result.censored and not result.sanitized:
                    rel_path = result.file_path.relative_to(censoring_source_path)
                    click.echo(f"  - {rel_path}: {result.reason}")

    # Generate report if requested
    if report_path:
        report_data = {
            "timestamp": datetime.now().isoformat(),
            "source_directory": str(from_path),
            "output_directory": str(output_path) if output_path else None,
            "total_files": len(all_artifact_paths),
            "clean_files": len(clean_files),
            "sanitized_files": len(sanitized_files),
            "excluded_files": len(excluded_files),
            "sanitized_details": [
                {
                    "file": str(r.file_path.relative_to(censoring_source_path)),
                    "reason": r.reason,
                }
                for r in censoring_results
                if r.sanitized
            ],
            "excluded_details": [
                {
                    "file": str(r.file_path.relative_to(censoring_source_path)),
                    "reason": r.reason,
                }
                for r in censoring_results
                if r.censored and not r.sanitized
            ],
        }

        if dry_run:
            click.echo(f"\nDry run: Would write report to {report_path}")
        else:
            try:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                with open(report_path, "w") as f:
                    json.dump(report_data, f, indent=2)
                click.echo(
                    f"\nCensoring report written to {report_path}\n{report_path.read_text()}"
                )
            except Exception as e:
                click.echo(f"Error writing report: {e}", err=True)

    # Handle output
    if dry_run:
        click.echo("\nDry run: No files were modified")
        if output_path:
            kept_count = len(clean_files) + len(sanitized_files)
            click.echo(
                f"Would copy source tree to {output_path} and remove {len(excluded_files)} excluded files"
            )
        else:
            click.echo(f"Would remove {len(excluded_files)} excluded files from {from_path}")

    elif output_path:
        # Files have already been copied and censored in the output directory
        # Now remove excluded files from the output directory
        if excluded_files:
            try:
                for excluded_file in excluded_files:
                    excluded_file.unlink()
                    if verbose:
                        rel_path = excluded_file.relative_to(output_path)
                        click.echo(f"  Removed from output: {rel_path}")

                kept_count = len(clean_files) + len(sanitized_files)
                click.echo(
                    f"\nProcessed artifacts in {output_path}: {kept_count} kept, {len(excluded_files)} removed"
                )

            except Exception as e:
                click.echo(f"Error removing excluded files from output: {e}", err=True)
                sys.exit(1)
        else:
            kept_count = len(clean_files) + len(sanitized_files)
            click.echo(f"\nProcessed artifacts in {output_path}: {kept_count} files, all clean!")

    else:
        # Remove only excluded files in-place (sanitized files are already redacted and kept)
        if excluded_files:
            try:
                for excluded_file in excluded_files:
                    excluded_file.unlink()
                    if verbose:
                        rel_path = excluded_file.relative_to(from_path)
                        click.echo(f"  Removed: {rel_path}")

                click.echo(f"\nRemoved {len(excluded_files)} excluded files from {from_path}")

            except Exception as e:
                click.echo(f"Error removing files: {e}", err=True)
                sys.exit(1)
        else:
            click.echo("\nNo files to remove - all artifacts are clean!")


@click.command("import")
@click.option("--from-mlflow", "mlflow_run_id", help="MLflow run ID to download artifacts from.")
@click.option("--from-mlflow-url", "mlflow_url", help="MLflow web UI URL.")
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Directory where downloaded artifacts will be saved.",
)
@click.option("--mlflow-tracking-uri", envvar="MLFLOW_TRACKING_URI")
@click.option("--artifact-path", default="")
@click.option("--timeout", default=300, type=click.IntRange(min=1))
@click.option("--mlflow-insecure-tls", is_flag=True)
@click.option("--mlflow-experiment", default=None)
@click.option("--mlflow-workspace", default=None)
@click.option(
    "--mlflow-secrets",
    "mlflow_secrets_path",
    type=click.Path(path_type=Path, exists=True),
    default=None,
)
@click.pass_context
def artifacts_import(
    ctx: click.Context,
    mlflow_run_id: str | None,
    mlflow_url: str | None,
    output_dir: Path,
    mlflow_tracking_uri: str | None,
    artifact_path: str,
    timeout: int,
    mlflow_insecure_tls: bool,
    mlflow_experiment: str | None,
    mlflow_workspace: str | None,
    mlflow_secrets_path: Path | None,
) -> None:
    """Download artifacts from MLflow."""

    try:
        run_artifacts_import(
            mlflow_run_id=mlflow_run_id,
            mlflow_url=mlflow_url,
            output_dir=output_dir,
            mlflow_tracking_uri=mlflow_tracking_uri,
            artifact_path=artifact_path,
            timeout=timeout,
            mlflow_insecure_tls=mlflow_insecure_tls,
            mlflow_experiment=mlflow_experiment,
            mlflow_workspace=mlflow_workspace,
            mlflow_secrets_path=mlflow_secrets_path,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"❌ artifacts import failed: {e}", err=True)
        sys.exit(3)


@click.command("html-export")
@click.option(
    "--input",
    "input_file",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help="Input JSON file (KPI data or regression report)",
)
@click.option(
    "--output",
    "output_file",
    type=click.Path(path_type=Path),
    help="Output HTML file (default: input file with .html extension)",
)
def html_export_cmd(input_file: Path, output_file: Path | None) -> None:
    """Convert JSON file (KPI data or regression report) to HTML."""
    from projects.caliper.engine.kpi.html_generator import HTMLGenerator

    try:
        generator = HTMLGenerator()
        html_output = generator.convert_json_to_html(input_file, output_file)
        click.echo(f"📄 Generated HTML report: {html_output}")

    except Exception as e:
        click.echo(f"❌ HTML generation failed: {e}", err=True)
        sys.exit(1)
