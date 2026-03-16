"""Type definitions and enums for benchmark platform."""

from enum import Enum


class RoutingMode(Enum):
    """
    Routing modes for LLMInferenceService.

    - DIRECT: KServe SVC only, no EPP
    - PREFIX_ESTIMATION: EPP with kv-cache-utilization-scorer
    - PREFIX_PRECISE: EPP with prefix-cache-scorer
    - PD_DISAGGREGATION: Prefill/Decode disaggregation
    - MULTI_REPLICA: Multiple vLLM replicas with load balancing
    """
    DIRECT = "direct"
    PREFIX_ESTIMATION = "prefix-estimation"
    PREFIX_PRECISE = "prefix-precise"
    PD_DISAGGREGATION = "pd-disaggregation"
    MULTI_REPLICA = "multi-replica"


class WorkloadType(Enum):
    """Workload generation types."""
    BALANCED = "balanced"
    SHORT = "short"
    LONG_CONTEXT = "long-context"
    HETEROGENEOUS = "heterogeneous"
    MULTI_TURN = "multi_turn"


class ClusterType(Enum):
    """GPU cluster types."""
    H200 = "h200"
    A100 = "a100"
    MI300X = "mi300x"
    B200 = "b200"


class DeploymentMode(Enum):
    """
    Deployment CRD type.

    - RHOAI: Uses LLMInferenceService CRD (supports EPP routing)
    - RHAIIS: Uses KServe ServingRuntime + InferenceService (direct only)
    """
    RHOAI = "rhoai"
    RHAIIS = "rhaiis"


class AcceleratorType(Enum):
    """
    GPU accelerator vendor.

    Used for vLLM image selection and accelerator-specific env_vars.
    """
    NVIDIA = "nvidia"  # H200, A100, etc.
    AMD = "amd"        # MI300X


class ScenarioStatus(Enum):
    """Status of a scenario execution."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PARTIAL = "partial"  # Results exist but post-processing failed


class FailureReason(Enum):
    """Categorized failure reasons for auditability."""
    VLLM_STARTUP_TIMEOUT = "vllm_startup_timeout"
    VLLM_CRASH = "vllm_crash"
    GUIDELLM_TIMEOUT = "guidellm_timeout"
    GUIDELLM_ERROR = "guidellm_error"
    POST_PROCESSING_ERROR = "post_processing_error"
    KFP_SUBMISSION_ERROR = "kfp_submission_error"
    KUEUE_ADMISSION_TIMEOUT = "kueue_admission_timeout"
    UNKNOWN = "unknown"
