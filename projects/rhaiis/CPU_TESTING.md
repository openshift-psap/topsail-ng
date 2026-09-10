# CPU Accelerator Testing Guide

This document covers testing CPU inference with vLLM on an OpenShift cluster, supporting
both upstream vLLM CPU builds (vanilla) and the Red Hat AI Inference Service (RHAIIS)
CPU image.

## Cluster Setup

### 1. Log in to OpenShift

```bash
oc login --token=<token> --server=<server>
```

If the cluster uses a self-signed or private CA, configure the trusted certificate
instead of skipping TLS verification:

```bash
# One-time: add the cluster CA to the system trust store (RHEL/Fedora)
sudo cp cluster-ca.crt /etc/pki/ca-trust/source/anchors/
sudo update-ca-trust

# Or pass the CA file directly to oc login
oc login --token=<token> --server=<server> --certificate-authority=cluster-ca.crt
```

### 2. Diagnose the cluster

Run the diagnostic toolbox to verify CPU instruction sets and KServe availability
before committing to a namespace. It lists the configured CPU image references but
does **not** verify registry access or pull-secret validity — confirm those
separately (e.g. `podman pull` or a test pod) before deploying:

```bash
./bin/run_toolbox rhaiis diagnose_cpu_cluster
```

The toolbox checks:
- Node resources (CPU, memory allocatable)
- AVX2 support (required) and AVX-512 (optional, ~2-3x faster if present)
- NUMA topology
- CPU Manager policy (static = dedicated CPUs; none = time-sliced)
- KServe CRD installation

#### Label worker nodes for CPU scheduling (one-time per cluster)

CPU presets set `rhaiis.deploy.node_selector` to
`rhaiis.io/cpu-benchmark: "true"` so InferenceService pods land on nodes
with AVX2 and enough allocatable CPU. Apply labels once after reviewing the
diagnostic output:

```bash
# Preview label commands (no cluster changes)
./bin/run_toolbox rhaiis diagnose_cpu_cluster --apply-labels --dry-run

# Apply rhaiis.io/* labels to worker nodes
./bin/run_toolbox rhaiis diagnose_cpu_cluster --apply-labels

# Remove rhaiis.io/* CPU labels (fast — skips oc debug checks)
./bin/run_toolbox rhaiis diagnose_cpu_cluster --remove-labels --dry-run
./bin/run_toolbox rhaiis diagnose_cpu_cluster --remove-labels
```

Labels written:

| Label | Meaning |
|-------|---------|
| `rhaiis.io/cpu-vllm-capable=true` | AVX2 present (minimum for vLLM CPU) |
| `rhaiis.io/cpu-avx512=true` | AVX-512 present |
| `rhaiis.io/cpu-amx=true` | Intel AMX present |
| `rhaiis.io/cpu-manager-static=true` | kubelet CPU manager policy is `static` |
| `rhaiis.io/cpu-benchmark=true` | AVX2 + allocatable CPU ≥ 8 cores (composite) |

CI preflight runs diagnose-only (no labeling) so shared clusters are not
modified automatically. Override the benchmark threshold with
`--min-benchmark-cpu 16` if your smoke preset requests 16 vCPU.

To disable scheduling constraints for a one-off test, clear the selector in
config or override at deploy time:

```yaml
rhaiis.deploy.node_selector: {}
```

### 3. Create the namespace

```bash
oc new-project forge-rhaiis
oc label namespace forge-rhaiis opendatahub.io/dashboard=true
```

### 4. Create secrets

HuggingFace token (required — the secret must exist even for ungated models):

```bash
oc create secret generic storage-config \
  --from-literal=HF_TOKEN=<your-hf-token> \
  -n forge-rhaiis
```

RHAIIS image pull secret (only needed when using the `cpu` or `cpu-smoke`
presets, i.e. `--cpu-flavor rhaiis` — not required for `cpu-vanilla` /
`vanilla-cpu-smoke`):

