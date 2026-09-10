# rhaiis

`rhaiis` is the Forge project for benchmarking AI inference engines on OpenShift
using KServe InferenceService.

Supports **vLLM**, **SGLang**, and **TRT-LLM** engines via a generic abstraction.
The workflow deploys an InferenceService with the selected engine, runs warmup
and/or profiler passes, executes multi-profile GuideLLM benchmarks, generates a
dashboard CSV via the Caliper postprocessing pipeline, syncs results to S3, and
cleans up.

## Workflow sequence

```
deploy_kserve_isvc         Deploy ServingRuntime + InferenceService
        |
wait_isvc_ready            Poll InferenceService status + health check
        |
  ┌─────┴─────┐
  │  Phase 1   │  For each workload profile:
  │  (warmup   │    - warmup run (optional) OR profiler-gated run
  │  /profiler)│    - profiler trace upload to S3
  └─────┬─────┘
  ┌─────┴─────┐
  │  Phase 2   │  For each workload profile (in its own artifact dir):
  │ (benchmark)│    - write per-workload test labels
  │            │    - run GuideLLM benchmark at configured rates
  └─────┬─────┘
        |
capture_isvc_state         Capture ISVC YAML, pod logs, events
        |
cleanup_isvc               Delete InferenceService + ServingRuntime
        |
Caliper postprocessing     Parse → KPI → CSV export (per-profile rows)
        |
S3 dashboard sync          Append CSV to consolidated dashboard on S3
        |
Regression analysis        Compare against baseline version (optional)
```

On failure at any step, `capture_isvc_state` and `cleanup_isvc` still run
(try/finally in the orchestration layer). Pipeline failures and non-critical
warnings are reported via Slack.

Each workload profile runs in its own `NextArtifactDir` with per-workload test
labels, so the Caliper parser creates separate records per profile. This ensures
each profile gets its own dashboard CSV rows.

The benchmark step uses the canonical `projects.guidellm.toolbox.run_guidellm_benchmark`
(shared with llm_d). rhaiis builds `guidellm_args` from its config and passes them
to the canonical runner.

## Configuration

Config is split into `config.yaml` (base) and `config.d/` (per-domain):

- [`orchestration/config.yaml`](./orchestration/config.yaml) — vaults, benchmarks, tests, caliper postprocessing
- [`orchestration/config.d/rhaiis.yaml`](./orchestration/config.d/rhaiis.yaml) — deploy defaults, engine config (vLLM/SGLang/TRT-LLM), images, gpu_types, S3 settings, profiler
- [`orchestration/config.d/models.yaml`](./orchestration/config.d/models.yaml) — 52 model definitions
- [`orchestration/config.d/workloads.yaml`](./orchestration/config.d/workloads.yaml) — workload profiles

Key sections:

| Section | Purpose |
|---------|---------|
| `rhaiis` | Namespace, accelerator, deploy settings, S3 config |
| `rhaiis.engine` | Active engine: `vllm` (default), `sglang`, or `trtllm` |
| `rhaiis.engines.{vllm,sglang,trtllm}` | Per-engine images, port, and default args |
| `rhaiis.engines.trtllm.trtllm_config` | TRT-LLM server config (KV cache, CUDA graphs, MoE) |
| `rhaiis.deploy` | Deploy settings (replicas, CPU/memory, image_pull_secrets list, storage) |
| `rhaiis.s3` | S3 bucket, vault, and credentials for dashboard CSV and profiler trace uploads |
| `rhaiis.profiler` | PyTorch profiler settings (enable, S3 prefix, rates, labels) |
| `models` | Model definitions (hf_model_id, per-model `vllm_args` overrides) |
| `workloads` | Benchmark profiles (data shape, rates, max_seconds) |
| `benchmarks.guidellm` | GuideLLM image, backend, timeout, PVC size, HF token secret, fs_group |
| `tests` | CI test mapping (model_key, workload_keys, version) |
| `caliper.postprocess` | Caliper postprocessing pipeline (parse, KPI, CSV export) |

