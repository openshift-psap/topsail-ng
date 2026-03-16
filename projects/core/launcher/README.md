# TOPSAIL Python Launcher

A modern Python/Click alternative to the bash launcher scripts, providing containerized development environment management for TOPSAIL using Toolbx and Podman.

## Overview

This Python launcher (`topsail_launcher.py`) provides the same functionality as the original bash scripts but with enhanced features:

- **Unified CLI**: Single command with subcommands instead of multiple scripts
- **Configurable environment**: YAML-based configuration with sensible defaults
- **Verbose mode**: Detailed logging of all subprocess execution
- **Status checking**: Built-in environment validation and readiness checks
- **Enhanced error handling**: Better error messages and recovery suggestions

## Quick Start

### 1. Setup Configuration

Copy and customize the configuration:

```bash
cp launcher_config.yaml.example launcher_config.yaml
```

Edit `launcher_config.yaml` to match your environment:

```yaml
# TOPSAIL Launcher Configuration
topsail_home: "/path/to/your/topsail-ng"
topsail_toolbox_name: "topsail-ng"
topsail_toolbox_command: "zsh"  # or "bash"
container_image: "localhost/topsail-ng:latest"
container_file: "projects/core/image/Containerfile"  # Relative to topsail_home
topsail_image_extra_pkg: "vim git-core zsh"

# Environment variables exported to container
exported_env_vars:
  - "PSAP_ODS_SECRET_PATH"
  - "KUBECONFIG"
  - "OPENSHIFT_BUILD_NAMESPACE"
  - "OPENSHIFT_BUILD_REFERENCE"
```

### 2. Build and Setup

```bash
# Check current status
./topsail_launcher.py status

# Build the container image
./topsail_launcher.py build

# Create/recreate the toolbox container
./topsail_launcher.py recreate

# Verify everything is ready
./topsail_launcher.py status
```

### 3. Start Development

```bash
# Enter the development environment
./topsail_launcher.py enter

# Run TOPSAIL commands
./topsail_launcher.py run llm-d prepare
./topsail_launcher.py run llm-d test
```

## Command Reference

### Core Commands

| Command | Description | Bash Equivalent |
|---------|-------------|------------------|
| `build` | Build container image | `./topsail_build` |
| `recreate` | Recreate toolbox container | `./recreate` |
| `enter` | Enter development environment | `./topsail_enter` |
| `run` | Run TOPSAIL commands | `./topsail_run` |
| `run-cmd` | Run toolbox commands | `./topsail_run_cmd` |

### Utility Commands

| Command | Description |
|---------|-------------|
| `status` | Show environment status and readiness |
| `config` | Display current configuration |
| `config --set KEY VALUE` | Update configuration |

### Examples

```bash
# Build with extra packages
./topsail_launcher.py build --extra-packages vim htop

# Enter environment in current directory
./topsail_launcher.py enter --here

# Run specific command in container
./topsail_launcher.py enter "pytest tests/"

# Check what environment variables will be exported
./topsail_launcher.py --verbose enter "env | grep TOPSAIL"

# Update configuration
./topsail_launcher.py config --set topsail_toolbox_command zsh
./topsail_launcher.py config --set container_image my-custom-image:latest
```

## Configuration Options

### Core Settings

- **`topsail_home`**: Path to TOPSAIL repository
- **`container_image`**: Image name for build and run operations
- **`container_file`**: Path to Containerfile (relative to `topsail_home`)
- **`topsail_toolbox_name`**: Container name for toolbox
- **`topsail_toolbox_command`**: Default shell command (bash/zsh)
- **`topsail_image_extra_pkg`**: Extra packages to install during build

### Environment Variables

The `exported_env_vars` list controls which environment variables are passed to the container:

```yaml
exported_env_vars:
  - "KUBECONFIG"
  - "PSAP_ODS_SECRET_PATH"
  - "OPENSHIFT_BUILD_NAMESPACE"
  - "OPENSHIFT_BUILD_REFERENCE"
  - "MY_CUSTOM_VAR"
```

Variables not in your environment are silently ignored.

## Verbose Mode

Use `-v` or `--verbose` to see detailed execution information:

```bash
./topsail_launcher.py --verbose status
./topsail_launcher.py --verbose build
./topsail_launcher.py --verbose enter "make test"
```

Verbose mode shows:
- Configuration loading details
- All subprocess commands being executed
- Environment variables being exported
- Container setup and execution details

## Status Checking

The `status` command provides comprehensive environment validation:

```bash
./topsail_launcher.py status
```

**Sample output:**
```
📊 TOPSAIL Development Environment Status:

🔧 Toolbox: ✅ Available
📁 topsail_home: ✅ Found at /path/to/topsail-ng
📦 Container Image: ✅ localhost/topsail-ng:latest available
🏗️  Container: ✅ topsail-ng exists
🐳 Containerfile: ✅ Found at /path/to/topsail-ng/projects/core/image/Containerfile

🚀 Status: ✅ Ready for development!
   💡 Use 'enter' to start working
```

## Troubleshooting

### Common Issues

**Container image not found:**
```bash
./topsail_launcher.py build
```

**Container doesn't exist:**
```bash
./topsail_launcher.py recreate
```

**Toolbox not available:**
The launcher automatically falls back to direct podman usage.

**Permission issues:**
Ensure your user is in the appropriate groups for container operations.

### Debugging

Use verbose mode to see exactly what commands are being executed:

```bash
./topsail_launcher.py --verbose build
./topsail_launcher.py --verbose enter "your-command"
```

### Configuration Issues

Check your current configuration:

```bash
./topsail_launcher.py config
```

Verify paths exist and are correct:

```bash
ls -la $(./topsail_launcher.py config | grep topsail_home | cut -d: -f2 | tr -d ' ')
```

## Migration from Bash Scripts

The Python launcher is designed as a drop-in replacement:

| Bash Script | Python Equivalent |
|-------------|-------------------|
| `./topsail_build` | `./topsail_launcher.py build` |
| `./topsail_enter` | `./topsail_launcher.py enter` |
| `./topsail_enter here` | `./topsail_launcher.py enter --here` |
| `./topsail_run <args>` | `./topsail_launcher.py run <args>` |
| `./topsail_run_cmd <args>` | `./topsail_launcher.py run-cmd <args>` |
| `./recreate <name> <image>` | `./topsail_launcher.py recreate` |

### Benefits of Python Launcher

- **Better error messages**: Clear feedback on what went wrong
- **Status validation**: Know if your environment is ready before starting work
- **Configurable exports**: Control which environment variables are passed through
- **Verbose debugging**: See exactly what commands are running
- **Unified interface**: Single command with consistent options

## Requirements

- Python 3.7+
- PyYAML (`pip install pyyaml`)
- Click (`pip install click`)
- Podman or Docker
- Toolbx (optional - will fall back to podman)

## Constitutional Compliance

This launcher follows TOPSAIL-NG's constitutional principles:

- **CI-First Testing**: Consistent containerized development environment
- **Reproducible Results**: Locked container configuration and environment
- **Scale-Aware Design**: Lightweight launcher with efficient container management
- **Observable Measurements**: Verbose mode provides complete execution visibility