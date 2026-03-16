"""Cluster operations service."""

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional
import yaml

from core.cluster import Cluster


class ClusterOperationsService:
    """
    Service for cluster management operations.

    Single Responsibility: Manage cluster secrets and configuration.
    """

    def __init__(
        self,
        mgmt_kubeconfig: Optional[str] = None,
        kfp_namespace: str = "kubeflow",
        clusters_config_path: Optional[Path] = None,
    ):
        self.mgmt_kubeconfig = mgmt_kubeconfig
        self.kfp_namespace = kfp_namespace
        self.clusters_config_path = clusters_config_path or (
            Path(__file__).parent.parent / "config" / "clusters.yaml"
        )

    def _run_oc(
        self,
        args: list,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess:
        """Run oc command on management cluster."""
        if self.mgmt_kubeconfig:
            cmd = ["oc", "--kubeconfig", self.mgmt_kubeconfig] + args
        else:
            cmd = ["oc"] + args
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def create_secret(self, cluster: Cluster) -> bool:
        """
        Create kubeconfig secret in KFP namespace.

        Returns True if successful.
        """
        path = cluster.kubeconfig_path_resolved
        if not path or not path.exists():
            print(f"Error: Kubeconfig not found: {path}")
            return False

        # Delete existing secret
        self._run_oc([
            "delete", "secret", cluster.kubeconfig_secret,
            "-n", self.kfp_namespace,
            "--ignore-not-found"
        ])

        # Create new secret
        result = self._run_oc([
            "create", "secret", "generic", cluster.kubeconfig_secret,
            f"--from-file=config={path}",
            "-n", self.kfp_namespace,
        ])

        return result.returncode == 0

    def update_status(self, cluster_id: str, verified: bool) -> None:
        """
        Update cluster verification status in clusters.yaml.

        Sets verified_at timestamp if verified=True.
        """
        if not self.clusters_config_path.exists():
            return

        with open(self.clusters_config_path) as f:
            data = yaml.safe_load(f) or {}

        if "clusters" not in data or cluster_id not in data["clusters"]:
            return

        if verified:
            data["clusters"][cluster_id]["verified_at"] = datetime.utcnow().isoformat()
            data["clusters"][cluster_id]["enabled"] = True
        else:
            data["clusters"][cluster_id]["verified_at"] = None

        with open(self.clusters_config_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def add_cluster(
        self,
        cluster_id: str,
        name: str,
        kubeconfig_path: str,
        gpu_type: str = "H200",
        namespace: str = "llm-d-bench",
    ) -> Cluster:
        """
        Add new cluster to clusters.yaml.

        Returns the created Cluster object.
        """
        # Load existing config
        if self.clusters_config_path.exists():
            with open(self.clusters_config_path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        if "clusters" not in data:
            data["clusters"] = {}

        # Add cluster entry
        data["clusters"][cluster_id] = {
            "name": name,
            "kubeconfig_path": kubeconfig_path,
            "kubeconfig_secret": f"{cluster_id}-kubeconfig",
            "namespace": namespace,
            "gpu_type": gpu_type,
            "kueue_queue": "benchmark-queue",
            "enabled": False,  # Not verified yet
        }

        # Write back
        with open(self.clusters_config_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        # Return Cluster object
        return Cluster.from_dict(cluster_id, data["clusters"][cluster_id])

    def onboard_cluster(
        self,
        cluster_id: str,
        kubeconfig_path: str,
        name: Optional[str] = None,
        gpu_type: str = "H200",
        namespace: str = "llm-d-bench",
    ) -> tuple[bool, Cluster]:
        """
        Full onboarding workflow for a new cluster.

        1. Add to clusters.yaml (or update existing)
        2. Create kubeconfig secret
        3. Update verified status

        Returns (success, cluster).
        """
        # Check kubeconfig exists
        kc_path = Path(kubeconfig_path).expanduser()
        if not kc_path.exists():
            print(f"Error: Kubeconfig not found: {kc_path}")
            return False, None

        # Add or update cluster config
        cluster = self.add_cluster(
            cluster_id=cluster_id,
            name=name or cluster_id,
            kubeconfig_path=kubeconfig_path,
            gpu_type=gpu_type,
            namespace=namespace,
        )

        # Create secret
        success = self.create_secret(cluster)

        if success:
            self.update_status(cluster_id, verified=True)

        return success, cluster
