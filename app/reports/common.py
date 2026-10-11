r"""What every report states about its data: the moment it was taken from.

Layer: L4 (reports) - reads L1 (`SqliteStore`) only.

Order 1q D9 (2026-10-11): these lived in `inheritance.py`, and the Space Report
and the timeline borrowed them from there. Digital Inheritance went; they stay.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from app.core.logging import logger

__all__ = ["report_generated_at", "data_timestamp_sentence"]

_log = logger.bind(component="reports.common")


def report_generated_at(store: Any) -> Optional[int]:
    r"""1b: "every report view states its data timestamp." A report is a
    snapshot of the catalogue, honest like everything else - this is the
    moment that snapshot was taken from, not the moment the report was
    drawn on screen.

    `MAX(files.indexed_at)` - the most recent file the index actually
    read - rather than inventing a separate "last run finished" marker
    that would need its own migration and its own place to be wrong.
    """
    try:
        row = store.conn.execute(
            "SELECT MAX(indexed_at) AS latest FROM files"
        ).fetchone()
    except Exception as exc:                      # noqa: BLE001 - a timestamp, not the report
        _log.debug("could not read the report's data timestamp: {}", exc)
        return None
    latest = row["latest"] if row is not None else None
    return int(latest) if latest else None


def data_timestamp_sentence(generated_at: Optional[int]) -> str:
    r"""1b's own words: "from the index as of last run, <date>." `""` for
    a fresh, empty index with nothing indexed yet - stating a timestamp
    that does not exist would be inventing one.
    """
    if not generated_at:
        return "This index has nothing recorded yet."
    when = time.strftime("%d %B %Y", time.localtime(int(generated_at)))
    return f"From the index as of last run, {when}."
