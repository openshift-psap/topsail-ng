#!/usr/bin/env python3
"""
Cluster onboarding and management for benchmark pipeline.

Usage:
    python cluster_manager.py list                     # List all clusters and status
    python cluster_manager.py check <cluster-id>      # Run sanity checks
    python cluster_manager.py onboard <cluster-id>    # Full onboarding workflow
    python cluster_manager.py add <cluster-id>        # Add new cluster interactively
    python cluster_manager.py secret <cluster-id>     # Create/update kubeconfig secret
"""

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.cluster import Cluster
from core.platform import PlatformConfig
from core.protocols import CheckResult
from registry.cluster_registry import ClusterRegistry
from services.cluster_validation import ClusterValidationService
from services.cluster_operations import ClusterOperationsService

# Configuration paths
CONFIG_DIR = Path(__file__).parent / "config"
CLUSTERS_CONFIG = CONFIG_DIR / "clusters.yaml"
PLATFORM_CONFIG = CONFIG_DIR / "platform.yaml"


class ClusterManager:
    """
    Manages cluster onboarding and validation.

    Uses ClusterRegistry for configuration and services for operations.
    """

    def __init__(self):
        # Load platform config
        self.platform = PlatformConfig.load(PLATFORM_CONFIG)

        # Initialize registry
        self.registry = ClusterRegistry(CLUSTERS_CONFIG, self.platform)

        # Initialize services
        self.validation = ClusterValidationService(
            mgmt_kubeconfig=self.platform.mgmt_kubeconfig or None,
            kfp_namespace=self.platform.kfp.namespace,
        )
        self.operations = ClusterOperationsService(
            mgmt_kubeconfig=self.platform.mgmt_kubeconfig or None,
            kfp_namespace=self.platform.kfp.namespace,
            clusters_config_path=CLUSTERS_CONFIG,
        )

    def list_clusters(self) -> None:
        """List all configured clusters with status."""
        print("=" * 70)
        print("Configured Clusters")
        print("=" * 70)
        print()

        clusters = self.registry.list_all()
        if not clusters:
            print("No clusters configured. Use 'cluster_manager.py add <cluster-id>' to add one.")
            return

        for cluster in clusters:
            status = self._quick_status(cluster)
            status_icon = "[OK]" if status else "[??]"
            enabled_icon = "" if cluster.enabled else " (disabled)"

            print(f"{status_icon} {cluster.id}{enabled_icon}")
            print(f"    Name:       {cluster.name}")
            print(f"    GPU Type:   {cluster.gpu_type}")
            print(f"    Namespace:  {cluster.namespace}")
            print(f"    Secret:     {cluster.kubeconfig_secret}")
            print(f"    Kubeconfig: {cluster.kubeconfig_path or '(not set)'}")
            if cluster.verified_at:
                print(f"    Verified:   {cluster.verified_at}")
            print()

        print("-" * 70)
        print(f"Default cluster: {self.platform.default_cluster}")
        print("Run 'cluster_manager.py check <cluster-id>' for detailed validation")
        print()

    def _quick_status(self, cluster: Cluster) -> bool:
        """Quick check if cluster secret exists."""
        result = self.validation.check_kubeconfig_secret(cluster)
        return result.passed

    def check_cluster(self, cluster_id: str) -> List[CheckResult]:
        """Run comprehensive sanity checks on a cluster."""
        if not self.registry.exists(cluster_id):
            print(f"Error: Cluster '{cluster_id}' not found in config.")
            print(f"Available clusters: {', '.join(c.id for c in self.registry.list_all())}")
            return []

        cluster = self.registry.get(cluster_id)
        results = self.validation.validate(cluster)

        print("=" * 70)
        print(f"Cluster Validation: {cluster_id}")
        print("=" * 70)
        print(f"Name:      {cluster.name}")
        print(f"GPU Type:  {cluster.gpu_type}")
        print(f"Namespace: {cluster.namespace}")
        print()

        # Print results
        passed = sum(1 for r in results if r.passed)
        total = len(results)

        for r in results:
            icon = "[PASS]" if r.passed else "[FAIL]"
            print(f"  {icon} {r.name}: {r.message}")
            if r.details and not r.passed:
                for line in str(r.details).split("\n")[:3]:
                    print(f"         {line}")

        print()
        print("-" * 70)
        print(f"Results: {passed}/{total} checks passed")
        print()

        if passed == total:
            print(f"Cluster '{cluster_id}' is ready for benchmarking!")
        else:
            print(f"Cluster '{cluster_id}' has issues.")
            print(f"Run 'cluster_manager.py onboard {cluster_id}' to fix.")

        return results

    def onboard_cluster(
        self,
        cluster_id: str,
        kubeconfig_path: Optional[str] = None,
    ) -> bool:
        """Full onboarding workflow for a cluster."""
        print("=" * 70)
        print(f"Onboarding Cluster: {cluster_id}")
        print("=" * 70)
        print()

        # Check if cluster exists
        if self.registry.exists(cluster_id):
            cluster = self.registry.get(cluster_id)
            print(f"Found existing config for '{cluster_id}'")

            # Use provided kubeconfig or existing
            if kubeconfig_path:
                kc_path = Path(kubeconfig_path).expanduser()
            elif cluster.kubeconfig_path:
                kc_path = cluster.kubeconfig_path_resolved
            else:
                print("Error: No kubeconfig path. Provide --kubeconfig.")
                return False
        else:
            if not kubeconfig_path:
                print(f"Error: Cluster '{cluster_id}' not in config.")
                print(f"Provide --kubeconfig or use 'add' command first.")
                return False

            kc_path = Path(kubeconfig_path).expanduser()
            print(f"Will create new cluster config for '{cluster_id}'")

        # Step 1: Validate kubeconfig file
        print("\n[1/4] Validating kubeconfig file...")
        if not kc_path.exists():
            print(f"  ERROR: Kubeconfig not found: {kc_path}")
            return False
        print(f"  Found: {kc_path}")

        # Step 2: Test connectivity
        print("\n[2/4] Testing cluster connectivity...")
        if self.registry.exists(cluster_id):
            cluster = self.registry.get(cluster_id)
        else:
            # Create temporary cluster for testing
            cluster = Cluster(
                id=cluster_id,
                name=cluster_id,
                kubeconfig_path=str(kubeconfig_path),
                kubeconfig_secret=f"{cluster_id}-kubeconfig",
            )

        result = self.validation.check_connectivity(cluster)
        if not result.passed:
            print(f"  ERROR: {result.message}")
            if result.details:
                print(f"  {result.details[:200]}")
            return False
        print("  Connected successfully")

        # Step 3: Create kubeconfig secret
        print(f"\n[3/4] Creating kubeconfig secret '{cluster.kubeconfig_secret}'...")
        if not self.registry.exists(cluster_id):
            # Add cluster to config first
            cluster = self.operations.add_cluster(
                cluster_id=cluster_id,
                name=cluster_id,
                kubeconfig_path=str(kubeconfig_path),
            )

        success = self.operations.create_secret(cluster)
        if not success:
            print("  ERROR: Failed to create secret")
            return False
        print("  Secret created")

        # Step 4: Update status
        print(f"\n[4/4] Updating cluster status...")
        self.operations.update_status(cluster_id, verified=True)
        print("  Status updated")

        # Reload registry and run validation
        self.registry = ClusterRegistry(CLUSTERS_CONFIG, self.platform)

        print("\n" + "=" * 70)
        print("Running validation checks...")
        print("=" * 70)
        results = self.check_cluster(cluster_id)

        passed = sum(1 for r in results if r.passed)
        return passed == len(results)

    def add_cluster(self, cluster_id: str) -> None:
        """Interactive add of new cluster."""
        print("=" * 70)
        print(f"Add New Cluster: {cluster_id}")
        print("=" * 70)
        print()

        if self.registry.exists(cluster_id):
            print(f"Cluster '{cluster_id}' already exists. Use 'onboard' to update.")
            return

        # Gather info interactively
        print("Enter cluster details (press Enter for defaults):\n")

        name = input(f"  Display name [{cluster_id}]: ").strip() or cluster_id

        default_kc = os.path.expanduser(f"~/.kube/{cluster_id}-kubeconfig")
        kubeconfig_path = input(f"  Kubeconfig path [{default_kc}]: ").strip() or default_kc

        gpu_type = input("  GPU type (H200/A100/MI300X) [H200]: ").strip().upper() or "H200"

        namespace = input(f"  Target namespace [{self.platform.default_namespace}]: ").strip()
        namespace = namespace or self.platform.default_namespace

        # Add cluster
        cluster = self.operations.add_cluster(
            cluster_id=cluster_id,
            name=name,
            kubeconfig_path=kubeconfig_path,
            gpu_type=gpu_type,
            namespace=namespace,
        )

        print(f"\nCluster '{cluster_id}' added to {CLUSTERS_CONFIG}")
        print(f"\nNext steps:")
        print(f"  1. Place kubeconfig at: {kubeconfig_path}")
        print(f"  2. Run: python cluster_manager.py onboard {cluster_id}")

    def create_secret(self, cluster_id: str) -> bool:
        """Create/update kubeconfig secret for cluster."""
        if not self.registry.exists(cluster_id):
            print(f"Error: Cluster '{cluster_id}' not found in config.")
            return False

        cluster = self.registry.get(cluster_id)
        print(f"Creating secret '{cluster.kubeconfig_secret}'...")

        success = self.operations.create_secret(cluster)

        if success:
            print("Secret created successfully")
        else:
            print("Failed to create secret")

        return success


