#!/usr/bin/env python3

from projects.core.dsl import (
    entrypoint,
    execute_tasks,
    shell,
    task,
)


@entrypoint
def run(*, name: str, namespace: str = ""):
    return execute_tasks(locals())


@task
def setup_directories(args, context):
    shell.mkdir("artifacts")
    return "Artifacts directory created"


@task
def get_current_timestamp(args, context):
    result = shell.run("date -Iseconds")
    context.capture_timestamp = result.stdout.strip()
    return f"Timestamp: {context.capture_timestamp}"


@task
def determine_target_namespace(args, context):
    if args.namespace:
        context.target_namespace = args.namespace
        return f"Using specified namespace: {context.target_namespace}"

    result = shell.run("oc project -q")
    context.target_namespace = result.stdout.strip()
    return f"Using current namespace: {context.target_namespace}"


@task
def capture_inferenceservice_yaml(args, context):
    shell.run(
        f"oc get inferenceservice {args.name} -n {context.target_namespace} -oyaml",
        stdout_dest=args.artifact_dir / "artifacts/inferenceservice.yaml",
        check=False,
    )
    return "InferenceService YAML captured"


@task
def capture_inferenceservice_json(args, context):
    shell.run(
        f"oc get inferenceservice {args.name} -n {context.target_namespace} -ojson",
        stdout_dest=args.artifact_dir / "artifacts/inferenceservice.json",
        check=False,
    )
    return "InferenceService JSON captured"


@task
def capture_servingruntime_yaml(args, context):
    shell.run(
        f"oc get servingruntime {args.name} -n {context.target_namespace} -oyaml",
        stdout_dest=args.artifact_dir / "artifacts/servingruntime.yaml",
        check=False,
    )
    return "ServingRuntime YAML captured"


@task
def capture_related_pods_yaml(args, context):
    shell.run(
        f"oc get pods -l serving.kserve.io/inferenceservice={args.name} "
        f"-n {context.target_namespace} -oyaml",
        stdout_dest=args.artifact_dir / "artifacts/inferenceservice.pods.yaml",
        check=False,
    )
    return "Related pods YAML captured"


@task
def capture_related_deployments(args, context):
    shell.run(
        f"oc get deployments -l serving.kserve.io/inferenceservice={args.name} "
        f"-n {context.target_namespace} -oyaml",
        stdout_dest=args.artifact_dir / "artifacts/inferenceservice.deployments.yaml",
        check=False,
    )
    return "Related deployments captured"


@task
def capture_namespace_pods(args, context):
    shell.run(
        f"oc get pods -owide -n {context.target_namespace}",
        stdout_dest=args.artifact_dir / "artifacts/namespace.pods.status",
        check=False,
    )
    return "Namespace pods status captured"


@task
def capture_namespace_services(args, context):
    shell.run(
        f"oc get svc -n {context.target_namespace}",
        stdout_dest=args.artifact_dir / "artifacts/namespace.services.status",
        check=False,
    )
    return "Namespace services captured"


@task
def capture_pod_logs(args, context):
    result = shell.run(
        f"oc get pods -l serving.kserve.io/inferenceservice={args.name} "
        f"-n {context.target_namespace} "
        f'-o jsonpath="{{.items[*].metadata.name}}"',
        check=False,
        log_stdout=False,
    )

    pod_names = result.stdout.strip().split()
    if not pod_names or not result.stdout.strip():
        return "No pods found to capture logs"

    log_file = args.artifact_dir / "artifacts/inferenceservice.pods.log"

    with open(log_file, "w") as f:
        for pod_name in pod_names:
            f.write(f"=== Logs for pod: {pod_name} ===\n")
            log_result = shell.run(
                f"oc logs {pod_name} -n {context.target_namespace} --all-containers=true",
                check=False,
                log_stdout=False,
            )
            f.write(log_result.stdout)
            f.write("\n")

    return f"Pod logs captured for {len(pod_names)} pods"


@task
def capture_pod_previous_logs(args, context):
    result = shell.run(
        f"oc get pods -l serving.kserve.io/inferenceservice={args.name} "
        f"-n {context.target_namespace} "
        f'-o jsonpath="{{.items[*].metadata.name}}"',
        check=False,
    )

    pod_names = result.stdout.strip().split()
    if not pod_names or not result.stdout.strip():
        return "No pods found to capture previous logs"

    log_file = args.artifact_dir / "artifacts/inferenceservice.pods.previous.log"

    with open(log_file, "w") as f:
        for pod_name in pod_names:
            f.write(f"=== Previous logs for pod: {pod_name} ===\n")
            log_result = shell.run(
                f"oc logs {pod_name} -n {context.target_namespace} --previous --all-containers=true",
                check=False,
                log_stdout=False,
            )
            f.write(log_result.stdout)
            f.write("\n")

    return f"Pod previous logs captured for {len(pod_names)} pods"


@task
def capture_inferenceservice_describe(args, context):
    shell.run(
        f"oc describe inferenceservice {args.name} -n {context.target_namespace}",
        stdout_dest=args.artifact_dir / "artifacts/inferenceservice.describe.txt",
        check=False,
    )
    return "InferenceService describe captured"


@task
def capture_pods_describe(args, context):
    result = shell.run(
        f"oc get pods -l serving.kserve.io/inferenceservice={args.name} "
        f"-n {context.target_namespace} "
        f'-o jsonpath="{{.items[*].metadata.name}}"',
        check=False,
    )

    pod_names = result.stdout.strip().split()
    if not pod_names or not result.stdout.strip():
        return "No pods found to describe"

    describe_file = args.artifact_dir / "artifacts/inferenceservice.pods.describe.txt"

    with open(describe_file, "w") as f:
        for pod_name in pod_names:
            f.write(f"=== Describe for pod: {pod_name} ===\n")
            describe_result = shell.run(
                f"oc describe pod {pod_name} -n {context.target_namespace}",
                check=False,
            )
            f.write(describe_result.stdout)
            f.write("\n")

    return f"Pod describe output captured for {len(pod_names)} pods"


@task
def capture_events(args, context):
    shell.run(
        f"oc get events -n {context.target_namespace} --sort-by=.lastTimestamp",
        stdout_dest=args.artifact_dir / "artifacts/namespace.events.txt",
        check=False,
    )
    return "Namespace events captured"


if __name__ == "__main__":
    run.main()