## Engine support

rhaiis supports three inference engines via a generic abstraction:

| Engine | Config key | Image source | TP arg | Notes |
|--------|-----------|-------------|--------|-------|
| vLLM | `rhaiis.engine: vllm` (default) | `rhaiis.engines.vllm.images` | `tensor-parallel-size` | No root required |
| SGLang | `rhaiis.engine: sglang` | `rhaiis.engines.sglang.images` | `tp-size` | No root required, uses `sglang serve` entrypoint |
| TRT-LLM | `rhaiis.engine: trtllm` | `rhaiis.engines.trtllm.images` | `tp_size` | Requires root (`anyuid` SCC), NGC pull secret |

Models define `vllm_args` with vLLM-style keys (e.g. `tensor-parallel-size`).
When running with SGLang or TRT-LLM, arguments are automatically translated
(e.g. `tensor-parallel-size` → `tp-size` for SGLang, `tp_size` for TRT-LLM).

Engine-specific args can also be set directly via `rhaiis.engines.<engine>.args.*`
in FournosJob `configOverrides`, which take precedence over model-level `vllm_args`.

TRT-LLM additionally supports a `trtllm_config` block for server-side configuration
(KV cache, CUDA graphs, MoE backend, etc.) that is serialized as a JSON config file
inside the container.

KServe manifests (ServingRuntime + InferenceService) are built as Python dicts in
`orchestration/manifests.py` with engine-specific container construction, then
passed to the generic `deploy_kserve_isvc` tool for application.

## Fournos integration

rhaiis supports Fournos-driven execution via `ci.py`:

```
bin/run_ci rhaiis ci resolve-fournos-config   # Populate spec.secretRefs + hardware
bin/run_ci rhaiis ci pre-cleanup              # No-op (avoid cleaning running resources)
bin/run_ci rhaiis ci prepare                  # Verify cluster, ensure namespace/SA
bin/run_ci rhaiis ci preflight                # Preflight checks
bin/run_ci rhaiis ci test                     # Deploy, benchmark, capture, cleanup
bin/run_ci rhaiis ci post-cleanup             # Detect pipeline failures, cleanup resources
bin/run_ci rhaiis ci export-artifacts         # Caliper export to MLflow
```

### FournosJob YAML (vLLM)

```yaml
apiVersion: fournos.dev/v1
kind: FournosJob
metadata:
  generateName: rhaiis-benchmark-
spec:
  owner: haumesh
  displayName: rhaiis-benchmark
  pipeline: forge-full
  cluster: zeus
  hardware:
    gpuType: h200
    gpuCount: 2
  secretRefs:
  - psap-forge-dashboard-s3
  - psap-forge-notifications
  executionEngine:
    forge:
      project: rhaiis
      args: [nvidia]
      configOverrides:
        tests.rhaiis.model_key: nemotron3super-120b-fp8
        rhaiis.engines.vllm.images.nvidia: vllm/vllm-openai:v0.24.0
        tests.rhaiis.version: "vLLM-0.24.0"
        tests.rhaiis.workload_keys: ["profile1","profile2","profile4"]
        rhaiis.engines.vllm.args.tensor-parallel-size: 2
        rhaiis.cluster_tag: "zeus2"
        rhaiis.deploy.image_pull_secrets: ["npalaska-image-pull"]
        caliper.postprocess.csv_dashboard.enabled: true
        benchmarks.guidellm.fs_group: 0  # opt-in for IBM Cloud clusters
  env:
    PULL_PULL_SHA: "<commit-sha-or-release-tag>"  # e.g. forge-rhaiis-v0.0.2
```

### FournosJob YAML (SGLang)

