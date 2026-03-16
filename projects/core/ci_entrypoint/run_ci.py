#!/usr/bin/env python3
"""
TOPSAIL-NG CI Orchestration Entrypoint

This script provides the unified entrypoint for CI operations across all projects
in the TOPSAIL-NG test harness. It follows the constitutional principle of
CI-First Testing by providing consistent, reliable CI integration.

Usage:
    run                           # List available projects
    run <project> <operation>     # Execute project operation
    run projects                  # Explicit project listing

Examples:
    run llm_d prepare
    run llm_d test
    run skeleton validate
"""


import os
import sys
import subprocess
import logging
import time
from pathlib import Path
from typing import List, Optional

TOPSAIL_HOME = Path(__file__).resolve().parent.parent.parent.parent


def setup_logging():
    """Set up logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s',
        handlers=[logging.StreamHandler(sys.stderr)]
    )

# Set up logging
setup_logging()
logger = logging.getLogger(__name__)

# Install click package using uv (as non-root user)
try:
    import click
except ImportError:
    print("📦 Installing click package...")

    # Try uv first with no-cache to avoid permission issues
    install_success = False
    try:
        subprocess.run(
            ["uv", "pip", "install", "--no-cache", "click"],
            check=True,
            capture_output=True
        )
        print("✅ Click package installed successfully with uv")
        install_success = True
    except (FileNotFoundError, subprocess.CalledProcessError):
        # Fallback to pip with user installation
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--user", "--no-cache-dir", "click"],
                check=True,
                capture_output=True
            )
            print("✅ Click package installed successfully with pip")
            install_success = True
        except subprocess.CalledProcessError as pip_error:
            print(f"❌ Failed to install click: {pip_error}")
            sys.exit(1)

    if install_success:
        # Ensure user site-packages is in path
        import site
        user_site = site.getusersitepackages()
        if user_site not in sys.path:
            sys.path.insert(0, user_site)

        # Also check for common install locations
        import os
        possible_paths = [
            os.path.expanduser("~/.local/lib/python3.11/site-packages"),
            os.path.expanduser("~/.local/lib/python3.12/site-packages"),
            os.path.expanduser("~/.local/lib/python3.13/site-packages"),
            user_site
        ]

        for path in possible_paths:
            if os.path.exists(path) and path not in sys.path:
                sys.path.insert(0, path)

        # Clear import cache and try again
        import importlib
        importlib.invalidate_caches()

        try:
            import click
        except ImportError:
            print("❌ Click installation failed - module not found after installation")
            print(f"🔍 Python path: {sys.path}")
            print(f"🔍 User site: {user_site}")
            sys.exit(1)

# Import CI preparation module
try:
    import prepare_ci
    logger.info("CI preparation module imported successfully")

    # Set up dual output as early as possible
    prepare_ci.setup_dual_output()

except ImportError as e:
    logger.warning(f"CI preparation module not available: {e}")
    prepare_ci = None


def find_project_directory(project_name: str) -> Optional[Path]:
    """
    Find the directory for the specified project.

    Args:
        project_name: Name of the project to find

    Returns:
        Path to project directory if found, None otherwise
    """
    # Look in the projects directory
    projects_dir = TOPSAIL_HOME / "projects"
    project_dir = projects_dir / project_name

    if project_dir.exists() and project_dir.is_dir():
        return project_dir

    return None


def find_ci_script(project_dir: Path, operation: str) -> Optional[Path]:
    """
    Find the appropriate CI script for the operation.

    Args:
        project_dir: Project directory path
        operation: Operation to perform (e.g., 'ci')

    Returns:
        Path to CI script if found, None otherwise
    """
    # Check possible locations for CI scripts
    possible_locations = [
        # Operation-specific script in project root
        project_dir / f"{operation}.py",
        # Operation-specific script in orchestration subdirectory
        project_dir / "orchestration" / f"{operation}.py",
    ]

    for script_path in possible_locations:
        if script_path.exists():
            return script_path

    return None


def get_available_projects() -> List[str]:
    """Get list of available projects."""

    projects_dir = TOPSAIL_HOME / "projects"

    if not projects_dir.exists():
        return []

    projects = []
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        if proj_dir.name.startswith('.'):
            continue

        projects.append(proj_dir.name)

    return sorted(projects)


def list_projects():
    """List all available projects."""
    projects = get_available_projects()

    if not projects:
        click.echo("📂 No projects found")
        return

    click.echo("📂 Available projects:")
    for project in projects:
        project_dir = find_project_directory(project)
        ci_script = find_ci_script(project_dir, "ci")
        status = "✅" if ci_script else "⚠️"
        click.echo(f"   {status} {project}")

    click.echo()
    click.echo("Usage:")
    click.echo("   run <project> <operation>  # Execute project operation")
    click.echo("   run projects               # List projects explicitly")
    click.echo()
    click.echo("Examples:")
    click.echo(f"   run {projects[0]} prepare")
    click.echo(f"   run {projects[0]} test")
    if len(projects) > 1:
        click.echo(f"   run {projects[1]} validate")



def show_project_operations(project: str):
    """Show available operations for a project by listing Python files."""
    click.echo(f"🔧 Available operations for project '{project}':")

    # Find project directory
    project_dir = find_project_directory(project)
    if not project_dir:
        click.echo(
            click.style(f"❌ ERROR: Project '{project}' not found.", fg='red'),
            err=True
        )
        return

    python_files = []
    def add_python_file(file_path):
        if not file_path.is_file():
            return
        # Skip files that are not executable
        if not os.access(file_path, os.X_OK):
            return

        operation_name = file_path.stem  # filename without .py extension
        python_files.append((operation_name, file_path))


    # List Python files in the project directory
    for file_path in (project_dir / "orchestration").glob("*.py"):
        add_python_file(file_path)

    for file_path in project_dir.glob("*.py"):
        add_python_file(file_path)

    if not python_files:
        click.echo("⚠️  No Python files found in project directory")
        click.echo(f"📁 Project directory: {project_dir}")
        return

    click.echo()
    click.echo("📄 Available Python files:")

    operation_files = []

    for operation_name, file_path in sorted(python_files):
        operation_files.append((operation_name, file_path))

    for operation_name, file_path in operation_files:
        click.echo(f"   📝 {operation_name}.py")

    click.echo(f"Usage: run {project} <filename_without_py>")
    click.echo()
    click.echo("Examples:")
    for operation_name, _ in python_files[:3]:
        click.echo(f"   run {project} {operation_name}")


def parse_cli_help(help_output: str) -> List[str]:
    """Parse CLI help output to extract available commands."""
    operations = []
    in_commands_section = False

    lines = help_output.split('\n')
    for line in lines:
        line = line.strip()

        # Look for "Commands:" section
        if line.lower().startswith('commands:'):
            in_commands_section = True
            continue

        # Stop when we hit another section
        if in_commands_section and line and not line.startswith(' '):
            break

        # Extract command names
        if in_commands_section and line.startswith(' '):
            # Format is typically: "  command_name  Description"
            parts = line.split()
            if parts:
                command_name = parts[0].strip()
                # Skip common help/utility commands
                if command_name not in ['--help', '--version', '-h', '-v']:
                    operations.append(command_name)

    return operations


def execute_project_operation(project: str, operation: str, args: tuple, verbose: bool, dry_run: bool):
    """Execute a project operation."""
    if verbose:
        click.echo(f"🚀 TOPSAIL-NG CI Orchestration")
        click.echo(f"Project: {project}")
        click.echo(f"Operation: {operation}")
        click.echo(f"Arguments: {list(args)}")

    # Execute CI preparation tasks
    if prepare_ci:
        try:
            prepare_ci.prepare(
                verbose=verbose,
                project=project,
                operation=operation,
                args=list(args),
            )
        except:
            click.echo(
                click.style("❌ ERROR: CI preparation failed", fg='red'),
                err=True
            )
            raise
    else:
        logger.warning("CI preparation module not available, skipping preparation")

    # Find project directory
    project_dir = find_project_directory(project)
    if not project_dir:
        click.echo(
            click.style(f"❌ ERROR: Project '{project}' not found.", fg='red'),
            err=True
        )

        available_projects = get_available_projects()
        if available_projects:
            click.echo(f"\n📂 Available projects:")
            for proj in available_projects:
                click.echo(f"   • {proj}")
        else:
            click.echo(f"📂 No projects found in projects/ directory")

        sys.exit(1)

    # Find CI script
    ci_script = find_ci_script(project_dir, operation)
    if not ci_script:
        click.echo(
            click.style(
                f"❌ ERROR: No CI script found for project '{project}' operation '{operation}'.",
                fg='red'
            ),
            err=True
        )
        click.echo(f"🔍 Expected: {project_dir}/ci.py or {project_dir}/{operation}.py")
        sys.exit(1)

    # Convert underscores to hyphens in args for Click compatibility
    click_args = [arg.replace('_', '-') for arg in args]

    # Prepare command - don't pass operation as it's just the script name
    cmd = [sys.executable, str(ci_script)] + click_args

    if verbose or dry_run:
        click.echo(f"\n🔧 Execution Details:")
        click.echo(f"   Command: {' '.join(cmd)}")
        click.echo(f"   Working Directory: {project_dir}")
        click.echo(f"   Script: {ci_script}")
        if any('_' in arg for arg in args):
            converted_args = [f"'{arg}' -> '{arg.replace('_', '-')}'" for arg in args if '_' in arg]
            click.echo(f"   Note: Converted underscores to hyphens: {', '.join(converted_args)}")

    if dry_run:
        click.echo(f"\n🧪 DRY RUN: Would execute the above command")
        click.echo(f"✨ Use --verbose to see execution details without --dry-run")
        return

    # Execute the command
    click.echo(f"▶️  Executing {project} {operation} {' '.join(args)}")

    try:
        # Track start time for duration calculation
        start_time = time.time()

        result = subprocess.run(
            cmd,
            cwd=project_dir,
            check=False  # Don't raise exception on non-zero exit
        )

        success = result.returncode == 0

        # Post-execution checks and status reporting
        if prepare_ci:
            status_message = prepare_ci.postchecks(project, operation, start_time, success)

            msg = click.style(status_message, fg='green' if success else 'red')
        else:
            # Fallback to simple messages if prepare_ci not available
            msg = click.style(f"✅ {project} {operation} completed successfully", fg='green') if success \
                else  click.style(f"❌ {project} {operation} failed with exit code {result.returncode}", fg='red')

        click.echo(msg, err=not success)

        sys.exit(result.returncode)

    except FileNotFoundError as e:
        click.echo(
            click.style(f"❌ ERROR: Failed to execute CI script: {e}", fg='red'),
            err=True
        )
        sys.exit(1)
    except Exception as e:
        click.echo(
            click.style(f"❌ ERROR: Unexpected error during execution: {e}", fg='red'),
            err=True
        )
        sys.exit(1)


@click.command()
@click.argument('project', required=False)
@click.argument('operation', required=False)
@click.argument('args', nargs=-1)
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose output')
@click.option('--dry-run', is_flag=True, help='Show what would be executed without running it')
def main(project, operation, args, verbose, dry_run):
    """
    TOPSAIL-NG CI Orchestration Entrypoint.

    \b
    Usage:
        run                           # List available projects
        run <project> <operation>     # Execute project operation
        run projects                  # Explicit project listing

    \b
    Examples:
        run llm-d prepare
        run llm-d test
        run skeleton validate
    """
    # No arguments - list projects
    if not project:
        list_projects()
        return

    # Special case: explicit "projects" command
    if project == "projects":
        list_projects()
        return

    # Need operation for project execution - show available operations
    if not operation:
        show_project_operations(project)
        sys.exit(1)

    # Execute project operation
    execute_project_operation(project, operation, args, verbose, dry_run)


if __name__ == "__main__":
    main()
