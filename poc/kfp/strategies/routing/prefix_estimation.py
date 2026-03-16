"""Prefix estimation routing - EPP with kv-cache-utilization-scorer."""

from .base import RoutingStrategy
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core.experiment import Experiment


class PrefixEstimationRouting(RoutingStrategy):
    """
    Prefix estimation routing mode.

    Uses EPP with kv-cache-utilization-scorer to estimate
    prefix cache utilization from vLLM metrics.
    """

    @property
    def name(self) -> str:
        return "prefix-estimation"

    def get_router_config(self, experiment: Experiment) -> str:
        """EPP router config with kv-cache-utilization-scorer."""
        return f"""router:
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
        name: {experiment.gateway_name}
        namespace: {experiment.gateway_namespace}"""

    def get_endpoint(self, deployment_name: str, namespace: str) -> str:
        """EPP Gateway endpoint."""
        return f"http://openshift-ai-inference-openshift-default.openshift-ingress.svc.cluster.local/{namespace}/{deployment_name}"

    def supports_prefix_cache_reset(self) -> bool:
        return True
