"""Laptop CLI: run a workload scenario on ECS Fargate.

Usage:
  python tools/run_task.py \\
    --scenario iot_core_topic_publish \\
    --config-file loadtest/configs/iot-100rps.json \\
    [--image-tag v1.2.3] \\
    [--no-wait] \\
    [--tail]

The tool:
1. Reads Terraform outputs for cluster / task-def / networking info.
2. Generates a unique RUN_ID (uuid4).
3. Uploads the config JSON to s3://<bucket>/configs/<RUN_ID>.json.
4. Calls ecs:RunTask with exactly 3 environment overrides:
     RUN_ID, SCENARIO, CONFIG_S3_URI
5. Optionally waits for the task to finish and tails CloudWatch logs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3

if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.tf_outputs import terraform_outputs

TF_DIR = Path(__file__).resolve().parents[1] / "infra" / "terraform"
STREAM_PREFIX = "run"  # must match awslogs-stream-prefix in ecs.tf
POLL_SECONDS = 15

# Resolve region from environment when available.  When neither var is set we
# leave region_name out of boto3 calls entirely so the SDK can apply its own
# resolution chain (profile, instance metadata, etc.) without us overriding it
# with an explicit None.
REGION: str | None = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
_REGION_KW: dict = {"region_name": REGION} if REGION else {}

def _upload_config(s3, bucket: str, run_id: str, config: dict) -> str:
    key = f"configs/{run_id}.json"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(config, indent=2).encode(),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{key}"


def _register_task_def_with_tag(ecs, task_def_arn: str, image_tag: str, ecr_url: str, container_name: str) -> str:
    """Describe the current task def, swap the image tag, register a new revision.

    Preserves all ECS-supported fields from the source revision so that nothing
    is silently dropped (volumes, placement constraints, runtime platform, etc.).
    Fields that are read-only in describe_task_definition (revision, status, ARN…)
    are never included — they are not valid inputs for register_task_definition.
    """
    if ".dkr.ecr." not in ecr_url or ".amazonaws.com" not in ecr_url:
        raise SystemExit(f"ecr_url does not look like a valid ECR URL: {ecr_url!r}")

    # include=["TAGS"] is required — without it the API omits resource tags from
    # the response, so the new revision would be registered with no tags and lose
    # all Terraform-managed cost-allocation tags (Project, Purpose, ManagedBy).
    resp = ecs.describe_task_definition(taskDefinition=task_def_arn, include=["TAGS"])
    td = resp["taskDefinition"]

    # Rebuild image URI with the new tag (strip any existing tag from the URL).
    base = ecr_url.split(":")[0]
    new_image = f"{base}:{image_tag}"

    # Shallow-copy each container definition so the original list is never mutated.
    containers = [{**c} for c in td.get("containerDefinitions") or []]
    if not any(c.get("name") == container_name for c in containers):
        raise SystemExit(
            f"Container '{container_name}' not found in task definition: {task_def_arn}"
        )
    for c in containers:
        if c.get("name") == container_name:
            c["image"] = new_image

    # Whitelist every key accepted by register_task_definition.  We copy from
    # describe_task_definition output only when the key is present and non-None,
    # so new fields added by AWS in the future are carried through automatically
    # as long as they share the same name in both APIs.
    _REQUIRED = {
        "family",
        "containerDefinitions",
    }
    # List-typed fields: default to [] rather than omitting them so the API
    # never receives explicit None for a list parameter.
    _LIST_FIELDS = {
        "volumes",
        "placementConstraints",
        "requiresCompatibilities",
        "inferenceAccelerators",
        "tags",
    }
    # Scalar / object fields carried through only when present in the source.
    _OPTIONAL = {
        "taskRoleArn",
        "executionRoleArn",
        "networkMode",
        "cpu",
        "memory",
        "runtimePlatform",
        "ephemeralStorage",
        "proxyConfiguration",
        "ipcMode",
        "pidMode",
    }

    payload: dict = {"family": td["family"], "containerDefinitions": containers}
    for key in _LIST_FIELDS:
        payload[key] = td.get(key) or []
    for key in _OPTIONAL:
        val = td.get(key)
        if val is not None:
            payload[key] = val

    new_td = ecs.register_task_definition(**payload)
    return new_td["taskDefinition"]["taskDefinitionArn"]


def _compute_log_stream_name(task_arn: str, stream_prefix: str, container_name: str) -> str:
    """CloudWatch stream name: {awslogs-stream-prefix}/{container}/{ecs-task-id}.
    The task ID is the last segment of the task ARN, NOT the run_id UUID.
    """
    task_id = task_arn.split("/")[-1]
    return f"{stream_prefix}/{container_name}/{task_id}"


def _tail_stream_incremental(logs, log_group: str, stream_name: str, start_ms: int) -> tuple[int, int]:
    """Fetch new log events from start_ms onward. Returns (new_start_ms, events_printed)."""
    printed = 0
    next_token = None
    last_token = None

    for _ in range(10):  # up to 10 pages per poll to handle log bursts
        kwargs: dict = {
            "logGroupName": log_group,
            "logStreamNames": [stream_name],
            "startTime": start_ms,
            "interleaved": True,
        }
        if next_token:
            kwargs["nextToken"] = next_token

        resp = logs.filter_log_events(**kwargs)

        for e in resp.get("events", []):
            ts = datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).strftime("%H:%M:%S")
            msg = (e.get("message") or "").rstrip("\n")
            if msg:
                print(f"{ts} | {msg}")
                printed += 1
            start_ms = max(start_ms, e["timestamp"] + 1)

        next_token = resp.get("nextToken")
        # CloudWatch can return the same nextToken when there are no new pages;
        # stop paging to avoid an infinite loop.
        if not next_token or next_token == last_token:
            break
        last_token = next_token

    return start_ms, printed


def _exit_code_from_task(task: dict) -> int | None:
    for c in task.get("containers", []):
        if c.get("exitCode") is not None:
            return c["exitCode"]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a workload scenario on ECS Fargate")
    ap.add_argument("--scenario", required=True, help="Scenario name (e.g. iot_core_topic_publish)")
    config_group = ap.add_mutually_exclusive_group(required=True)
    config_group.add_argument("--config-file", metavar="PATH", help="Path to scenario config JSON file")
    config_group.add_argument("--config-json", metavar="JSON", help="Inline scenario config as a JSON string")
    ap.add_argument("--image-tag", help="ECR image tag to use (registers new task-def revision)")
    ap.add_argument("--no-wait", action="store_true", help="Return immediately after submitting the task")
    ap.add_argument("--tail", action="store_true", help="Tail CloudWatch logs after the task finishes")
    ap.add_argument("--poll-seconds", type=int, default=POLL_SECONDS, help=f"Polling interval in seconds (default: {POLL_SECONDS})")
    ap.add_argument(
        "--sqs-monitor-interval-seconds",
        type=int,
        default=None,
        metavar="N",
        help="Inject sqs_monitor_interval_seconds into the runner config: poll SQS queue depth "
             "every N seconds from inside the Fargate container. Overrides any value already "
             "in the config file. Omit to leave the config file value unchanged (default: disabled).",
    )
    ap.add_argument(
        "--sqs-monitor-arns",
        nargs="+",
        default=None,
        metavar="ARN",
        help="Inject sqs_monitor_arns into the runner config: monitor only these ARNs instead of "
             "all queues in sqs_queue_arns. Omit to monitor all queues.",
    )
    args = ap.parse_args()

    # --sqs-monitor-arns is meaningless without an interval; catch it early.
    if args.sqs_monitor_arns and args.sqs_monitor_interval_seconds is None:
        ap.error("--sqs-monitor-arns requires --sqs-monitor-interval-seconds")

    # --- Load config ---
    if args.config_file:
        with open(args.config_file, encoding="utf-8") as fh:
            config = json.load(fh)
    else:
        config = json.loads(args.config_json)

    # --- Merge CLI SQS monitor args into runner config (CLI wins over file) ---
    if args.sqs_monitor_interval_seconds is not None or args.sqs_monitor_arns is not None:
        if "runner" not in config or not isinstance(config.get("runner"), dict):
            config["runner"] = {}
        if args.sqs_monitor_interval_seconds is not None:
            config["runner"]["sqs_monitor_interval_seconds"] = args.sqs_monitor_interval_seconds
        if args.sqs_monitor_arns is not None:
            config["runner"]["sqs_monitor_arns"] = args.sqs_monitor_arns

    # --- Terraform outputs ---
    outs = terraform_outputs(TF_DIR)
    cluster_arn = outs["ecs_cluster_arn"]
    task_def_arn = outs["task_definition_arn"]
    container_name = outs["container_name"]
    subnet_ids = outs["public_subnet_ids"]
    sg_id = outs["task_security_group_id"]
    log_group = outs["log_group_name"]
    bucket = outs["artifacts_bucket_name"]
    ecr_url = outs["ecr_repo_url"]

    session = boto3.Session(**_REGION_KW)
    s3 = session.client("s3")
    ecs = session.client("ecs")

    # --- Optional image tag override ---
    if args.image_tag:
        print(f"Registering new task-def revision with image tag: {args.image_tag}")
        task_def_arn = _register_task_def_with_tag(ecs, task_def_arn, args.image_tag, ecr_url, container_name)
        print(f"  New task def: {task_def_arn}")

    # --- RUN_ID + config upload ---
    run_id = str(uuid.uuid4())
    config_s3_uri = _upload_config(s3, bucket, run_id, config)
    print(f"Run ID   : {run_id}")
    print(f"Scenario : {args.scenario}")
    print(f"Config   : {config_s3_uri}")

    # --- RunTask (exactly 3 overrides) ---
    resp = ecs.run_task(
        cluster=cluster_arn,
        taskDefinition=task_def_arn,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnet_ids,
                "securityGroups": [sg_id],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": container_name,
                    "environment": [
                        {"name": "RUN_ID", "value": run_id},
                        {"name": "SCENARIO", "value": args.scenario},
                        {"name": "CONFIG_S3_URI", "value": config_s3_uri},
                    ],
                }
            ]
        },
    )

    failures = resp.get("failures", [])
    if failures:
        for f in failures:
            print(f"RunTask failure: {f}", file=sys.stderr)
        return 1

    task_arn = resp["tasks"][0]["taskArn"]
    log_stream_name = _compute_log_stream_name(task_arn, STREAM_PREFIX, container_name)
    print(f"Task ARN : {task_arn}")
    print(f"Log stream: {log_stream_name}")
    print(f"Console  : https://console.aws.amazon.com/ecs/v2/clusters/{cluster_arn.split('/')[-1]}/tasks/{task_arn.split('/')[-1]}")

    if args.no_wait:
        print("--no-wait: exiting without waiting for task completion.")
        return 0

    # --- Wait for task to stop, tailing logs incrementally while running ---
    print(f"\nWaiting for task to stop (polling every {args.poll_seconds}s)...")

    logs_client = session.client("logs") if args.tail and log_group else None
    tail_start_ms = int(time.time() * 1000)
    last_status = None

    while True:
        resp2 = ecs.describe_tasks(cluster=cluster_arn, tasks=[task_arn])
        desc_failures = resp2.get("failures", [])
        tasks_list = resp2.get("tasks", [])

        if not tasks_list:
            # ECS returns an empty tasks list (and a failures entry) when the task
            # ARN is unknown — e.g. wrong cluster, cross-region mismatch, or the
            # task record has expired (~1 h after stopping).
            reason = desc_failures[0].get("reason", "unknown") if desc_failures else "no tasks returned"
            print(
                f"describe_tasks returned no task (reason={reason!r}); cannot determine outcome.",
                file=sys.stderr,
            )
            return 1

        task = tasks_list[0]
        status = task.get("lastStatus", "UNKNOWN")

        if status != last_status:
            print(f"  Task status: {status}")
            last_status = status

        if logs_client and status in ("RUNNING", "DEPROVISIONING", "STOPPED"):
            try:
                tail_start_ms, _ = _tail_stream_incremental(
                    logs_client, log_group, log_stream_name, tail_start_ms
                )
            except logs_client.exceptions.ResourceNotFoundException:
                pass  # stream not yet created; retry next poll

        if status == "STOPPED":
            break

        time.sleep(args.poll_seconds)

    stop_reason = task.get("stoppedReason", "")
    exit_code = _exit_code_from_task(task)

    # One final log drain after STOPPED to catch any last events
    if logs_client:
        try:
            _tail_stream_incremental(logs_client, log_group, log_stream_name, tail_start_ms)
        except Exception:  # noqa: BLE001
            pass

    print(f"\nTask stopped. Exit code: {exit_code}  Reason: {stop_reason}")
    if exit_code is None:
        print("WARNING: no exit code — task may have been OOM-killed or spot-interrupted.", file=sys.stderr)
        return 1

    print(f"\nArtifacts: s3://{bucket}/runs/{run_id}/")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
