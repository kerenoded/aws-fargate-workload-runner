"""S3 artifact uploader.

Rules:
- Upload failures are best-effort and MUST NOT change the exit code.
- Only this module performs uploads; worker threads never call S3 directly.
- Periodic upload overwrites the same key each time using a flushed snapshot.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class S3Uploader:
    """Best-effort S3 uploader for run artifacts."""

    def __init__(self, bucket: str, prefix: str) -> None:
        """
        Args:
            bucket: ARTIFACTS_BUCKET (no s3:// prefix)
            prefix: e.g. "runs/<RUN_ID>/"
        """
        self._bucket = bucket
        self._prefix = prefix.rstrip("/") + "/"
        self._s3 = boto3.client("s3")
        self._periodic_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # One-shot uploads
    # ------------------------------------------------------------------

    def upload_bytes(self, key_suffix: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
        """Upload raw bytes to <prefix><key_suffix>. Returns True on success."""
        key = f"{self._prefix}{key_suffix}"
        try:
            self._s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
            logger.debug("Uploaded s3://%s/%s (%d bytes)", self._bucket, key, len(data))
            return True
        except (ClientError, BotoCoreError) as exc:
            logger.warning("Upload failed s3://%s/%s: %s", self._bucket, key, exc)
            return False

    def upload_file(self, key_suffix: str, path: Path, content_type: str = "application/octet-stream") -> bool:
        """Upload a local file. Returns True on success."""
        return self.upload_bytes(key_suffix, path.read_bytes(), content_type)

    def upload_summary(self, summary: dict[str, Any]) -> bool:
        """Upload summary.json. Best-effort; never raises."""
        data = json.dumps(summary, indent=2).encode()
        return self.upload_bytes("summary.json", data, "application/json")

    def upload_metrics_snapshot(self, snapshot: bytes) -> bool:
        """Upload a flushed metrics.jsonl snapshot. Best-effort; never raises."""
        return self.upload_bytes("metrics.jsonl", snapshot, "application/x-ndjson")

    # ------------------------------------------------------------------
    # Periodic upload (optional, default off)
    # ------------------------------------------------------------------

    def start_periodic_upload(self, interval_seconds: int, metrics_snapshot_fn: Any) -> None:
        """Start background periodic overwrite of metrics.jsonl.

        Args:
            interval_seconds: seconds between uploads (>0)
            metrics_snapshot_fn: callable returning bytes snapshot of metrics file
        """
        if interval_seconds <= 0:
            return
        self._stop_event.clear()
        self._periodic_thread = threading.Thread(
            target=self._periodic_loop,
            args=(interval_seconds, metrics_snapshot_fn),
            daemon=True,
            name="metrics-uploader",
        )
        self._periodic_thread.start()

    def stop_periodic_upload(self) -> None:
        """Signal the periodic upload thread to stop and wait for it."""
        self._stop_event.set()
        if self._periodic_thread is not None:
            self._periodic_thread.join(timeout=10)
            self._periodic_thread = None

    def _periodic_loop(self, interval: int, snapshot_fn: Any) -> None:
        while not self._stop_event.wait(timeout=interval):
            try:
                snapshot = snapshot_fn()
                self.upload_metrics_snapshot(snapshot)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Periodic metrics upload error (ignored): %s", exc)
