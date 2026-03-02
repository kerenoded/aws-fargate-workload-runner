"""In-container SQS queue depth monitor.

Runs as a background daemon thread during a Fargate task.
Periodically calls sqs:GetQueueAttributes on the configured queues and
prints one structured line per queue to stdout (→ CloudWatch Logs).

Output format (machine-parsable / CloudWatch Logs Insights friendly):
  [SQS][HH:MM:SS][run_id=RUN_ID] queue=NAME visible=N in_flight=N delayed=N
  [SQS][age][HH:MM:SS][run_id=RUN_ID] queue=NAME oldest_age_sec=X  ← printed at start, end, and every 4 min

Configuration (in the ``runner`` section of the run config JSON):
  "sqs_monitor_arns"              — list of SQS ARNs to monitor
  "sqs_monitor_interval_seconds"  — poll interval in seconds (0 = disabled)

The task role must have sqs:GetQueueAttributes and cloudwatch:GetMetricData.
When enable_sqs_permissions=true in Terraform both permissions are included
automatically. Errors are best-effort: they are logged but never affect
the scenario exit code.
"""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import Sequence

import boto3
from botocore.exceptions import ClientError

from awfr.config import ConfigError

logger = logging.getLogger(__name__)

_THROTTLE_CODES = frozenset({"ThrottlingException", "RequestThrottled", "Throttling"})
_MAX_QUEUES = 20
_ATTRS = [
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
]
# Fetched once on the first poll per queue; helps explain in-flight behaviour.
_STARTUP_ATTRS = [
    "VisibilityTimeout",
    "ReceiveMessageWaitTimeSeconds",
]


# All AWS partitions that host SQS.  China uses a different endpoint domain.
_VALID_PARTITIONS = frozenset({"aws", "aws-us-gov", "aws-cn"})


def _arn_to_queue_url(arn: str) -> str:
    """Convert an SQS ARN → HTTPS queue URL for standard, GovCloud, and China partitions.

    Supported ARN formats:
      arn:aws:sqs:REGION:ACCOUNT:NAME          (standard)
      arn:aws-us-gov:sqs:REGION:ACCOUNT:NAME   (GovCloud)
      arn:aws-cn:sqs:REGION:ACCOUNT:NAME       (China)

    Pure string conversion — no API calls.
    Raises ConfigError on bad format so the worker fails fast (exit 2).
    """
    parts = arn.split(":")
    if (
        len(parts) != 6
        or parts[0] != "arn"
        or parts[1] not in _VALID_PARTITIONS
        or parts[2] != "sqs"
    ):
        raise ConfigError(
            f"Invalid SQS ARN {arn!r} in sqs_monitor_arns — "
            "expected format: arn:aws:sqs:REGION:ACCOUNT:NAME "
            "(also accepts aws-us-gov and aws-cn partitions)"
        )
    _, partition, _, region, account, name = parts
    if not region or not account or not name:
        raise ConfigError(
            f"SQS ARN {arn!r} has an empty region, account, or queue name"
        )
    # China endpoints use amazonaws.com.cn; all other partitions use amazonaws.com.
    domain = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"
    return f"https://sqs.{region}.{domain}/{account}/{name}"


