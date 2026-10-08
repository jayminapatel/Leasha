"""Is the window keeping up? The implementation is `app.core.lag_monitor`.

Layer: L5

Moved down on 2026-10-08 so the pipeline bench (`app/index/pipeline_bench.py`)
can measure the window's own monitor without `app/index` importing from
`app/ui`. Every public name is re-exported here unchanged: `app.main` installs
it as `window.lag_monitor`, the indexer yields to it, and the tests import it
from this path. A test that needs to change a threshold should patch
`app.core.lag_monitor`, where the functions look their names up.
"""

from __future__ import annotations

from app.core.lag_monitor import (  # noqa: F401 - re-exports
    BEAT_MS, DUMP_INTERVAL_S, LONG_STALL_S, NOTE_INTERVAL_S, RECENT_WINDOW_S, STALL_S,
    SWITCH_INTERVAL_S, LagMonitor, install, log, tighten_switch_interval,
)

__all__ = ["LagMonitor", "install", "tighten_switch_interval",
           "BEAT_MS", "STALL_S", "SWITCH_INTERVAL_S",
           "DUMP_INTERVAL_S", "NOTE_INTERVAL_S", "RECENT_WINDOW_S", "LONG_STALL_S", "log"]
