#!/usr/bin/env python3
"""
RHOAI End-to-End Benchmark Pipeline - KFP v2

Uses LLMInferenceService (KServe) for deployment.
Mounts kubeconfig secret for multi-cluster execution.
Streams vLLM logs from remote cluster to KFP dashboard.
"""

from kfp import dsl
from kfp.dsl import Output, Artifact
from kfp import kubernetes
from typing import NamedTuple


@dsl.component(base_image="python:3.9-slim")
def wait_for_kueue_admission(
    workload_name: str,
    namespace: str = "benchmark-system",
    timeout_seconds: int = 7200,
    poll_interval: int = 30,
) -> NamedTuple("Outputs", [("admitted_flavor", str), ("admission_time", str)]):
    """Wait for Kueue workload to be admitted before proceeding.

    This component runs on the management cluster using in-cluster credentials.
    It polls the Kueue workload status until admitted or timeout.

    This allows KFP runs to be immediately visible in the UI while waiting
    for GPU resources to become available.
    """
    import subprocess
    import time
    import json
    import os
    import urllib.request
    import tarfile
    from datetime import datetime

    os.environ["PYTHONUNBUFFERED"] = "1"

    # Install oc CLI to /tmp (writable location)
    print("Installing OpenShift CLI...", flush=True)
    oc_url = "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz"
    tar_path = "/tmp/oc.tar.gz"
    bin_path = "/tmp/bin"
    os.makedirs(bin_path, exist_ok=True)
    urllib.request.urlretrieve(oc_url, tar_path)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(bin_path)
    os.chmod(f"{bin_path}/oc", 0o755)
    os.environ["PATH"] = f"{bin_path}:{os.environ.get('PATH', '')}"
    print("OpenShift CLI installed.", flush=True)

    print("=" * 60, flush=True)
    print("Stage 0: Waiting for Kueue Admission", flush=True)
    print("=" * 60, flush=True)
    print(f"Workload: {workload_name}", flush=True)
    print(f"Namespace: {namespace}", flush=True)
    print(f"Timeout: {timeout_seconds}s", flush=True)
    print(f"Poll Interval: {poll_interval}s", flush=True)
    print("", flush=True)

    start = time.time()
    last_status = None

    while time.time() - start < timeout_seconds:
        elapsed = int(time.time() - start)

        # Check admission status (uses in-cluster credentials on management cluster)
        result = subprocess.run([
            "oc", "get", "workload", workload_name, "-n", namespace,
            "-o", "jsonpath={.status.admission.podSetAssignments[0].flavors}"
        ], capture_output=True, text=True)

        if result.returncode == 0 and result.stdout:
            try:
                flavors = json.loads(result.stdout)
                flavor = flavors.get("nvidia.com/gpu") or flavors.get("amd.com/gpu")
                if flavor:
                    admission_time = datetime.utcnow().isoformat()
                    print("", flush=True)
                    print("=" * 60, flush=True)
                    print("Workload ADMITTED!", flush=True)
                    print("=" * 60, flush=True)
                    print(f"Flavor: {flavor}", flush=True)
                    print(f"Time: {admission_time}", flush=True)
                    print(f"Wait Duration: {elapsed}s", flush=True)
                    print("", flush=True)
                    print("Proceeding to deployment...", flush=True)

                    from collections import namedtuple
                    Outputs = namedtuple("Outputs", ["admitted_flavor", "admission_time"])
                    return Outputs(flavor, admission_time)
            except json.JSONDecodeError:
                pass

        # Check workload conditions for status updates
        cond_result = subprocess.run([
            "oc", "get", "workload", workload_name, "-n", namespace,
            "-o", "jsonpath={.status.conditions[?(@.type=='Admitted')].status}"
        ], capture_output=True, text=True)

        current_status = cond_result.stdout.strip() if cond_result.returncode == 0 else "Unknown"
        if current_status != last_status:
            print(f"Admission status: {current_status}", flush=True)
            last_status = current_status

        # Progress update every poll interval
        remaining = timeout_seconds - elapsed
        print(f"Waiting for admission... ({elapsed}s elapsed, {remaining}s remaining)", flush=True)

        time.sleep(poll_interval)

    # Timeout - raise error to fail the pipeline
    raise TimeoutError(f"Kueue workload '{workload_name}' not admitted within {timeout_seconds}s")


