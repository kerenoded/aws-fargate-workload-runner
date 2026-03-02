"""Explicit scenario registry.

SCENARIOS is the single source of truth for all valid scenario names.
Add new scenarios here — do not rely on dynamic imports or reflection.

To register a new scenario:
  1. Create src/awfr/scenarios/<name>.py with a module-level run() function.
  2. Import the module below.
  3. Add an entry to SCENARIOS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from awfr.scenarios import iot_core_topic_publish, sqs_enqueue

if TYPE_CHECKING:
    from awfr.config import RunEnv
    from awfr.metrics import MetricsWriter

# Keys must match the SCENARIO env var value exactly.
SCENARIOS: dict[str, Callable[[RunEnv, MetricsWriter], dict]] = {
    "iot_core_topic_publish": iot_core_topic_publish.run,
    "sqs_enqueue": sqs_enqueue.run,
}
