# Quickstart: Troubleshooting and Artifact Management

**Feature**: Troubleshooting and Artifact Management
**Date**: 2026-02-25
**Implementation Phase**: Phase 4 (Test Harness Application Layer)

## Overview

This quickstart guide covers implementing and using the troubleshooting and artifact management system that provides comprehensive post-mortem debugging capabilities for the TOPSAIL-NG test harness.

## Quick Start for Developers

### 1. Instrumenting Toolbox Commands

Transform existing toolbox commands to include artifact capture:

**Before** (basic command):
```python
def deploy_operator(config):
    result = kubectl_apply(config.manifest_path)
    return result
```

**After** (instrumented command):
```python
from topsail_artifacts import task, artifact_context

@task("Deploy OpenShift AI operator", capture_context=True)
def deploy_operator(config):
    with artifact_context() as ctx:
        ctx.log_input_parameters({
            'operator_version': config.version,
            'namespace': config.namespace,
            'manifest_path': config.manifest_path
        })

        result = kubectl_apply(config.manifest_path)
        ctx.log_output(result)
        ctx.capture_file(config.manifest_path, "Operator manifest")

        return result
```

### 2. Automatic Directory Organization

The orchestration layer automatically organizes artifacts:

```python
# Orchestration automatically creates structure:
artifacts/
└── 20240225_143022_perf_test_001/
    ├── 0001_pre_cleanup/
    ├── 0002_prepare/
    │   └── 0001_deploy_operator/
    │       ├── input_parameters.json
    │       ├── stdout.log
    │       ├── stderr.log
    │       ├── sources/
    │       │   └── operator-manifest.yaml
    │       └── system_state/
    │           ├── pods.json
    │           └── services.json
    ├── 0003_test/
    └── 0004_post_cleanup/
```

### 3. Investigation Dashboard Access

Access the investigation dashboard:

```bash
# Start the dashboard server
python -m topsail_artifacts.dashboard --port 8050

# Navigate to execution
http://localhost:8050/dashboard/20240225_143022_perf_test_001
```

## Quick Start for Investigators

### 1. Finding Failed Executions

```python
from topsail_artifacts import ArtifactClient

client = ArtifactClient(api_key="your-key")

# Find recent failures
failed_executions = client.list_executions(
    status="failed",
    date_from="2024-02-24",
    limit=10
)

for execution in failed_executions:
    print(f"Failed execution: {execution.execution_id}")
    print(f"Duration: {execution.duration_seconds}s")
    print(f"Command count: {execution.command_count}")
```

### 2. Analyzing Execution Timeline

```python
# Get timeline for failed execution
execution_id = "20240225_143022_perf_test_001"
timeline = client.get_timeline(execution_id)

# Find failure points
for event in timeline:
    if event.event_type == "command_end" and "failed" in event.metadata:
        print(f"Failed command: {event.description}")
        print(f"At: {event.timestamp}")
        print(f"Duration: {event.duration_ms}ms")
```

### 3. Examining Command Context

```python
# Get detailed command information
command_id = "0003_test/0005_performance_test"
command = client.get_command(execution_id, command_id)

print(f"Command: {command.command_name}")
print(f"Exit code: {command.exit_code}")
print(f"Input parameters: {command.input_parameters}")

# Download logs
stdout = client.download_artifact(execution_id, command.artifacts['stdout'])
stderr = client.download_artifact(execution_id, command.artifacts['stderr'])

print("STDOUT:", stdout.decode())
print("STDERR:", stderr.decode())
```

## Implementation Guide

### Phase 1: Core Infrastructure (Weeks 1-2)

#### 1. Artifact Capture Framework

**File**: `src/common/artifact_capture.py`

