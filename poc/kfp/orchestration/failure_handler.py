"""Failure handling for benchmark scenarios."""

import time
import subprocess
from typing import Optional, Tuple, List, Callable
from datetime import datetime

from .artifact_collector import ArtifactCollector


class FailureHandler:
    """
    Handles failures during scenario execution.

    Responsibilities:
    - Wait for vLLM pod readiness with timeout
    - Collect artifacts on failure
    - Categorize failure reasons
    - Enable graceful continuation to next scenario
    """

    def __init__(
        self,
        artifact_collector: ArtifactCollector,
        vllm_startup_timeout: int = 3600,      # 1 hour
        vllm_poll_interval: int = 30,          # 30 seconds
        guidellm_timeout: int = 7200,          # 2 hours
        kubeconfig_path: Optional[str] = None,
    ):
        self.artifact_collector = artifact_collector
        self.vllm_startup_timeout = vllm_startup_timeout
        self.vllm_poll_interval = vllm_poll_interval
        self.guidellm_timeout = guidellm_timeout
        self.kubeconfig_path = kubeconfig_path

    def _kubectl(self, *args, timeout: int = 30) -> Tuple[int, str, str]:
        """Run kubectl command and return (returncode, stdout, stderr)."""
        cmd = ["kubectl"]
        if self.kubeconfig_path:
            cmd.extend(["--kubeconfig", self.kubeconfig_path])
        cmd.extend(args)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Command timed out after {timeout}s"
        except Exception as e:
            return -1, "", str(e)

    def wait_for_vllm_ready(
        self,
        namespace: str,
        pod_selector: str,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Wait for vLLM pod to be ready.

        Args:
            namespace: Kubernetes namespace
            pod_selector: Label selector for vLLM pod
            on_progress: Optional callback for progress updates

        Returns:
            Tuple of (success, error_message)
        """
        start_time = time.time()
        last_status = ""

        while (time.time() - start_time) < self.vllm_startup_timeout:
            # Check pod status
            rc, stdout, stderr = self._kubectl(
                "get", "pods", "-n", namespace,
                "-l", pod_selector,
                "-o", "jsonpath={.items[0].status.phase}",
            )

            if rc == 0:
                phase = stdout.strip()

                # Check if running
                if phase == "Running":
                    # Verify container is ready
                    rc2, ready, _ = self._kubectl(
                        "get", "pods", "-n", namespace,
                        "-l", pod_selector,
                        "-o", "jsonpath={.items[0].status.containerStatuses[0].ready}",
                    )
                    if ready.strip() == "true":
                        if on_progress:
                            on_progress("vLLM pod is ready")
                        return True, None

                # Check for failure states
                if phase in ("Failed", "Error"):
                    return False, f"Pod entered {phase} state"

                # Progress update
                if phase != last_status:
                    last_status = phase
                    if on_progress:
                        elapsed = int(time.time() - start_time)
                        on_progress(f"Pod status: {phase} ({elapsed}s elapsed)")

            # Check for pod crash/restart
            rc3, restarts, _ = self._kubectl(
                "get", "pods", "-n", namespace,
                "-l", pod_selector,
                "-o", "jsonpath={.items[0].status.containerStatuses[0].restartCount}",
            )
            if rc3 == 0 and restarts.strip():
                restart_count = int(restarts.strip())
                if restart_count > 2:
                    return False, f"Pod crashed {restart_count} times"

            time.sleep(self.vllm_poll_interval)

        return False, f"Timeout after {self.vllm_startup_timeout}s waiting for vLLM"

    def handle_vllm_failure(
        self,
        namespace: str,
        pod_selector: str,
        isvc_name: Optional[str],
        artifacts_path: str,
        error_message: str,
    ) -> Tuple[str, str, List[str]]:
        """
        Handle vLLM startup failure.

        Args:
            namespace: Kubernetes namespace
            pod_selector: Label selector for vLLM pod
            isvc_name: InferenceService name (optional)
            artifacts_path: S3 path for artifacts
            error_message: Error message from wait_for_vllm_ready

        Returns:
            Tuple of (failure_reason, failure_message, artifact_urls)
        """
        # Determine failure reason
        if "timeout" in error_message.lower():
            reason = "vllm_startup_timeout"
        elif "crash" in error_message.lower():
            reason = "vllm_crash"
        else:
            reason = "vllm_startup_error"

        # Collect artifacts
        artifacts = self.artifact_collector.collect_failure_bundle(
            namespace=namespace,
            pod_selector=pod_selector,
            isvc_name=isvc_name,
        )

        # Upload to S3
        artifact_urls = self.artifact_collector.upload_to_s3(
            artifacts=artifacts,
            s3_prefix=artifacts_path.replace("s3://", "").split("/", 1)[1] if "s3://" in artifacts_path else artifacts_path,
        )

        return reason, error_message, artifact_urls

    def handle_guidellm_failure(
        self,
        namespace: str,
        guidellm_pod_selector: str,
        artifacts_path: str,
        error_message: str,
        partial_results_path: Optional[str] = None,
    ) -> Tuple[str, str, List[str]]:
        """
        Handle GuideLLM execution failure.

        Args:
            namespace: Kubernetes namespace
            guidellm_pod_selector: Label selector for GuideLLM pod
            artifacts_path: S3 path for artifacts
            error_message: Error description
            partial_results_path: Path to partial results JSON (if any)

        Returns:
            Tuple of (failure_reason, failure_message, artifact_urls)
        """
        if "timeout" in error_message.lower():
            reason = "guidellm_timeout"
        else:
            reason = "guidellm_error"

        # Collect GuideLLM pod logs
        artifacts = {
            "guidellm-logs.txt": self.artifact_collector.collect_pod_logs(
                namespace=namespace,
                label_selector=guidellm_pod_selector,
                tail_lines=2000,
            ),
        }

        # Include partial results if available
        if partial_results_path:
            try:
                import json
                with open(partial_results_path, 'r') as f:
                    artifacts["partial-results.json"] = f.read()
            except Exception:
                pass

        # Upload to S3
        artifact_urls = self.artifact_collector.upload_to_s3(
            artifacts=artifacts,
            s3_prefix=artifacts_path.replace("s3://", "").split("/", 1)[1] if "s3://" in artifacts_path else artifacts_path,
        )

        return reason, error_message, artifact_urls

    def handle_kfp_failure(
        self,
        error_message: str,
        artifacts_path: str,
    ) -> Tuple[str, str, List[str]]:
        """
        Handle KFP submission failure.

        Returns:
            Tuple of (failure_reason, failure_message, artifact_urls)
        """
        reason = "kfp_submission_error"

        artifacts = {
            "kfp-error.txt": f"KFP Submission Error\n{'-'*40}\n{error_message}\nTimestamp: {datetime.utcnow().isoformat()}\n",
        }

        artifact_urls = self.artifact_collector.upload_to_s3(
            artifacts=artifacts,
            s3_prefix=artifacts_path.replace("s3://", "").split("/", 1)[1] if "s3://" in artifacts_path else artifacts_path,
        )

        return reason, error_message, artifact_urls

    def cleanup_failed_scenario(
        self,
        namespace: str,
        workload_name: str,
        isvc_name: Optional[str] = None,
    ) -> None:
        """
        Clean up resources after a failed scenario.

        Args:
            namespace: Kubernetes namespace
            workload_name: Kueue workload name
            isvc_name: InferenceService name (optional)
        """
        # Delete Kueue workload
        self._kubectl(
            "delete", "workload", workload_name,
            "-n", namespace,
            "--ignore-not-found=true",
        )

        # Delete InferenceService if specified
        if isvc_name:
            self._kubectl(
                "delete", "inferenceservice", isvc_name,
                "-n", namespace,
                "--ignore-not-found=true",
            )
