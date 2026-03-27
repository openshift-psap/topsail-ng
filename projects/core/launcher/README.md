# FORGE Python Launcher

A modern Python/Click alternative to the bash launcher scripts, providing containerized development environment management for FORGE using Toolbx and Podman.

## Overview

This Python launcher (`forge_launcher.py`) provides the same functionality as the original bash scripts but with enhanced features:

- **Unified CLI**: Single command with subcommands instead of multiple scripts
- **Configurable environment**: YAML-based configuration with sensible defaults
- **Verbose mode**: Detailed logging of all subprocess execution
- **Status checking**: Built-in environment validation and readiness checks
- **Enhanced error handling**: Better error messages and recovery suggestions

## Quick Start

### 1. Setup Configuration

The launcher automatically copies `launcher_config.yaml.example` to `launcher_config.yaml` on first run if the config file doesn't exist. You can also copy it manually:

```bash
cp launcher_config.yaml.example launcher_config.yaml
```

Edit `launcher_config.yaml` to match your environment:

```yaml
# FORGE Launcher Configuration
forge_home: "/path/to/your/forge"
forge_toolbox_name: "forge"
forge_toolbox_command: "zsh"  # or "bash"
container_image: "localhost/forge:latest"
container_file: "projects/core/image/Containerfile"  # Relative to forge_home
forge_image_extra_pkg: "vim git-core zsh"

# Environment variables exported from current environment to container
exported_env_vars:
  - "PSAP_ODS_SECRET_PATH"
  - "KUBECONFIG"
  - "OPENSHIFT_BUILD_NAMESPACE"
  - "OPENSHIFT_BUILD_REFERENCE"

# Custom environment variables (key/value pairs) to set in the container
custom_env_vars:
  ARTIFACT_DIR: "/tmp/forge-artifacts"
  CUSTOM_CONFIG: "production"
  DEBUG_LEVEL: "verbose"
```

> **Note**: The `launcher_config.yaml` file is ignored by git, so you can safely add personal credentials and paths without them being committed to version control.

### 2. Build and Setup

```bash
# Check current status
./forge_launcher.py status

# Build the container image
./forge_launcher.py build

# Create/recreate the toolbox container
./forge_launcher.py recreate

# Verify everything is ready
./forge_launcher.py status
```

### 3. Start Development

```bash
# Enter the development environment
./forge_launcher.py enter

# Run FORGE commands
./forge_launcher.py run llm-d prepare
./forge_launcher.py run llm-d test
```

## Command Reference

### Core Commands

| Command | Description | Bash Equivalent |
|---------|-------------|------------------|
| `build` | Build container image | `./forge_build` |
| `recreate` | Recreate toolbox container | `./recreate` |
| `enter` | Enter development environment | `./forge_enter` |
| `run` | Run FORGE commands | `./forge_run` |
| `run-cmd` | Run toolbox commands | `./forge_run_cmd` |

### Utility Commands

| Command | Description |
|---------|-------------|
| `status` | Show environment status and readiness |
| `config` | Display current configuration |
| `config --set KEY VALUE` | Update configuration |
| `config --set-env VAR VALUE` | Set custom environment variable |
| `config --edit` | Edit configuration file with $EDITOR |

### Examples

```bash
# Build with extra packages
./forge_launcher.py build --extra-packages vim htop

# Enter environment in current directory
./forge_launcher.py enter --here

# Run specific command in container
./forge_launcher.py enter "pytest tests/"

# Check what environment variables will be exported
./forge_launcher.py --verbose enter "env | grep FORGE"

# Update configuration
./forge_launcher.py config --set forge_toolbox_command zsh
./forge_launcher.py config --set container_image my-custom-image:latest

# Set custom environment variables
./forge_launcher.py config --set-env ARTIFACT_DIR /tmp/my-artifacts
./forge_launcher.py config --set-env DEBUG_LEVEL verbose

# Edit config file directly
./forge_launcher.py config --edit
```

## Configuration Options

### Core Settings

- **`forge_home`**: Path to FORGE repository
- **`container_image`**: Image name for build and run operations
- **`container_file`**: Path to Containerfile (relative to `forge_home`)
- **`forge_toolbox_name`**: Container name for toolbox
- **`forge_toolbox_command`**: Default shell command (bash/zsh)
- **`forge_image_extra_pkg`**: Extra packages to install during build

