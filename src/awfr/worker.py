"""In-container worker entrypoint.

Lifecycle:
  1. Read env vars + download S3 config
  2. Validate config; fail fast (exit 2)
  3. Start optional periodic metrics upload
  4. Execute selected scenario
  5. Stop periodic upload; upload final artifacts (best-effort)
  6. Exit with typed exit code

SIGTERM/SIGINT: flush metrics, attempt summary upload, preserve exit code.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awfr import exit_codes
from awfr.config import AuthError, ConfigError, load_run_env
from awfr.metrics import MetricsWriter
from awfr.scenarios.registry import SCENARIOS
from awfr.sqs_monitor import SQSMonitor
from awfr.uploader import S3Uploader

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("awfr.worker")


def _build_summary(
    run_env: Any,
    outcome: str,
    exit_code: int,
    started_at: str,
    finished_at: str,
    counters: dict[str, Any],
    error_summary: str | None,
) -> dict[str, Any]:
    return {
        "run_id": run_env.run_id,
        "scenario": run_env.scenario,
        "outcome": outcome,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "counters": counters,
        "error_summary": error_summary,
    }


def run() -> int:
    """Main worker loop. Returns integer exit code."""
    started_at = datetime.now(tz=timezone.utc).isoformat()

    # ------------------------------------------------------------------ #
    # 1. Load environment + config                                         #
    # ------------------------------------------------------------------ #
    try:
        run_env = load_run_env()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", flush=True)
        return exit_codes.CONFIG_ERROR
    except AuthError as exc:
        print(f"AUTH ERROR: {exc}", flush=True)
        return exit_codes.AUTH_ERROR

    print(
        f"START run_id={run_env.run_id} scenario={run_env.scenario} "
        f"config={run_env.config_s3_uri} bucket={run_env.artifacts_bucket}",
        flush=True,
    )

    # ------------------------------------------------------------------ #
    # 2. Resolve scenario                                                  #
    # ------------------------------------------------------------------ #
    scenario_fn = SCENARIOS.get(run_env.scenario)
    if scenario_fn is None:
        print(
            f"CONFIG ERROR: Unknown scenario '{run_env.scenario}'. "
            f"Valid scenarios: {sorted(SCENARIOS)}",
            flush=True,
        )
        return exit_codes.CONFIG_ERROR

    # ------------------------------------------------------------------ #
    # 3. Set up local artifacts                                            #
    # ------------------------------------------------------------------ #
    work_dir = Path(tempfile.mkdtemp(prefix="awfr-"))
    metrics_writer = MetricsWriter(work_dir / "metrics.jsonl")
    summary_path = work_dir / "summary.json"

    uploader = S3Uploader(
        bucket=run_env.artifacts_bucket,
        prefix=run_env.artifacts_prefix,
    )

    # ------------------------------------------------------------------ #
    # 4. SIGTERM / SIGINT handler                                          #
    # ------------------------------------------------------------------ #
    interrupted = False
    final_exit_code = exit_codes.SUCCESS

    def _signal_handler(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        print(f"SIGNAL {signum} received; will flush and exit after current work.", flush=True)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # ------------------------------------------------------------------ #
    # 5. Start periodic upload (optional, default off)                     #
    # ------------------------------------------------------------------ #
    interval = int(run_env.runner_config.get("metrics_upload_interval_seconds", 0))
    if interval > 0:
        uploader.start_periodic_upload(interval, metrics_writer.snapshot_bytes)

    # ------------------------------------------------------------------ #
    # 5b. Start SQS queue monitor (optional, default off)                  #
    # ------------------------------------------------------------------ #
    sqs_monitor: SQSMonitor | None = None
    sqs_monitor_interval = int(run_env.runner_config.get("sqs_monitor_interval_seconds", 0))
    # sqs_monitor_arns in config is optional — omit to monitor all queues baked into the
    # task definition via SQS_QUEUE_ARNS (set to a comma-separated subset to filter).
    _cfg_arns = run_env.runner_config.get("sqs_monitor_arns")
    if _cfg_arns is None:
        # Not specified → fall back to task-def env var (all queues)
        _env_val = os.environ.get("SQS_QUEUE_ARNS", "").strip()
        sqs_monitor_arns: list[str] = [a.strip() for a in _env_val.split(",") if a.strip()]
    else:
        sqs_monitor_arns = list(_cfg_arns)
    if sqs_monitor_arns and sqs_monitor_interval > 0:
        try:
            sqs_monitor = SQSMonitor(
                arns=sqs_monitor_arns,
                interval_seconds=sqs_monitor_interval,
                run_id=run_env.run_id,
            )
            sqs_monitor.start()
        except ConfigError as exc:
            print(f"CONFIG ERROR: {exc}", flush=True)
            uploader.stop_periodic_upload()
            return exit_codes.CONFIG_ERROR

    # ------------------------------------------------------------------ #
    # 6. Run scenario                                                      #
    # ------------------------------------------------------------------ #
    counters: dict[str, Any] = {}
    error_summary: str | None = None
    outcome = "unknown"

    try:
        counters = scenario_fn(
            run_env=run_env,
            metrics_writer=metrics_writer,
        )
        outcome = "success"
        final_exit_code = exit_codes.SUCCESS

    except ConfigError as exc:
        outcome = "config_error"
        error_summary = str(exc)
        final_exit_code = exit_codes.CONFIG_ERROR
        print(f"CONFIG ERROR: {exc}", flush=True)

    except AuthError as exc:
        outcome = "auth_error"
        error_summary = str(exc)
        final_exit_code = exit_codes.AUTH_ERROR
        print(f"AUTH ERROR: {exc}", flush=True)

    except KeyboardInterrupt:
        outcome = "interrupted"
        error_summary = "Interrupted by signal"
        final_exit_code = exit_codes.RUNTIME_ERROR
        print("INTERRUPTED: signal received during scenario", flush=True)

    except Exception as exc:  # noqa: BLE001
        outcome = "runtime_error"
        error_summary = f"{type(exc).__name__}: {exc}"
        final_exit_code = exit_codes.RUNTIME_ERROR
        print(f"RUNTIME ERROR: {type(exc).__name__}: {exc}", flush=True)
        logger.exception("Unhandled scenario error")

    # ------------------------------------------------------------------ #
    # 7. Stop monitors                                                     #
    # ------------------------------------------------------------------ #
    if sqs_monitor is not None:
        sqs_monitor.stop()
    uploader.stop_periodic_upload()

    # ------------------------------------------------------------------ #
    # 8. Write + upload final artifacts (best-effort; never changes code)  #
    # ------------------------------------------------------------------ #
    finished_at = datetime.now(tz=timezone.utc).isoformat()

    summary = _build_summary(
        run_env=run_env,
        outcome=outcome,
        exit_code=final_exit_code,
        started_at=started_at,
        finished_at=finished_at,
        counters=counters,
        error_summary=error_summary,
    )

    try:
        summary_path.write_text(json.dumps(summary, indent=2))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write local summary.json: %s", exc)

    # Upload metrics snapshot then summary
    uploader.upload_metrics_snapshot(metrics_writer.snapshot_bytes())
    uploader.upload_summary(summary)

    # ------------------------------------------------------------------ #
    # 9. Final stdout line                                                 #
    # ------------------------------------------------------------------ #
    print(
        f"END run_id={run_env.run_id} outcome={outcome} exit_code={final_exit_code} "
        f"counters={json.dumps(counters)}",
        flush=True,
    )

    return final_exit_code