class SQSMonitor:
    """Background daemon thread that polls SQS queue attributes."""

    def __init__(
        self,
        arns: Sequence[str],
        interval_seconds: int,
        run_id: str,
    ) -> None:
        """
        Args:
            arns: list of SQS ARNs to monitor (arn:aws:sqs:REGION:ACCOUNT:NAME).
            interval_seconds: seconds between polls; must be > 0.
            run_id: run identifier included in every log line.

        Raises:
            ConfigError: if any ARN is malformed (fails fast, exit 2).
        """
        if interval_seconds <= 0:
            raise ConfigError(
                f"sqs_monitor_interval_seconds must be > 0 (got {interval_seconds}); "
                "set to 0 in config to disable monitoring — do not construct SQSMonitor"
            )

        if len(arns) > _MAX_QUEUES:
            logger.warning(
                "sqs_monitor_arns contains %d queues; capping at %d",
                len(arns),
                _MAX_QUEUES,
            )
            arns = list(arns)[:_MAX_QUEUES]

        # Validate + convert all ARNs once at construction (fail fast)
        self._queue_urls: list[str] = [_arn_to_queue_url(a) for a in arns]
        self._interval = interval_seconds
        self._run_id = run_id
        self._sqs = boto3.client("sqs")
        self._cw = boto3.client("cloudwatch")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Track which queues produced AccessDenied so we only warn once
        self._access_denied: set[str] = set()
        # Track which queues have had their one-time startup config line printed
        self._startup_done: set[str] = set()
        # Timestamp of the last oldest_age print; None = not yet printed
        self._last_age_ts: datetime | None = None

    def start(self) -> None:
        """Start the background polling thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="sqs-monitor",
        )
        self._thread.start()
        logger.debug("SQSMonitor started (interval=%ds, queues=%d)", self._interval, len(self._queue_urls))

    def stop(self) -> None:
        """Signal the thread to stop, wait for it to finish, then print a final age snapshot."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        # Print a final oldest_age snapshot after the scenario has ended.
        active_names = [
            url.rstrip("/").rsplit("/", 1)[-1]
            for url in self._queue_urls
            if url not in self._access_denied
        ]
        if active_names:
            ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
            self._print_oldest_ages(ts, active_names)

    def _loop(self) -> None:
        # Poll immediately on first iteration, then every interval seconds.
        # Jitter (±10 %) spreads polls across tasks to avoid synchronised bursts.
        first = True
        while True:
            if first:
                first = False
            else:
                jitter = random.uniform(0.9, 1.1)
                if self._stop_event.wait(timeout=self._interval * jitter):
                    break  # stop requested
            self._poll_all()

    def _poll_all(self) -> None:
        now = datetime.now(tz=timezone.utc)
        ts = now.strftime("%H:%M:%S")

        # Build the list of active (non-denied) queues for this cycle.
        active = [
            (url, url.rstrip("/").rsplit("/", 1)[-1])
            for url in self._queue_urls
            if url not in self._access_denied
        ]

        # Print oldest_age on the first poll and then at most every 4 minutes.
        # CloudWatch metric has 1–2 min lag so high-frequency fetching adds cost without value.
        should_print_age = (
            self._last_age_ts is None
            or (now - self._last_age_ts) >= timedelta(minutes=4)
        )
        if should_print_age and active:
            self._print_oldest_ages(ts, [name for _, name in active])
            self._last_age_ts = now

        for url, queue_name in active:
            first_poll = url not in self._startup_done
            fetch_attrs = _ATTRS + _STARTUP_ATTRS if first_poll else _ATTRS
            startup_attrs_ok = True

            try:
                resp = self._sqs.get_queue_attributes(
                    QueueUrl=url,
                    AttributeNames=fetch_attrs,
                )
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("AccessDenied", "AccessDeniedException"):
                    self._access_denied.add(url)
                    print(
                        f"[SQS] WARNING: AccessDenied for queue {queue_name!r} — "
                        "ensure the ECS task role has sqs:GetQueueAttributes "
                        "(enable_sqs_permissions=true in Terraform). Skipping this queue.",
                        flush=True,
                    )
                    continue
                elif code == "InvalidAttributeName" and first_poll:
                    # Some queue types don't support all startup attrs; retry with base attrs.
                    logger.warning(
                        "SQS: startup attrs unsupported for queue %s; falling back to base attrs",
                        queue_name,
                    )
                    startup_attrs_ok = False
                    self._startup_done.add(url)  # don't retry startup attrs again
                    try:
                        resp = self._sqs.get_queue_attributes(
                            QueueUrl=url,
                            AttributeNames=_ATTRS,
                        )
                    except ClientError as retry_exc:
                        logger.warning("SQS error for queue %s on retry: %s", queue_name, retry_exc)
                        continue
                elif code in _THROTTLE_CODES:
                    logger.debug("SQS throttle for queue %s; skipping this poll cycle", queue_name)
                    continue
                else:
                    logger.warning("SQS error for queue %s: %s", queue_name, exc)
                    continue

            attrs = resp.get("Attributes", {})

            # One-time startup line: queue config that doesn't change during the run.
            if first_poll:
                if startup_attrs_ok:
                    visibility_timeout = attrs.get("VisibilityTimeout", "?")
                    wait_time = attrs.get("ReceiveMessageWaitTimeSeconds", "?")
                    print(
                        f"[SQS][config][run_id={self._run_id}] "
                        f"queue={queue_name} visibility_timeout_sec={visibility_timeout} "
                        f"receive_wait_sec={wait_time}",
                        flush=True,
                    )
                self._startup_done.add(url)

            visible = attrs.get("ApproximateNumberOfMessages", "?")
            in_flight = attrs.get("ApproximateNumberOfMessagesNotVisible", "?")
            delayed = attrs.get("ApproximateNumberOfMessagesDelayed", "?")
            print(
                f"[SQS][{ts}][run_id={self._run_id}] "
                f"queue={queue_name} visible={visible} in_flight={in_flight} delayed={delayed}",
                flush=True,
            )

    def _print_oldest_ages(self, ts: str, queue_names: list[str]) -> None:
        """Fetch and print one [SQS][age] line per queue. Best-effort."""
        ages = self._fetch_oldest_age(queue_names)
        for name in queue_names:
            print(
                f"[SQS][age][{ts}][run_id={self._run_id}] "
                f"queue={name} oldest_age_sec={ages.get(name, 'N/A')}",
                flush=True,
            )

    def _fetch_oldest_age(self, queue_names: list[str]) -> dict[str, str]:
        """Batch-fetch ApproximateAgeOfOldestMessage from CloudWatch for all active queues.

        Issues a single GetMetricData call for all queues. Returns a dict mapping
        queue_name → value string (int seconds, or "N/A" if no datapoint available).
        Best-effort: returns all "N/A" on any CloudWatch error.
        """
        if not queue_names:
            return {}

        now = datetime.now(tz=timezone.utc)
        queries = [
            {
                "Id": f"m{i}",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/SQS",
                        "MetricName": "ApproximateAgeOfOldestMessage",
                        "Dimensions": [{"Name": "QueueName", "Value": name}],
                    },
                    "Period": 60,
                    "Stat": "Maximum",
                },
                "ReturnData": True,
            }
            for i, name in enumerate(queue_names)
        ]

        try:
            resp = self._cw.get_metric_data(
                MetricDataQueries=queries,
                StartTime=now - timedelta(minutes=5),
                EndTime=now,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _THROTTLE_CODES:
                logger.debug("CloudWatch throttle; oldest_age_sec will be N/A this cycle")
            else:
                logger.warning("CloudWatch error fetching ApproximateAgeOfOldestMessage: %s", exc)
            return {name: "N/A" for name in queue_names}

        result: dict[str, str] = {}
        for r in resp.get("MetricDataResults", []):
            idx = int(r["Id"][1:])  # strip leading "m"
            name = queue_names[idx]
            timestamps = r.get("Timestamps", [])
            values = r.get("Values", [])
            if values:
                # Datapoints are returned in ascending timestamp order; pick the most recent.
                _, latest_value = max(zip(timestamps, values), key=lambda tv: tv[0])
                result[name] = str(int(latest_value))
            else:
                result[name] = "N/A"

        # Fill in any queues missing from the response.
        for name in queue_names:
            result.setdefault(name, "N/A")

        return result