### Environment Variables

The launcher supports two types of environment variables:

#### 1. Exported Environment Variables

The `exported_env_vars` list controls which environment variables from your current shell are passed to the container:

```yaml
exported_env_vars:
  - "KUBECONFIG"
  - "PSAP_ODS_SECRET_PATH"
  - "OPENSHIFT_BUILD_NAMESPACE"
  - "OPENSHIFT_BUILD_REFERENCE"
  - "MY_CUSTOM_VAR"
```

Variables not in your environment are silently ignored.

#### 2. Custom Environment Variables

The `custom_env_vars` dictionary allows you to set specific key/value pairs directly in the container:

```yaml
custom_env_vars:
  ARTIFACT_DIR: "/tmp/forge-artifacts"
  CUSTOM_CONFIG: "production"
  DEBUG_LEVEL: "verbose"
  API_ENDPOINT: "https://api.example.com"
```

These variables are set regardless of your current environment.

#### Setting Environment Variables via CLI

```bash
# Set a custom environment variable
./forge_launcher.py config --set-env ARTIFACT_DIR /tmp/my-artifacts
./forge_launcher.py config --set-env DEBUG_LEVEL verbose

# View current environment variables
./forge_launcher.py config
```

## Verbose Mode

Use `-v` or `--verbose` to see detailed execution information:

```bash
./forge_launcher.py --verbose status
./forge_launcher.py --verbose build
./forge_launcher.py --verbose enter "make test"
```

Verbose mode shows:
- Configuration loading details
- All subprocess commands being executed
- Environment variables being exported
- Container setup and execution details

## Status Checking

The `status` command provides comprehensive environment validation:

```bash
./forge_launcher.py status
```

**Sample output:**
```
📊 FORGE Development Environment Status:

🔧 Toolbox: ✅ Available
📁 forge_home: ✅ Found at /path/to/forge
📦 Container Image: ✅ localhost/forge:latest available
🏗️  Container: ✅ forge exists
🐳 Containerfile: ✅ Found at /path/to/forge/projects/core/image/Containerfile

🚀 Status: ✅ Ready for development!
   💡 Use 'enter' to start working
```

## Troubleshooting

### Common Issues

**Container image not found:**
```bash
./forge_launcher.py build
```

**Container doesn't exist:**
```bash
./forge_launcher.py recreate
```

**Toolbox not available:**
The launcher automatically falls back to direct podman usage.

**Permission issues:**
Ensure your user is in the appropriate groups for container operations.

### Debugging

Use verbose mode to see exactly what commands are being executed:

```bash
./forge_launcher.py --verbose build
./forge_launcher.py --verbose enter "your-command"
```

### Configuration Issues

Check your current configuration:

```bash
./forge_launcher.py config
```

Edit configuration directly:

```bash
# Open config file in your default editor
./forge_launcher.py config --edit

# Or edit specific settings via CLI
./forge_launcher.py config --set forge_home /new/path
./forge_launcher.py config --set-env CUSTOM_VAR value
```

Verify paths exist and are correct:

```bash
ls -la $(./forge_launcher.py config | grep forge_home | cut -d: -f2 | tr -d ' ')
```

**Note**: The configuration file `launcher_config.yaml` is automatically ignored by git, so you can safely store personal paths and credentials.

## Migration from Bash Scripts

The Python launcher is designed as a drop-in replacement:

| Bash Script | Python Equivalent |
|-------------|-------------------|
| `./forge_build` | `./forge_launcher.py build` |
| `./forge_enter` | `./forge_launcher.py enter` |
| `./forge_enter here` | `./forge_launcher.py enter --here` |
| `./forge_run <args>` | `./forge_launcher.py run <args>` |
| `./forge_run_cmd <args>` | `./forge_launcher.py run-cmd <args>` |
| `./recreate <name> <image>` | `./forge_launcher.py recreate` |

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

This launcher follows FORGE's constitutional principles:

- **CI-First Testing**: Consistent containerized development environment
- **Reproducible Results**: Locked container configuration and environment
- **Scale-Aware Design**: Lightweight launcher with efficient container management
- **Observable Measurements**: Verbose mode provides complete execution visibility