@dsl.component(base_image="python:3.9-slim")
def deploy_rhoai_model(
    model_name: str,
    namespace: str,
    tp: int,
    routing_mode: str,
    kueue_queue_name: str,
    vllm_args: str,
    run_uuid: str,
    llmis_yaml: Output[Artifact],
    enable_auth: str = "false",
    replicas: int = 1,
    gateway_name: str = "openshift-ai-inference",
    gateway_namespace: str = "openshift-ingress",
) -> NamedTuple("Outputs", [("deployment_name", str), ("target_endpoint", str), ("direct_endpoint", str)]):
    """Deploy model as LLMInferenceService on target cluster."""
    import subprocess
    import os
    import urllib.request
    import tarfile

    # Force unbuffered output for KFP log streaming
    os.environ["PYTHONUNBUFFERED"] = "1"

    # Install oc CLI to /tmp (writable location)
    print("Installing OpenShift CLI...", flush=True)
    oc_url = "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz"
    tar_path = "/tmp/oc.tar.gz"
    bin_path = "/tmp/bin"
    os.makedirs(bin_path, exist_ok=True)
    urllib.request.urlretrieve(oc_url, tar_path)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(bin_path)
    os.chmod(f"{bin_path}/oc", 0o755)
    os.environ["PATH"] = f"{bin_path}:{os.environ.get('PATH', '')}"
    print("OpenShift CLI installed.", flush=True)

    print("=" * 60, flush=True)
    print("Stage 1: Deploy RHOAI Model (LLMInferenceService)", flush=True)
    print("=" * 60, flush=True)

    kubeconfig = "/kubeconfig/config"

    # Generate deployment name (ensure no trailing hyphen)
    base_name = model_name.lower().replace("/", "-").replace(".", "")[:42].rstrip("-")
    if run_uuid:
        # Use last 5 chars of uuid (more unique than first 5)
        uuid_suffix = run_uuid[-5:].strip("-")
        deployment_name = f"{base_name}-{uuid_suffix}".rstrip("-")
    else:
        deployment_name = base_name

    print(f"Model: {model_name}", flush=True)
    print(f"Deployment: {deployment_name}", flush=True)
    print(f"Namespace: {namespace}", flush=True)
    print(f"TP: {tp}", flush=True)
    print(f"Routing Mode: {routing_mode}", flush=True)
    print("", flush=True)

    def oc(args, input_data=None):
        cmd = ["oc", "--kubeconfig", kubeconfig] + args
        print(f"$ {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, input=input_data)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(f"stderr: {result.stderr}")
        return result

    # Check connectivity
    print("Checking cluster connectivity...", flush=True)
    result = oc(["cluster-info"])
    if result.returncode != 0:
        raise RuntimeError(f"Cannot connect to cluster: {result.stderr}")

    # Check if LLMInferenceService already exists
    result = oc(["get", "llminferenceservice", deployment_name, "-n", namespace])
    if result.returncode == 0:
        print(f"LLMInferenceService {deployment_name} already exists - reusing", flush=True)
        # Retrieve existing YAML for artifact
        result = oc(["get", "llminferenceservice", deployment_name, "-n", namespace, "-o", "yaml"])
        if result.returncode == 0:
            os.makedirs(os.path.dirname(llmis_yaml.path), exist_ok=True)
            with open(llmis_yaml.path, "w") as f:
                f.write(result.stdout)
            print(f"Saved existing LLMInferenceService YAML to artifact", flush=True)
    else:
        # Model URI references PVC
        model_dir_name = model_name.replace("/", "-")
        model_uri = f"pvc://models-storage/models/{model_dir_name}"

        # Build VLLM_ARGS as YAML (8-space indentation to match args list)
        vllm_args_yaml = ""
        for arg in vllm_args.split(","):
            arg = arg.strip()
            if arg:
                vllm_args_yaml += f'\n        - "{arg}"'

        # Build labels
        # NOTE: Kueue labels removed to prevent double-gating on target cluster.
        # Only management cluster Kueue should control scheduling via hollow workloads.
        extra_labels = ""
        pod_extra_labels = ""
        if run_uuid:
            extra_labels += f"\n    deployment_uuid: {run_uuid}"
            pod_extra_labels += f"\n        deployment_uuid: {run_uuid}"

        # Router config based on routing mode
        if routing_mode == "direct":
            router_config = """router:
    route: {}
    gateway: {}
    scheduler: {}"""
        elif routing_mode == "prefix-estimation":
            router_config = f"""router:
    scheduler:
      template:
        containers:
          - name: main
            args:
              - '-v=4'
              - '--cert-path'
              - /var/run/kserve/tls
              - '--pool-group'
              - inference.networking.x-k8s.io
              - '--pool-name'
              - '{{{{ ChildName .ObjectMeta.Name `-inference-pool` }}}}'
              - '--pool-namespace'
              - '{{{{ .ObjectMeta.Namespace }}}}'
              - '--zap-encoder'
              - json
              - '--grpc-port'
              - '9002'
              - '--grpc-health-port'
              - '9003'
              - '--secure-serving'
              - '--model-server-metrics-scheme'
              - https
              - '--config-text'
              - |
                apiVersion: inference.networking.x-k8s.io/v1alpha1
                kind: EndpointPickerConfig
                plugins:
                - type: single-profile-handler
                - type: queue-scorer
                - type: kv-cache-utilization-scorer
                schedulingProfiles:
                - name: default
                  plugins:
                  - pluginRef: queue-scorer
                    weight: 1
                  - pluginRef: kv-cache-utilization-scorer
                    weight: 2
    route: {{}}
    gateway:
      gatewayRef:
        name: {gateway_name}
        namespace: {gateway_namespace}"""
        elif routing_mode == "prefix-precise":
            router_config = f"""router:
    scheduler:
      template:
        containers:
          - name: main
            args:
              - '-v=4'
              - '--cert-path'
              - /var/run/kserve/tls
              - '--pool-group'
              - inference.networking.x-k8s.io
              - '--pool-name'
              - '{{{{ ChildName .ObjectMeta.Name `-inference-pool` }}}}'
              - '--pool-namespace'
              - '{{{{ .ObjectMeta.Namespace }}}}'
              - '--zap-encoder'
              - json
              - '--grpc-port'
              - '9002'
              - '--grpc-health-port'
              - '9003'
              - '--secure-serving'
              - '--model-server-metrics-scheme'
              - https
              - '--config-text'
              - |
                apiVersion: inference.networking.x-k8s.io/v1alpha1
                kind: EndpointPickerConfig
                plugins:
                - type: single-profile-handler
                - type: queue-scorer
                - type: prefix-cache-scorer
                schedulingProfiles:
                - name: default
                  plugins:
                  - pluginRef: queue-scorer
                    weight: 1
                  - pluginRef: prefix-cache-scorer
                    weight: 3
    route: {{}}
    gateway:
      gatewayRef:
        name: {gateway_name}
        namespace: {gateway_namespace}"""
        else:
            raise ValueError(f"Unknown routing mode: {routing_mode}")

        # Generate LLMInferenceService YAML
        llm_yaml = f"""apiVersion: serving.kserve.io/v1alpha1
kind: LLMInferenceService
metadata:
  name: {deployment_name}
  namespace: {namespace}
  labels:
    app: vllm-inference
    model: {deployment_name}
    deployment-mode: rhoai{extra_labels}
  annotations:
    security.opendatahub.io/enable-auth: "{enable_auth}"
    llm-d-bench/routing-mode: "{routing_mode}"
spec:
  model:
    name: {model_name}
    uri: {model_uri}
  replicas: {replicas}
  {router_config}
  template:
    metadata:
      labels:
        app: vllm-inference
        model: {deployment_name}{pod_extra_labels}
    containers:
    - name: main
      command: ["python3", "-m", "vllm.entrypoints.openai.api_server"]
      args:
        - "--port=8000"
        - "--host=0.0.0.0"
        - "--model=/mnt/models"
        - "--served-model-name={model_name}"
        - "--tensor-parallel-size={tp}"
        - "--enable-ssl-refresh"
        - "--ssl-certfile=/var/run/kserve/tls/tls.crt"
        - "--ssl-keyfile=/var/run/kserve/tls/tls.key"{vllm_args_yaml}
      resources:
        limits:
          nvidia.com/gpu: "{tp}"
        requests:
          nvidia.com/gpu: "{tp}"
      livenessProbe:
        httpGet:
          path: /health
          port: 8000
          scheme: HTTPS
        initialDelaySeconds: 400
        periodSeconds: 10
"""

        print("Generated LLMInferenceService manifest:", flush=True)
        print("=" * 60, flush=True)
        print(llm_yaml, flush=True)
        print("=" * 60, flush=True)

        # Apply
        result = oc(["apply", "-f", "-"], input_data=llm_yaml)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to apply LLMInferenceService: {result.stderr}")

        print("LLMInferenceService created successfully!", flush=True)

        # Save YAML to artifact
        os.makedirs(os.path.dirname(llmis_yaml.path), exist_ok=True)
        with open(llmis_yaml.path, "w") as f:
            f.write(llm_yaml)
        print(f"Saved LLMInferenceService YAML to artifact", flush=True)

    # Calculate endpoints
    direct_endpoint = f"https://{deployment_name}-kserve-workload-svc.{namespace}.svc.cluster.local:8000"
    epp_endpoint = f"http://{gateway_name}-openshift-default.{gateway_namespace}.svc.cluster.local/{namespace}/{deployment_name}"

    if routing_mode == "direct":
        target_endpoint = direct_endpoint
    else:
        target_endpoint = epp_endpoint

    print("", flush=True)
    print(f"DEPLOYMENT_NAME: {deployment_name}", flush=True)
    print(f"DIRECT_ENDPOINT: {direct_endpoint}", flush=True)
    print(f"TARGET_ENDPOINT: {target_endpoint}", flush=True)

    from collections import namedtuple
    Outputs = namedtuple("Outputs", ["deployment_name", "target_endpoint", "direct_endpoint"])
    return Outputs(deployment_name, target_endpoint, direct_endpoint)


@dsl.component(base_image="python:3.9-slim")
def wait_for_endpoint(
    deployment_name: str,
    target_endpoint: str,
    namespace: str,
    timeout_seconds: int = 3600,
) -> str:
    """Wait for inference endpoint to be healthy."""
    import subprocess
    import time
    import os
    import urllib.request
    import tarfile

    # Force unbuffered output for KFP log streaming
    os.environ["PYTHONUNBUFFERED"] = "1"

    # Install oc CLI to /tmp (writable location)
    print("Installing OpenShift CLI...", flush=True)
    oc_url = "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz"
    tar_path = "/tmp/oc.tar.gz"
    bin_path = "/tmp/bin"
    os.makedirs(bin_path, exist_ok=True)
    urllib.request.urlretrieve(oc_url, tar_path)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(bin_path)
    os.chmod(f"{bin_path}/oc", 0o755)
    os.environ["PATH"] = f"{bin_path}:{os.environ.get('PATH', '')}"
    print("OpenShift CLI installed.", flush=True)

    print("=" * 60, flush=True)
    print("Stage 2: Wait for Endpoint", flush=True)
    print("=" * 60, flush=True)

    kubeconfig = "/kubeconfig/config"

    print(f"Deployment: {deployment_name}", flush=True)
    print(f"Endpoint: {target_endpoint}", flush=True)
    print(f"Timeout: {timeout_seconds}s", flush=True)

    def oc(args, timeout=120):
        cmd = ["oc", "--kubeconfig", kubeconfig] + args
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    # Wait for LLMInferenceService to be ready
    print("Waiting for LLMInferenceService to be ready...", flush=True)
    start = time.time()
    while time.time() - start < timeout_seconds:
        result = oc(["get", "llminferenceservice", deployment_name, "-n", namespace,
                     "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}"])
        if result.stdout.strip() == "True":
            print("LLMInferenceService is Ready!", flush=True)
            break
        print(f"Waiting... ({int(time.time() - start)}s)", flush=True)
        time.sleep(15)

    # Test endpoint via curl from a pod
    print("", flush=True)
    print(f"Testing endpoint: {target_endpoint}/v1/models", flush=True)

    # Find the vLLM KServe pod (use KServe-specific label)
    result = oc(["get", "pods", "-n", namespace,
                 "-l", f"app.kubernetes.io/name={deployment_name}",
                 "-o", "jsonpath={.items[0].metadata.name}"])
    pod_name = result.stdout.strip()

    if pod_name:
        elapsed = 0
        while elapsed < 300:
            result = oc(["exec", "-n", namespace, pod_name, "-c", "main", "--",
                         "curl", "-sk", f"{target_endpoint}/v1/models"])
            if "data" in result.stdout:
                print("Endpoint is healthy!", flush=True)
                print(result.stdout[:500], flush=True)
                return "healthy"
            print(f"Waiting for endpoint... ({elapsed}s)", flush=True)
            time.sleep(15)
            elapsed += 15

    print("WARNING: Health check timed out", flush=True)
    return "timeout"


@dsl.component(base_image="python:3.9-slim")
def stream_vllm_logs(
    deployment_name: str,
    namespace: str,
    duration_seconds: int = 1800,
):
    """Stream vLLM pod logs from remote cluster to KFP dashboard."""
    import subprocess
    import time
    import sys
    import os
    import urllib.request
    import tarfile

    # Force unbuffered output for KFP log streaming
    os.environ["PYTHONUNBUFFERED"] = "1"

    # Install oc CLI to /tmp (writable location)
    print("Installing OpenShift CLI...", flush=True)
    oc_url = "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz"
    tar_path = "/tmp/oc.tar.gz"
    bin_path = "/tmp/bin"
    os.makedirs(bin_path, exist_ok=True)
    urllib.request.urlretrieve(oc_url, tar_path)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(bin_path)
    os.chmod(f"{bin_path}/oc", 0o755)
    os.environ["PATH"] = f"{bin_path}:{os.environ.get('PATH', '')}"
    print("OpenShift CLI installed.", flush=True)

    print("=" * 60, flush=True)
    print("Streaming vLLM Logs from Remote Cluster", flush=True)
    print("=" * 60, flush=True)

    kubeconfig = "/kubeconfig/config"

    print(f"Deployment: {deployment_name}", flush=True)
    print(f"Namespace: {namespace}", flush=True)
    print(f"Duration: {duration_seconds}s", flush=True)
    print("", flush=True)

    def oc(args, timeout=60):
        cmd = ["oc", "--kubeconfig", kubeconfig] + args
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    # Wait for vLLM KServe pod to be ready
    # Use KServe-specific label to avoid matching guidellm pods
    print("Waiting for vLLM KServe pod...", flush=True)
    pod_name = None
    start = time.time()

    while time.time() - start < 600:
        # Look for KServe inference pod (not guidellm)
        result = oc(["get", "pods", "-n", namespace,
                     "-l", f"app.kubernetes.io/name={deployment_name}",
                     "-o", "jsonpath={.items[0].metadata.name}"])
        if result.stdout.strip():
            pod_name = result.stdout.strip()
            # Check phase
            phase_result = oc(["get", "pod", pod_name, "-n", namespace,
                               "-o", "jsonpath={.status.phase}"])
            if phase_result.stdout.strip() == "Running":
                print(f"vLLM Pod {pod_name} is Running", flush=True)
                break
        print(f"Waiting for vLLM pod... ({int(time.time() - start)}s)", flush=True)
        time.sleep(10)

    if not pod_name:
        print("ERROR: Could not find vLLM KServe pod", flush=True)
        return

    print("", flush=True)
    print("=" * 60, flush=True)
    print(f"STREAMING LOGS FROM: {pod_name}", flush=True)
    print("=" * 60, flush=True)
    print("", flush=True)
    sys.stdout.flush()

    # Stream logs using Popen for real-time output
    cmd = ["oc", "--kubeconfig", kubeconfig, "logs", "-f", pod_name,
           "-n", namespace, "-c", "main"]

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        start_time = time.time()
        while time.time() - start_time < duration_seconds:
            line = process.stdout.readline()
            if line:
                print(line, end='', flush=True)
            elif process.poll() is not None:
                break

        process.terminate()
    except Exception as e:
        print(f"Log streaming ended: {e}", flush=True)

    print("", flush=True)
    print("=" * 60, flush=True)
    print("vLLM Log Streaming Complete", flush=True)
    print("=" * 60, flush=True)


@dsl.component(base_image="python:3.9-slim")
def run_guidellm_benchmark(
    deployment_name: str,
    model_name: str,
    target_endpoint: str,
    namespace: str,
    guidellm_image: str,
    guidellm_rate: str,
    guidellm_data: str,
    guidellm_max_seconds: str,
    accelerator: str,
    version: str,
    benchmark_results: Output[Artifact],
    benchmark_logs: Output[Artifact],
) -> str:
    """Run GuideLLM benchmark on target cluster."""
    import subprocess
    import time
    import os
    import sys
    import urllib.request
    import tarfile

    # Force unbuffered output for KFP log streaming
    os.environ["PYTHONUNBUFFERED"] = "1"

    # Install oc CLI to /tmp (writable location)
    print("Installing OpenShift CLI...", flush=True)
    oc_url = "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz"
    tar_path = "/tmp/oc.tar.gz"
    bin_path = "/tmp/bin"
    os.makedirs(bin_path, exist_ok=True)
    urllib.request.urlretrieve(oc_url, tar_path)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(bin_path)
    os.chmod(f"{bin_path}/oc", 0o755)
    os.environ["PATH"] = f"{bin_path}:{os.environ.get('PATH', '')}"
    print("OpenShift CLI installed.", flush=True)

    print("=" * 60, flush=True)
    print("Stage 3: Run GuideLLM Benchmark", flush=True)
    print("=" * 60, flush=True)

    kubeconfig = "/kubeconfig/config"
    benchmark_pod = f"guidellm-{deployment_name[:40]}"

    print(f"Model: {model_name}", flush=True)
    print(f"Target: {target_endpoint}", flush=True)
    print(f"Rates: {guidellm_rate}", flush=True)
    print(f"Data: {guidellm_data}", flush=True)
    print(f"Max Seconds: {guidellm_max_seconds}", flush=True)
    print(f"Benchmark Pod: {benchmark_pod}", flush=True)
    print("", flush=True)

    def oc(args, input_data=None, timeout=7200):
        cmd = ["oc", "--kubeconfig", kubeconfig] + args
        return subprocess.run(cmd, capture_output=True, text=True, input=input_data, timeout=timeout)

    # Delete existing pod if any
    oc(["delete", "pod", benchmark_pod, "-n", namespace, "--ignore-not-found"])
    time.sleep(2)

    # Create benchmark pod
    pod_yaml = f"""apiVersion: v1
kind: Pod
metadata:
  name: {benchmark_pod}
  namespace: {namespace}
  labels:
    app: guidellm-benchmark
    model: {deployment_name}
spec:
  restartPolicy: Never
  containers:
  - name: guidellm
    image: {guidellm_image}
    env:
    - name: GUIDELLM_TARGET
      value: "{target_endpoint}"
    - name: GUIDELLM_MODEL
      value: "{model_name}"
    - name: GUIDELLM_PROCESSOR
      value: "{model_name}"
    - name: GUIDELLM_BACKEND_TYPE
      value: "openai_http"
    - name: GUIDELLM_RATE_TYPE
      value: "concurrent"
    - name: GUIDELLM_RATE
      value: "{guidellm_rate}"
    - name: GUIDELLM_DATA
      value: "{guidellm_data}"
    - name: GUIDELLM_MAX_SECONDS
      value: "{guidellm_max_seconds}"
    - name: MLFLOW_ENABLED
      value: "false"
    - name: ACCELERATOR
      value: "{accelerator}"
    - name: VERSION
      value: "{version}"
    - name: SSL_CERT_FILE
      value: "/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt"
    - name: HF_HOME
      value: /tmp/.huggingface
    - name: GUIDELLM__REQUEST_TIMEOUT
      value: "1000"
    command: ["/bin/bash", "-c"]
    args:
      - |
        echo "========================================"
        echo "Starting GuideLLM Benchmark"
        echo "========================================"
        echo "Target: $GUIDELLM_TARGET"
        echo "Model: $GUIDELLM_MODEL"
        echo "Rates: $GUIDELLM_RATE"
        echo "Data: $GUIDELLM_DATA"
        echo ""

        python3 -m benchmark.main \\
          --target "$GUIDELLM_TARGET" \\
          --model "$GUIDELLM_MODEL" \\
          --rate "$GUIDELLM_RATE" \\
          --backend-type "$GUIDELLM_BACKEND_TYPE" \\
          --rate-type "$GUIDELLM_RATE_TYPE" \\
          --data "$GUIDELLM_DATA" \\
          --max-seconds $GUIDELLM_MAX_SECONDS \\
          --accelerator "$ACCELERATOR" \\
          --version "$VERSION"

        echo ""
        echo "=== BENCHMARK_RESULTS_JSON_START ==="
        if [ -f /benchmark-results/benchmark_output.json ]; then
          cat /benchmark-results/benchmark_output.json
        elif [ -f /tmp/benchmark_output.json ]; then
          cat /tmp/benchmark_output.json
        else
          echo '{{"status": "completed", "rates": "{guidellm_rate}"}}'
        fi
        echo "=== BENCHMARK_RESULTS_JSON_END ==="
    resources:
      requests:
        cpu: "2"
        memory: "4Gi"
      limits:
        cpu: "4"
        memory: "8Gi"
    volumeMounts:
    - name: results
      mountPath: /benchmark-results
  volumes:
  - name: results
    emptyDir: {{}}
"""

    print("Creating benchmark pod...", flush=True)
    result = oc(["apply", "-f", "-"], input_data=pod_yaml)
    if result.returncode != 0:
        print(f"Warning: {result.stderr}", flush=True)

    print("Waiting for pod to start...", flush=True)
    oc(["wait", f"pod/{benchmark_pod}", "-n", namespace, "--for=condition=Ready", "--timeout=300s"])

    # Stream logs in real-time
    print("", flush=True)
    print("=" * 60, flush=True)
    print("STREAMING BENCHMARK OUTPUT", flush=True)
    print("=" * 60, flush=True)
    print("", flush=True)
    sys.stdout.flush()

    cmd = ["oc", "--kubeconfig", kubeconfig, "logs", "-f", benchmark_pod, "-n", namespace]
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    logs = []
    for line in process.stdout:
        print(line, end='', flush=True)
        logs.append(line)

    process.wait()
    full_logs = ''.join(logs)

    # Extract results
    start_marker = "=== BENCHMARK_RESULTS_JSON_START ==="
    end_marker = "=== BENCHMARK_RESULTS_JSON_END ==="

    benchmark_json = '{"status": "completed"}'
    if start_marker in full_logs and end_marker in full_logs:
        start = full_logs.index(start_marker) + len(start_marker)
        end = full_logs.index(end_marker)
        benchmark_json = full_logs[start:end].strip()
        print("\nExtracted benchmark results successfully", flush=True)

    # Save artifacts
    os.makedirs(os.path.dirname(benchmark_results.path), exist_ok=True)
    with open(benchmark_results.path, "w") as f:
        f.write(benchmark_json)

    os.makedirs(os.path.dirname(benchmark_logs.path), exist_ok=True)
    with open(benchmark_logs.path, "w") as f:
        f.write(full_logs)
    print(f"Saved benchmark logs to artifact ({len(full_logs)} chars)", flush=True)

    # Cleanup pod
    print("\nCleaning up benchmark pod...", flush=True)
    oc(["delete", "pod", benchmark_pod, "-n", namespace, "--ignore-not-found"])

    return "completed"


@dsl.component(
    base_image="python:3.9-slim",
    packages_to_install=["mlflow==2.19.0", "sagemaker-mlflow", "boto3", "pandas"]
)
def upload_to_mlflow(
    benchmark_results: dsl.Input[Artifact],
    llmis_yaml: dsl.Input[Artifact],
    benchmark_logs: dsl.Input[Artifact],
    model_name: str,
    accelerator: str,
    version: str,
    tp: int,
    run_uuid: str,
    guidellm_data: str,
    guidellm_rate: str,
    batch_id: str = "",
    scenario_id: str = "",
) -> str:
    """Post-process GuideLLM results and upload to AWS SageMaker MLflow."""
    import json
    import os
    import csv
    import re
    import sys
    from typing import Any, Dict

    os.environ["PYTHONUNBUFFERED"] = "1"

    print("=" * 60, flush=True)
    print("Stage: Upload to MLflow", flush=True)
    print("=" * 60, flush=True)

    # AWS SageMaker MLflow ARN (psap-benchmark-runs app)
    MLFLOW_TRACKING_ARN = "arn:aws:sagemaker:us-east-1:194365112018:mlflow-app/app-6KQLLW4J4ZQV"

    print(f"Model: {model_name}", flush=True)
    print(f"Accelerator: {accelerator}", flush=True)
    print(f"Version: {version}", flush=True)
    print(f"Run UUID: {run_uuid}", flush=True)
    print("", flush=True)

    # Read and parse benchmark JSON from artifact
    try:
        with open(benchmark_results.path, "r") as f:
            benchmark_json = f.read()
        data = json.loads(benchmark_json)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"ERROR: Failed to read/parse benchmark JSON: {e}", flush=True)
        return "failed"

    benchmarks = data.get("benchmarks", [])
    if not benchmarks:
        print("WARNING: No benchmarks found in results", flush=True)
        return "no_benchmarks"

    # Helper to safely get nested values
    def get_nested(d: Dict[str, Any], *keys, default=None):
        for key in keys:
            if not isinstance(d, dict):
                return default
            d = d.get(key, default)
        return d

    # Extract token counts from guidellm_data
    tokens = dict(re.findall(r"(\w+)=([\d.]+)", guidellm_data))
    prompt_tokens = tokens.get("prompt_tokens", "0")
    output_tokens = tokens.get("output_tokens", "0")

    # Import MLflow
    try:
        import mlflow
        import sagemaker_mlflow
        print(f"MLflow version: {mlflow.__version__}", flush=True)
    except ImportError as e:
        print(f"ERROR: Failed to import MLflow: {e}", flush=True)
        return "mlflow_import_failed"

    # Connect to SageMaker MLflow
    print(f"Connecting to MLflow: {MLFLOW_TRACKING_ARN}", flush=True)
    mlflow.set_tracking_uri(MLFLOW_TRACKING_ARN)

    # Single experiment for all benchmark runs
    experiment_name = "psap-benchmark-runs"
    print(f"Experiment: {experiment_name}", flush=True)

    try:
        mlflow.set_experiment(experiment_name)
    except Exception as e:
        print(f"ERROR: Failed to set experiment: {e}", flush=True)
        return "experiment_failed"

    # Use scenario_id as run name if provided, otherwise fall back to run_uuid
    mlflow_run_name = scenario_id if scenario_id else run_uuid
    print(f"Run name: {mlflow_run_name}", flush=True)

    # Extract workload from scenario_id (e.g., "qwen3-0-6b_balanced_direct_tp1" -> "balanced")
    workload = ""
    if scenario_id and "_" in scenario_id:
        parts = scenario_id.split("_")
        if len(parts) >= 2:
            workload = parts[1]  # balanced, short, etc.

    # Extract args from GuideLLM output
    args = data.get("args", {})

    # Start MLflow run
    try:
        with mlflow.start_run(run_name=mlflow_run_name) as run:
            # Log tags
            mlflow.set_tag("run_uuid", run_uuid)
            mlflow.set_tag("pipeline_run_uuid", run_uuid)
            mlflow.set_tag("model", model_name)
            mlflow.set_tag("accelerator", accelerator)
            mlflow.set_tag("version", version)
            mlflow.set_tag("rate_type", args.get("rate_type", "concurrent"))

            # New tags for batch/scenario tracking
            if batch_id:
                mlflow.set_tag("batch_id", batch_id)
            if scenario_id:
                mlflow.set_tag("scenario_id", scenario_id)
            if workload:
                mlflow.set_tag("workload", workload)

            # Log parameters
            params = {
                "model": model_name,
                "tp": tp,
                "accelerator": accelerator,
                "version": version,
                "run_uuid": run_uuid,
                "rates": guidellm_rate,
                "prompt_tokens": prompt_tokens,
                "output_tokens": output_tokens,
            }
            if args.get("backend_type"):
                params["backend_type"] = args.get("backend_type")
            if args.get("target"):
                params["target"] = args.get("target")
            if args.get("max_seconds"):
                params["max_seconds"] = args.get("max_seconds")

            mlflow.log_params(params)
            print(f"Logged {len(params)} parameters", flush=True)

            # Log metrics from each benchmark (with concurrency as step)
            csv_rows = []
            for benchmark in benchmarks:
                # Get concurrency step
                config = benchmark.get("config") or benchmark.get("args", {})
                try:
                    concurrency = int(config.get("strategy", {}).get("streams", 0))
                except (KeyError, TypeError):
                    concurrency = 0

                all_metrics = benchmark.get("metrics", {})
                scheduler_metrics = benchmark.get("scheduler_metrics", {})
                run_stats = benchmark.get("run_stats", {})
                requests_made = scheduler_metrics.get("requests_made", {}) or run_stats.get("requests_made", {})

                # Extract metrics (matching llm-d-bench pattern)
                metric_map = {
                    "total_requests": requests_made.get("total"),
                    "successful_requests": requests_made.get("successful"),
                    "failed_requests": requests_made.get("errored"),
                    "throughput_requests_per_sec": get_nested(all_metrics, "requests_per_second", "successful", "mean"),
                    "total_tokens_per_second": get_nested(all_metrics, "tokens_per_second", "successful", "mean"),
                    "throughput_output_tokens_per_sec": get_nested(all_metrics, "output_tokens_per_second", "successful", "mean"),
                    "request_concurrency_mean": get_nested(all_metrics, "request_concurrency", "successful", "mean"),
                    "latency_mean_sec": get_nested(all_metrics, "request_latency", "successful", "mean"),
                    "latency_median_sec": get_nested(all_metrics, "request_latency", "successful", "median"),
                    "latency_p99_sec": get_nested(all_metrics, "request_latency", "successful", "percentiles", "p99"),
                    "ttft_mean_ms": get_nested(all_metrics, "time_to_first_token_ms", "successful", "mean"),
                    "ttft_median_ms": get_nested(all_metrics, "time_to_first_token_ms", "successful", "median"),
                    "ttft_p95_ms": get_nested(all_metrics, "time_to_first_token_ms", "successful", "percentiles", "p95"),
                    "ttft_p99_ms": get_nested(all_metrics, "time_to_first_token_ms", "successful", "percentiles", "p99"),
                    "tpot_mean_ms": get_nested(all_metrics, "time_per_output_token_ms", "successful", "mean"),
                    "tpot_median_ms": get_nested(all_metrics, "time_per_output_token_ms", "successful", "median"),
                    "tpot_p95_ms": get_nested(all_metrics, "time_per_output_token_ms", "successful", "percentiles", "p95"),
                    "tpot_p99_ms": get_nested(all_metrics, "time_per_output_token_ms", "successful", "percentiles", "p99"),
                    "itl_mean_ms": get_nested(all_metrics, "inter_token_latency_ms", "successful", "mean"),
                    "itl_median_ms": get_nested(all_metrics, "inter_token_latency_ms", "successful", "median"),
                    "itl_p95_ms": get_nested(all_metrics, "inter_token_latency_ms", "successful", "percentiles", "p95"),
                    "itl_p99_ms": get_nested(all_metrics, "inter_token_latency_ms", "successful", "percentiles", "p99"),
                }

                # Filter out None values and log
                metrics = {k: v for k, v in metric_map.items() if v is not None}

                # Add calculated metrics
                if metrics.get("total_requests", 0) > 0 and "failed_requests" in metrics:
                    metrics["error_rate"] = metrics["failed_requests"] / metrics["total_requests"]

                metrics["concurrency"] = concurrency

                # Log each metric with concurrency as step
                for key, value in metrics.items():
                    mlflow.log_metric(key, value, step=concurrency)

                print(f"Logged {len(metrics)} metrics for concurrency={concurrency}", flush=True)

                # Build CSV row
                csv_rows.append({
                    "run": f"{accelerator}-{model_name}-{tp}",
                    "accelerator": accelerator,
                    "model": model_name,
                    "version": version,
                    "prompt_toks": prompt_tokens,
                    "output_toks": output_tokens,
                    "TP": tp,
                    "measured_concurrency": metrics.get("request_concurrency_mean", ""),
                    "intended_concurrency": concurrency,
                    "measured_rps": metrics.get("throughput_requests_per_sec", ""),
                    "output_tok_per_sec": metrics.get("throughput_output_tokens_per_sec", ""),
                    "ttft_mean_ms": metrics.get("ttft_mean_ms", ""),
                    "ttft_p99_ms": metrics.get("ttft_p99_ms", ""),
                    "tpot_mean_ms": metrics.get("tpot_mean_ms", ""),
                    "tpot_p99_ms": metrics.get("tpot_p99_ms", ""),
                    "latency_mean_sec": metrics.get("latency_mean_sec", ""),
                    "latency_p99_sec": metrics.get("latency_p99_sec", ""),
                    "successful_requests": metrics.get("successful_requests", ""),
                    "failed_requests": metrics.get("failed_requests", ""),
                    "uuid": run_uuid,
                })

            # Generate and upload CSV
            if csv_rows:
                csv_path = "/tmp/benchmark_results.csv"
                fieldnames = list(csv_rows[0].keys())
                with open(csv_path, "w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(csv_rows)

                mlflow.log_artifact(csv_path, "results")
                print(f"Uploaded CSV artifact: {csv_path}", flush=True)

            # Upload raw benchmark output.json
            mlflow.log_artifact(benchmark_results.path, "results")
            print(f"Uploaded benchmark_output.json to results/", flush=True)

            # Upload LLMInferenceService YAML
            if os.path.exists(llmis_yaml.path):
                mlflow.log_artifact(llmis_yaml.path, "deployment")
                print(f"Uploaded llminferenceservice.yaml to deployment/", flush=True)

            # Upload benchmark logs
            if os.path.exists(benchmark_logs.path):
                mlflow.log_artifact(benchmark_logs.path, "logs")
                print(f"Uploaded guidellm_logs.txt to logs/", flush=True)

            run_id = run.info.run_id
            print("", flush=True)
            print("=" * 60, flush=True)
            print(f"MLflow upload complete!", flush=True)
            print(f"Run ID: {run_id}", flush=True)
            print(f"Experiment: {experiment_name}", flush=True)
            print("=" * 60, flush=True)

            return run_id

    except Exception as e:
        print(f"ERROR: MLflow upload failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return "failed"


@dsl.component(base_image="python:3.9-slim")
def cleanup_rhoai_deployment(
    deployment_name: str,
    namespace: str,
):
    """Cleanup LLMInferenceService deployment."""
    import subprocess
    import os
    import urllib.request
    import tarfile

    # Force unbuffered output for KFP log streaming
    os.environ["PYTHONUNBUFFERED"] = "1"

    # Install oc CLI to /tmp (writable location)
    print("Installing OpenShift CLI...", flush=True)
    oc_url = "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz"
    tar_path = "/tmp/oc.tar.gz"
    bin_path = "/tmp/bin"
    os.makedirs(bin_path, exist_ok=True)
    urllib.request.urlretrieve(oc_url, tar_path)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(bin_path)
    os.chmod(f"{bin_path}/oc", 0o755)
    os.environ["PATH"] = f"{bin_path}:{os.environ.get('PATH', '')}"
    print("OpenShift CLI installed.", flush=True)

    print("=" * 60, flush=True)
    print("Stage 4: Cleanup Deployment", flush=True)
    print("=" * 60, flush=True)

    kubeconfig = "/kubeconfig/config"

    def oc(args):
        cmd = ["oc", "--kubeconfig", kubeconfig] + args
        result = subprocess.run(cmd, capture_output=True, text=True)
        print(result.stdout, flush=True)
        return result

    print(f"Deleting LLMInferenceService: {deployment_name}", flush=True)
    oc(["delete", "llminferenceservice", deployment_name, "-n", namespace, "--ignore-not-found"])

    print("Waiting for cleanup...", flush=True)
    import time
    time.sleep(10)

    print("Cleanup complete!", flush=True)


@dsl.component(base_image="python:3.9-slim")
def cleanup_kueue_workload(
    workload_name: str,
    namespace: str = "benchmark-system",
):
    """Delete Kueue workload on management cluster to release quota.

    This component runs on the management cluster where KFP is installed,
    so it uses in-cluster credentials (no kubeconfig needed).
    """
    import subprocess
    import os
    import urllib.request
    import tarfile

    os.environ["PYTHONUNBUFFERED"] = "1"

    # Install oc CLI to /tmp (writable location)
    print("Installing OpenShift CLI...", flush=True)
    oc_url = "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz"
    tar_path = "/tmp/oc.tar.gz"
    bin_path = "/tmp/bin"
    os.makedirs(bin_path, exist_ok=True)
    urllib.request.urlretrieve(oc_url, tar_path)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(bin_path)
    os.chmod(f"{bin_path}/oc", 0o755)
    os.environ["PATH"] = f"{bin_path}:{os.environ.get('PATH', '')}"
    print("OpenShift CLI installed.", flush=True)

    print("=" * 60, flush=True)
    print("Stage: Cleanup Kueue Workload", flush=True)
    print("=" * 60, flush=True)
    print(f"Workload: {workload_name}", flush=True)
    print(f"Namespace: {namespace}", flush=True)

    # Use in-cluster credentials (pipeline runs on management cluster)
    result = subprocess.run(
        ["oc", "delete", "workload", workload_name, "-n", namespace, "--ignore-not-found"],
        capture_output=True, text=True
    )

    if result.returncode == 0:
        print(f"Deleted workload: {workload_name}", flush=True)
        print("Quota released!", flush=True)
    else:
        print(f"Warning: Failed to delete workload: {result.stderr}", flush=True)


# ===========================================
# Sub-Pipelines
# ===========================================

@dsl.pipeline(name="cleanup", description="Cleanup: deployment and Kueue workload removal")
def cleanup_pipeline(
    deployment_name: str,
    namespace: str,
    workload_name: str,
    skip_cleanup: bool,
):
    """Cleanup sub-pipeline: deployment and Kueue workload removal."""
    # Cleanup deployment on target cluster (conditional)
    # IMPORTANT: Disable caching to ensure cleanup always runs
    with dsl.If(skip_cleanup == False, name="cleanup-deployment"):
        cleanup_task = cleanup_rhoai_deployment(
            deployment_name=deployment_name,
            namespace=namespace,
        )
        cleanup_task.set_caching_options(enable_caching=False)
        # Note: Using literal secret name because KFP v2 doesn't pass pipeline
        # parameters into conditional blocks
        kubernetes.use_secret_as_volume(
            cleanup_task,
            secret_name="h200-kubeconfig",
            mount_path='/kubeconfig',
        )

    # Always cleanup Kueue workload to release quota
    # IMPORTANT: Disable caching to ensure cleanup always runs
    kueue_cleanup_task = cleanup_kueue_workload(
        workload_name=workload_name,
        namespace="benchmark-system",
    )
    kueue_cleanup_task.set_caching_options(enable_caching=False)


# ===========================================
# Main Pipeline Definition
# ===========================================

@dsl.pipeline(
    name="rhoai-benchmark-v2",
    description="RHOAI Benchmark with LLMInferenceService, secret mounting, and log streaming"
)
def rhoai_benchmark_pipeline(
    model_name: str = "Qwen/Qwen3-0.6B",
    namespace: str = "llm-d-bench",
    tp: int = 4,
    routing_mode: str = "direct",
    vllm_args: str = "--max-model-len=8192,--gpu-memory-utilization=0.95",
    kueue_queue_name: str = "benchmark-queue",
    run_uuid: str = "",
    guidellm_image: str = "image-registry.openshift-image-registry.svc:5000/llm-d-bench/guidellm-custom:v0.5.3",
    guidellm_rate: str = "1,10",
    guidellm_data: str = "prompt_tokens=1000,output_tokens=1000",
    guidellm_max_seconds: str = "120",
    accelerator: str = "H200",
    version: str = "RHOAI-3.2",
    mlflow_enabled: str = "false",  # Enable to upload results to AWS SageMaker MLflow
    skip_cleanup: bool = False,
    kubeconfig_secret: str = "h200-kubeconfig",
    workload_name: str = "",  # Kueue workload name for admission tracking
    kueue_admission_timeout: int = 7200,  # 2 hours default
    batch_id: str = "",  # Batch ID for MLflow grouping
    scenario_id: str = "",  # Scenario ID for MLflow run naming
):
    """
    RHOAI Benchmark Pipeline with:
    - Kueue admission waiting (immediate UI visibility)
    - LLMInferenceService deployment (KServe)
    - Kubeconfig secret mounted to all components
    - vLLM log streaming to dashboard (parallel)
    - GuideLLM benchmark execution
    """

    # Stage 0: Wait for Kueue admission (runs immediately, shows in UI)
    # This allows the pipeline to be visible while waiting for GPU resources
    admission_task = wait_for_kueue_admission(
        workload_name=workload_name,
        namespace="benchmark-system",
        timeout_seconds=kueue_admission_timeout,
        poll_interval=30,
    )
    admission_task.set_display_name("0-queue-admission")
    admission_task.set_caching_options(enable_caching=False)

    # Stage 1: Deploy (only after Kueue admission)
    deploy_task = deploy_rhoai_model(
        model_name=model_name,
        namespace=namespace,
        tp=tp,
        routing_mode=routing_mode,
        kueue_queue_name=kueue_queue_name,
        vllm_args=vllm_args,
        run_uuid=run_uuid,
    )
    deploy_task.set_display_name("1-deploy-vllm")
    kubernetes.use_secret_as_volume(
        deploy_task,
        secret_name=kubeconfig_secret,
        mount_path='/kubeconfig',
    )
    deploy_task.after(admission_task)  # Wait for Kueue admission before deploying

    # Stage 2: Wait for endpoint
    wait_task = wait_for_endpoint(
        deployment_name=deploy_task.outputs["deployment_name"],
        target_endpoint=deploy_task.outputs["target_endpoint"],
        namespace=namespace,
    )
    wait_task.set_display_name("2-wait-endpoint")
    kubernetes.use_secret_as_volume(
        wait_task,
        secret_name=kubeconfig_secret,
        mount_path='/kubeconfig',
    )

    # Stage 2a: Stream vLLM logs (starts immediately after deploy, parallel with wait)
    log_task = stream_vllm_logs(
        deployment_name=deploy_task.outputs["deployment_name"],
        namespace=namespace,
        duration_seconds=1800,
    )
    log_task.set_display_name("2-vllm-server-logs")
    log_task.after(deploy_task)  # Start streaming immediately after deployment
    kubernetes.use_secret_as_volume(
        log_task,
        secret_name=kubeconfig_secret,
        mount_path='/kubeconfig',
    )

    # Stage 3: Run benchmark (after endpoint is ready)
    benchmark_task = run_guidellm_benchmark(
        deployment_name=deploy_task.outputs["deployment_name"],
        model_name=model_name,
        target_endpoint=deploy_task.outputs["target_endpoint"],
        namespace=namespace,
        guidellm_image=guidellm_image,
        guidellm_rate=guidellm_rate,
        guidellm_data=guidellm_data,
        guidellm_max_seconds=guidellm_max_seconds,
        accelerator=accelerator,
        version=version,
    )
    benchmark_task.set_display_name("3-run-guidellm")
    benchmark_task.after(wait_task)
    kubernetes.use_secret_as_volume(
        benchmark_task,
        secret_name=kubeconfig_secret,
        mount_path='/kubeconfig',
    )

    # Stage 4: Post-Processing - Upload to MLflow (conditional)
    with dsl.If(mlflow_enabled == "true", name="post-processing"):
        mlflow_task = upload_to_mlflow(
            benchmark_results=benchmark_task.outputs["benchmark_results"],
            llmis_yaml=deploy_task.outputs["llmis_yaml"],
            benchmark_logs=benchmark_task.outputs["benchmark_logs"],
            model_name=model_name,
            accelerator=accelerator,
            version=version,
            tp=tp,
            run_uuid=run_uuid,
            guidellm_data=guidellm_data,
            guidellm_rate=guidellm_rate,
            batch_id=batch_id,
            scenario_id=scenario_id,
        )
        mlflow_task.set_display_name("4-upload-mlflow")
        mlflow_task.after(benchmark_task)
        kubernetes.use_secret_as_env(
            mlflow_task,
            secret_name="aws-credentials",
            secret_key_to_env={
                "AWS_ACCESS_KEY_ID": "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY",
                "AWS_DEFAULT_REGION": "AWS_DEFAULT_REGION",
            }
        )

    # Stage 5: Cleanup (sub-pipeline)
    cleanup_task = cleanup_pipeline(
        deployment_name=deploy_task.outputs["deployment_name"],
        namespace=namespace,
        workload_name=run_uuid,
        skip_cleanup=skip_cleanup,
    )
    cleanup_task.set_display_name("5-cleanup")
    cleanup_task.after(benchmark_task)


if __name__ == "__main__":
    from kfp import compiler
    compiler.Compiler().compile(
        pipeline_func=rhoai_benchmark_pipeline,
        package_path="rhoai_benchmark_pipeline_v2.yaml"
    )
    print("Pipeline compiled to: rhoai_benchmark_pipeline_v2.yaml")
