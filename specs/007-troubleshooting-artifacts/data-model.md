# Data Model: Troubleshooting and Artifact Management

**Date**: 2026-02-25
**Feature**: Troubleshooting and Artifact Management

## Core Entities

### Execution Artifact Directory

**Purpose**: Organized storage structure for all artifacts generated during a specific test execution

**Attributes**:
- `execution_id`: Unique identifier for test execution session
- `start_timestamp`: ISO timestamp when execution began
- `end_timestamp`: ISO timestamp when execution completed
- `execution_status`: Status (running, completed, failed, aborted)
- `directory_path`: File system path to artifact root
- `total_size`: Total storage size of all artifacts
- `phase_count`: Number of execution phases captured
- `metadata`: Dictionary of execution-level metadata

**Relationships**:
- Contains multiple `Orchestration Phase Artifact`
- Links to `Execution Timeline` for chronological ordering
- References multiple `Toolbox Command Context` entries

**Validation Rules**:
- `execution_id` must be unique across all executions
- `directory_path` must follow naming convention: `YYYYMMDD_HHMMSS_{execution_id}`
- `start_timestamp` must precede `end_timestamp`
- `total_size` must not exceed system storage limits

### Toolbox Command Context

**Purpose**: Complete execution context for individual toolbox commands

**Attributes**:
- `command_id`: Unique identifier within execution
- `command_name`: Descriptive name from Python DSL
- `sequence_number`: Execution order within phase
- `input_parameters`: Serialized command inputs
- `start_time`: Command start timestamp
- `end_time`: Command completion timestamp
- `exit_code`: Command exit status
- `stdout_path`: Path to stdout capture file
- `stderr_path`: Path to stderr capture file
- `source_files_path`: Directory containing source configurations
- `system_state_path`: Directory with cluster state captures

**Relationships**:
- Belongs to one `Orchestration Phase Artifact`
- Contains one `System State Capture`
- Links to `Execution Timeline` for chronological positioning

**Validation Rules**:
- `command_name` must be non-empty and descriptive
- `sequence_number` must be unique within phase
- All file paths must exist and be readable
- `exit_code` must be valid system exit code

### Orchestration Phase Artifact

**Purpose**: Artifact collection for specific orchestration phases

**Attributes**:
- `phase_id`: Unique identifier within execution
- `phase_name`: Standard phase name (pre_cleanup, prepare, test, post_cleanup)
- `phase_number`: Execution sequence (0001, 0002, etc.)
- `start_timestamp`: Phase start time
- `end_timestamp`: Phase completion time
- `status`: Phase execution status
- `command_count`: Number of commands executed in phase
- `directory_path`: Phase-specific artifact directory

**Relationships**:
- Belongs to one `Execution Artifact Directory`
- Contains multiple `Toolbox Command Context` entries
- Contributes to `Execution Timeline`

**Validation Rules**:
- `phase_name` must be one of: pre_cleanup, prepare, test, post_cleanup
- `phase_number` must follow format: nnnn (e.g., 0001, 0002)
- `directory_path` must follow naming: `{phase_number}_{phase_name}/`
- Phase order must be chronologically consistent

### System State Capture

**Purpose**: Snapshots of cluster and system state at specific execution points

**Attributes**:
- `capture_id`: Unique identifier for state snapshot
- `capture_timestamp`: When snapshot was taken
- `capture_type`: Type of state capture (pre_command, post_command, error_state)
- `cluster_resources`: Serialized cluster resource state
- `system_metrics`: System performance metrics at capture time
- `configuration_state`: Active configuration values
- `error_context`: Error information if applicable
- `file_paths`: List of files containing detailed state data

**Relationships**:
- Belongs to one `Toolbox Command Context`
- References `Execution Timeline` for temporal context

**Validation Rules**:
- `capture_timestamp` must be within command execution timeframe
- `capture_type` must be valid enumerated value
- State data must be serializable and reconstructible
- File paths must be relative to command context directory

### Execution Timeline

**Purpose**: Chronological sequence of operations and artifacts

**Attributes**:
- `timeline_id`: Unique identifier for execution timeline
- `execution_start`: Overall execution start timestamp
- `execution_end`: Overall execution end timestamp
- `total_duration`: Calculated execution duration
- `event_count`: Total number of timeline events
- `events`: Ordered list of timeline events

**Timeline Event Structure**:
- `timestamp`: Event occurrence time
- `event_type`: Type (phase_start, phase_end, command_start, command_end, error)
- `entity_id`: Related entity identifier
- `description`: Human-readable event description
- `metadata`: Event-specific additional data

**Relationships**:
- Links to one `Execution Artifact Directory`
- References multiple `Orchestration Phase Artifact` entries
- Contains timeline events for all `Toolbox Command Context` entries

**Validation Rules**:
- Events must be chronologically ordered
- Timeline must span complete execution duration
- All entity references must be valid
- No timeline gaps during active execution

## State Transitions

### Execution Artifact Directory States
1. `initializing` → `running` → `completed` | `failed` | `aborted`

### Toolbox Command Context States
1. `pending` → `running` → `completed` | `failed`

### System State Capture States
1. `scheduled` → `capturing` → `captured` | `failed`

## Storage Schema

### Directory Structure
```
artifacts/
└── YYYYMMDD_HHMMSS_{execution_id}/
    ├── execution_metadata.json
    ├── timeline.json
    ├── 0001_pre_cleanup/
    │   ├── phase_metadata.json
    │   └── 0001_{command_name}/
    │       ├── input_parameters.json
    │       ├── stdout.log
    │       ├── stderr.log
    │       ├── sources/
    │       └── system_state/
    ├── 0002_prepare/
    ├── 0003_test/
    └── 0004_post_cleanup/
```

### File Formats
- **Metadata files**: JSON format for structured data
- **Log files**: Plain text with UTF-8 encoding
- **Configuration files**: Preserve original format (YAML, JSON, etc.)
- **State captures**: JSON format with optional compression for large datasets

## Data Integrity

### Validation Rules
- All JSON files must be well-formed and validate against schemas
- File paths must be relative and not contain directory traversal attacks
- Timestamps must use ISO 8601 format with timezone information
- Binary data must be base64 encoded in JSON structures

### Consistency Checks
- Timeline events must reference existing entities
- Directory structure must match metadata declarations
- File sizes must not exceed configured limits
- All referenced files must exist and be readable

### Error Handling
- Partial artifact capture is acceptable for investigation purposes
- Missing files should be noted in metadata without failing entire capture
- Corrupted files should be quarantined but not deleted
- Recovery procedures should attempt to reconstruct missing metadata