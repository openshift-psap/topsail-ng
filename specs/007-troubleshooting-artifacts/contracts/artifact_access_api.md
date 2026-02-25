# Artifact Access API Contract

**Interface**: Programmatic Artifact Access
**Version**: 1.0.0
**Audience**: Automation tools, investigation scripts, CI integrations

## Overview

The Artifact Access API provides programmatic access to execution artifacts for automated analysis, CI integration, and custom investigation tools. Supports both REST API and Python library interfaces.

## REST API Endpoints

### Authentication

All API endpoints require authentication via API key or session token.

**Header**: `Authorization: Bearer <token>` or `X-API-Key: <key>`

### Execution Management

#### GET /api/v1/executions

**Purpose**: List execution artifacts with filtering and pagination

**Query Parameters**:
- `limit` (int): Results per page (1-1000, default: 50)
- `offset` (int): Pagination offset (default: 0)
- `status` (str): Filter by status ('running', 'completed', 'failed', 'aborted')
- `date_from` (str): ISO 8601 date filter start
- `date_to` (str): ISO 8601 date filter end
- `phase` (str): Filter executions containing specific phase
- `search` (str): Text search across execution metadata

**Response**:
```json
{
  "executions": [
    {
      "execution_id": "20240225_143022_perf_test_001",
      "start_timestamp": "2024-02-25T14:30:22.123Z",
      "end_timestamp": "2024-02-25T15:45:18.456Z",
      "status": "completed",
      "phases": ["pre_cleanup", "prepare", "test", "post_cleanup"],
      "total_size_bytes": 150984732,
      "command_count": 23,
      "metadata": {
        "test_type": "performance",
        "cluster_size": "large",
        "ai_workload": "inference"
      }
    }
  ],
  "pagination": {
    "total_count": 1247,
    "limit": 50,
    "offset": 0,
    "has_next": true
  }
}
```

#### GET /api/v1/executions/{execution_id}

**Purpose**: Get detailed execution information

**Response**:
```json
{
  "execution_id": "20240225_143022_perf_test_001",
  "start_timestamp": "2024-02-25T14:30:22.123Z",
  "end_timestamp": "2024-02-25T15:45:18.456Z",
  "status": "completed",
  "total_duration_seconds": 4556,
  "artifact_directory": "/artifacts/20240225_143022_perf_test_001",
  "phases": [
    {
      "phase_id": "0001",
      "phase_name": "pre_cleanup",
      "start_timestamp": "2024-02-25T14:30:22.123Z",
      "end_timestamp": "2024-02-25T14:32:15.789Z",
      "duration_seconds": 113,
      "command_count": 3,
      "status": "completed"
    }
  ],
  "summary_stats": {
    "total_commands": 23,
    "failed_commands": 0,
    "artifacts_captured": 156,
    "total_size_bytes": 150984732
  },
  "metadata": {}
}
```

### Artifact Access

#### GET /api/v1/executions/{execution_id}/artifacts

**Purpose**: List artifacts for execution with filtering

**Query Parameters**:
- `phase` (str): Filter by phase name
- `command` (str): Filter by command name
- `type` (str): Filter by artifact type ('log', 'config', 'state', 'metadata', 'source')
- `size_min` (int): Minimum file size in bytes
- `size_max` (int): Maximum file size in bytes

**Response**:
```json
{
  "artifacts": [
    {
      "artifact_id": "0002_prepare/0001_deploy_operator/stdout.log",
      "artifact_type": "log",
      "file_size_bytes": 45123,
      "created_timestamp": "2024-02-25T14:35:42.567Z",
      "phase_name": "prepare",
      "command_name": "deploy_operator",
      "description": "Command stdout output",
      "content_type": "text/plain",
      "download_url": "/api/v1/artifacts/download/...",
      "preview_available": true
    }
  ]
}
```

#### GET /api/v1/executions/{execution_id}/artifacts/{artifact_path}

**Purpose**: Download specific artifact file

**Response**: File content with appropriate content-type headers

**Headers**:
- `Content-Type`: MIME type of artifact
- `Content-Disposition`: Filename for download
- `Content-Length`: File size in bytes

#### GET /api/v1/executions/{execution_id}/artifacts/{artifact_path}/preview

**Purpose**: Get text preview of artifact (first 10KB for text files)

**Response**:
```json
{
  "preview": "First 10KB of file content...",
  "is_truncated": true,
  "total_size_bytes": 45123,
  "content_type": "text/plain"
}
```

### Timeline and Context

#### GET /api/v1/executions/{execution_id}/timeline

**Purpose**: Get complete execution timeline

**Query Parameters**:
- `phase` (str): Filter events by phase
- `event_type` (str): Filter by event type
- `time_from` (str): ISO timestamp filter start
- `time_to` (str): ISO timestamp filter end

