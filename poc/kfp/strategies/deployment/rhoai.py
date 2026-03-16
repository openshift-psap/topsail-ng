"""RHOAI deployment strategy using LLMInferenceService.

Wraps existing deployment logic for consistency with strategy pattern.
"""

from typing import Dict, Any


class RHOAIDeploymentStrategy:
    """
    LLMInferenceService deployment for RHOAI.

    Single Responsibility: Generate LLMInferenceService manifests.
    Note: Actual deployment is handled by pipeline component;
    this strategy provides configuration and endpoint patterns.
    """

    def generate_manifests(
        self,
        model_id: str,
        deployment_name: str,
        namespace: str,
        tensor_parallel: int,
        runtime_args: Dict[str, Any],
        routing_mode: str = "direct",
        kueue_queue: str = "benchmark-queue",
        enable_auth: bool = False,
        replicas: int = 1,
        batch_id: str = "",
        scenario_id: str = "",
        **kwargs,
    ) -> str:
        """
        Generate LLMInferenceService YAML.

        Note: This is primarily for documentation/preview.
        The actual deployment is handled by the deploy_rhoai_model pipeline component.
        """
        # Format runtime args
        vllm_args_yaml = self._format_vllm_args(runtime_args)

        manifest = f"""apiVersion: serving.kserve.io/v1alpha1
kind: LLMInferenceService
metadata:
  name: {deployment_name}
  namespace: {namespace}
  labels:
    app: llm-benchmark
    deployment-mode: rhoai
    batch-id: "{batch_id}"
    scenario-id: "{scenario_id}"
    kueue.x-k8s.io/queue-name: {kueue_queue}
spec:
  modelSpec:
    modelArtifact:
      uri: hf://{model_id}
    acceleratorType: gpu
    numGpus: {tensor_parallel}
    autoScaling:
      minReplicas: {replicas}
      maxReplicas: {replicas}
    vllmConfig:
      vllmArgs:
{vllm_args_yaml}
"""
        return manifest

    def _format_vllm_args(self, runtime_args: Dict[str, Any]) -> str:
        """Format runtime_args as YAML vllmArgs list."""
        if not runtime_args:
            return ""

        lines = []
        for key, val in runtime_args.items():
            if isinstance(val, bool):
                if val:
                    lines.append(f"      - --{key}")
            else:
                lines.append(f"      - --{key}={val}")
        return "\n".join(lines)

    def get_endpoint_url(
        self,
        deployment_name: str,
        namespace: str,
        routing_mode: str = "direct",
    ) -> str:
        """
        Get endpoint URL based on routing mode.

        For direct mode: KServe service URL
        For EPP modes: EPP endpoint URL
        """
        if routing_mode == "direct":
            return f"http://{deployment_name}-predictor.{namespace}.svc.cluster.local:8080"
        else:
            # EPP endpoint (placeholder - actual URL determined at runtime)
            return f"http://{deployment_name}-epp.{namespace}.svc.cluster.local:8080"

    def supports_routing(self) -> bool:
        """RHOAI supports EPP routing modes."""
        return True

    def get_resource_kind(self) -> str:
        """Primary resource kind."""
        return "LLMInferenceService"

    @staticmethod
    def sanitize_name(model_id: str) -> str:
        """Sanitize model ID for K8s resource name."""
        return model_id.lower().replace("/", "-").replace(".", "")[:42]