```yaml
apiVersion: fournos.dev/v1
kind: FournosJob
metadata:
  generateName: sglang-benchmark-
spec:
  owner: haumesh
  displayName: sglang-benchmark
  pipeline: forge-full
  cluster: zeus
  hardware:
    gpuType: h200
    gpuCount: 2
  secretRefs:
  - psap-forge-dashboard-s3
  - psap-forge-notifications
  executionEngine:
    forge:
      project: rhaiis
      args: [nvidia]
      configOverrides:
        tests.rhaiis.model_key: nemotron3super-120b-fp8
        tests.rhaiis.version: "SGLang-0.5.11"
        tests.rhaiis.workload_keys: ["profile1"]
        rhaiis.engine: sglang
        rhaiis.engines.sglang.args.tp-size: 2
        rhaiis.engines.sglang.args.disable-radix-cache: true
        rhaiis.engines.sglang.args.mem-fraction-static: 0.90
        rhaiis.engines.sglang.args.context-length: 8192
        rhaiis.engines.sglang.args.trust-remote-code: true
        rhaiis.deploy.image_pull_secrets: ["npalaska-image-pull"]
        caliper.postprocess.csv_dashboard.enabled: true
        benchmarks.guidellm.fs_group: 0
  env:
    PULL_PULL_SHA: "<commit-sha-or-release-tag>"  # e.g. forge-rhaiis-v0.0.2
```

### FournosJob YAML (TRT-LLM)

TRT-LLM requires the `anyuid` SCC on the target cluster (`oc adm policy add-scc-to-user anyuid -z default -n <namespace>`).

```yaml
apiVersion: fournos.dev/v1
kind: FournosJob
metadata:
  generateName: trtllm-benchmark-
spec:
  owner: haumesh
  displayName: trtllm-benchmark
  pipeline: forge-full
  cluster: zeus
  hardware:
    gpuType: h200
    gpuCount: 2
  secretRefs:
  - psap-forge-dashboard-s3
  - psap-forge-notifications
  executionEngine:
    forge:
      project: rhaiis
      args: [nvidia]
      configOverrides:
        tests.rhaiis.model_key: nemotron3super-120b-fp8
        tests.rhaiis.version: "TRT-LLM-1.3.0rc13"
        tests.rhaiis.workload_keys: ["profile1"]
        rhaiis.engine: trtllm
        rhaiis.deploy.image_pull_secrets: ["npalaska-image-pull", "ngc-secret"]
        rhaiis.deploy.memory_request: "256Gi"
        # Engine args
        rhaiis.engines.trtllm.args.tp_size: 2
        rhaiis.engines.trtllm.args.ep_size: 2
        rhaiis.engines.trtllm.args.max_batch_size: 256
        rhaiis.engines.trtllm.args.max_num_tokens: 8192
        rhaiis.engines.trtllm.args.trust_remote_code: true
        # TRT-LLM server config
        rhaiis.engines.trtllm.trtllm_config.kv_cache_config.dtype: fp8
        rhaiis.engines.trtllm.trtllm_config.kv_cache_config.free_gpu_memory_fraction: 0.8
        rhaiis.engines.trtllm.trtllm_config.cuda_graph_config.enable_padding: true
        rhaiis.engines.trtllm.trtllm_config.cuda_graph_config.max_batch_size: 256
        rhaiis.engines.trtllm.trtllm_config.enable_attention_dp: true
        rhaiis.engines.trtllm.trtllm_config.moe_config.backend: TRTLLM
        caliper.postprocess.csv_dashboard.enabled: true
        benchmarks.guidellm.fs_group: 0
  env:
    PULL_PULL_SHA: "<commit-sha-or-release-tag>"  # e.g. forge-rhaiis-v0.0.2
```

### GitHub PR workflow

Jobs can also be triggered via PR comments on `openshift-psap/forge`:

```
/test fournos rhaiis nvidia
/pipeline forge-full
/cluster zeus
/var tests.rhaiis.model_key: nemotron3super-120b-fp8
/var tests.rhaiis.version: vLLM-0.24.0
/var tests.rhaiis.workload_keys: ["profile1"]
/var rhaiis.engines.vllm.args.tensor-parallel-size: 2
```

