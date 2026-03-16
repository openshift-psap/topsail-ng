# RHOAI Benchmark Pipeline

Batch orchestration for LLM inference benchmarking using Kubeflow Pipelines (KFP), Kueue GPU scheduling, and MLflow tracking.

## Quick Start

```bash
# 1. Setup (one-time)
cd /Users/memehta/workspace/topsail-ng/poc/kfp
pip install kfp pyyaml
python benchmark_api.py setup  # Creates kubeconfig secrets

# 2. List scenarios
python scenario_runner.py list -s config/scenarios.yaml

# 3. Run benchmarks
python scenario_runner.py run -s config/scenarios.yaml

# 4. Check status
python scenario_runner.py status --runs <run-id-1>,<run-id-2>
```

## Architecture Overview

```
+------------------+     +------------------+     +------------------+
|  scenarios.yaml  | --> | BatchOrchestrator| --> | KFP Pipeline     |
|  (matrix config) |     | (expand+submit)  |     | (target cluster) |
+------------------+     +------------------+     +------------------+
                                                          |
                                                          v
                                                  +------------------+
                                                  | AWS MLflow       |
                                                  | (results)        |
                                                  +------------------+
```

**Key concepts:**
- **Batch**: Parent container for a benchmark run (e.g., `batch-20260315-143022`)
- **Scenario**: Single benchmark config (e.g., `qwen3-0-6b_balanced_direct_tp1`)
- **Matrix expansion**: `models x workloads x routing x tensor-parallel-size`

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed design documentation.

## CLI Reference

### scenario_runner.py (Primary CLI)

```bash
# Run all scenarios from config
python scenario_runner.py run -s config/scenarios.yaml

# Dry run (prints what would execute)
python scenario_runner.py run -s config/scenarios.yaml --dry-run

# Filter to specific model
python scenario_runner.py run -s config/scenarios.yaml -f model_id=Qwen/Qwen3-0.6B

# Override target cluster
python scenario_runner.py run -s config/scenarios.yaml --cluster mi300x-cluster

# List scenarios without running
python scenario_runner.py list -s config/scenarios.yaml

# Export scenarios to JSON
python scenario_runner.py export -s config/scenarios.yaml -o scenarios.json

# Check KFP run status
python scenario_runner.py status --runs <uuid1>,<uuid2>,<uuid3>

# List recent batches
python scenario_runner.py batches --limit 10

# Delete/cleanup a batch (terminates KFP runs)
python scenario_runner.py delete --batch batch-20260315-143022 --dry-run
```

### benchmark_api.py (Low-level API)

```bash
# Setup kubeconfig secrets
python benchmark_api.py setup

# Check cluster status
python benchmark_api.py status

# Check GPU quota
python benchmark_api.py quota

# Submit single benchmark (bypasses batch system)
python benchmark_api.py rhoai \
  --model "Qwen/Qwen3-0.6B" \
  --gpu-type H200 \
  --tp 1 \
  --rate "1,10,50" \
  --max-seconds 120

# Cleanup Kueue workload
python benchmark_api.py cleanup --name <workload-name>
```

### cluster_manager.py (Cluster Onboarding)

```bash
# List all configured clusters with status
python cluster_manager.py list

# Run validation checks on a cluster
python cluster_manager.py check h200-cluster

# Add new cluster interactively
python cluster_manager.py add new-cluster

# Full onboarding workflow (creates secret, validates)
python cluster_manager.py onboard new-cluster --kubeconfig ~/.kube/new-config

# Create/update kubeconfig secret only
python cluster_manager.py secret h200-cluster
```

## Configuration

### scenarios.yaml

```yaml
# Target cluster (can override via CLI: --cluster mi300x-cluster)
target_cluster: h200-cluster

# Common defaults for all models
common:
  runtime_args:
    max-model-len: 8192
    gpu-memory-utilization: 0.92
    disable-log-requests: true

# Workload profiles
workloads:
  balanced:
    input_tokens: 1000
    output_tokens: 1000
    max-model-len: 8192
  short:
    input_tokens: 128
    output_tokens: 128
    max-model-len: 8192
  long-context:
    input_tokens: 8192
    output_tokens: 1024
    max-model-len: 10000

# Routing modes
routing:
  direct:
    description: "Direct vLLM access"
    epp_enabled: false

# Models with per-model matrix
models:
  Qwen/Qwen3-0.6B:              # Key = HuggingFace model ID
    deploy:
      name: qwen3-0-6b
    matrix:
      workloads: [balanced, short]
      routing: [direct]
      tensor-parallel-size: [1]

  openai/gpt-oss-120b:
    deploy:
      name: gpt-oss-120b
    matrix:
      workloads: [balanced, short]
      routing: [direct]
      tensor-parallel-size: [1, 2, 4]  # 2 x 1 x 3 = 6 scenarios
```