```bash
oc create secret docker-registry rhaiis-pull-secret \
  --docker-server=registry.redhat.io \
  --docker-username=<user> --docker-password=<token> \
  -n forge-rhaiis
```

### 5. Create the model cache PVC

The default storage config uses `storage_source: hf` with `storage_pvc:
model-pvc`. KServe mounts this PVC at `/mnt/models` so vLLM can persist the
HuggingFace download cache across pod restarts. Create it once per namespace:

```bash
oc create -n forge-rhaiis -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: model-pvc
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 50Gi
EOF
```

> **Tip**: check `oc get storageclass` first and add `storageClassName: <name>`
> if the cluster has no default StorageClass. 50 Gi is enough for TinyLlama
> and Qwen3-0.6B; use 200 Gi for Llama 3.1 8B.

Before running `cpu-chat-baseline` or a Fournos 8B job, resize or recreate the
PVC at 200 Gi:

```bash
# Recreate at 200 Gi (delete the old PVC only if no pod is mounted)
oc delete pvc model-pvc -n forge-rhaiis
oc create -n forge-rhaiis -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: model-pvc
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 200Gi
EOF
```

### 6. Set artifact directory

```bash
export ARTIFACT_DIR=/tmp/rhaiis-artifacts
mkdir -p $ARTIFACT_DIR
```

## Single-Run Tests

### Smoke test

After cluster setup (steps 1–6 above), run a smoke test to confirm the stack
is working end-to-end before committing to a longer benchmark.

#### Dry-run (no cluster required)

```bash
# Vanilla — confirm config with no cluster
python -m projects.rhaiis.orchestration.cli test \
  --preset vanilla-cpu-smoke \
  --namespace forge-rhaiis \
  --dry-run

# RHAIIS — confirm config with no cluster
python -m projects.rhaiis.orchestration.cli test \
  --preset cpu-smoke \
  --namespace forge-rhaiis \
  --image-pull-secret rhaiis-pull-secret \
  --dry-run
```

#### Live smoke test (cluster required)

```bash
# Vanilla (no image pull secret needed)
python -m projects.rhaiis.orchestration.cli test \
  --preset vanilla-cpu-smoke \
  --namespace forge-rhaiis

# RHAIIS (requires registry.redhat.io pull secret)
python -m projects.rhaiis.orchestration.cli test \
  --preset cpu-smoke \
  --namespace forge-rhaiis \
  --image-pull-secret rhaiis-pull-secret
```

> **Note**: on first run vLLM downloads the model from HuggingFace into the
> PVC (`/mnt/models`). TinyLlama is ~1 GB; allow 5-10 minutes. Subsequent
> runs reuse the cached weights.

### Baseline workloads

```bash
# Vanilla
python -m projects.rhaiis.orchestration.cli test \
  --preset vanilla-cpu-chat-baseline \
  --namespace forge-rhaiis

# RHAIIS
python -m projects.rhaiis.orchestration.cli test \
  --preset cpu-chat-baseline \
  --namespace forge-rhaiis \
  --image-pull-secret rhaiis-pull-secret
```

## Concurrent Load Matrix

The concurrent load test sweeps `models × cpu_requests × workloads`, matching
the format-results `concurrent-load` suite.

### Run the matrix

#### Vanilla (recommended starting point)

> **Note**: TinyLlama's `max-model-len` is 2048; do not use `cpu-rag-baseline`
> with it (prompt_tokens=7680 exceeds the context limit). Use `qwen3-0-6b-cpu`
> or another model without a 2048 cap for RAG workloads.

