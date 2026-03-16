# Implementation Plan: Resiliency & Auditability for Benchmark Runs

## Principles to Follow

1. **Model key = HuggingFace ID** - Direct mapping for download
2. **Per-model matrix expansion** - workloads × routing × runtime_args
3. **Hierarchical run IDs** - Batch (parent) → Scenario (child)
4. **Deterministic scenario naming** - `{model}_{workload}_{routing}_{tp}`
5. **Failure isolation** - One scenario failure doesn't block others
6. **Artifact collection on failure** - Logs, events, pod describe
7. **KFP + MLflow correlation** - Same IDs across systems
8. **Full auditability** - Config snapshot, git commit, timestamps

---

## Phase 1: Core Data Structures

### Task 1.1: Create BatchRun and ScenarioRun types
- [x] Create `core/batch.py` with BatchRun dataclass
- [x] Create `core/scenario_run.py` with ScenarioRun dataclass
- [x] Add status enum: pending | running | completed | failed | skipped

### Task 1.2: Update Experiment to use HF ID as primary key
- [x] Modify `registry/model_registry.py` - key = hf_model_id
- [x] Remove redundant `hf_model_id` field (key IS the hf_model_id)
- [ ] Update `core/experiment.py` to reflect this (optional - ScenarioRun replaces it)

---

## Phase 2: Per-Model Matrix Expansion

### Task 2.1: Update scenarios.yaml schema
- [x] Document new schema with per-model `matrix:` block
- [x] Support `tensor-parallel-size: [1, 2, 4]` expansion
- [x] Support `workloads: [balanced, short]` expansion
- [x] Support `routing: [direct]` expansion

### Task 2.2: Update ScenarioGenerator
- [x] Modify `scenario_generator/generator.py`
- [x] Iterate over models, expand each model's matrix
- [x] Generate deterministic scenario_id: `{model_short}_{workload}_{routing}_tp{tp}`
- [x] Merge common defaults → model defaults → matrix overrides

---

## Phase 3: Batch Orchestration

### Task 3.1: Create BatchOrchestrator
- [x] Create `orchestration/batch_orchestrator.py`
- [x] Generate batch_id: `batch-{YYYYMMDD}-{HHMMSS}`
- [x] Generate batch_uuid for correlation
- [x] Snapshot full config at batch start
- [x] Record git commit hash

### Task 3.2: Sequential scenario execution with isolation
- [x] Execute scenarios in sequence
- [x] Wrap each scenario in try/except
- [x] On failure: collect artifacts, mark failed, continue
- [x] Track timing: started_at, completed_at, duration_seconds

---

## Phase 4: Failure Handling

### Task 4.1: vLLM startup failure handling
- [x] Create `orchestration/failure_handler.py`
- [x] Wait up to 3600s for vLLM pod ready
- [x] On timeout: collect pod logs, events, describe
- [x] Upload artifacts to S3: `s3://bucket/{batch_id}/{scenario_id}/`
- [x] Mark scenario as failed with reason

### Task 4.2: GuideLLM failure handling
- [x] Timeout after 7200s
- [x] Collect partial results if JSON exists
- [x] Upload whatever artifacts available
- [x] Mark scenario as failed

### Task 4.3: Artifact collection utilities
- [x] `collect_pod_logs(namespace, pod_selector)` → str
- [x] `collect_pod_events(namespace)` → str
- [x] `collect_pod_describe(namespace, pod_name)` → str
- [x] `upload_artifacts(batch_id, scenario_id, artifacts: Dict[str, str])`

---

## Phase 5: KFP + MLflow Correlation (Hybrid Approach)

### Task 5.1: KFP naming convention (Model-centric)
- [x] KFP Experiment = model_id (e.g., `openai-gpt-oss-120b`)
- [x] KFP Run name = `{workload}_{routing}_tp{tp}`
- [x] Pass batch_uuid and scenario_uuid as pipeline params
- [x] Create `model_id_to_kfp_experiment()` helper

### Task 5.2: MLflow correlation (Nested runs)
- [x] MLflow Experiment = `psap-benchmark-runs` (single experiment)
- [x] Parent Run = batch_id (groups scenarios)
- [x] Child Runs = scenario_id (nested under parent)
- [x] Tags: batch_id, batch_uuid, scenario_uuid, model_id
- [x] Create `MLflowNestedRunManager` helper

### Task 5.3: Update benchmark_processor
- [ ] Accept batch_id and scenario_id
- [ ] Tag MLflow runs with correlation IDs
- [ ] Upload artifacts to consistent S3 paths

---

## Phase 6: Auditability

### Task 6.1: Config snapshot
- [ ] Save full scenarios.yaml at batch start
- [ ] Save expanded scenario list (post-matrix expansion)
- [ ] Store in S3: `s3://bucket/{batch_id}/config/`

### Task 6.2: Execution log
- [ ] Create `{batch_id}/execution_log.json`
- [ ] Log each scenario: start time, end time, status, artifacts
- [ ] Update after each scenario completes

### Task 6.3: Summary report
- [ ] Generate batch summary at end
- [ ] Total scenarios, passed, failed, skipped
- [ ] Duration, failure reasons
- [ ] Links to MLflow runs

---

## File Structure

```
poc/kfp/
├── core/
│   ├── batch.py              # NEW: BatchRun dataclass
│   ├── scenario_run.py       # NEW: ScenarioRun dataclass
│   ├── experiment.py         # UPDATED
│   └── types.py              # UPDATED: add Status enum
├── orchestration/            # NEW directory
│   ├── __init__.py
│   ├── batch_orchestrator.py # Main batch execution logic
│   ├── failure_handler.py    # Failure handling utilities
│   └── artifact_collector.py # Log/event collection
├── scenario_generator/
│   └── generator.py          # UPDATED: per-model matrix
├── registry/
│   └── model_registry.py     # UPDATED: key = hf_id
├── config/
│   └── scenarios.yaml        # UPDATED: new schema
└── scenario_runner.py        # UPDATED: use BatchOrchestrator
```

---

## Acceptance Criteria

- [ ] Can run 24-scenario matrix with single command
- [ ] If scenario 5 fails, scenarios 6-24 still execute
- [ ] Failed scenarios have logs in S3
- [ ] Can find all scenarios from a batch in KFP UI
- [ ] Can find all scenarios from a batch in MLflow
- [ ] Can reproduce exact run from config snapshot
- [ ] Execution log shows timing and status for each scenario
