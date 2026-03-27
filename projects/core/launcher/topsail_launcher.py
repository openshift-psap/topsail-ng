#!/usr/bin/env python3
"""
TOPSAIL Development Environment Launcher

A simplified Python/Click version of the bash launcher scripts that provides
containerized development environment management for TOPSAIL using Toolbx and Podman.

Constitutional Compliance:
- CI-First Testing: Consistent containerized development environment
- Reproducible Results: Locked container configuration and environment
- Scale-Aware Design: Lightweight launcher with efficient container management
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import click
import yaml

CONFIG_FILE = Path(__file__).resolve().parent / 'launcher_config.yaml'
CONFIG_EXAMPLE_FILE = Path(__file__).resolve().parent / 'launcher_config.yaml.example'

class TopsailLauncher:
    """Manages TOPSAIL containerized development environment."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, str]:
        """Load configuration from default and custom config files."""
        config = {
            # Default configuration
            'topsail_home': os.path.expanduser('/path/to/topsail'),
            'topsail_toolbox_command': 'bash',
            'topsail_toolbox_name': 'topsail-ng',
            'topsail_image_extra_pkg': '',
            'container_image': 'topsail-ng',  # Configurable image name for build and run
            'container_file': None,  # Will be set dynamically based on topsail_home
            'exported_env_vars': [  # Environment variables to export to container
                'PSAP_ODS_SECRET_PATH',
                'KUBECONFIG',
                'OPENSHIFT_BUILD_NAMESPACE',
                'OPENSHIFT_BUILD_REFERENCE'
            ],
            'custom_env_vars': {}  # Custom environment variables as key/value pairs
        }

        # Copy from example if config doesn't exist
        if not CONFIG_FILE.exists() and CONFIG_EXAMPLE_FILE.exists():
            try:
                import shutil
                shutil.copy2(CONFIG_EXAMPLE_FILE, CONFIG_FILE)
                if self.verbose:
                    click.echo(f"📄 Copied example config from {CONFIG_EXAMPLE_FILE} to {CONFIG_FILE}")
            except Exception as e:
                click.echo(f"⚠️  Warning: Failed to copy example config: {e}", err=True)

        # Load custom config if it exists
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    custom_config = yaml.safe_load(f)
                    if custom_config:
                        config.update(custom_config)
                    if self.verbose:
                        click.echo(f"📄 Loaded custom config from {CONFIG_FILE}")
            except Exception as e:
                click.echo(f"⚠️  Warning: Failed to load config {CONFIG_FILE}: {e}", err=True)

        # Expand environment variables
        for key, value in config.items():
            if isinstance(value, str):
                config[key] = os.path.expandvars(os.path.expanduser(value))

        # Set container file path - always relative to topsail_home
        if not config['container_file']:
            # Use default path
            container_file_path = 'projects/core/image/Containerfile'
        else:
            container_file_path = config['container_file']

        # Always resolve relative to topsail_home unless it's already absolute
        if not os.path.isabs(container_file_path):
            config['container_file'] = os.path.join(config['topsail_home'], container_file_path)
        else:
            config['container_file'] = container_file_path

        # Ensure exported_env_vars is a list
        if isinstance(config['exported_env_vars'], str):
            # Handle comma-separated string from YAML
            config['exported_env_vars'] = [var.strip() for var in config['exported_env_vars'].split(',')]
        elif not isinstance(config['exported_env_vars'], list):
            # Fallback to default if invalid
            config['exported_env_vars'] = ['PSAP_ODS_SECRET_PATH', 'KUBECONFIG', 'OPENSHIFT_BUILD_NAMESPACE', 'OPENSHIFT_BUILD_REFERENCE']

        # Ensure custom_env_vars is a dictionary
        if not isinstance(config.get('custom_env_vars'), dict):
            config['custom_env_vars'] = {}

        return config

    def _has_toolbox(self) -> bool:
        """Check if toolbox is available."""
        try:
            cmd = ['toolbox', '--help']
            if self.verbose:
                click.echo(f"🔍 Checking toolbox: {' '.join(cmd)}")
            subprocess.run(cmd, capture_output=True, check=False, timeout=5)
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _get_container_env(self) -> Dict[str, str]:
        """Get environment variables to pass to the container."""
        # Core environment variables (always included)
        env = {
            'TOPSAIL_HOME': self.config['topsail_home'],
            'PYTHONPATH': self.config['topsail_home'],
            'HOME': os.environ.get('HOME', ''),
        }

        # Add configurable environment variables (exported from current environment)
        exported_vars = self.config.get('exported_env_vars', [])
        if self.verbose:
            click.echo(f"📋 Configured environment variables to export: {exported_vars}")

        for var in exported_vars:
            if var in os.environ:
                env[var] = os.environ[var]
                if self.verbose:
                    click.echo(f"   ✅ {var}={os.environ[var]}")
            else:
                if self.verbose:
                    click.echo(f"   ⚠️  {var} not found in environment")

        # Add custom environment variables (direct key/value pairs)
        custom_vars = self.config.get('custom_env_vars', {})
        if custom_vars and self.verbose:
            click.echo(f"📋 Custom environment variables: {list(custom_vars.keys())}")

        for var, value in custom_vars.items():
            env[var] = str(value)  # Ensure value is string
            if self.verbose:
                click.echo(f"   ✅ {var}={value}")

        return env

    def _image_exists(self) -> bool:
        """Check if the container image exists."""
        try:
            cmd = ['podman', 'image', 'exists', self.config['container_image']]
            if self.verbose:
                click.echo(f"🔍 Checking image: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, check=False, timeout=10)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _container_exists(self) -> bool:
        """Check if the toolbox container exists."""
        try:
            cmd = ['podman', 'inspect', '--type', 'container', self.config['topsail_toolbox_name']]
            if self.verbose:
                click.echo(f"🔍 Checking container: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, check=False, timeout=10)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _run_toolbox_command(self, command: str, working_dir: Optional[str] = None) -> int:
        """Run a command in the toolbox environment."""
        if self.verbose:
            click.echo(f"🔧 Running in container: {command}")

        env_vars = self._get_container_env()
        env_setup = '; '.join(f'export {k}="{v}"' for k, v in env_vars.items() if v)

        if working_dir:
            full_command = f"cd {working_dir} && {env_setup}; {command}"
        else:
            full_command = f"{env_setup}; {command}"

        if self._has_toolbox():
            cmd = [
                'toolbox', 'run', '-c', self.config['topsail_toolbox_name'],
                '--', 'bash', '-c', full_command
            ]
        else:
            # Fallback to podman
            env_args = []
            for k, v in env_vars.items():
                if v:
                    env_args.extend(['--env', f'{k}={v}'])

            topsail_home = self.config['topsail_home']
            home = os.environ.get('HOME', '')

            cmd = [
                'podman', 'run', '--rm', '-it',
                '--security-opt', 'label=disable',
                '--cgroupns', 'host',
                '--network=host',
                '-v', f'{topsail_home}:{topsail_home}:Z',
                '-w', working_dir or topsail_home
            ] + env_args + [
                self.config['container_image'],
                '/bin/bash', '-c', full_command
            ]

        if self.verbose:
            click.echo(f"🚀 Executing container command:")
            click.echo(f"   Command: {' '.join(cmd)}")
            if env_vars:
                click.echo(f"   Environment variables:")
                for k, v in env_vars.items():
                    if v:
                        click.echo(f"      {k}={v}")
            if working_dir:
                click.echo(f"   Working directory: {working_dir}")

        try:
            return subprocess.run(cmd).returncode
        except KeyboardInterrupt:
            click.echo("\n⚠️  Interrupted by user")
            return 1
        except Exception as e:
            click.echo(f"❌ Command failed: {e}", err=True)
            return 1

    def build_image(self, extra_packages: List[str] = None) -> int:
        """Build the TOPSAIL container image."""
        click.echo("🔨 Building TOPSAIL container image...")

        topsail_home = self.config['topsail_home']
        container_file = Path(self.config['container_file'])
        image_name = self.config['container_image']

        if not container_file.exists():
            click.echo(f"❌ Containerfile not found at {container_file}", err=True)
            return 1

        # Build base image
        build_cmd = [
            'podman', 'build',
            str(topsail_home),
            '-f', str(container_file),
            '-t', image_name
        ]

        if self.verbose:
            click.echo(f"🔨 Executing: {' '.join(build_cmd)}")

        try:
            result = subprocess.run(build_cmd, check=False)
            if result.returncode != 0:
                click.echo("❌ Base image build failed", err=True)
                return result.returncode

            # Build overlay with extra packages if needed
            packages = extra_packages or []
            if self.config.get('topsail_image_extra_pkg'):
                packages.extend(self.config['topsail_image_extra_pkg'].split())

            if packages:
                overlay_dockerfile = f"""FROM {image_name}
ENTRYPOINT []
CMD []
USER 0
RUN dnf install -y --quiet {' '.join(packages)}
USER 1001
"""
                overlay_cmd = [
                    'podman', 'build',
                    '--tag', image_name,
                    '--from', image_name,
                    '--file', '-'
                ]

                if self.verbose:
                    click.echo(f"🔨 Executing overlay build: {' '.join(overlay_cmd)}")
                    click.echo(f"📝 Overlay Dockerfile:")
                    for line in overlay_dockerfile.split('\n'):
                        if line.strip():
                            click.echo(f"   {line}")

                overlay_proc = subprocess.Popen(
                    overlay_cmd,
                    stdin=subprocess.PIPE,
                    text=True
                )
                overlay_proc.communicate(overlay_dockerfile)

                if overlay_proc.returncode != 0:
                    click.echo("❌ Overlay build failed", err=True)
                    return overlay_proc.returncode

            click.echo("✅ Container image built successfully!")
            return 0

        except Exception as e:
            click.echo(f"❌ Build failed: {e}", err=True)
            return 1

    def recreate_container(self) -> int:
        """Recreate the toolbox container."""
        container_name = self.config['topsail_toolbox_name']
        image_name = self.config['container_image']

        click.echo(f"♻️  Recreating container: {container_name}")

        # Stop and remove existing container
        try:
            stop_cmd = ['podman', 'stop', container_name]
            rm_cmd = ['podman', 'rm', container_name]

            if self.verbose:
                click.echo(f"🛑 Executing: {' '.join(stop_cmd)}")
            subprocess.run(stop_cmd, capture_output=True, check=False)

            if self.verbose:
                click.echo(f"🗑️  Executing: {' '.join(rm_cmd)}")
            subprocess.run(rm_cmd, capture_output=True, check=False)
        except Exception:
            pass  # Container might not exist

        # Create new container
        if self._has_toolbox():
            cmd = ['toolbox', 'create', container_name, '--image', image_name]
        else:
            topsail_home = self.config['topsail_home']
            cmd = [
                'podman', 'run',
                '--name', container_name,
                '-v', f'{topsail_home}:/topsail',
                '--detach', '--tty',
                '--entrypoint', '/bin/bash',
                image_name
            ]

        if self.verbose:
            click.echo(f"🏗️  Executing: {' '.join(cmd)}")

        try:
            result = subprocess.run(cmd, check=False)
            if result.returncode == 0:
                click.echo("✅ Container recreated successfully!")
            else:
                click.echo("❌ Container recreation failed", err=True)
            return result.returncode
        except Exception as e:
            click.echo(f"❌ Recreation failed: {e}", err=True)
            return 1


@click.group()
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose output')
@click.pass_context
def cli(ctx, verbose):
    """TOPSAIL Development Environment Launcher."""
    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose
    ctx.obj['launcher'] = TopsailLauncher(verbose)


@cli.command()
@click.option('--extra-packages', '-p', multiple=True, help='Extra packages to install')
@click.pass_context
def build(ctx, extra_packages):
    """Build the TOPSAIL container image."""
    launcher = ctx.obj['launcher']
    sys.exit(launcher.build_image(list(extra_packages)))


@cli.command()
@click.pass_context
def recreate(ctx):
    """Recreate the TOPSAIL toolbox container."""
    launcher = ctx.obj['launcher']
    sys.exit(launcher.recreate_container())


@cli.command()
@click.argument('command', required=False)
@click.argument('args', nargs=-1)
@click.option('--here', is_flag=True, help='Stay in current directory')
@click.pass_context
def enter(ctx, command, args, here):
    """Enter the TOPSAIL development environment."""
    launcher = ctx.obj['launcher']

    if command:
        full_command = f"{command} {' '.join(args)}"
    elif here:
        full_command = launcher.config['topsail_toolbox_command']
        working_dir = os.getcwd()
    else:
        full_command = launcher.config['topsail_toolbox_command']
        working_dir = launcher.config['topsail_home']

    if not here and not command:
        working_dir = launcher.config['topsail_home']
    else:
        working_dir = os.getcwd() if here else None

    sys.exit(launcher._run_toolbox_command(full_command, working_dir))


@cli.command()
@click.argument('args', nargs=-1)
@click.pass_context
def run(ctx, args):
    """Run TOPSAIL's main run command in the container."""
    launcher = ctx.obj['launcher']
    command = f"./run {' '.join(args)}"
    working_dir = launcher.config['topsail_home']
    sys.exit(launcher._run_toolbox_command(command, working_dir))


@cli.command()
@click.argument('args', nargs=-1)
@click.pass_context
def run_cmd(ctx, args):
    """Run TOPSAIL's run_toolbox.py command in the container."""
    launcher = ctx.obj['launcher']
    command = f"./run_toolbox.py {' '.join(args)}"
    working_dir = launcher.config['topsail_home']
    sys.exit(launcher._run_toolbox_command(command, working_dir))


@cli.command()
@click.pass_context
def status(ctx):
    """Show status of TOPSAIL development environment."""
    launcher = ctx.obj['launcher']

    click.echo("📊 TOPSAIL Development Environment Status:")
    click.echo()

    # Check toolbox availability
    if launcher._has_toolbox():
        click.echo("🔧 Toolbox: ✅ Available")
    else:
        click.echo("🔧 Toolbox: ❌ Not available (using podman fallback)")

    # Check topsail_home
    topsail_home = Path(launcher.config['topsail_home'])
    if topsail_home.exists():
        click.echo(f"📁 topsail_home: ✅ Found at {topsail_home}")
    else:
        click.echo(f"📁 topsail_home: ❌ Not found at {topsail_home}")

    # Check container image
    image_name = launcher.config['container_image']
    if launcher._image_exists():
        click.echo(f"📦 Container Image: ✅ {image_name} available")
    else:
        click.echo(f"📦 Container Image: ❌ {image_name} not found")
        click.echo(f"   💡 Run 'build' to create the image")

    # Check container
    container_name = launcher.config['topsail_toolbox_name']
    if launcher._container_exists():
        click.echo(f"🏗️  Container: ✅ {container_name} exists")
    else:
        click.echo(f"🏗️  Container: ❌ {container_name} not found")
        click.echo(f"   💡 Run 'recreate' to create the container")

    # Check Containerfile
    containerfile = Path(launcher.config['container_file'])
    if containerfile.exists():
        click.echo(f"🐳 Containerfile: ✅ Found at {containerfile}")
    else:
        click.echo(f"🐳 Containerfile: ❌ Not found at {containerfile}")

    click.echo()

    # Overall readiness check
    ready = (
        topsail_home.exists() and
        launcher._image_exists() and
        launcher._container_exists()
    )

    if ready:
        click.echo("🚀 Status: ✅ Ready for development!")
        click.echo("   💡 Use 'enter' to start working")
    else:
        click.echo("⚠️  Status: ❌ Setup required")
        if not topsail_home.exists():
            click.echo(f"   📝 Set topsail_home: config --set topsail_home /path/to/topsail")
        if not launcher._image_exists():
            click.echo(f"   🔨 Build image: build")
        if not launcher._container_exists():
            click.echo(f"   ♻️  Create container: recreate")


@cli.command()
@click.option('--set', 'set_config', nargs=2, metavar='KEY VALUE', help='Set a configuration value')
@click.option('--set-env', 'set_env', nargs=2, metavar='VAR VALUE', help='Set a custom environment variable')
@click.option('--pass-env', 'pass_env', metavar='VAR', help='Add environment variable to exported list')
@click.option('--edit', is_flag=True, help='Edit configuration file with $EDITOR')
@click.pass_context
def config(ctx, set_config, set_env, pass_env, edit):
    """Show current configuration, set values, manage environment variables, or edit the config file."""
    launcher = ctx.obj['launcher']

    if edit:
        # Edit configuration file with $EDITOR
        editor = os.environ.get('EDITOR', 'nano')  # Default to nano if EDITOR not set

        # Create config file if it doesn't exist
        if not CONFIG_FILE.exists():
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, 'w') as f:
                yaml.dump({}, f)

        try:
            result = subprocess.run([editor, str(CONFIG_FILE)])
            if result.returncode == 0:
                click.echo(f"✅ Configuration file edited successfully")
            else:
                click.echo(f"⚠️  Editor exited with code {result.returncode}")
        except Exception as e:
            click.echo(f"❌ Failed to open editor: {e}", err=True)
            sys.exit(1)
        return

    if set_config or set_env or pass_env:
        # Load existing config
        config = {}
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    config = yaml.safe_load(f) or {}
            except Exception:
                config = {}

        # Ensure custom_env_vars and exported_env_vars exist in config
        if 'custom_env_vars' not in config:
            config['custom_env_vars'] = {}
        if 'exported_env_vars' not in config:
            config['exported_env_vars'] = []

        success_msg = ""

        if set_config:
            # Set regular configuration
            key, value = set_config

            # Validate that the key exists in the current config schema
            if key not in launcher.config:
                valid_keys = list(launcher.config.keys())
                click.echo(f"❌ Invalid config key '{key}'. Valid keys are: {', '.join(sorted(valid_keys))}", err=True)
                click.echo(f"💡 For environment variables, use: config --set-env {key} {value}", err=True)
                sys.exit(1)

            config[key] = value
            success_msg = f"✅ Set {key} = {value}"

        if set_env:
            # Set custom environment variable
            var, value = set_env
            config['custom_env_vars'][var] = value
            success_msg = f"✅ Set environment variable {var} = {value}"

        if pass_env:
            # Add environment variable to exported list
            if pass_env not in config['exported_env_vars']:
                config['exported_env_vars'].append(pass_env)
                success_msg = f"✅ Added {pass_env} to exported environment variables"
            else:
                success_msg = f"ℹ️  {pass_env} is already in exported environment variables"

        # Save config
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, 'w') as f:
                yaml.dump(config, f, indent=2, default_flow_style=False)
            click.echo(success_msg)
        except Exception as e:
            click.echo(f"❌ Failed to save config: {e}", err=True)
            sys.exit(1)
    else:
        # Show configuration
        click.echo("📋 Current TOPSAIL Launcher Configuration:")
        click.echo(f"📄 Config file: {CONFIG_FILE}")
        click.echo()

        for key, value in launcher.config.items():
            if key == 'custom_env_vars' and isinstance(value, dict):
                click.echo(f"  {key}:")
                if value:
                    for env_var, env_value in value.items():
                        click.echo(f"    {env_var}: {env_value}")
                else:
                    click.echo(f"    (none)")
            else:
                click.echo(f"  {key}: {value}")

        click.echo()
        click.echo(f"🔧 Toolbox available: {'✅ Yes' if launcher._has_toolbox() else '❌ No (using podman)'}")

        # Check if TOPSAIL_HOME exists
        topsail_home = Path(launcher.config['topsail_home'])
        if topsail_home.exists():
            click.echo(f"📁 TOPSAIL_HOME: ✅ Found at {topsail_home}")
        else:
            click.echo(f"📁 TOPSAIL_HOME: ❌ Not found at {topsail_home}")

        click.echo()
        click.echo("💡 Usage examples:")
        click.echo("   config --set topsail_home /path/to/topsail      # Config settings")
        click.echo("   config --set-env CUSTOM_VAR custom_value        # Custom env vars")
        click.echo("   config --pass-env MY_TOKEN                      # Export host env var")
        click.echo("   config --edit                                   # Edit config file")



if __name__ == '__main__':
    cli()
