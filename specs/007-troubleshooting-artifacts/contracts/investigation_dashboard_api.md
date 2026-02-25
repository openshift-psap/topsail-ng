# Investigation Dashboard API Contract

**Interface**: Dash-based Investigation Dashboard
**Version**: 1.0.0
**Audience**: PSAP team investigators, dashboard integrators

## Overview

The Investigation Dashboard provides a web-based interface for exploring execution artifacts, reconstructing timelines, and conducting post-mortem analysis. Built with Dash and Plotly, it offers interactive visualization and filtering capabilities.

## Dashboard Endpoints

### GET /dashboard/{execution_id}

**Purpose**: Load main investigation dashboard for specific execution

**Parameters**:
- `execution_id` (str): Target execution identifier

**Returns**: HTML dashboard page with embedded Plotly components

**Query Parameters**:
- `phase` (str): Focus on specific phase (optional)
- `command` (str): Highlight specific command (optional)
- `view` (str): Dashboard view type ('timeline', 'artifacts', 'comparison') (default: 'timeline')

### GET /api/executions

**Purpose**: List available executions for investigation

**Query Parameters**:
- `limit` (int): Maximum results (default: 50)
- `offset` (int): Pagination offset (default: 0)
- `status` (str): Filter by execution status (optional)
- `date_from` (str): ISO date filter start (optional)
- `date_to` (str): ISO date filter end (optional)

**Response Schema**:
```json
{
  "executions": [
    {
      "execution_id": "string",
      "start_timestamp": "ISO datetime",
      "end_timestamp": "ISO datetime",
      "status": "completed|failed|running",
      "phase_count": "integer",
      "total_size": "integer",
      "metadata": {}
    }
  ],
  "total_count": "integer",
  "has_more": "boolean"
}
```

### GET /api/execution/{execution_id}/timeline

**Purpose**: Get execution timeline data for visualization

**Response Schema**:
```json
{
  "execution_id": "string",
  "timeline": [
    {
      "timestamp": "ISO datetime",
      "event_type": "phase_start|phase_end|command_start|command_end|error",
      "entity_id": "string",
      "description": "string",
      "duration": "integer (milliseconds)",
      "metadata": {}
    }
  ],
  "total_duration": "integer (milliseconds)",
  "phases": [
    {
      "phase_id": "string",
      "phase_name": "string",
      "start_time": "ISO datetime",
      "end_time": "ISO datetime",
      "command_count": "integer",
      "status": "string"
    }
  ]
}
```

### GET /api/execution/{execution_id}/artifacts

**Purpose**: List artifacts for specific execution with filtering

**Query Parameters**:
- `phase` (str): Filter by phase name (optional)
- `command` (str): Filter by command name (optional)
- `artifact_type` (str): Filter by type ('log', 'config', 'state', 'metadata') (optional)

**Response Schema**:
```json
{
  "artifacts": [
    {
      "artifact_id": "string",
      "artifact_type": "log|config|state|metadata",
      "file_path": "string",
      "file_size": "integer",
      "created_timestamp": "ISO datetime",
      "phase_name": "string",
      "command_name": "string",
      "description": "string"
    }
  ]
}
```

## Dashboard Components

### Timeline Visualization Component

**Component**: `ExecutionTimelineChart`

**Props**:
- `execution_id` (str): Target execution
- `highlight_phase` (str): Phase to highlight (optional)
- `highlight_command` (str): Command to highlight (optional)
- `zoom_range` (tuple): Time range to zoom (optional)

**Features**:
- Interactive timeline with phase and command markers
- Hover details showing execution context
- Click navigation to detailed artifact views
- Zoom and pan functionality
- Error highlighting with red markers

### Artifact Explorer Component

**Component**: `ArtifactExplorer`

**Props**:
- `execution_id` (str): Target execution
- `initial_filter` (dict): Initial filter state (optional)

**Features**:
- Hierarchical artifact browser
- File content preview for text artifacts
- Download functionality for artifact files
- Search and filter capabilities
- Metadata display in sidebar