```bash
# TinyLlama — chat workloads only (max-model-len: 2048)
python -m projects.rhaiis.orchestration.cli concurrent-load \
  --preset cpu-vanilla \
  --models tinyllama-cpu \
  --cpu-requests 8,16 \
  --workloads cpu-chat-baseline \
  --namespace forge-rhaiis \
  --continue-on-error

# Qwen3-0.6B — chat + RAG (no max-model-len cap)
python -m projects.rhaiis.orchestration.cli concurrent-load \
  --preset cpu-vanilla \
  --models qwen3-0-6b-cpu \
  --cpu-requests 8,16 \
  --workloads cpu-chat-baseline,cpu-rag-baseline \
  --namespace forge-rhaiis \
  --continue-on-error
```

#### RHAIIS

```bash
# cpu-chat-baseline preset pins model (llama31-8b-w8a8-cpu), flavor (rhaiis),
# memory (64 Gi), and VLLM_CPU_KVCACHE_SPACE=24
python -m projects.rhaiis.orchestration.cli concurrent-load \
  --preset cpu-chat-baseline \
  --cpu-requests 8,16 \
  --workloads cpu-chat-baseline,cpu-code-baseline \
  --namespace forge-rhaiis \
  --image-pull-secret rhaiis-pull-secret \
  --continue-on-error
```

`--cpu-flavor` defaults to the config value (`vanilla`) when omitted; passing
`--preset cpu-chat-baseline` or `--preset vanilla-cpu-chat-baseline` sets the
flavor (and KV=24) without explicit `--cpu-flavor` or `--models` flags.

### Matrix dimensions

| Dimension | Default | Notes |
|---|---|---|
| `--models` | `tinyllama-cpu` | See CPU models below |
| `--cpu-requests` | `8,16,32` | Limit to 8,16 on nodes with <24 vCPUs |
| `--workloads` | `cpu-chat-baseline` | See CPU workloads below |
| `--cpu-flavor` | config default (`vanilla`) | Pass explicitly to override; omitting preserves preset value |

### CPU models

| Key | Model | Notes |
|---|---|---|
| `tinyllama-cpu` | TinyLlama/TinyLlama-1.1B-Chat-v1.0 | Ungated, good for CI |
| `qwen3-0-6b-cpu` | Qwen/Qwen3-0.6B | Ungated, smallest |
| `llama-3-2-1b-cpu` | meta-llama/Llama-3.2-1B-Instruct | Ungated |
| `llama-3-2-3b-cpu` | meta-llama/Llama-3.2-3B-Instruct | Gated, requires HF_TOKEN |
| `granite-3-2-2b-cpu` | ibm-granite/granite-3.2-2b-instruct | Ungated |
| `llama31-8b-w8a8-cpu` | RedHatAI/Meta-Llama-3.1-8B-Instruct-quantized.w8a8 | Gated, RHAIIS production model |

### CPU workloads

| Key | ISL | OSL | Phase | Notes |
|---|---|---|---|---|
| `cpu-smoke` | 512 | 512 | — | CI-friendly, 120s, rates 1/2/4 |
| `cpu-chat-baseline` | 512 | 512 | 1 (fixed) | 600s, rates 1-32 |
| `cpu-rag-baseline` | 7680 | 512 | 1 (fixed) | 600s, rates 1-16 |
| `cpu-code-baseline` | 1024 | 1024 | 1 (fixed) | 600s, rates 1-32 |
| `cpu-summarization-baseline` | 2048 | 256 | 1 (fixed) | 600s, rates 1-16 |
| `cpu-chat-realistic` | 512±128 | 512±128 | 2 (variable) | No caching |
| `cpu-code-realistic` | 1024±256 | 1024±256 | 2 (variable) | No caching |

### vLLM images

| Flavor | Image |
|---|---|
| `vanilla` | `docker.io/vllm/vllm-openai-cpu:v0.25.1` |
| `rhaiis` | `registry.redhat.io/rhaii/vllm-cpu-rhel9:3.5.0-1786546771` |

## Presets

CPU-specific presets are defined in `presets.d/presets.yaml` and can be
combined with model/workload presets:

