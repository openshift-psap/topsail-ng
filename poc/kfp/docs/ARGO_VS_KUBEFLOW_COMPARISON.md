# Argo Workflows vs Kubeflow Pipelines Comparison

This document tracks differences observed while working with both systems.

---

## UI Behavior

| Feature | Argo Workflows | Kubeflow Pipelines |
|---------|---------------|-------------------|
| **Auto-refresh runs list** | Yes - WebSocket updates in real-time | No - requires manual browser refresh |
| **Run details updates** | Real-time via WebSocket | Polls periodically (more frequent than list) |
| **Log streaming** | Native real-time streaming | Supported but less responsive |

---

## Architecture

| Aspect | Argo Workflows | Kubeflow Pipelines |
|--------|---------------|-------------------|
| **Core engine** | Argo is the workflow engine | KFP v2 uses Argo as backend (or Tekton) |
| **Abstraction level** | Lower-level, more control | Higher-level, ML-focused abstractions |
| **Artifact handling** | Manual S3/GCS config | Built-in artifact store with typed artifacts |
| **SDK** | YAML-first, Go/Python SDKs | Python SDK with decorators (@dsl.component) |

---

## Workflow Definition

| Feature | Argo Workflows | Kubeflow Pipelines |
|---------|---------------|-------------------|
| **Definition format** | YAML (native) | Python SDK compiles to YAML |
| **Parameterization** | Template parameters | Pipeline parameters with types |
| **Conditionals** | `when` expressions | `dsl.If()`, `dsl.Condition()` |
| **Loops** | `withItems`, `withSequence` | `dsl.ParallelFor()` |

---

## Secrets & Config

| Feature | Argo Workflows | Kubeflow Pipelines |
|---------|---------------|-------------------|
| **Secret as volume** | Native - just use `volumes` in pod spec | Requires `kfp.kubernetes` extension, not intuitive |
| **Secret as env var** | Native `envFrom` / `secretKeyRef` | `kubernetes.use_secret_as_env()` helper |
| **ConfigMaps** | Native support | `kubernetes.use_config_map_as_volume()` |
| **Service accounts** | Per-workflow or per-step | Per-component via `set_service_account()` |

**Key Limitation**: KFP v2 doesn't easily support mounting Secrets as volumes like Argo does. You need:
- Install `kfp-kubernetes` extension
- Use `kubernetes.use_secret_as_volume(task, secret_name, mount_path)`
- Cannot use dynamic/parameterized secret names inside conditional blocks

---

## Execution & Scheduling

| Feature | Argo Workflows | Kubeflow Pipelines |
|---------|---------------|-------------------|
| **Cron scheduling** | CronWorkflow CRD | Recurring Run in UI/SDK |
| **Retry policies** | Built-in `retryStrategy` | `set_retry()` on components |
| **Timeout handling** | `activeDeadlineSeconds` | `set_timeout()` on components |
| **Resource limits** | Native pod spec | `set_cpu_limit()`, `set_memory_limit()` |

---

## Observability

| Feature | Argo Workflows | Kubeflow Pipelines |
|---------|---------------|-------------------|
| **Metrics** | Prometheus metrics built-in | Requires additional setup |
| **Experiment tracking** | Not built-in | Native experiments, runs, artifacts |
| **ML metadata** | Not built-in | MLMD integration for lineage |
| **Artifact visualization** | Manual | Built-in viewers for common types |

---

## Notes & Observations

### 2026-03-13: UI Auto-Refresh
- KFP UI runs list does not auto-refresh; user must manually refresh browser
- Argo UI uses WebSockets for real-time updates
- Workaround: Use browser auto-refresh extension or CLI polling

### 2026-03-13: Secret Mounting Complexity
- KFP v2 requires `kfp-kubernetes` extension for secret mounting
- Argo: Simply add `volumes` and `volumeMounts` to pod spec (native K8s)
- KFP: Must use `kubernetes.use_secret_as_volume(task, secret_name, mount_path)`
- KFP limitation: Cannot use pipeline parameters for secret names inside `dsl.If()` blocks

---

## When to Use Which

**Choose Argo Workflows when:**
- Need low-level control over Kubernetes resources
- Non-ML workloads (CI/CD, data processing, infrastructure automation)
- Real-time UI updates are important
- Already have Argo ecosystem (Events, CD, Rollouts)

**Choose Kubeflow Pipelines when:**
- ML-specific workflows (training, serving, evaluation)
- Need experiment tracking and artifact management
- Want Python-native workflow definition
- Integrating with broader Kubeflow ecosystem (Notebooks, Katib, KServe)