Note: `/var` directives use `key: value` format (colon required).
If `PULL_PULL_SHA` is not set, the PR HEAD commit is used automatically.

Presets from `args` are applied via `project.args` → `presets.d/presets.yaml`.
Config overrides use dot-notation to set any nested config value.

Available configOverrides:

| Key | Description |
|-----|-------------|
| `tests.rhaiis.model_key` | Model key from config.d/models.yaml |
| `tests.rhaiis.version` | Version label for dashboard and regression |
| `tests.rhaiis.workload_keys` | List of workload profiles to run |
| `tests.rhaiis.warmup` | Enable warmup pass before benchmarks |
| `tests.rhaiis.slack_user` | Slack user ID for failure notifications |
| `tests.rhaiis.compare_version` | Baseline version for regression comparison |
| `rhaiis.engine` | Inference engine: `vllm` (default), `sglang`, `trtllm` |
| `rhaiis.namespace` | Kubernetes namespace |
| `rhaiis.cluster_tag` | Cluster identifier for dashboard grouping |
| `rhaiis.deploy.image_pull_secrets` | List of image pull secret names |
| `rhaiis.deploy.storage_pvc` | PVC name for model storage |
| `rhaiis.deploy.replicas` | Number of predictor replicas |
| `rhaiis.deploy.memory_request` | Memory request for predictor (e.g. `256Gi` for TRT-LLM) |
| `rhaiis.engines.vllm.args.*` | vLLM CLI args (e.g. `tensor-parallel-size`, `gpu-memory-utilization`) |
| `rhaiis.engines.sglang.args.*` | SGLang CLI args (e.g. `tp-size`, `mem-fraction-static`, `context-length`) |
| `rhaiis.engines.trtllm.args.*` | TRT-LLM CLI args (e.g. `tp_size`, `ep_size`, `max_batch_size`) |
| `rhaiis.engines.trtllm.trtllm_config.*` | TRT-LLM server config (kv_cache, cuda_graph, moe) |
| `rhaiis.profiler.enabled` | Enable PyTorch profiler |
| `rhaiis.agent_analysis.enabled` | Enable AI agent regression analysis |
| `caliper.postprocess.csv_dashboard.enabled` | Enable dashboard CSV S3 sync |
| `benchmarks.guidellm.timeout` | Benchmark timeout in seconds |
| `benchmarks.guidellm.hf_token_secret` | K8s secret name for HF_TOKEN (omit to skip) |
| `benchmarks.guidellm.fs_group` | Pod-level fsGroup for PVC permissions (disabled by default) |

Monitoring Fournos jobs:
```bash
export KUBECONFIG=~/kubeconfigs/psap-automation-kubeconfig
oc get fournosjobs -n psap-automation              # Job status
oc get workloads -n psap-automation                # Queue status
oc get pipelineruns -n psap-automation | grep <name>  # Pipeline progress
oc logs -f <pod-name> -n psap-automation -c step-main # Live logs
oc patch fournosjob <name> -n psap-automation \
  --type merge -p '{"spec":{"shutdown":"Stop"}}'   # Stop a job
```

## Main entrypoints

- CLI: [`orchestration/cli.py`](./orchestration/cli.py)
- CI: [`orchestration/ci.py`](./orchestration/ci.py) (Fournos pipeline)
- CI test: [`orchestration/test_rhaiis.py`](./orchestration/test_rhaiis.py)
- Test phase: [`orchestration/test_phase.py`](./orchestration/test_phase.py)
- Manifests: [`orchestration/manifests.py`](./orchestration/manifests.py) (KServe ServingRuntime + InferenceService builders)
- Runtime config: [`orchestration/runtime_config.py`](./orchestration/runtime_config.py) (engine-aware config helpers)
- Analysis: [`orchestration/analysis.py`](./orchestration/analysis.py)
- Notifications: [`orchestration/notifications.py`](./orchestration/notifications.py)