**Response**:
```json
{
  "timeline": [
    {
      "timestamp": "2024-02-25T14:30:22.123Z",
      "event_type": "execution_start",
      "entity_id": "20240225_143022_perf_test_001",
      "description": "Test execution started",
      "duration_ms": null,
      "metadata": {}
    },
    {
      "timestamp": "2024-02-25T14:35:42.567Z",
      "event_type": "command_start",
      "entity_id": "0002_prepare/0001_deploy_operator",
      "description": "Deploy OpenShift AI operator",
      "duration_ms": 45230,
      "metadata": {
        "input_parameters": {...},
        "expected_duration_ms": 60000
      }
    }
  ]
}
```

#### GET /api/v1/executions/{execution_id}/commands/{command_id}

**Purpose**: Get detailed command execution context

**Response**:
```json
{
  "command_id": "0002_prepare/0001_deploy_operator",
  "command_name": "Deploy OpenShift AI operator",
  "sequence_number": 1,
  "phase_name": "prepare",
  "start_timestamp": "2024-02-25T14:35:42.567Z",
  "end_timestamp": "2024-02-25T14:36:27.797Z",
  "duration_seconds": 45,
  "exit_code": 0,
  "input_parameters": {
    "operator_version": "v1.2.3",
    "namespace": "openshift-ai",
    "timeout": 300
  },
  "artifacts": {
    "stdout": "0002_prepare/0001_deploy_operator/stdout.log",
    "stderr": "0002_prepare/0001_deploy_operator/stderr.log",
    "input_params": "0002_prepare/0001_deploy_operator/input_parameters.json",
    "sources": "0002_prepare/0001_deploy_operator/sources/",
    "system_state": "0002_prepare/0001_deploy_operator/system_state/"
  },
  "system_state_captures": [
    {
      "capture_timestamp": "2024-02-25T14:36:30.000Z",
      "capture_type": "post_command",
      "state_files": ["pods.json", "services.json", "deployments.json"]
    }
  ]
}
```

## Python Library Interface

### Installation
```bash
pip install topsail-artifacts
```

### Basic Usage

```python
from topsail_artifacts import ArtifactClient

# Initialize client
client = ArtifactClient(
    base_url="https://artifacts.example.com",
    api_key="your-api-key"
)

# List executions
executions = client.list_executions(
    status="completed",
    date_from="2024-02-20",
    limit=10
)

# Get execution details
execution = client.get_execution("20240225_143022_perf_test_001")
print(f"Execution took {execution.duration_seconds} seconds")

# Access artifacts
artifacts = client.list_artifacts(
    execution.execution_id,
    artifact_type="log"
)

# Download artifact
content = client.download_artifact(
    execution.execution_id,
    "0002_prepare/0001_deploy_operator/stdout.log"
)

# Get timeline
timeline = client.get_timeline(execution.execution_id)
for event in timeline:
    print(f"{event.timestamp}: {event.description}")
```

### Advanced Features

```python
# Stream large artifacts
with client.stream_artifact(execution_id, artifact_path) as stream:
    for chunk in stream:
        process_chunk(chunk)

# Search across executions
results = client.search_artifacts(
    query="error AND deployment",
    execution_filter={"status": "failed"},
    time_range={"hours": 24}
)

# Batch operations
with client.batch_mode():
    for execution_id in execution_ids:
        artifacts = client.list_artifacts(execution_id)
        # Operations batched for efficiency
```

### Exception Handling

```python
from topsail_artifacts.exceptions import (
    ExecutionNotFound,
    ArtifactNotFound,
    AccessDenied,
    APIError
)

try:
    execution = client.get_execution("invalid_id")
except ExecutionNotFound as e:
    print(f"Execution not found: {e.execution_id}")
except AccessDenied as e:
    print(f"Access denied: {e.message}")
except APIError as e:
    print(f"API error {e.status_code}: {e.message}")
```

## Webhook Integration

### Event Notifications

Register webhooks to receive notifications about artifact events:

#### POST /api/v1/webhooks

**Request**:
```json
{
  "url": "https://your-service.com/webhook",
  "events": ["execution.completed", "execution.failed"],
  "secret": "webhook-secret-for-verification",
  "active": true
}
```

**Webhook Payload**:
```json
{
  "event": "execution.completed",
  "timestamp": "2024-02-25T15:45:18.456Z",
  "data": {
    "execution_id": "20240225_143022_perf_test_001",
    "status": "completed",
    "duration_seconds": 4556,
    "artifact_count": 156
  },
  "signature": "sha256=..."
}
```

## Rate Limiting

- **API Limits**: 1000 requests per hour per API key
- **Download Limits**: 100GB per hour per API key
- **Concurrent Downloads**: 10 simultaneous per API key
- **Search Limits**: 100 search requests per hour per API key

## Data Retention

- **Active Executions**: Retained indefinitely
- **Completed Executions**: Configurable retention (default 90 days)
- **Failed Executions**: Extended retention (default 180 days)
- **Large Artifacts**: May be archived to cold storage after 30 days

## Security

### API Key Management

- API keys scoped to specific execution patterns
- Key rotation supported with overlap periods
- Audit logging for all API access

### Data Privacy

- Sensitive data automatically redacted in API responses
- Configurable field masking for compliance
- Encryption in transit and at rest

### Access Control

- Role-based access to execution artifacts
- Team-based isolation for sensitive executions
- Audit trail for all artifact access