### clusters.yaml

```yaml
clusters:
  h200-cluster:
    name: "H200 Production"
    kubeconfig_path: "~/.kube/h200-kubeconfig"  # Local path for CLI
    kubeconfig_secret: "h200-kubeconfig"         # Secret in KFP namespace
    namespace: "llm-d-bench"
    gpu_type: "H200"
    kueue_queue: "benchmark-queue"
    enabled: true

  mi300x-cluster:
    name: "MI300X Test"
    kubeconfig_path: "~/.kube/mi300x-kubeconfig"
    kubeconfig_secret: "mi300x-kubeconfig"
    namespace: "llm-d-bench"
    gpu_type: "MI300X"
    kueue_queue: "benchmark-queue"
    enabled: true
```

## Pipeline Stages

The KFP pipeline executes these stages:

| Stage | Description | Timeout |
|-------|-------------|---------|
| 0-queue-admission | Wait for Kueue GPU quota | 2h |
| 1-deploy-vllm | Create LLMInferenceService | - |
| 2-wait-endpoint | Wait for model ready | 1h |
| 2-vllm-server-logs | Stream vLLM logs (parallel) | - |
| 3-run-guidellm | Execute benchmark | configurable |
| 4-upload-mlflow | Upload to AWS MLflow | - |
| 5-cleanup | Delete resources | - |

## MLflow Integration

Results are uploaded to AWS SageMaker MLflow:
- **Experiment**: `psap-benchmark-runs` (single experiment for all runs)
- **Run name**: `{scenario_id}` (e.g., `qwen3-0-6b_balanced_direct_tp1`)
- **Tags**: `batch_id`, `scenario_id`, `model_id`, `workload`, `routing`, `tensor_parallel`

Access: [SageMaker Unified Studio](https://us-east-1.sagemaker.aws.amazon.com/)

## Troubleshooting

### Pipeline fails to start

```bash
# Check KFP pods
oc get pods -n kubeflow

# Check pipeline submission logs
python benchmark_api.py status
```

### Kueue workload stuck

```bash
# Check workload status
oc get workload -n benchmark-system

# Check ClusterQueue
oc get clusterqueue benchmark-queue -o wide

# Manual cleanup
python benchmark_api.py cleanup --name <workload-name>
```

### Model deployment timeout

Large models (70B+) can take 20+ minutes to load. Check:
```bash
# vLLM pod logs on target cluster
oc logs -f <vllm-pod> -n llm-perf

# Pod events
oc describe pod <vllm-pod> -n llm-perf
```

### kubeconfig secret not found

```bash
# Recreate secrets
python benchmark_api.py setup
```

## Files

| File | Purpose |
|------|---------|
| `scenario_runner.py` | Primary CLI for batch execution |
| `benchmark_api.py` | KFP submission + Kueue management |
| `cluster_manager.py` | Cluster onboarding + validation CLI |
| `rhoai_benchmark_pipeline_v2.py` | KFP pipeline definition |
| `config/scenarios.yaml` | Model + workload configuration |
| `config/clusters.yaml` | Target cluster definitions (single source of truth) |
| `config/platform.yaml` | Management cluster + KFP settings |
| `core/batch.py` | BatchRun dataclass |
| `core/scenario_run.py` | ScenarioRun dataclass |
| `core/cluster.py` | Cluster dataclass |
| `core/platform.py` | PlatformConfig loader |
| `core/protocols.py` | ClusterProvider protocol |
| `services/cluster_validation.py` | Cluster sanity checks |
| `services/cluster_operations.py` | Secret creation, onboarding |
| `orchestration/batch_orchestrator.py` | Main orchestration logic |
| `registry/cluster_registry.py` | ClusterProvider implementation |
| `scenario_generator/generator.py` | Matrix expansion |
| `ARCHITECTURE.md` | Detailed architecture + design decisions |

## UI Access

- **KFP UI**: https://ds-pipeline-dspa-kfp-prototype.apps.mehulvalidation.example.com
- **MLflow**: AWS SageMaker Unified Studio