```python
import asyncio
import json
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

class ArtifactCaptureManager:
    def __init__(self, base_path: Path):
        self.base_path = base_path
        self.current_execution = None

    async def start_execution(self, execution_id: str):
        self.current_execution = {
            'id': execution_id,
            'start_time': datetime.utcnow(),
            'phases': []
        }
        execution_path = self.base_path / execution_id
        execution_path.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def capture_phase(self, phase_name: str):
        phase_id = f"{len(self.current_execution['phases']):04d}_{phase_name}"
        phase_path = self.base_path / self.current_execution['id'] / phase_id
        phase_path.mkdir(exist_ok=True)

        yield PhaseCapture(phase_path, phase_name)
```

#### 2. Python DSL Implementation

**File**: `src/toolbox/dsl/python_dsl.py`

```python
from functools import wraps
import inspect
import json
from typing import Any, Dict

def task(name: str, capture_context: bool = True, **options):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if capture_context:
                async with artifact_context(name) as ctx:
                    # Capture input parameters
                    sig = inspect.signature(func)
                    bound_args = sig.bind(*args, **kwargs)
                    await ctx.log_input_parameters(dict(bound_args.arguments))

                    # Execute function
                    result = await func(*args, **kwargs)

                    # Capture output
                    await ctx.log_output(result)
                    return result
            else:
                return await func(*args, **kwargs)

        wrapper._task_name = name
        wrapper._task_options = options
        return wrapper
    return decorator
```

### Phase 2: Dashboard Implementation (Weeks 3-4)

#### 1. Dash Application Structure

**File**: `src/post_processing/investigation_dashboard.py`

```python
import dash
from dash import dcc, html, Input, Output
import plotly.graph_objects as go
import plotly.express as px

class InvestigationDashboard:
    def __init__(self, artifact_client):
        self.app = dash.Dash(__name__)
        self.client = artifact_client
        self.setup_layout()
        self.setup_callbacks()

    def setup_layout(self):
        self.app.layout = html.Div([
            dcc.Location(id='url', refresh=False),
            html.Div(id='page-content')
        ])

    def setup_callbacks(self):
        @self.app.callback(
            Output('timeline-chart', 'figure'),
            [Input('execution-dropdown', 'value')]
        )
        def update_timeline(execution_id):
            timeline = self.client.get_timeline(execution_id)
            return self.create_timeline_chart(timeline)

    def create_timeline_chart(self, timeline):
        fig = go.Figure()

        for event in timeline:
            fig.add_trace(go.Scatter(
                x=[event.timestamp],
                y=[event.event_type],
                mode='markers',
                name=event.description,
                marker=dict(
                    size=10,
                    color='red' if 'error' in event.event_type else 'blue'
                )
            ))

        fig.update_layout(
            title="Execution Timeline",
            xaxis_title="Time",
            yaxis_title="Event Type"
        )

        return fig
```

#### 2. Timeline Visualization

**File**: `src/post_processing/timeline_reconstruction.py`

```python
import plotly.graph_objects as go
from plotly.subplots import make_subplots

class TimelineReconstructor:
    def __init__(self, client):
        self.client = client

    def create_gantt_chart(self, execution_id: str):
        timeline = self.client.get_timeline(execution_id)
        execution = self.client.get_execution(execution_id)

        fig = make_subplots(
            rows=len(execution.phases),
            cols=1,
            subplot_titles=[f"Phase: {p.phase_name}" for p in execution.phases]
        )

        for i, phase in enumerate(execution.phases, 1):
            phase_events = [e for e in timeline if e.entity_id.startswith(phase.phase_id)]

            fig.add_trace(
                go.Bar(
                    x=[event.duration_ms for event in phase_events],
                    y=[event.description for event in phase_events],
                    orientation='h',
                    name=phase.phase_name
                ),
                row=i, col=1
            )

        return fig
```

### Phase 3: Integration Testing (Week 5)

#### 1. End-to-End Test

**File**: `tests/integration/test_end_to_end_capture.py`

