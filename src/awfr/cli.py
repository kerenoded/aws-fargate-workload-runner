"""awfr CLI entry point.

Usage (in-container):
  awfr worker

Usage (laptop, via tools/):
  python tools/run_task.py --scenario iot_core_topic_publish ...
  python tools/fetch_run.py <RUN_ID>
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("Usage: awfr <command>")
        print("Commands: worker, scenarios")
        sys.exit(1)

    cmd = args[0]

    if cmd == "worker":
        from awfr.worker import run
        sys.exit(run())

    elif cmd == "scenarios":
        from awfr.scenarios.registry import SCENARIOS
        for name in sorted(SCENARIOS):
            print(name)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