| Preset | Flavor | Model key | Workload key |
|---|---|---|---|
| `cpu` | rhaiis | (from config) | (from config) |
| `cpu-vanilla` | vanilla | (from config) | (from config) |
| `cpu-smoke` | rhaiis | `tinyllama-cpu` | `cpu-smoke` |
| `vanilla-cpu-smoke` | vanilla | `tinyllama-cpu` | `cpu-smoke` |
| `cpu-chat-baseline` | rhaiis | `llama31-8b-w8a8-cpu` | `cpu-chat-baseline` |
| `vanilla-cpu-chat-baseline` | vanilla | `llama31-8b-w8a8-cpu` | `cpu-chat-baseline` |

Example with a preset:

```bash
python -m projects.rhaiis.orchestration.cli test \
  -p cpu-smoke \
  --namespace forge-rhaiis \
  --image-pull-secret rhaiis-pull-secret \
  --dry-run
```

## Config validation

Run offline (no cluster required) to verify CPU image selection, LD_PRELOAD
isolation, max-model-len precedence, and resource Guaranteed QoS:

```bash
pytest projects/rhaiis/orchestration/test_cpu_config.py \
       projects/rhaiis/orchestration/test_cpu_node_labels.py
```

Or run the scripts directly:

```bash
PYTHONPATH=$PWD python projects/rhaiis/orchestration/test_cpu_config.py
PYTHONPATH=$PWD python projects/rhaiis/orchestration/test_cpu_node_labels.py
```

### Related automated tests

| Suite | Scope |
|-------|-------|
| `projects/rhaiis/orchestration/test_cpu_*.py` | CPU config, manifests, node label helpers |
| `projects/fournos_launcher/tests/` | Fournos `submit_and_wait` regression (job launch/wait plumbing used by CPU CI; not RHAIIS-specific logic) |

Both suites are registered in `pyproject.toml` `testpaths` and run with `pytest`.

## Cleanup

```bash
python -m projects.rhaiis.orchestration.cli cleanup \
  --deployment-name <name> \
  --namespace forge-rhaiis
```

## Troubleshooting

### Pod stuck in Pending

```bash
oc describe pod -n forge-rhaiis -l serving.kserve.io/inferenceservice=<name> | grep -A 10 "Events:"
```

Common causes: insufficient CPU/memory on node, missing PVC.
Node has ~23.5 vCPUs on the lab cluster — limit `--cpu-requests` to `8,16`.

### vLLM crashes: KV cache too large

```
ValueError: Available memory on node 0 (X GiB) is less than requested
memory for kv (10.0 GiB).
```

The default `VLLM_CPU_KVCACHE_SPACE=10` (10 GiB) is tuned for nodes with
16–32 GiB per NUMA node. If you have more RAM (e.g. for Llama 3.1 8B + RAG),
raise it per-model via `env_vars.VLLM_CPU_KVCACHE_SPACE` in
`config.d/models.yaml` or set
`rhaiis.accelerator_env_vars.cpu.VLLM_CPU_KVCACHE_SPACE` in a preset.

### `secret "storage-config" not found`

```bash
oc create secret generic storage-config \
  --from-literal=HF_TOKEN=<token> \
  -n forge-rhaiis
```

### vLLM slow to start

First startup compiles with Torch Inductor (~5-10 min on CPU without AVX-512).
Subsequent runs on the same node reuse the AOT cache and start much faster.

### `oneDNN linear fallback` warning

Expected on CPUs without AVX-512 (e.g. Haswell E5-2620 v3). Performance impact
but functionally correct.

## Known Limitations

- **No bare-metal support**: CPU testing in forge is OpenShift/KServe-only.
  For bare-metal NUMA/cpuset testing, use the format-results Ansible playbooks.
- **No CPU Manager pinning**: CPU Manager must be enabled on the cluster with
  static policy for dedicated CPU allocation. Without it, CPUs are time-sliced
  and benchmark results will have higher variance.
- **No Phase 3**: Prefix-caching (Phase 3) workloads are defined in config but
  not yet part of the concurrent load suite (matching format-results).
