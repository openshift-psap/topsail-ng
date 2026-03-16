"""RHAIIS deployment strategy using KServe.

Generates ServingRuntime + InferenceService manifests.
Follows existing kserve.j2 template patterns from model-furnace.
"""

from typing import Dict, Any, Optional


class RHAIISDeploymentStrategy:
    """
    KServe ServingRuntime + InferenceService deployment.

    Single Responsibility: Generate KServe manifests for RHAIIS deployment.
    """

    # Accelerator-specific env_vars (auto-injected)
    # Based on model-furnace/deploy_model/templates/kserve.j2:40-43
    ACCELERATOR_ENV_VARS: Dict[str, Dict[str, str]] = {
        "amd": {
            "VLLM_USE_V1": "1",  # Required for AMD
        },
        "nvidia": {},  # No special env_vars for NVIDIA
    }

    def generate_manifests(
        self,
        model_id: str,
        deployment_name: str,
        namespace: str,
        tensor_parallel: int,
        runtime_args: Dict[str, Any],
        vllm_image: str,
        accelerator: str = "nvidia",
        model_pvc: str = "models-storage",
        model_path: str = "",
        global_env_vars: Optional[Dict[str, str]] = None,
        model_env_vars: Optional[Dict[str, str]] = None,
        batch_id: str = "",
        scenario_id: str = "",
        replicas: int = 1,
        **kwargs,
    ) -> str:
        """
        Generate ServingRuntime + InferenceService YAML.

        Args:
            model_id: HuggingFace model identifier
            deployment_name: K8s resource name (sanitized)
            namespace: Target namespace
            tensor_parallel: Number of GPUs
            runtime_args: vLLM runtime arguments
            vllm_image: Container image URL
            accelerator: "nvidia" or "amd"
            model_pvc: PVC name for model storage
            model_path: Path to model on PVC
            global_env_vars: env_vars from scenario config
            model_env_vars: env_vars from model config
            batch_id: Batch identifier for labels
            scenario_id: Scenario identifier for labels
            replicas: Number of replicas

        Returns:
            YAML string with ServingRuntime + InferenceService
        """
        # Merge env_vars: accelerator defaults → global → model
        env_vars = self._merge_env_vars(accelerator, global_env_vars, model_env_vars)

        # Format runtime args
        vllm_args_yaml = self._format_vllm_args(runtime_args)

        # Format env_vars
        env_vars_yaml = self._format_env_vars(env_vars)

        # GPU resource key based on accelerator
        gpu_resource = f"{accelerator}.com/gpu"

        # Build storage URI
        storage_uri = f"pvc://{model_pvc}{model_path}"

        manifest = f"""apiVersion: serving.kserve.io/v1alpha1
kind: ServingRuntime
metadata:
  name: {deployment_name}
  namespace: {namespace}
  labels:
    opendatahub.io/dashboard: "true"
    app: llm-benchmark
    deployment-mode: rhaiis
    batch-id: "{batch_id}"
  annotations:
    opendatahub.io/template-display-name: ServingRuntime for vLLM | llm-d-bench
spec:
  builtInAdapter:
    modelLoadingTimeoutMillis: 300000
  containers:
  - name: kserve-container
    image: {vllm_image}
    command:
    - python
    - -m
    - vllm.entrypoints.openai.api_server
    args:
    - --model=/mnt/models
    - --served-model-name={model_id}
    - --port=8080
    - --tensor-parallel-size={tensor_parallel}
{vllm_args_yaml}
    env:
    - name: HF_HUB_OFFLINE
      value: "0"
{env_vars_yaml}
    ports:
    - containerPort: 8080
      protocol: TCP
    volumeMounts:
    - name: shared-memory
      mountPath: /dev/shm
  multiModel: false
  supportedModelFormats:
  - autoSelect: true
    name: pytorch
  volumes:
  - name: shared-memory
    emptyDir:
      medium: Memory
      sizeLimit: 8Gi
---
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: {deployment_name}
  namespace: {namespace}
  labels:
    opendatahub.io/dashboard: "true"
    app: llm-benchmark
    deployment-mode: rhaiis
    batch-id: "{batch_id}"
    scenario-id: "{scenario_id}"
  annotations:
    serving.kserve.io/deploymentMode: RawDeployment
    serving.kserve.io/enable-prometheus-scraping: "true"
    prometheus.io/scrape: "true"
    prometheus.io/path: "/metrics"
    prometheus.io/port: "8080"
spec:
  predictor:
    minReplicas: {replicas}
    model:
      modelFormat:
        name: pytorch
      runtime: {deployment_name}
      storageUri: {storage_uri}
      resources:
        limits:
          {gpu_resource}: "{tensor_parallel}"
        requests:
          {gpu_resource}: "{tensor_parallel}"
"""
        return manifest

    def _merge_env_vars(
        self,
        accelerator: str,
        global_env_vars: Optional[Dict[str, str]],
        model_env_vars: Optional[Dict[str, str]],
    ) -> Dict[str, str]:
        """
        Merge env_vars from all levels.

        Order: Accelerator defaults → Global → Model (later wins on conflict)
        """
        result = dict(self.ACCELERATOR_ENV_VARS.get(accelerator, {}))
        if global_env_vars:
            result.update(global_env_vars)
        if model_env_vars:
            result.update(model_env_vars)
        return result

    def _format_vllm_args(self, runtime_args: Dict[str, Any]) -> str:
        """Format runtime_args as YAML args list."""
        if not runtime_args:
            return ""

        lines = []
        for key, val in runtime_args.items():
            # Skip tensor-parallel-size as it's already set
            if key == "tensor-parallel-size":
                continue
            if isinstance(val, bool):
                if val:
                    lines.append(f"    - --{key}")
            else:
                lines.append(f"    - --{key}={val}")
        return "\n".join(lines)

    def _format_env_vars(self, env_vars: Dict[str, str]) -> str:
        """Format env_vars as YAML."""
        if not env_vars:
            return ""

        lines = []
        for key, val in env_vars.items():
            lines.append(f"    - name: {key}")
            lines.append(f'      value: "{val}"')
        return "\n".join(lines)

    def get_endpoint_url(self, deployment_name: str, namespace: str) -> str:
        """KServe predictor endpoint pattern."""
        return f"http://{deployment_name}-predictor.{namespace}.svc.cluster.local:8080"

    def supports_routing(self) -> bool:
        """RHAIIS doesn't support EPP routing."""
        return False

    def get_resource_kind(self) -> str:
        """Primary resource kind."""
        return "InferenceService"

    @staticmethod
    def sanitize_name(model_id: str) -> str:
        """
        Sanitize model ID for K8s resource name.

        Same logic as kserve.j2 template:
        lowercase | replace / with - | remove . | truncate to 42 chars
        """
        return model_id.lower().replace("/", "-").replace(".", "")[:42]