def main():
    parser = argparse.ArgumentParser(
        description="Cluster onboarding and management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cluster_manager.py list
  python cluster_manager.py check h200-cluster
  python cluster_manager.py add new-cluster
  python cluster_manager.py onboard new-cluster --kubeconfig ~/.kube/new-config
  python cluster_manager.py secret h200-cluster
        """
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # List command
    subparsers.add_parser("list", help="List all clusters and status")

    # Check command
    check_parser = subparsers.add_parser("check", help="Run sanity checks on cluster")
    check_parser.add_argument("cluster_id", help="Cluster ID to check")

    # Onboard command
    onboard_parser = subparsers.add_parser("onboard", help="Full onboarding workflow")
    onboard_parser.add_argument("cluster_id", help="Cluster ID to onboard")
    onboard_parser.add_argument("--kubeconfig", "-k", help="Path to kubeconfig file")

    # Add command
    add_parser = subparsers.add_parser("add", help="Add new cluster interactively")
    add_parser.add_argument("cluster_id", help="Cluster ID to add")

    # Secret command
    secret_parser = subparsers.add_parser("secret", help="Create/update kubeconfig secret")
    secret_parser.add_argument("cluster_id", help="Cluster ID")

    args = parser.parse_args()
    manager = ClusterManager()

    if args.command == "list":
        manager.list_clusters()
    elif args.command == "check":
        results = manager.check_cluster(args.cluster_id)
        sys.exit(0 if all(r.passed for r in results) else 1)
    elif args.command == "onboard":
        success = manager.onboard_cluster(args.cluster_id, args.kubeconfig)
        sys.exit(0 if success else 1)
    elif args.command == "add":
        manager.add_cluster(args.cluster_id)
    elif args.command == "secret":
        success = manager.create_secret(args.cluster_id)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
