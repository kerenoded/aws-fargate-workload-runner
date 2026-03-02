"""Local metrics writer.

Appends JSONL rows to a local file throughout the run.
The single-threaded uploader reads a snapshot of this file; workers never upload.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MetricsWriter:
    """Thread-safe append-only JSONL writer."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: dict[str, Any]) -> None:
        """Append a metric record to the JSONL file (thread-safe)."""
        record.setdefault("ts", datetime.now(tz=timezone.utc).isoformat())
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()

    def snapshot_bytes(self) -> bytes:
        """Return a consistent byte snapshot of the current file (flushed)."""
        with self._lock:
            return self._path.read_bytes()