### Comparison Dashboard Component

**Component**: `ExecutionComparison`

**Props**:
- `execution_ids` (list): List of executions to compare
- `comparison_mode` (str): 'timeline', 'performance', 'artifacts'

**Features**:
- Side-by-side timeline comparison
- Performance metric comparison charts
- Diff view for configuration artifacts
- Export functionality for comparison reports

## Interactive Features

### Real-time Updates

Dashboard supports WebSocket connections for real-time updates during ongoing executions:

**WebSocket Endpoint**: `/ws/execution/{execution_id}`

**Message Format**:
```json
{
  "type": "timeline_update|artifact_added|status_change",
  "timestamp": "ISO datetime",
  "data": {
    // Event-specific payload
  }
}
```

### Filtering and Search

**Advanced Filter API**: `POST /api/filter`

**Request Schema**:
```json
{
  "execution_ids": ["string"],
  "time_range": {
    "start": "ISO datetime",
    "end": "ISO datetime"
  },
  "phases": ["string"],
  "commands": ["string"],
  "status_codes": ["integer"],
  "text_search": "string",
  "artifact_types": ["string"]
}
```

### Export Functionality

**Export API**: `POST /api/export`

**Request Schema**:
```json
{
  "execution_id": "string",
  "export_format": "pdf|json|csv",
  "include_artifacts": "boolean",
  "filter": {
    // Filter criteria
  }
}
```

**Returns**: File download or URL to generated export

## Configuration Integration

### Dashboard Configuration Schema

```json
{
  "dashboard_settings": {
    "default_view": "timeline",
    "auto_refresh_interval": 30,
    "max_timeline_events": 10000,
    "artifact_preview_size_limit": "1MB",
    "export_size_limit": "100MB"
  },
  "visualization_settings": {
    "timeline_colors": {
      "phase_start": "#2E86AB",
      "command_start": "#A23B72",
      "error": "#F18F01",
      "success": "#C73E1D"
    },
    "chart_height": 600,
    "enable_animations": true
  }
}
```

### Authentication Integration

Dashboard integrates with existing authentication:
- Session-based authentication for web interface
- API key authentication for programmatic access
- Role-based access control for sensitive executions

## Performance Optimizations

### Lazy Loading

- Artifact content loaded on-demand
- Timeline data paginated for large executions
- Background prefetching for common navigation patterns

### Caching Strategy

- Execution metadata cached with 5-minute TTL
- Artifact lists cached until execution completes
- Timeline data cached permanently after execution completion

### Data Streaming

Large artifact files streamed rather than loaded in memory:
- Chunked reading for log files
- Progressive loading for visualization data
- Configurable memory limits

## Error Handling

### Error Response Format

```json
{
  "error": {
    "code": "ARTIFACT_NOT_FOUND",
    "message": "Requested artifact does not exist",
    "details": {
      "execution_id": "string",
      "artifact_path": "string"
    },
    "timestamp": "ISO datetime"
  }
}
```

### Common Error Codes

- `EXECUTION_NOT_FOUND`: Requested execution does not exist
- `ARTIFACT_NOT_FOUND`: Specific artifact file missing
- `ACCESS_DENIED`: Insufficient permissions
- `EXPORT_TOO_LARGE`: Export request exceeds size limits
- `TIMELINE_CORRUPTED`: Timeline data inconsistent

### Graceful Degradation

- Partial timeline display when some data missing
- Placeholder content for unavailable artifacts
- Alternative visualization modes for corrupted data
- Offline mode with cached data when server unavailable

## Integration Points

### CI Pipeline Integration

Dashboard provides webhook endpoints for CI notification:
- `POST /api/webhooks/execution_started`
- `POST /api/webhooks/execution_completed`
- `POST /api/webhooks/execution_failed`

### Alert Integration

Dashboard can trigger alerts based on execution patterns:
- Performance regression detection
- Error pattern recognition
- Resource usage anomaly detection
- Automated stakeholder notification