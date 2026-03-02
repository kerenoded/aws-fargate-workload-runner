"""Scenario: IoT Core topic publish via boto3 IoT Data Plane.

Uses the IoT Data Plane Publish API (not MQTT). No MQTT libraries, no
hardcoded endpoints. device_count represents logical sender IDs for
topic/payload shaping only — no MQTT connections or device registrations.

Config fields (under 'config' in the S3 JSON):
  topic_pattern          str    e.g. "devices/{device_id}/events"
  device_count           int    number of logical device IDs to cycle through
  publish_rate_per_second float  target publish rate (token bucket)
  duration_seconds       int    how long to run
  payload_template       dict   JSON template; {device_id} interpolated
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


class IotCoreTopicPublish:
    """IoT Core topic publish scenario."""

    REQUIRED_CONFIG = (
        "topic_pattern",
        "device_count",
        "publish_rate_per_second",
        "duration_seconds",
        "payload_template",
    )

    def __init__(self, run_env: RunEnv, metrics_writer: MetricsWriter) -> None:
        self._run_env = run_env
        self._metrics = metrics_writer

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Execute the scenario. Returns counters dict."""
        cfg = self._validate_config()

        topic_pattern: str = cfg["topic_pattern"]
        device_count: int = cfg["device_count"]
        rate: float = cfg["publish_rate_per_second"]
        duration: int = cfg["duration_seconds"]
        payload_template: dict[str, Any] = cfg["payload_template"]
        concurrency: int = cfg.get("concurrency", 4)

        # Resolve IoT Data endpoint once at startup.
        iot_data_client = self._resolve_iot_data_client()

        # Shared counters (modified under a lock).
        counters = {"publishes_attempted": 0, "publishes_succeeded": 0, "errors": 0}
        lock = threading.Lock()
        abort_event = threading.Event()
        fatal_error: list[BaseException] = []
        work_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=concurrency * 2)

        # Start workers.
        workers = [
            threading.Thread(
                target=self._worker,
                args=(iot_data_client, work_queue, counters, lock, abort_event, fatal_error),
                name=f"iot-worker-{i}",
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
            success_key="publishes_succeeded",
            error_key="errors",
            abort_event=abort_event,
        )

        # Token bucket feed loop.
        deadline = time.monotonic() + duration
        token_interval = 1.0 / rate if rate > 0 else 0.0
        device_cursor = 0
        next_token_at = time.monotonic()

        while time.monotonic() < deadline:
            if abort_event.is_set():
                break
            now = time.monotonic()
            if now < next_token_at:
                time.sleep(min(next_token_at - now, 0.1))  # wake up to check abort_event
                continue

            device_id = f"device-{device_cursor % device_count:06d}"
            device_cursor += 1
            topic = topic_pattern.replace("{device_id}", device_id)
            payload = json.dumps(
                {k: (v.replace("{device_id}", device_id) if isinstance(v, str) else v)
                 for k, v in payload_template.items()}
            )

            work_queue.put({"topic": topic, "payload": payload, "device_id": device_id})
            next_token_at += token_interval

        # Poison pills to stop workers.
        for _ in workers:
            work_queue.put(None)
        for w in workers:
            w.join()

        if fatal_error:
            raise fatal_error[0]

        return dict(counters)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _validate_config(self) -> dict[str, Any]:
        cfg = self._run_env.scenario_config
        missing = [k for k in self.REQUIRED_CONFIG if k not in cfg]
        if missing:
            raise ConfigError(f"Missing required config fields: {missing}")

        if not isinstance(cfg["device_count"], int) or cfg["device_count"] < 1:
            raise ConfigError("'device_count' must be a positive integer.")
        if not isinstance(cfg["duration_seconds"], int) or cfg["duration_seconds"] < 1:
            raise ConfigError("'duration_seconds' must be a positive integer.")
        rate = cfg["publish_rate_per_second"]
        if not isinstance(rate, (int, float)) or rate <= 0:
            raise ConfigError("'publish_rate_per_second' must be a positive number.")
        if not isinstance(cfg["payload_template"], dict):
            raise ConfigError("'payload_template' must be a JSON object.")

        return cfg

    def _resolve_iot_data_client(self) -> Any:
        """Resolve IoT Data ATS endpoint and return a boto3 iot-data client."""
        try:
            iot_mgmt = boto3.client("iot")
            resp = iot_mgmt.describe_endpoint(endpointType="iot:Data-ATS")
            endpoint = resp["endpointAddress"]
        except NoCredentialsError as exc:
            raise AuthError(f"No AWS credentials for IoT endpoint resolution: {exc}") from exc
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("AccessDenied", "403"):
                raise AuthError(f"Access denied calling iot:DescribeEndpoint: {exc}") from exc
            raise AuthError(f"Failed to resolve IoT endpoint: {exc}") from exc

        return boto3.client("iot-data", endpoint_url=f"https://{endpoint}")

    def _worker(
        self,
        iot_data: Any,
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

            topic = item["topic"]
            payload = item["payload"]
            device_id = item["device_id"]
            t_start = time.monotonic()

            with lock:
                counters["publishes_attempted"] += 1

            try:
                # NOTE: IAM action for boto3 iot-data publish() is iot:Publish.
                # Verify at smoke-test time; update Terraform if the action name differs.
                iot_data.publish(topic=topic, payload=payload.encode(), qos=0)
                latency_ms = (time.monotonic() - t_start) * 1000

                with lock:
                    counters["publishes_succeeded"] += 1

                self._metrics.append({
                    "event": "publish",
                    "device_id": device_id,
                    "topic": topic,
                    "status": "ok",
                    "latency_ms": round(latency_ms, 2),
                })

            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("AccessDenied", "403"):
                    err = AuthError(f"Access denied publishing to IoT topic: {exc}")
                    fatal_error.append(err)
                    abort_event.set()
                    return
                with lock:
                    counters["errors"] += 1
                self._metrics.append({
                    "event": "publish",
                    "device_id": device_id,
                    "topic": topic,
                    "status": "error",
                    "error_code": code,
                })
            except Exception as exc:  # noqa: BLE001
                with lock:
                    counters["errors"] += 1
                self._metrics.append({
                    "event": "publish",
                    "device_id": device_id,
                    "topic": topic,
                    "status": "error",
                    "error_code": type(exc).__name__,
                })


def run(run_env: RunEnv, metrics_writer: MetricsWriter) -> dict[str, Any]:
    """Module-level entrypoint called by the scenario registry."""
    return IotCoreTopicPublish(run_env=run_env, metrics_writer=metrics_writer).run()
