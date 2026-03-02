"""Scenario: SQS enqueue.

Sends messages to an SQS queue using the boto3 SQS client.

Config fields (under 'config' in the S3 JSON):
  queue_url              str    full SQS queue URL
  message_template       dict   JSON body template; {message_id} interpolated
  enqueue_rate_per_second float  target enqueue rate (token bucket)
  duration_seconds       int    how long to run
  concurrency            int    worker pool size (default 4)
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from awfr.config import AuthError, ConfigError
from awfr import progress

if TYPE_CHECKING:
    from awfr.config import RunEnv
    from awfr.metrics import MetricsWriter


class SqsEnqueue:
    """SQS enqueue scenario."""

    REQUIRED_CONFIG = (
        "queue_url",
        "message_template",
        "enqueue_rate_per_second",
        "duration_seconds",
    )

    def __init__(self, run_env: RunEnv, metrics_writer: MetricsWriter) -> None:
        self._run_env = run_env
        self._metrics = metrics_writer

    def run(self) -> dict[str, Any]:
        """Execute the scenario. Returns counters dict."""
        cfg = self._validate_config()

        queue_url: str = cfg["queue_url"]
        message_template: dict[str, Any] = cfg["message_template"]
        rate: float = cfg["enqueue_rate_per_second"]
        duration: int = cfg["duration_seconds"]
        concurrency: int = cfg.get("concurrency", 4)

        try:
            sqs = boto3.client("sqs")
        except NoCredentialsError as exc:
            raise AuthError(f"No AWS credentials for SQS: {exc}") from exc

        counters = {"sends_attempted": 0, "sends_succeeded": 0, "errors": 0}
        lock = threading.Lock()
        abort_event = threading.Event()
        fatal_error: list[BaseException] = []
        work_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=concurrency * 2)

        workers = [
            threading.Thread(
                target=self._worker,
                args=(sqs, queue_url, work_queue, counters, lock, abort_event, fatal_error),
                name=f"sqs-worker-{i}",
                daemon=True,
            )
            for i in range(concurrency)
        ]
        for w in workers:
            w.start()

        # Start progress reporter (prints every 10 s while the scenario runs).
        progress.start(
            duration_seconds=duration,
            counters=counters,
            lock=lock,
            success_key="sends_succeeded",
            error_key="errors",
            abort_event=abort_event,
        )

        deadline = time.monotonic() + duration
        token_interval = 1.0 / rate if rate > 0 else 0.0
        cursor = 0
        next_token_at = time.monotonic()

        while time.monotonic() < deadline:
            if abort_event.is_set():
                break
            now = time.monotonic()
            if now < next_token_at:
                time.sleep(min(next_token_at - now, 0.1))  # wake up to check abort_event
                continue

            message_id = f"msg-{cursor:010d}"
            cursor += 1
            body = json.dumps(
                {k: (v.replace("{message_id}", message_id) if isinstance(v, str) else v)
                 for k, v in message_template.items()}
            )
            work_queue.put({"body": body, "message_id": message_id})
            next_token_at += token_interval

        for _ in workers:
            work_queue.put(None)
        for w in workers:
            w.join()

        if fatal_error:
            raise fatal_error[0]

        return dict(counters)

    def _validate_config(self) -> dict[str, Any]:
        cfg = self._run_env.scenario_config
        missing = [k for k in self.REQUIRED_CONFIG if k not in cfg]
        if missing:
            raise ConfigError(f"Missing required config fields: {missing}")

        if not isinstance(cfg["duration_seconds"], int) or cfg["duration_seconds"] < 1:
            raise ConfigError("'duration_seconds' must be a positive integer.")
        rate = cfg["enqueue_rate_per_second"]
        if not isinstance(rate, (int, float)) or rate <= 0:
            raise ConfigError("'enqueue_rate_per_second' must be a positive number.")
        if not isinstance(cfg["message_template"], dict):
            raise ConfigError("'message_template' must be a JSON object.")
        if not cfg["queue_url"].startswith("https://"):
            raise ConfigError("'queue_url' must be a full SQS HTTPS URL.")

        return cfg

    def _worker(
        self,
        sqs: Any,
        queue_url: str,
        work_queue: queue.Queue[dict[str, Any] | None],
        counters: dict[str, int],
        lock: threading.Lock,
        abort_event: threading.Event,
        fatal_error: list[BaseException],
    ) -> None:
        while True:
            item = work_queue.get()
            if item is None:
                return

            body = item["body"]
            message_id = item["message_id"]
            t_start = time.monotonic()

            with lock:
                counters["sends_attempted"] += 1

            try:
                sqs.send_message(QueueUrl=queue_url, MessageBody=body)
                latency_ms = (time.monotonic() - t_start) * 1000

                with lock:
                    counters["sends_succeeded"] += 1

                self._metrics.append({
                    "event": "send",
                    "message_id": message_id,
                    "status": "ok",
                    "latency_ms": round(latency_ms, 2),
                })

            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("AccessDenied", "403"):
                    err = AuthError(f"Access denied sending to SQS queue: {exc}")
                    fatal_error.append(err)
                    abort_event.set()
                    return
                with lock:
                    counters["errors"] += 1
                self._metrics.append({
                    "event": "send",
                    "message_id": message_id,
                    "status": "error",
                    "error_code": code,
                })
            except Exception as exc:  # noqa: BLE001
                with lock:
                    counters["errors"] += 1
                self._metrics.append({
                    "event": "send",
                    "message_id": message_id,
                    "status": "error",
                    "error_code": type(exc).__name__,
                })


def run(run_env: RunEnv, metrics_writer: MetricsWriter) -> dict[str, Any]:
    """Module-level entrypoint called by the scenario registry."""
    return SqsEnqueue(run_env=run_env, metrics_writer=metrics_writer).run()