## Toolbox commands

| Command | Source | Purpose |
|---------|--------|---------|
| `deploy_kserve_isvc` | [rhaiis](./toolbox/deploy_kserve_isvc/) | Apply pre-built KServe InferenceService + ServingRuntime manifests |
| `wait_isvc_ready` | [rhaiis](./toolbox/wait_isvc_ready/) | Poll InferenceService readiness with health check |
| `run_guidellm_benchmark` | [canonical](../guidellm/toolbox/run_guidellm_benchmark/) | Run GuideLLM benchmark (shared with llm_d) |
| `capture_isvc_state` | [rhaiis](./toolbox/capture_isvc_state/) | Capture InferenceService YAML, pod logs, events, describe output |
| `cleanup_isvc` | [rhaiis](./toolbox/cleanup_isvc/) | Delete InferenceService, ServingRuntime, wait for deletion |
| `enable_profiler_gate` | [rhaiis](./toolbox/enable_profiler_gate/) | Enable/disable the PyTorch profiler gate file on the vLLM pod |
| `verify_profiler_prereqs` | [rhaiis](./toolbox/verify_profiler_prereqs/) | Verify profiler prerequisites (gate file, sitecustomize.py) |
| `copy_profiler_traces` | [rhaiis](./toolbox/copy_profiler_traces/) | Copy Chrome trace JSON files from the vLLM pod |

## Usage

```bash
# Activate the virtualenv
source ~/test_foo/python3_virt/bin/activate

# Dry run (prints config without deploying)
python3 -m projects.rhaiis.orchestration.cli test \
  --model qwen3-0_6b --workload profile1 --dry-run

# Dry run with a specific model
python3 -m projects.rhaiis.orchestration.cli test \
  --model llama-4-scout-fp8 --workload profile2 --dry-run

# Full E2E test
python3 -m projects.rhaiis.orchestration.cli test \
  --model qwen3-0_6b \
  --workload profile1 \
  --namespace kserve-e2e-perf \
  --image-pull-secret npalaska-image-pull

# Cleanup only
python3 -m projects.rhaiis.orchestration.cli cleanup \
  --deployment-name qwen3-0-6b --namespace kserve-e2e-perf

# CI resolve dry-run (shows what Fournos would resolve)
PYTHONPATH=$PWD python3 projects/rhaiis/orchestration/ci.py \
  resolve-fournos-config --dry-run
```

## CLI overrides

The CLI accepts flags that override workload profile defaults. This is useful for
quick validation runs without changing `config.yaml`.

```bash
# Override rates and max-seconds for a quick test (2 rates, 60s each)
python3 -m projects.rhaiis.orchestration.cli test \
  --model qwen3-0_6b \
  --workload profile1 \
  --namespace kserve-e2e-perf \
  --image-pull-secret npalaska-image-pull \
  --rates 1,5 --max-seconds 60

# Override tensor-parallel size
python3 -m projects.rhaiis.orchestration.cli test \
  --model llama-3-1-8b-fp8 \
  --tensor-parallel 2 \
  --namespace kserve-e2e-perf

# Override vLLM image
python3 -m projects.rhaiis.orchestration.cli test \
  --model qwen3-0_6b \
  --vllm-image quay.io/custom/vllm:latest \
  --namespace kserve-e2e-perf
```

Available overrides:

| Flag | Default source | Description |
|------|---------------|-------------|
| `--rates` | `workloads.<key>.rates` | Comma-separated concurrency levels (e.g. `1,5,50`) |
| `--max-seconds` | `workloads.<key>.max_seconds` | Max benchmark duration per rate |
| `--tensor-parallel` | engine args | Tensor parallel size (translated per engine) |
| `--vllm-image` | `rhaiis.engines.<engine>.images.<accelerator>` | Serving container image |
| `--accelerator` | `rhaiis.accelerator` | `nvidia` or `amd` |
| `--replicas` | `rhaiis.deploy.replicas` | Number of predictor replicas |
| `--storage-source` | `rhaiis.deploy.storage_source` | `hf` (HuggingFace download) or `pvc` |
| `--storage-pvc` | `rhaiis.deploy.storage_pvc` | PVC name for model storage |
| `--image-pull-secret` | `rhaiis.deploy.image_pull_secrets` | Image pull secret name |
| `--service-account-name` | `rhaiis.deploy.service_account_name` | Service account for predictor |
| `--deployment-name` | derived from model HF ID | InferenceService name |

## Result extraction

GuideLLM results are extracted using `oc cp` with a gzip fallback for large files:

1. GuideLLM job writes `benchmarks.json` to a PVC mounted at `/results`
2. A copy pod is created on the same node (required for ReadWriteOnce PVC)
3. Results are copied via `oc cp` from the copy pod
4. If `oc cp` fails for large files (e.g. `unexpected EOF`), the file is gzipped
   inside the pod, copied compressed, and decompressed locally
5. PVC, job, and copy pod are deleted

## Postprocessing and dashboard CSV

After benchmarks complete, `run_and_postprocess()` runs the Caliper pipeline:

1. **Parse**: `RhaiisParser` extends `GuideLLMParser` with extra metrics (p1/p999
   percentiles, mean latencies, token counts, throughput) from raw `benchmarks.json`
2. **KPI generate**: `RhaiisKpiHandler` emits 33 KPI types per rate point per profile
3. **CSV export**: `RhaiisPlugin.export_dashboard_csv` maps KPIs to the dashboard CSV
   schema with all metadata columns (model, version, TP, accelerator, UUID, etc.)
4. **S3 sync**: The CSV is appended to the consolidated dashboard CSV on S3
5. **Regression check**: Optionally compares current vs. baseline version

Dashboard CSV columns include throughput (output tok/s, total tok/s), latency
percentiles (TTFT/TPOT/ITL at median, p1, p95, p99, p999, mean), request latency,
token counts, concurrency, and run metadata.

## Notifications

Pipeline events are reported to Slack via the topsail bot:

- **Failure alerts**: Sent when the pipeline crashes (exceptions, image pull failures,
  infrastructure errors). Includes error details, model, accelerator, TP, version,
  and job ID.
- **Warning alerts**: Sent when the pipeline completes with non-critical warnings
  (e.g. profiler trace upload failed, predictor log upload failed).
- **Regression alerts**: Sent when regression analysis detects performance changes,
  with a link to the dashboard filtered by the relevant configuration.

Notifications are sent to the `psap-rhaiis-alerts` Slack channel. The post-cleanup
step also detects infrastructure failures that occur before the test step runs.

## PyTorch profiling

When `rhaiis.profiler.enabled: true`, the pipeline runs profiler-gated benchmarks
before the main benchmarks:

1. Verify profiler prerequisites on the vLLM pod (gate file, sitecustomize.py)
2. Enable the profiler gate with a workload-specific label (e.g. `isl1000_osl1000`)
3. Run a GuideLLM benchmark at the configured profiler rates
4. Disable the profiler gate
5. Copy Chrome trace JSON files from the vLLM pod
6. Upload traces to S3 organized by accelerator/model/TP/version/profile

Traces are viewable in `chrome://tracing` or Perfetto UI.

## Parallel job isolation

Multiple FournosJobs can run concurrently on the same cluster. Each job gets a
unique deployment name by appending the last 5 characters of the FJOB name
(e.g. `nvidia-nemotron-3-super-120b-a1-54r5f`). Resource cleanup is scoped to
the current job's suffix to avoid interfering with other running jobs.

## MLflow export

Results are exported to MLflow via the caliper pipeline. The export step runs as
a separate CI command (`export-artifacts`) in the Fournos pipeline.

MLflow tags set on each run:

| Tag | Example |
|-----|---------|
| `project` | rhaiis |
| `model_key` | nemotron3super-120b-fp8 |
| `hf_model_id` | nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8 |
| `accelerator` | nvidia |
| `tensor_parallel_size` | 2 |
| `vllm_image` | vllm/vllm-openai:v0.24.0 |
| `vllm_version` | v0.24.0 |
| `workload_key` | profile1,profile2,profile4 |
| `rates` | 1,50,100,200,300 |
| `guidellm_backend_type` | openai_http |

MLflow experiment: `forge-rhaiis`

## Available models

52 models are defined in `config.d/models.yaml`. Key families:

| Family | Key examples | TP size |
|--------|-------------|---------|
| Llama-4 Scout | `llama-4-scout`, `llama-4-scout-fp8`, `llama-4-scout-int4` | 2-4 |
| Llama-4 Maverick | `llama-4-maverick`, `llama-4-maverick-fp8` | 8 |
| Llama-3.3-70B | `llama-3-3-70b`, `llama-3-3-70b-fp8`, `-w8a8`, `-w4a16` | 4 |
| Llama-3.1-8B | `llama-3-1-8b`, `llama-3-1-8b-fp8`, `-w8a8`, `-w4a16` | 1 |
| Llama-3.1-405B | `llama-3-1-405b`, `llama-3-1-405b-fp8`, `-w8a8` | 8 |
| Nemotron 120B | `nemotron3super-120b-fp8` | 2 |
| Granite 3.1 8B | `granite-3-1-8b-instruct`, `-fp8`, `-w4a16`, `-w8a8` | 1 |
| Mistral Small 3.1 | `mistral-2503`, `-fp8`, `-w4a16`, `-w8a8` | 1 |
| Qwen3 235B | `qwen3-235b-instruct`, `-fp8` | 4 |
| DeepSeek | `deepseek-r1-0528`, `deepseek-v3-2`, `deepseek-v4-pro` | 8 |
| Phi-4 | `phi-4`, `phi-4-fp8`, `-w4a16`, `-w8a8` | 1 |
| Validation | `qwen3-0_6b` | 1 |

Full list: `grep "^[a-z]" orchestration/config.d/models.yaml`

## Workload profiles

| Key | Prompt tokens | Output tokens | Rates | Max seconds |
|-----|--------------|---------------|-------|-------------|
| `profile1` | 1000 | 1000 | 1, 50, 100, 200, 300 | 450 |
| `profile2` | 512 (stdev 128) | 2048 (stdev 512) | 1, 50, 100, 200, 300 | 450 |
| `profile3` | 2048 | 128 | 1, 50, 100, 200, 300 | 450 |
| `profile4` | 8000 | 1000 | 1, 25, 50, 75, 100 | 450 |

## Presets

Presets in `presets.d/presets.yaml` provide shortcuts for common configurations:

```bash
# Use presets instead of specifying model/workload/accelerator separately
python3 -m projects.rhaiis.orchestration.cli test \
  --preset llama-8b --preset profile1 \
  --namespace kserve-e2e-perf

# Available model presets: llama-8b, llama-70b, llama-405b, llama-4-scout,
#   llama-4-maverick, granite-8b, mistral-24b, qwen25-7b, qwen3-235b,
#   deepseek-r1, deepseek-v3, gpt-oss
# Workload presets: profile1, profile2, profile3, profile4
# Accelerator presets: nvidia, amd
#
# Spec decoding comparison (H200 Zeus): presets.d/spec-decoding.yaml
#   deepseek-v4-baseline | deepseek-v4-mtp | deepseek-v4-dspark | deepseek-v4-dspark-dynamic
python3 -m projects.rhaiis.orchestration.cli test \
  --preset deepseek-v4-dspark
```

Per-cluster presets (e.g. `presets.d/mehulvalidation.yaml`) set cluster-specific
defaults like namespace, image pull secret, and service account.