```python
import pytest
from topsail_artifacts import ArtifactCaptureManager, task

@pytest.mark.asyncio
async def test_complete_workflow():
    # Setup
    capture_manager = ArtifactCaptureManager(Path("/tmp/test_artifacts"))
    execution_id = "test_execution_001"

    await capture_manager.start_execution(execution_id)

    # Simulate toolbox command with artifact capture
    @task("Test command", capture_context=True)
    async def test_command(param1: str, param2: int):
        return {"result": "success", "processed": param2 * 2}

    # Execute command
    result = await test_command("test_value", 42)

    # Verify artifacts
    execution_path = Path(f"/tmp/test_artifacts/{execution_id}")
    assert execution_path.exists()

    # Check phase directory structure
    phase_dirs = list(execution_path.glob("*_test_phase"))
    assert len(phase_dirs) == 1

    # Verify command artifacts
    command_dir = phase_dirs[0] / "0001_test_command"
    assert (command_dir / "input_parameters.json").exists()
    assert (command_dir / "stdout.log").exists()

    # Verify captured data
    with open(command_dir / "input_parameters.json") as f:
        params = json.load(f)
        assert params["param1"] == "test_value"
        assert params["param2"] == 42
```

## Configuration

### Artifact Capture Settings

**File**: `config/artifact_settings.yaml`

```yaml
artifact_capture:
  enabled: true
  storage:
    base_path: "/var/lib/topsail/artifacts"
    max_size_gb: 500
    retention_days: 90
    compression: true

  capture_levels:
    minimal:
      inputs: true
      outputs: true
      logs: false
      system_state: false
    standard:
      inputs: true
      outputs: true
      logs: true
      system_state: true
    verbose:
      inputs: true
      outputs: true
      logs: true
      system_state: true
      environment: true
      source_files: true

dashboard:
  enabled: true
  port: 8050
  auth_required: true
  max_timeline_events: 10000
  auto_refresh_seconds: 30

api:
  rate_limits:
    requests_per_hour: 1000
    downloads_per_hour_gb: 100
  authentication:
    api_key_required: true
    session_timeout_minutes: 60
```

### CI Integration

**File**: `.github/workflows/artifact-analysis.yml`

```yaml
name: Artifact Analysis
on:
  workflow_run:
    workflows: ["Performance Tests"]
    types: [completed]

jobs:
  analyze:
    runs-on: ubuntu-latest
    if: ${{ github.event.workflow_run.conclusion == 'failure' }}
    steps:
      - name: Analyze Failed Execution
        run: |
          python scripts/auto_investigate.py \
            --execution-id ${{ github.event.workflow_run.id }} \
            --generate-report \
            --notify-teams
```

## Troubleshooting

### Common Issues

**Issue**: Artifact capture consuming too much storage
**Solution**:
```bash
# Check storage usage
du -sh /var/lib/topsail/artifacts/

# Clean old artifacts
python -m topsail_artifacts.cleanup --older-than 30d

# Adjust retention settings in config
```

**Issue**: Dashboard not showing recent executions
**Solution**:
```bash
# Restart dashboard service
systemctl restart topsail-dashboard

# Check artifact indexing
python -m topsail_artifacts.reindex
```

**Issue**: Performance impact during test execution
**Solution**:
```yaml
# Use minimal capture for performance-critical tests
artifact_capture:
  default_level: minimal
  async_capture: true
  buffer_size_mb: 100
```

## Next Steps

1. **Phase 4**: Implement advanced analysis features (performance regression detection, pattern recognition)
2. **Phase 5**: Add machine learning-based failure prediction
3. **Phase 6**: Integrate with external monitoring and alerting systems

## Resources

- **API Documentation**: [contracts/artifact_access_api.md](contracts/artifact_access_api.md)
- **Dashboard Guide**: [contracts/investigation_dashboard_api.md](contracts/investigation_dashboard_api.md)
- **Python DSL Reference**: [contracts/python_dsl_api.md](contracts/python_dsl_api.md)
- **Data Model**: [data-model.md](data-model.md)
- **Research**: [research.md](research.md)