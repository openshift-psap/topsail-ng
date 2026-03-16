"""Cluster validation service."""

import base64
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from core.cluster import Cluster
from core.protocols import CheckResult


class ClusterValidationService:
    """
    Service for validating cluster configurations.

    Single Responsibility: Validate cluster connectivity and requirements.
    Fetches kubeconfig from K8s secret (no local file dependency).
    """

    def __init__(self, mgmt_kubeconfig: Optional[str] = None, kfp_namespace: str = "kubeflow"):
        self.mgmt_kubeconfig = mgmt_kubeconfig
        self.kfp_namespace = kfp_namespace
        self._kubeconfig_cache: dict[str, Path] = {}  # secret_name -> temp file path

    def _run_oc(
        self,
        args: List[str],
        kubeconfig: Optional[str] = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess:
        """Run oc command with specified kubeconfig."""
        kubeconfig = kubeconfig or self.mgmt_kubeconfig
        if kubeconfig:
            cmd = ["oc", "--kubeconfig", kubeconfig] + args
        else:
            cmd = ["oc"] + args
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _get_kubeconfig_from_secret(self, secret_name: str) -> Optional[Path]:
        """
        Fetch kubeconfig from K8s secret and cache in temp file.

        Returns path to temp file containing kubeconfig, or None if fetch fails.
        """
        # Check cache first
        if secret_name in self._kubeconfig_cache:
            cached_path = self._kubeconfig_cache[secret_name]
            if cached_path.exists():
                return cached_path

        try:
            # Fetch base64-encoded kubeconfig from secret
            result = self._run_oc([
                "get", "secret", secret_name,
                "-n", self.kfp_namespace,
                "-o", "jsonpath={.data.config}"
            ])

            if result.returncode != 0 or not result.stdout.strip():
                return None

            # Decode and write to temp file
            kubeconfig_content = base64.b64decode(result.stdout.strip()).decode()

            # Create temp file (persists for duration of validation)
            temp_file = Path(tempfile.gettempdir()) / f"kc-{secret_name}.yaml"
            temp_file.write_text(kubeconfig_content)

            self._kubeconfig_cache[secret_name] = temp_file
            return temp_file

        except Exception:
            return None

    def _get_cluster_kubeconfig(self, cluster: Cluster) -> Optional[str]:
        """
        Get kubeconfig for cluster - tries secret first, then local file.

        Returns path to kubeconfig file or None.
        """
        # Try fetching from secret first (preferred)
        if cluster.kubeconfig_secret:
            secret_path = self._get_kubeconfig_from_secret(cluster.kubeconfig_secret)
            if secret_path:
                return str(secret_path)

        # Fallback to local file if configured
        if cluster.kubeconfig_path:
            local_path = cluster.kubeconfig_path_resolved
            if local_path and local_path.exists():
                return str(local_path)

        return None

    def validate(self, cluster: Cluster) -> List[CheckResult]:
        """Run all validation checks on a cluster."""
        results = []
        results.append(self.check_kubeconfig_secret(cluster))
        results.append(self.check_kubeconfig_available(cluster))
        results.append(self.check_connectivity(cluster))
        results.append(self.check_namespace(cluster))
        results.append(self.check_gpu_nodes(cluster))
        results.append(self.check_llm_crd(cluster))
        results.append(self.check_kueue_queue(cluster))
        return results

    def check_kubeconfig_available(self, cluster: Cluster) -> CheckResult:
        """Check if kubeconfig is available (from secret or local file)."""
        kubeconfig = self._get_cluster_kubeconfig(cluster)
        if kubeconfig:
            # Check source
            if cluster.kubeconfig_secret and cluster.kubeconfig_secret in str(kubeconfig):
                return CheckResult(
                    name="Kubeconfig Available",
                    passed=True,
                    message=f"Retrieved from secret '{cluster.kubeconfig_secret}'",
                )
            else:
                return CheckResult(
                    name="Kubeconfig Available",
                    passed=True,
                    message=f"Local file: {kubeconfig}",
                )
        return CheckResult(
            name="Kubeconfig Available",
            passed=False,
            message="No kubeconfig available",
            details=f"Secret '{cluster.kubeconfig_secret}' not found or empty",
        )

    def check_kubeconfig_secret(self, cluster: Cluster) -> CheckResult:
        """Check if kubeconfig secret exists in KFP namespace."""
        try:
            result = self._run_oc([
                "get", "secret", cluster.kubeconfig_secret,
                "-n", self.kfp_namespace,
                "-o", "jsonpath={.metadata.creationTimestamp}"
            ])

            if result.returncode == 0:
                return CheckResult(
                    name="Kubeconfig Secret",
                    passed=True,
                    message=f"Secret '{cluster.kubeconfig_secret}' exists",
                )
            return CheckResult(
                name="Kubeconfig Secret",
                passed=False,
                message=f"Secret '{cluster.kubeconfig_secret}' not found",
                details=result.stderr[:200] if result.stderr else None,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                name="Kubeconfig Secret",
                passed=False,
                message="Timeout checking secret",
            )
        except Exception as e:
            return CheckResult(
                name="Kubeconfig Secret",
                passed=False,
                message=f"Error: {e}",
            )

    def check_connectivity(self, cluster: Cluster) -> CheckResult:
        """Check if cluster is reachable."""
        kubeconfig = self._get_cluster_kubeconfig(cluster)
        if not kubeconfig:
            return CheckResult(
                name="Connectivity",
                passed=False,
                message="No kubeconfig available",
            )

        try:
            result = self._run_oc(
                ["cluster-info"],
                kubeconfig=kubeconfig,
                timeout=15,
            )

            if result.returncode == 0:
                lines = result.stdout.split("\n")
                api_line = next((l for l in lines if "Kubernetes" in l), "")
                return CheckResult(
                    name="Connectivity",
                    passed=True,
                    message="Connected successfully",
                    details=api_line[:80] if api_line else None,
                )
            return CheckResult(
                name="Connectivity",
                passed=False,
                message="Connection failed",
                details=result.stderr[:200] if result.stderr else None,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                name="Connectivity",
                passed=False,
                message="Connection timeout",
            )
        except Exception as e:
            return CheckResult(
                name="Connectivity",
                passed=False,
                message=f"Error: {e}",
            )

    def check_namespace(self, cluster: Cluster) -> CheckResult:
        """Check if target namespace exists on cluster."""
        kubeconfig = self._get_cluster_kubeconfig(cluster)
        if not kubeconfig:
            return CheckResult(
                name="Target Namespace",
                passed=False,
                message="No kubeconfig available",
            )

        try:
            result = self._run_oc(
                ["get", "namespace", cluster.namespace, "-o", "name"],
                kubeconfig=kubeconfig,
            )

            if result.returncode == 0:
                return CheckResult(
                    name="Target Namespace",
                    passed=True,
                    message=f"Namespace '{cluster.namespace}' exists",
                )
            return CheckResult(
                name="Target Namespace",
                passed=False,
                message=f"Namespace '{cluster.namespace}' not found",
                details=f"Create with: oc create namespace {cluster.namespace}",
            )
        except Exception as e:
            return CheckResult(
                name="Target Namespace",
                passed=False,
                message=f"Error: {e}",
            )

    def check_gpu_nodes(self, cluster: Cluster) -> CheckResult:
        """Check if GPU nodes are available."""
        kubeconfig = self._get_cluster_kubeconfig(cluster)
        if not kubeconfig:
            return CheckResult(
                name="GPU Nodes",
                passed=False,
                message="No kubeconfig available",
            )

        try:
            # Check for NVIDIA GPUs
            result = self._run_oc(
                ["get", "nodes", "-o", "jsonpath={.items[*].status.capacity.nvidia\\.com/gpu}"],
                kubeconfig=kubeconfig,
            )

            if result.returncode == 0 and result.stdout.strip():
                gpus = [g for g in result.stdout.split() if g and g != "0"]
                if gpus:
                    total = sum(int(g) for g in gpus)
                    return CheckResult(
                        name="GPU Nodes",
                        passed=True,
                        message=f"Found {len(gpus)} nodes with {total} GPUs",
                    )

            # Fallback: check node labels
            result = self._run_oc(
                ["get", "nodes", "-l", f"nvidia.com/gpu.product={cluster.gpu_type}",
                 "-o", "jsonpath={.items[*].metadata.name}"],
                kubeconfig=kubeconfig,
            )

            if result.returncode == 0 and result.stdout.strip():
                nodes = result.stdout.split()
                return CheckResult(
                    name="GPU Nodes",
                    passed=True,
                    message=f"Found {len(nodes)} {cluster.gpu_type} nodes",
                )

            return CheckResult(
                name="GPU Nodes",
                passed=False,
                message=f"No {cluster.gpu_type} GPU nodes found",
                details="Check node labels and GPU operator",
            )
        except Exception as e:
            return CheckResult(
                name="GPU Nodes",
                passed=False,
                message=f"Error: {e}",
            )

    def check_llm_crd(self, cluster: Cluster) -> CheckResult:
        """Check if LLMInferenceService CRD is installed."""
        kubeconfig = self._get_cluster_kubeconfig(cluster)
        if not kubeconfig:
            return CheckResult(
                name="LLMInferenceService CRD",
                passed=False,
                message="No kubeconfig available",
            )

        try:
            result = self._run_oc(
                ["get", "crd", "llminferenceservices.serving.kserve.io", "-o", "name"],
                kubeconfig=kubeconfig,
            )

            if result.returncode == 0:
                return CheckResult(
                    name="LLMInferenceService CRD",
                    passed=True,
                    message="CRD installed",
                )
            return CheckResult(
                name="LLMInferenceService CRD",
                passed=False,
                message="CRD not found",
                details="Install KServe/RHAIIS",
            )
        except Exception as e:
            return CheckResult(
                name="LLMInferenceService CRD",
                passed=False,
                message=f"Error: {e}",
            )

    def check_kueue_queue(self, cluster: Cluster) -> CheckResult:
        """Check if Kueue ClusterQueue exists on management cluster."""
        try:
            result = self._run_oc([
                "get", "clusterqueue", cluster.kueue_queue, "-o", "name"
            ])

            if result.returncode == 0:
                return CheckResult(
                    name="Kueue ClusterQueue",
                    passed=True,
                    message=f"ClusterQueue '{cluster.kueue_queue}' exists",
                )
            return CheckResult(
                name="Kueue ClusterQueue",
                passed=False,
                message=f"ClusterQueue '{cluster.kueue_queue}' not found",
                details="Create ClusterQueue with hollow flavor",
            )
        except Exception as e:
            return CheckResult(
                name="Kueue ClusterQueue",
                passed=False,
                message=f"Error: {e}",
            )
