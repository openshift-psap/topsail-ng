"""Artifact collection utilities for failure handling and auditability."""

import subprocess
import json
from datetime import datetime
from typing import Dict, Optional, List
from pathlib import Path
import tempfile
import os


class ArtifactCollector:
    """
    Collects artifacts from Kubernetes and uploads to S3.

    Used for:
    - Failure debugging (pod logs, events, describe)
    - Auditability (config snapshots, execution logs)
    """

    def __init__(
        self,
        kubeconfig_path: Optional[str] = None,
        s3_bucket: str = "sagemaker-us-east-1-194365112018",
    ):
        self.kubeconfig_path = kubeconfig_path
        self.s3_bucket = s3_bucket

    def _kubectl(self, *args, timeout: int = 30) -> str:
        """Run kubectl command and return output."""
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
            if result.returncode != 0:
                return f"ERROR: {result.stderr}"
            return result.stdout
        except subprocess.TimeoutExpired:
            return f"ERROR: kubectl command timed out after {timeout}s"
        except Exception as e:
            return f"ERROR: {str(e)}"

    def collect_pod_logs(
        self,
        namespace: str,
        pod_name: Optional[str] = None,
        label_selector: Optional[str] = None,
        container: Optional[str] = None,
        tail_lines: int = 1000,
    ) -> str:
        """
        Collect logs from pod(s).

        Args:
            namespace: Kubernetes namespace
            pod_name: Specific pod name (optional if using label_selector)
            label_selector: Label selector (e.g., "app=vllm")
            container: Specific container name (optional)
            tail_lines: Number of lines to tail

        Returns:
            Pod logs as string
        """
        args = ["logs", "-n", namespace, f"--tail={tail_lines}"]

        if pod_name:
            args.append(pod_name)
        elif label_selector:
            args.extend(["-l", label_selector])
        else:
            return "ERROR: Must provide pod_name or label_selector"

        if container:
            args.extend(["-c", container])

        # Add timestamp prefix
        header = f"=== Pod Logs collected at {datetime.utcnow().isoformat()} ===\n"
        header += f"Namespace: {namespace}, Pod: {pod_name or label_selector}\n"
        header += "=" * 60 + "\n"

        return header + self._kubectl(*args)

    def collect_pod_events(
        self,
        namespace: str,
        field_selector: Optional[str] = None,
    ) -> str:
        """
        Collect events from namespace.

        Args:
            namespace: Kubernetes namespace
            field_selector: Optional field selector (e.g., "involvedObject.name=my-pod")

        Returns:
            Events as string
        """
        args = ["get", "events", "-n", namespace, "-o", "wide", "--sort-by=.lastTimestamp"]

        if field_selector:
            args.extend(["--field-selector", field_selector])

        header = f"=== Events collected at {datetime.utcnow().isoformat()} ===\n"
        header += f"Namespace: {namespace}\n"
        header += "=" * 60 + "\n"

        return header + self._kubectl(*args)

    def collect_pod_describe(
        self,
        namespace: str,
        pod_name: Optional[str] = None,
        label_selector: Optional[str] = None,
    ) -> str:
        """
        Collect pod describe output.

        Args:
            namespace: Kubernetes namespace
            pod_name: Specific pod name
            label_selector: Label selector

        Returns:
            Pod describe output as string
        """
        args = ["describe", "pod", "-n", namespace]

        if pod_name:
            args.append(pod_name)
        elif label_selector:
            args.extend(["-l", label_selector])
        else:
            return "ERROR: Must provide pod_name or label_selector"

        header = f"=== Pod Describe collected at {datetime.utcnow().isoformat()} ===\n"
        header += f"Namespace: {namespace}, Pod: {pod_name or label_selector}\n"
        header += "=" * 60 + "\n"

        return header + self._kubectl(*args)

    def collect_pod_yaml(
        self,
        namespace: str,
        pod_name: Optional[str] = None,
        label_selector: Optional[str] = None,
    ) -> str:
        """
        Collect pod YAML definition.
        """
        args = ["get", "pod", "-n", namespace, "-o", "yaml"]

        if pod_name:
            args.append(pod_name)
        elif label_selector:
            args.extend(["-l", label_selector])

        return self._kubectl(*args)

    def collect_isvc_status(self, namespace: str, isvc_name: str) -> str:
        """
        Collect InferenceService status.
        """
        args = ["get", "inferenceservice", isvc_name, "-n", namespace, "-o", "yaml"]
        return self._kubectl(*args)

    def collect_failure_bundle(
        self,
        namespace: str,
        pod_selector: str,
        isvc_name: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Collect complete failure debugging bundle.

        Returns:
            Dictionary mapping artifact name to content
        """
        artifacts = {}

        # Pod logs
        artifacts["vllm-pod-logs.txt"] = self.collect_pod_logs(
            namespace=namespace,
            label_selector=pod_selector,
            tail_lines=2000,
        )

        # Pod events
        artifacts["pod-events.txt"] = self.collect_pod_events(namespace=namespace)

        # Pod describe
        artifacts["pod-describe.txt"] = self.collect_pod_describe(
            namespace=namespace,
            label_selector=pod_selector,
        )

        # Pod YAML
        artifacts["pod-yaml.yaml"] = self.collect_pod_yaml(
            namespace=namespace,
            label_selector=pod_selector,
        )

        # InferenceService status (if provided)
        if isvc_name:
            artifacts["isvc-status.yaml"] = self.collect_isvc_status(
                namespace=namespace,
                isvc_name=isvc_name,
            )

        return artifacts

    def upload_to_s3(
        self,
        artifacts: Dict[str, str],
        s3_prefix: str,
    ) -> List[str]:
        """
        Upload artifacts to S3.

        Args:
            artifacts: Dictionary mapping filename to content
            s3_prefix: S3 path prefix (e.g., "psap-benchmark-runs/batch-xxx/scenario-xxx")

        Returns:
            List of S3 URIs for uploaded files
        """
        uploaded = []

        with tempfile.TemporaryDirectory() as tmpdir:
            for filename, content in artifacts.items():
                local_path = Path(tmpdir) / filename
                local_path.write_text(content)

                s3_uri = f"s3://{self.s3_bucket}/{s3_prefix}/{filename}"

                try:
                    result = subprocess.run(
                        ["aws", "s3", "cp", str(local_path), s3_uri],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if result.returncode == 0:
                        uploaded.append(s3_uri)
                    else:
                        print(f"WARNING: Failed to upload {filename}: {result.stderr}")
                except Exception as e:
                    print(f"WARNING: Failed to upload {filename}: {e}")

        return uploaded

    def save_execution_log(
        self,
        batch_id: str,
        scenarios: List[Dict],
        s3_prefix: str,
    ) -> Optional[str]:
        """
        Save execution log to S3.

        Args:
            batch_id: Batch identifier
            scenarios: List of scenario dictionaries
            s3_prefix: S3 path prefix

        Returns:
            S3 URI of uploaded file
        """
        log_content = {
            "batch_id": batch_id,
            "generated_at": datetime.utcnow().isoformat(),
            "scenarios": scenarios,
        }

        artifacts = {
            "execution_log.json": json.dumps(log_content, indent=2, default=str)
        }

        uploaded = self.upload_to_s3(artifacts, s3_prefix)
        return uploaded[0] if uploaded else None

    def save_config_snapshot(
        self,
        config_content: str,
        expanded_scenarios: List[Dict],
        s3_prefix: str,
    ) -> List[str]:
        """
        Save configuration snapshot for reproducibility.

        Args:
            config_content: Original YAML content
            expanded_scenarios: Post-matrix-expansion scenario list
            s3_prefix: S3 path prefix

        Returns:
            List of S3 URIs
        """
        artifacts = {
            "scenarios.yaml": config_content,
            "expanded_scenarios.json": json.dumps(expanded_scenarios, indent=2, default=str),
        }

        return self.upload_to_s3(artifacts, f"{s3_prefix}/config")
