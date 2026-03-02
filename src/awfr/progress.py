"""Shared progress reporter for awfr scenarios.

Starts a background daemon thread that prints a single status line every
INTERVAL_SECONDS while a scenario is running, plus a final summary line
when the run completes (so even sub-interval runs always show stats).

Example output (every 5 s, 100 s run):
  [progress]  5%  |    5s / 100s  |  requests:     50  (ok:     50, err:    0)
  [progress] 10%  |   10s / 100s  |  requests:    100  (ok:     98, err:    2)
  ...
  [progress] 100%  |  100s / 100s  |  requests:   1000  (ok:    997, err:    3)
"""

from __future__ import annotations

import threading
import time

INTERVAL_SECONDS = 5


def start(
    duration_seconds: int,
    counters: dict,
    lock: threading.Lock,
    success_key: str,
    error_key: str,
    abort_event: threading.Event,
) -> threading.Thread:
    """Spawn and return a daemon thread that reports progress every INTERVAL_SECONDS.

    Args:
        duration_seconds: Total planned run length — used to compute the percentage.
        counters:         Shared counter dict owned by the scenario.
        lock:             The same lock the scenario uses to guard ``counters``.
        success_key:      Key in ``counters`` that holds the success count.
        error_key:        Key in ``counters`` that holds the error count.
        abort_event:      Event that is set when the scenario ends or aborts early.
                          The reporter stops as soon as this is set.
    """
    t = threading.Thread(
        target=_reporter,
        args=(duration_seconds, counters, lock, success_key, error_key, abort_event),
        name="progress-reporter",
        daemon=True,
    )
    t.start()
    return t


def _reporter(
    duration_seconds: int,
    counters: dict,
    lock: threading.Lock,
    success_key: str,
    error_key: str,
    abort_event: threading.Event,
) -> None:
    started_at = time.monotonic()
    deadline = started_at + duration_seconds
    next_report = started_at + INTERVAL_SECONDS

    while True:
        now = time.monotonic()

        # Stop when the scenario is done (deadline passed or aborted early).
        if abort_event.is_set() or now >= deadline:
            break

        # Sleep in small chunks so we react quickly to abort_event.
        sleep_for = min(next_report - now, 1.0)
        if sleep_for > 0:
            time.sleep(sleep_for)
            continue

        elapsed = now - started_at
        pct = min(100, int(elapsed / duration_seconds * 100))

        with lock:
            ok = counters.get(success_key, 0)
            err = counters.get(error_key, 0)

        total = ok + err
        print(
            f"[progress] {pct:3d}%"
            f"  |  {int(elapsed):>{len(str(duration_seconds))}}s / {duration_seconds}s"
            f"  |  requests: {total:6d}  (ok: {ok:6d}, err: {err:4d})",
            flush=True,
        )

        next_report += INTERVAL_SECONDS

    # Always print a final summary so short runs (duration ≤ INTERVAL_SECONDS) still
    # show stats, and so any run gets a definitive "done" line.
    elapsed_final = time.monotonic() - started_at
    with lock:
        ok = counters.get(success_key, 0)
        err = counters.get(error_key, 0)
    total = ok + err
    print(
        f"[progress] 100%"
        f"  |  {int(elapsed_final):>{len(str(duration_seconds))}}s / {duration_seconds}s"
        f"  |  requests: {total:6d}  (ok: {ok:6d}, err: {err:4d})",
        flush=True,
    )
