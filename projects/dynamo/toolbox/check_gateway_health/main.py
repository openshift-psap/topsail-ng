"""
Dynamo Gateway Stack Health Check

Automated 6-step diagnostic that walks the inference data path bottom-up:
  Gateway proxy → Gateway programmed → HTTPRoute accepted →
  EPP running → ext_proc connectivity → EPP→Worker routing

Stops at the first failure and prints the fix. Designed for the three
gateway controllers we encounter: agentgateway, kgateway, Istio.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

from projects.core.dsl import entrypoint, execute_tasks, task
from projects.core.dsl.utils.k8s import oc, oc_get_json

logger = logging.getLogger(__name__)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[34mINFO\033[0m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _print_step(num: int, title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  Step {num}: {title}")
    print(f"{'='*60}")


def _print_result(status: str, message: str) -> None:
    print(f"  [{status}] {message}")


def _print_fix(title: str, command: str) -> None:
    print(f"  {BOLD}Fix — {title}{RESET}")
    for line in command.strip().split("\n"):
        print(f"    $ {line}")


def _get_pods_by_label(namespace: str, label: str) -> list[dict]:
    data = oc_get_json("pods", namespace=namespace, selector=label, ignore_not_found=True)
    if not data:
        return []
    return data.get("items", [])


def _get_conditions(obj: dict) -> dict[str, dict]:
    conditions = {}
    for c in obj.get("status", {}).get("conditions", []):
        conditions[c["type"]] = c
    return conditions


@entrypoint
def run(
    *,
    namespace: str,
    gateway_name: str = "inference-gateway",
    httproute_name: str | None = None,
    model_name: str = "Qwen/Qwen3-0.6B",
    artifact_dir: Path | None = None,
    skip_smoke: bool = False,
) -> int:
    execute_tasks(locals())
    return 0


@task
def step_1_gateway_proxy_pod(args, ctx):
    """Check if the gateway proxy pod is running."""
    _print_step(1, "Gateway proxy pod running?")

    label = f"gateway.networking.k8s.io/gateway-name={args.gateway_name}"
    pods = _get_pods_by_label(args.namespace, label)

    if not pods:
        _print_result(FAIL, "No gateway proxy pods found")
        _print_result(INFO, f"Label selector: {label}")

        # Check if GatewayClass exists
        result = oc("get", "gatewayclass", "--no-headers", check=False)
        if result.returncode != 0 or not result.stdout.strip():
            _print_fix(
                "No GatewayClass — install a gateway controller",
                "# agentgateway (recommended for Dynamo):\n"
                "helm upgrade -i agentgateway-crds oci://cr.agentgateway.dev/charts/agentgateway-crds \\\n"
                "  --create-namespace --namespace agentgateway-system --version v1.0.0\n"
                "helm upgrade -i agentgateway oci://cr.agentgateway.dev/charts/agentgateway \\\n"
                "  --namespace agentgateway-system --version v1.0.0 \\\n"
                "  --set inferenceExtension.enabled=true --wait",
            )
        else:
            _print_result(INFO, f"GatewayClasses found:\n{result.stdout.strip()}")
            _print_fix(
                "Gateway exists but no pods — check Gateway resource",
                f"oc get gateway {args.gateway_name} -n {args.namespace} -o yaml",
            )
        ctx.healthy = False
        return

    pod = pods[0]
    pod_name = pod["metadata"]["name"]
    phase = pod["status"].get("phase", "Unknown")
    containers = pod["status"].get("containerStatuses", [])
    ready = all(c.get("ready", False) for c in containers)
    restarts = sum(c.get("restartCount", 0) for c in containers)

    if phase == "Running" and ready:
        _print_result(PASS, f"Pod {pod_name} is Running and Ready (restarts={restarts})")
        ctx.healthy = True
        ctx.gw_pod_name = pod_name
        return

    if phase == "Running" and not ready:
        _print_result(WARN, f"Pod {pod_name} Running but NOT Ready (restarts={restarts})")
        if restarts > 3:
            _print_result(FAIL, "CrashLoopBackOff — checking logs for root cause")
            _diagnose_gateway_crash(args.namespace, pod_name)
        ctx.healthy = False
        return

    # Check events for SCC issues
    events = oc(
        "get", "events", "-n", args.namespace,
        "--field-selector", f"reason=FailedCreate",
        "--sort-by=.metadata.creationTimestamp",
        "-o", "json", check=False,
    )
    if events.returncode == 0:
        event_data = json.loads(events.stdout)
        for event in event_data.get("items", []):
            msg = event.get("message", "")
            if "forbidden" in msg.lower() and args.gateway_name in event.get("involvedObject", {}).get("name", ""):
                if "NET_BIND_SERVICE" in msg:
                    _print_result(FAIL, "SCC blocks NET_BIND_SERVICE capability")
                    _print_fix(
                        "Allow NET_BIND_SERVICE in dynamo-frontend-scc",
                        f"oc patch scc dynamo-frontend-scc --type merge \\\n"
                        f"  -p '{{\"allowedCapabilities\":[\"NET_BIND_SERVICE\"]}}'\n"
                        f"oc adm policy add-scc-to-user dynamo-frontend-scc \\\n"
                        f"  -z {args.gateway_name} -n {args.namespace}\n"
                        f"oc rollout restart deployment {args.gateway_name} -n {args.namespace}",
                    )
                elif "runAsUser" in msg or "fsGroup" in msg:
                    uid_match = re.search(r"Invalid value: (?:\[)?(\d+)", msg)
                    uid = uid_match.group(1) if uid_match else "?"
                    _print_result(FAIL, f"SCC blocks runAsUser/fsGroup {uid}")
                    _print_fix(
                        "Grant SCC to gateway service account",
                        f"oc adm policy add-scc-to-user dynamo-frontend-scc \\\n"
                        f"  -z {args.gateway_name} -n {args.namespace}\n"
                        f"oc rollout restart deployment {args.gateway_name} -n {args.namespace}",
                    )
                else:
                    _print_result(FAIL, f"SCC rejection: {msg[:120]}...")
                ctx.healthy = False
                return

    _print_result(FAIL, f"Pod {pod_name} in phase {phase}")
    ctx.healthy = False


def _diagnose_gateway_crash(namespace: str, pod_name: str) -> None:
    logs = oc("logs", pod_name, "-n", namespace, "--previous", "--tail=50", check=False)
    if logs.returncode != 0:
        logs = oc("logs", pod_name, "-n", namespace, "--tail=50", check=False)
    if logs.returncode != 0:
        _print_result(INFO, "Could not retrieve logs")
        return

    log_text = logs.stdout or ""

    if "too many open files" in log_text.lower() or "socket" in log_text.lower() and "failed" in log_text.lower():
        _print_result(FAIL, "File descriptor exhaustion — too many clusters loaded")
        _print_fix(
            "Switch from kgateway to agentgateway",
            "# kgateway loads ALL cluster endpoints and exhausts fd limits on busy clusters.\n"
            "# Install agentgateway instead:\n"
            "helm upgrade -i agentgateway oci://cr.agentgateway.dev/charts/agentgateway \\\n"
            "  --namespace agentgateway-system --version v1.0.0 \\\n"
            "  --set inferenceExtension.enabled=true --wait\n"
            "# Then recreate Gateway with gatewayClassName: agentgateway",
        )
        return

    if "x509" in log_text or "certificate" in log_text.lower() and "unknown authority" in log_text.lower():
        _print_result(FAIL, "Istio CA certificate mismatch")
        _print_fix(
            "Compare CA fingerprints (requires team coordination to fix)",
            "# What pods trust:\n"
            "oc get cm istio-ca-root-cert -n istio-system \\\n"
            "  -o jsonpath='{.data.root-cert\\.pem}' | openssl x509 -noout -fingerprint\n"
            "# What istiod signs with:\n"
            "oc get secret istio-ca-secret -n istio-system \\\n"
            "  -o jsonpath='{.data.ca-cert\\.pem}' | base64 -d | openssl x509 -noout -fingerprint\n"
            "# If different → CA was rotated. Delete istio-ca-secret + restart istiod.",
        )
        return

    # Generic crash
    error_lines = [l for l in log_text.split("\n") if any(k in l.lower() for k in ["error", "fatal", "critical", "panic", "abort"])]
    if error_lines:
        _print_result(INFO, "Error lines from logs:")
        for line in error_lines[:5]:
            print(f"    {line.strip()[:120]}")
    else:
        _print_result(INFO, "No obvious error pattern — check full logs:")
        print(f"    $ oc logs {pod_name} -n {namespace} --previous")


@task
def step_2_gateway_programmed(args, ctx):
    """Check if the Gateway resource is programmed."""
    if not getattr(ctx, "healthy", True):
        _print_step(2, "Gateway programmed?")
        _print_result(WARN, "Skipped — gateway pod not healthy (Step 1)")
        return

    _print_step(2, "Gateway programmed?")

    gw_data = oc_get_json(
        f"gateway/{args.gateway_name}", namespace=args.namespace, ignore_not_found=True
    )

    if not gw_data:
        _print_result(FAIL, f"Gateway '{args.gateway_name}' not found in namespace {args.namespace}")
        _print_fix(
            "Create a Gateway",
            f"cat <<EOF | oc apply -n {args.namespace} -f -\n"
            "apiVersion: gateway.networking.k8s.io/v1\n"
            "kind: Gateway\n"
            "metadata:\n"
            f"  name: {args.gateway_name}\n"
            "spec:\n"
            "  gatewayClassName: agentgateway\n"
            "  listeners:\n"
            "    - name: http\n"
            "      port: 80\n"
            "      protocol: HTTP\n"
            "      allowedRoutes:\n"
            "        namespaces:\n"
            "          from: Same\n"
            "EOF",
        )
        ctx.healthy = False
        return

    conditions = _get_conditions(gw_data)
    programmed = conditions.get("Programmed", {})
    accepted = conditions.get("Accepted", {})

    if accepted.get("status") != "True":
        _print_result(FAIL, f"Gateway not accepted: {accepted.get('message', '?')}")
        gw_class = gw_data["spec"]["gatewayClassName"]
        _print_fix(
            f"GatewayClass '{gw_class}' may not exist or controller is down",
            f"oc get gatewayclass {gw_class}",
        )
        ctx.healthy = False
        return

    if programmed.get("status") == "True":
        gw_class = gw_data["spec"]["gatewayClassName"]
        _print_result(PASS, f"Gateway programmed (class={gw_class})")
        ctx.gateway_class = gw_class
    else:
        _print_result(FAIL, f"Gateway not programmed: {programmed.get('message', '?')}")
        ctx.healthy = False


@task
def step_3_httproute_accepted(args, ctx):
    """Check if the HTTPRoute is accepted and refs resolved."""
    if not getattr(ctx, "healthy", True):
        _print_step(3, "HTTPRoute accepted?")
        _print_result(WARN, "Skipped — earlier step failed")
        return

    _print_step(3, "HTTPRoute accepted?")

    # Find HTTPRoutes in namespace
    route_data = oc_get_json("httproutes", namespace=args.namespace, ignore_not_found=True)
    if not route_data or not route_data.get("items"):
        _print_result(FAIL, f"No HTTPRoutes found in {args.namespace}")
        _print_fix(
            "Create an HTTPRoute pointing to the InferencePool",
            f"cat <<EOF | oc apply -n {args.namespace} -f -\n"
            "apiVersion: gateway.networking.k8s.io/v1\n"
            "kind: HTTPRoute\n"
            "metadata:\n"
            "  name: dynamo-route\n"
            "spec:\n"
            "  parentRefs:\n"
            "    - group: gateway.networking.k8s.io\n"
            "      kind: Gateway\n"
            f"      name: {args.gateway_name}\n"
            "  rules:\n"
            "    - matches:\n"
            "        - path:\n"
            "            type: PathPrefix\n"
            "            value: /\n"
            "      backendRefs:\n"
            "        - group: inference.networking.k8s.io\n"
            "          kind: InferencePool\n"
            "          name: POOL_NAME\n"
            "          port: 8000\n"
            "EOF",
        )
        ctx.healthy = False
        return

    routes = route_data["items"]
    target_route = None
    if args.httproute_name:
        target_route = next((r for r in routes if r["metadata"]["name"] == args.httproute_name), None)
    else:
        target_route = routes[0]

    if not target_route:
        _print_result(FAIL, f"HTTPRoute '{args.httproute_name}' not found")
        ctx.healthy = False
        return

    route_name = target_route["metadata"]["name"]
    all_ok = True

    for parent in target_route.get("status", {}).get("parents", []):
        gw_name = parent.get("parentRef", {}).get("name", "?")
        conditions = {c["type"]: c for c in parent.get("conditions", [])}

        accepted = conditions.get("Accepted", {})
        resolved = conditions.get("ResolvedRefs", {})

        if accepted.get("status") != "True":
            msg = accepted.get("message", "?")
            _print_result(FAIL, f"Route '{route_name}' NOT accepted by gateway '{gw_name}': {msg[:80]}")
            if "namespace" in msg.lower() and "not allowed" in msg.lower():
                _print_fix(
                    "Gateway listener restricts namespaces",
                    f"oc get gateway {gw_name} -n $(oc get httproute {route_name} -n {args.namespace} \\\n"
                    f"  -o jsonpath='{{.spec.parentRefs[0].namespace}}') \\\n"
                    f"  -o jsonpath='{{.spec.listeners[0].allowedRoutes.namespaces}}'",
                )
            all_ok = False
        elif resolved.get("status") != "True":
            msg = resolved.get("message", "?")
            _print_result(FAIL, f"Route '{route_name}' refs NOT resolved on '{gw_name}': {msg[:80]}")
            if "InferencePool" in msg and "not found" in msg:
                _print_fix(
                    "InferencePool doesn't exist yet",
                    f"oc get inferencepools -n {args.namespace}\n"
                    f"# Dynamo operator creates the pool from DGD. Check DGD status:\n"
                    f"oc get dynamographdeployment -n {args.namespace} -o yaml | grep -A5 state:",
                )
            all_ok = False
        else:
            _print_result(PASS, f"Route '{route_name}' accepted by '{gw_name}', refs resolved")

    # Extract pool name for later steps
    for rule in target_route["spec"].get("rules", []):
        for ref in rule.get("backendRefs", []):
            if ref.get("kind") == "InferencePool":
                ctx.pool_name = ref["name"]
                break

    ctx.httproute_name = route_name
    if not all_ok:
        ctx.healthy = False


@task
def step_4_epp_running(args, ctx):
    """Check if the EPP pod is running and healthy."""
    if not getattr(ctx, "healthy", True):
        _print_step(4, "EPP running?")
        _print_result(WARN, "Skipped — earlier step failed")
        return

    _print_step(4, "EPP running?")

    pods = _get_pods_by_label(args.namespace, "nvidia.com/dynamo-component-type=epp")

    if not pods:
        _print_result(FAIL, "No EPP pods found")
        _print_fix(
            "Check DGD has an Epp service defined",
            f"oc get dynamographdeployment -n {args.namespace} -o yaml | grep -A3 'componentType: epp'",
        )
        ctx.healthy = False
        return

    pod = pods[0]
    pod_name = pod["metadata"]["name"]
    phase = pod["status"].get("phase", "Unknown")
    containers = pod["status"].get("containerStatuses", [])
    ready = all(c.get("ready", False) for c in containers)
    restarts = sum(c.get("restartCount", 0) for c in containers)

    if phase == "Running" and ready:
        _print_result(PASS, f"EPP pod {pod_name} Running and Ready (restarts={restarts})")
        ctx.epp_pod = pod_name
        return

    if phase == "Running" and not ready:
        _print_result(WARN, f"EPP pod {pod_name} Running but NOT Ready (restarts={restarts})")
        _print_result(INFO, "EPP startup probe waits for worker discovery — can take up to 30 min for large models")

        # Check if workers exist
        workers = _get_pods_by_label(args.namespace, "nvidia.com/dynamo-component-class=worker")
        ready_workers = [w for w in workers if w["status"].get("phase") == "Running"
                         and all(c.get("ready", False) for c in w["status"].get("containerStatuses", []))]

        if not workers:
            _print_result(FAIL, "No worker pods found — EPP can't discover endpoints")
        elif not ready_workers:
            _print_result(WARN, f"{len(workers)} worker pod(s) exist but none fully ready (2/2)")
            _print_result(INFO, "Wait for workers to be 2/2 Ready, then EPP will pass startup probe")
        else:
            _print_result(INFO, f"{len(ready_workers)} worker(s) ready — EPP should discover them soon")
        ctx.epp_pod = pod_name
        return

    # Check for SCC
    _print_result(FAIL, f"EPP pod in phase {phase}")
    _print_fix(
        "Grant SCC to EPP service account",
        f"oc adm policy add-scc-to-user dynamo-frontend-scc \\\n"
        f"  -z epp-serviceaccount -n {args.namespace}\n"
        f"oc rollout restart deployment -n {args.namespace} \\\n"
        f"  $(oc get deploy -n {args.namespace} -l nvidia.com/dynamo-component-type=epp -o name)",
    )
    ctx.healthy = False


@task
def step_5_extproc_connectivity(args, ctx):
    """Check if gateway proxy can reach EPP via ext_proc."""
    if not getattr(ctx, "healthy", True):
        _print_step(5, "Gateway → EPP connectivity (ext_proc)?")
        _print_result(WARN, "Skipped — earlier step failed")
        return

    _print_step(5, "Gateway → EPP connectivity (ext_proc)?")

    # Try hitting /v1/models through the gateway
    gw_svc = f"inference-gateway.{args.namespace}.svc.cluster.local"
    result = oc(
        "run", "gw-check", "-n", args.namespace,
        "--rm", "-i", "--restart=Never",
        "--image=curlimages/curl:8.11.1",
        "--", "curl", "-s", "--max-time", "10",
        f"http://{gw_svc}/v1/models",
        check=False,
    )

    response = result.stdout or ""

    if result.returncode == 0 and "data" in response:
        _print_result(PASS, "Gateway → EPP → Worker path is healthy")
        try:
            models = json.loads(response)
            model_ids = [m.get("id", "?") for m in models.get("data", [])]
            _print_result(INFO, f"Models available: {', '.join(model_ids)}")
        except json.JSONDecodeError:
            pass
        return

    if result.returncode == 0 and response == "":
        _print_result(FAIL, "500 empty body — likely Istio sidecar intercepting ext_proc gRPC")

        # Check if gateway proxy pod has istio-proxy container
        gw_pod = getattr(ctx, "gw_pod_name", None)
        if gw_pod:
            pod_data = oc_get_json(f"pod/{gw_pod}", namespace=args.namespace, ignore_not_found=True)
            if pod_data:
                container_names = [c["name"] for c in pod_data["spec"]["containers"]]
                if "istio-proxy" in container_names:
                    _print_result(FAIL, f"Confirmed: istio-proxy sidecar present on {gw_pod}")
                    _print_fix(
                        "Exclude Istio sidecar from gateway proxy",
                        "# For agentgateway — use AgentgatewayParameters:\n"
                        f"oc apply --server-side -n {args.namespace} -f - <<'EOF'\n"
                        "apiVersion: agentgateway.dev/v1alpha1\n"
                        "kind: AgentgatewayParameters\n"
                        "metadata:\n"
                        "  name: inference-gateway-params\n"
                        "spec:\n"
                        "  deployment:\n"
                        "    spec:\n"
                        "      template:\n"
                        "        metadata:\n"
                        "          annotations:\n"
                        '            sidecar.istio.io/inject: "false"\n'
                        "EOF",
                    )
        ctx.healthy = False
        return

    # Connection refused or timeout
    _print_result(FAIL, f"Gateway not reachable (exit={result.returncode})")
    stderr = result.stderr or ""
    if "Connection refused" in response or "Connection refused" in stderr:
        _print_result(INFO, "Gateway proxy not listening — check Step 1")
    elif "timed out" in response.lower() or "timed out" in stderr.lower():
        _print_result(INFO, "Request timed out — EPP may be overloaded or unreachable")
    else:
        _print_result(INFO, f"Response: {response[:100]}")
    ctx.healthy = False


@task
def step_6_inference_smoke(args, ctx):
    """Send a smoke inference request through the full stack."""
    if not getattr(ctx, "healthy", True) or args.skip_smoke:
        _print_step(6, "Inference smoke test")
        if args.skip_smoke:
            _print_result(INFO, "Skipped (--skip-smoke)")
        else:
            _print_result(WARN, "Skipped — earlier step failed")
        return

    _print_step(6, "Inference smoke test")

    gw_svc = f"inference-gateway.{args.namespace}.svc.cluster.local"
    payload = json.dumps({
        "model": args.model_name,
        "prompt": "test",
        "max_tokens": 5,
        "temperature": 0,
    })

    result = oc(
        "run", "smoke-check", "-n", args.namespace,
        "--rm", "-i", "--restart=Never",
        "--image=curlimages/curl:8.11.1",
        "--", "curl", "-s", "--max-time", "30",
        "-X", "POST",
        f"http://{gw_svc}/v1/completions",
        "-H", "Content-Type: application/json",
        "-d", payload,
        check=False,
    )

    response = result.stdout or ""

    if result.returncode == 0 and response:
        if '"choices"' in response:
            try:
                data = json.loads(response)
                tokens = data.get("usage", {}).get("total_tokens", "?")
            except json.JSONDecodeError:
                tokens = "?"
            _print_result(PASS, f"Inference working — {tokens} tokens generated")
            return
        try:
            data = json.loads(response)
            if "message" in data and "Worker ID required" in data["message"]:
                _print_result(FAIL, "Worker requires --router-mode direct but EPP didn't set routing headers")
                _print_result(INFO, "EPP ext_proc may not be wired — check InferencePool endpointPickerRef")
                ctx.healthy = False
                return
            if "message" in data and "RoutingFailed" in data.get("message", ""):
                _print_result(FAIL, "EPP RoutingFailed — can't find eligible workers")
                _print_fix(
                    "Check worker discovery",
                    f"oc logs -n {args.namespace} -l nvidia.com/dynamo-component-type=epp --tail=20 \\\n"
                    f"  | grep -i 'discover\\|instance\\|added\\|snapshot'\n"
                    f"oc get dynamoworkermetadatas -n {args.namespace}",
                )
                ctx.healthy = False
                return
            if "error" in data or "message" in data:
                _print_result(FAIL, f"Error: {data.get('message', data.get('error', '?'))[:120]}")
                ctx.healthy = False
                return
        except json.JSONDecodeError:
            pass

    _print_result(FAIL, f"Smoke test failed (exit={result.returncode})")
    if response:
        _print_result(INFO, f"Response: {response[:150]}")
    ctx.healthy = False


@task
def summary(args, ctx):
    """Print final summary."""
    print(f"\n{'='*60}")
    if getattr(ctx, "healthy", True):
        print(f"  {PASS}  Dynamo gateway stack is healthy in {args.namespace}")
    else:
        print(f"  {FAIL}  Issues found — see above for fixes")
    print(f"{'='*60}\n")
