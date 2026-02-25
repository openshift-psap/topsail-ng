# Python DSL API Contract

**Interface**: Toolbox Command Instrumentation API
**Version**: 1.0.0
**Audience**: Toolbox command developers

## Overview

The Python DSL API provides decorators and context managers for instrumenting toolbox commands with comprehensive artifact capture. This API enables clear task naming and automatic context preservation following patterns similar to Ansible YAML structure.

## Core Decorators

### @task(name, capture_context=True, **options)

**Purpose**: Decorator for instrumenting toolbox command functions with artifact capture

**Parameters**:
- `name` (str): Human-readable task description (required)
- `capture_context` (bool): Enable automatic artifact capture (default: True)
- `timeout` (int): Task timeout in seconds (optional)
- `retry_count` (int): Number of retry attempts on failure (optional)
- `capture_level` (str): Verbosity level ('minimal', 'standard', 'verbose') (optional)

**Returns**: Decorated function with artifact capture capabilities

**Example**:
```python
@task("Deploy OpenShift AI operator", capture_context=True, timeout=300)
def deploy_operator(config):
    # Function implementation
    pass
```

**Error Handling**:
- Raises `TaskDecoratorError` for invalid parameters
- Captures exceptions in artifact context for debugging

### @capture_system_state(when='post', include=['pods', 'services'])

**Purpose**: Decorator for capturing specific system state information

**Parameters**:
- `when` (str): When to capture ('pre', 'post', 'both') (default: 'post')
- `include` (list): Resource types to capture (default: ['pods', 'services', 'deployments'])
- `namespace` (str): Kubernetes namespace scope (optional)

**Example**:
```python
@capture_system_state(when='both', include=['pods', 'services', 'configmaps'])
@task("Scale cluster nodes")
def scale_cluster(node_count):
    # Implementation
    pass
```

## Context Managers

### artifact_context(**options)

**Purpose**: Context manager providing detailed artifact capture control

**Parameters**:
- `command_name` (str): Override command name (optional)
- `capture_inputs` (bool): Capture input parameters (default: True)
- `capture_outputs` (bool): Capture command outputs (default: True)
- `capture_environment` (bool): Capture environment variables (default: False)

**Methods Available in Context**:
- `log_input_parameters(params: dict)`: Explicitly log input parameters
- `log_output(data: any)`: Log command output data
- `capture_file(file_path: str, description: str)`: Include file in artifacts
- `add_metadata(key: str, value: any)`: Add custom metadata
- `mark_checkpoint(description: str)`: Mark execution checkpoint

**Example**:
```python
@task("Configure cluster authentication")
def configure_auth(auth_config):
    with artifact_context(capture_environment=True) as ctx:
        ctx.log_input_parameters(auth_config)

        # Execute configuration
        result = apply_auth_config(auth_config)

        ctx.log_output(result)
        ctx.capture_file("/tmp/auth_status.yaml", "Authentication status")
        ctx.mark_checkpoint("Authentication configured successfully")

        return result
```

## Utility Functions

### get_execution_context() -> ExecutionContext

**Purpose**: Get current execution context information

**Returns**: ExecutionContext object with execution metadata

**Properties**:
- `execution_id`: Current execution identifier
- `phase_name`: Current execution phase
- `artifact_directory`: Path to current artifact directory
- `start_time`: Execution start timestamp

### log_structured_data(data: dict, filename: str)

**Purpose**: Log structured data to artifact directory

**Parameters**:
- `data`: Dictionary to log as JSON
- `filename`: Name for the artifact file

### capture_command_output(command: str, **options)

**Purpose**: Execute command with output capture

**Parameters**:
- `command`: Command string to execute
- `timeout`: Command timeout (optional)
- `capture_env`: Include environment in capture (optional)

**Returns**: CommandResult with stdout, stderr, exit_code, and artifact paths

## Error Handling

### Exception Classes

- `ArtifactCaptureError`: Base exception for artifact capture issues
- `TaskDecoratorError`: Invalid decorator usage
- `ContextManagerError`: Context manager usage issues
- `StorageError`: File system storage problems

### Error Recovery

```python
@task("Deploy with error handling", capture_context=True)
def deploy_with_recovery(config):
    try:
        with artifact_context() as ctx:
            result = deploy_service(config)
            ctx.log_output(result)
            return result
    except DeploymentError as e:
        # Error automatically captured in artifacts
        log.error(f"Deployment failed: {e}")
        # Attempt recovery
        return rollback_deployment()
```

## Performance Considerations

### Async Capture

The API supports asynchronous artifact capture to minimize performance impact:

```python
@task("High performance operation", capture_context=True)
async def high_perf_operation(data):
    async with artifact_context() as ctx:
        await ctx.async_log_input_parameters(data)
        result = await perform_operation(data)
        await ctx.async_log_output(result)
        return result
```

### Capture Levels

- `minimal`: Basic input/output logging only
- `standard`: Standard capture with system state
- `verbose`: Full capture including environment and detailed state

## Integration Requirements

### Configuration Schema

The API expects toolbox commands to follow configuration contract validation:

```python
@task("Validated operation", capture_context=True)
@requires_config_fields(['api_endpoint', 'credentials', 'timeout'])
def validated_operation(config):
    # Implementation with validated configuration
    pass
```

### Secret Handling Integration

Automatic integration with CI secrets vault:

```python
@task("Operation with secrets", capture_context=True)
def operation_with_secrets(config):
    with artifact_context() as ctx:
        # Secrets automatically masked in artifacts
        ctx.log_input_parameters(config)  # Secret values redacted
        # Implementation
        pass
```

## Backward Compatibility

The DSL API maintains compatibility with existing toolbox commands through:
- Optional decorator application
- Graceful degradation when artifact capture fails
- Configurable capture levels including 'disabled' mode
- Automatic detection of legacy command patterns