# Skeleton Project

This is a template/skeleton project that demonstrates how to create a new project within the **TOPSAIL-NG** test harness framework.

## Overview

This skeleton shows the essential structure and patterns for building projects that comply with TOPSAIL-NG's constitutional principles:

- **CI-First Testing**: Structured phases ensure consistent CI integration
- **Observable Measurements**: Command execution logging and timing
- **Reproducible Results**: Deterministic operations with clear success/failure
- **Scale-Aware Design**: Efficient synchronous operations
- **AI Platform Specificity**: OpenShift AI focused testing patterns

## Project Structure

```
skeleton/
├── orchestration/
│   └── ci.py          # Main CI script with Click-based CLI
├── README.md          # This documentation
├── config.yaml        # Project configuration (optional)
├── tests/             # Test scripts and data (optional)
└── scripts/           # Helper scripts (optional)
```

## Quick Start

### 1. Run Individual Phases

```bash
# From the TOPSAIL-NG root directory

# Prepare environment
./run_ci skeleton ci prepare

# Run tests
./run_ci skeleton ci test

# Clean up
./run_ci skeleton ci cleanup
```

### 2. Development Options

```bash
# Verbose output
./run_ci skeleton ci --verbose test

# See all available commands
./run_ci skeleton ci --help
```

## Creating Your Own Project

### Step 1: Copy Skeleton

```bash
cp -r projects/skeleton projects/your-project-name
cd projects/your-project-name
```

### Step 2: Customize

1. **Update `orchestration/ci.py`**:
   - Change `self.project_name` to your project name
   - Replace placeholder `echo` commands with actual test logic
   - Update the CLI description and help text

2. **Update `README.md`**:
   - Document your project's purpose and usage
   - Add specific setup instructions

3. **Add configuration** (optional):
   - Create `config.yaml` for project-specific settings
   - Reference it in your CI script

### Step 3: Implement Test Logic

Replace the example `echo` commands with your actual test logic:

#### Prepare Phase
```python
def prepare(self):
    self.log("Starting prepare phase...")

    # Example: Install dependencies
    if not self.execute_command(
        "oc apply -f manifests/setup.yaml",
        "Deploy setup resources"
    ):
        return 1

    # Example: Validate environment
    if not self.execute_command(
        "oc get nodes",
        "Check cluster nodes"
    ):
        return 1

    self.log("Prepare phase completed!", "success")
    return 0
```

#### Test Phase
```python
def test(self):
    self.log("Starting test phase...")

    # Example: Run performance tests
    if not self.execute_command(
        "python scripts/performance_test.py --config config.yaml",
        "Running performance tests"
    ):
        return 1

    # Example: Run functional tests
    if not self.execute_command(
        "pytest tests/ -v",
        "Running functional tests"
    ):
        return 1

    self.log("Test phase completed!", "success")
    return 0
```

#### Cleanup Phase
```python
def cleanup(self):
    self.log("Starting cleanup phase...")

    # Example: Remove test resources
    self.execute_command(
        "oc delete -f manifests/",
        "Cleanup test resources"
    )

    # Example: Generate reports
    self.execute_command(
        "python scripts/generate_report.py",
        "Generate final report"
    )

    self.log("Cleanup phase completed!", "success")
    return 0
```

## Key Patterns

### 1. Phase Structure

Each project should implement these standard phases:
- **prepare**: Set up environment and dependencies
- **test**: Execute main testing logic
- **cleanup**: Clean up resources and finalize

### 2. Command Execution

Use the `execute_command` method for consistent execution and logging:

```python
# Basic command execution
success = self.execute_command("your-command", "Description")
if not success:
    return 1  # Exit with error

# Command with complex logic
result = self.execute_command(
    "kubectl get pods -o json",
    "Check pod status"
)
```

### 3. Error Handling

Always check command results and handle failures appropriately:

```python
if not self.execute_command("critical-command", "Critical step"):
    self.log("Critical step failed!", "error")
    return 1  # Exit with error code

# Cleanup commands can be non-critical
self.execute_command("cleanup-command", "Optional cleanup")
# Continue regardless of success
```

### 4. Logging

Use the logging methods for consistent output:

```python
self.log("Starting operation", "info")      # ℹ️ [project] Starting operation
self.log("Operation completed", "success")  # ✅ [project] Operation completed
self.log("Warning occurred", "warning")     # ⚠️ [project] Warning occurred
self.log("Error occurred", "error")         # ❌ [project] Error occurred
```

### 5. Verbose Mode

The framework automatically handles verbose mode:

```python
# In verbose mode, command details are automatically shown
# Your execute_command calls will show:
# - Command being executed
# - Command output (if any)
# - Execution duration
```

## Click CLI Structure

The skeleton uses Click groups to organize commands:

```python
@click.group()
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose output')
@click.pass_context
def cli(ctx, verbose):
    """Project CI Operations for TOPSAIL-NG."""
    ctx.ensure_object(types.SimpleNamespace)
    ctx.obj.verbose = verbose
    ctx.obj.runner = YourProjectTestRunner(verbose)

@cli.command()
@click.pass_context
def prepare(ctx):
    """Prepare phase - Set up environment and dependencies."""
    runner = ctx.obj.runner
    exit_code = runner.prepare()
    sys.exit(exit_code)
```

## Best Practices

### 1. Constitutional Compliance

- ✅ **CI-First**: Design for automated execution without user interaction
- ✅ **Observable**: Log important events and command execution
- ✅ **Reproducible**: Use deterministic operations and clear error codes
- ✅ **Scale-Aware**: Keep operations efficient and focused
- ✅ **AI Platform Specific**: Focus on OpenShift AI scenarios and tooling

### 2. Error Handling

- Always validate prerequisites in prepare phase
- Check command results and fail fast on errors
- Provide meaningful error messages with context
- Clean up resources even when tests fail (use try/except if needed)

### 3. Command Design

- Make commands idempotent when possible
- Use meaningful descriptions for all execute_command calls
- Test commands locally before adding to CI
- Consider timeouts for long-running operations

### 4. Configuration

- Keep project configuration in `config.yaml` or environment variables
- Make tests configurable for different environments
- Document all configuration options
- Use sensible defaults

## Testing the Skeleton

```bash
# Test individual phases
./run_ci skeleton ci prepare
./run_ci skeleton ci test
./run_ci skeleton ci cleanup

# Test with verbose output
./run_ci skeleton ci --verbose prepare

# See all available commands
./run_ci skeleton ci --help
```

## Integration with CI Systems

The skeleton is designed for easy CI integration:

```bash
# In your CI pipeline
./run_ci your-project ci prepare || exit 1
./run_ci your-project ci test || exit 1
./run_ci your-project ci cleanup  # Always run cleanup
```

## Next Steps

1. **Study the Code**: Review `orchestration/ci.py` to understand the patterns
2. **Copy and Customize**: Create your own project based on this skeleton
3. **Implement Tests**: Replace placeholder `echo` commands with real test logic
4. **Test Integration**: Verify your project works with the run_ci entrypoint
5. **Add Documentation**: Document your specific test scenarios and setup

## Support

- Review other projects in `projects/` for more examples
- Check the main TOPSAIL-NG documentation
- Study the run_ci entrypoint code in `projects/core/ci_entrypoint/`

