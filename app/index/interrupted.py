r"""Did the last index run end without finishing, and how far had it got?

Layer: L3

Work order `dates-live-log-and-interrupted-runs` 3a. **A run that is killed
leaves nothing wrong behind, and until now also nothing said.** Resume is per
file: every file the run had not finished is still `PENDING`, so the next run
simply carries on. But the person who pulled the plug, or whose machine
restarted overnight for an update, opened the window to an Indexing page that
looked exactly as it would after a run that had finished - and the sensible
conclusion from a page that says nothing is that the week-long index is gone.

**The evidence already exists, and this only reads it.** `run_lock.publish`
writes a description of the run under `run:active` when the lock is taken and
again at every checkpoint, and `IndexRunLock.release` takes it down however the
run ends - finished, stopped with the Stop button, stopped by the disk guard, or
ended by an exception. The operating system releases the mutex of a process that
dies, but nothing is left to take the row down. So **a record with its mutex
free is a run that did not end**, which is exactly the rule `run_lock` already
uses to refuse to treat a crashed run as a live one. A Pause is a run that is
still alive and still holds its mutex, so it is never reported here either.

Read on a worker, never on the UI thread: it touches the store and probes the
mutex (non-negotiable #5). Never raises: this is one line on a page and one
line on a terminal, and a failure to read it must cost that line only.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.logging import logger

__all__ = ["ARCHIVE_RESUME_PREFIX", "read_part_read_archives", "read_unfinished_run"]

#: `index_state` key prefix of an archive's folder cursor (0w 3b), written by
#: `Pipeline._persist_resume_progress` and read here for 3c. Declared here,
#: and imported by the pipeline, so the Indexing page's worker never has to
#: import the pipeline to know what to look for.
ARCHIVE_RESUME_PREFIX = "resume:archive:"

_log = logger.bind(component="index.interrupted")


def read_unfinished_run(store: Any, *, lock_dir: Any = None) -> Optional[dict[str, Any]]:
    """What the last run said about itself, if it ended without finishing.

    `None` when there is nothing to report: no record (the last run ended
    cleanly, or none ever ran), or a record whose lock is held (a run is going
    on right now, in this process or another).

    Otherwise a plain dict, so a presenter can word it without a store:

    * `stopped_at` - the last time the run wrote its record, which is at most a
      checkpoint before it stopped. "About", honestly, not "exactly".
    * `started_at`, `owner` - as the run published them.
    * `seen`, `indexed`, `walk_complete` - its last published counts.
    * `not_reached` - files it had not got to, or `None` when that cannot be
      known. Only a counting pass (`app.cli scan`) of the same folders knows
      how many files there are; without one, a walk that had not finished
      cannot say how much was left, and inventing a number is worse than
      saying so.
    """
    from app.core.run_lock import active_run, is_indexing

    try:
        record = active_run(store)
        if not record:
            return None
        if is_indexing(store, lock_dir=lock_dir):
            return None
    except Exception as exc:                     # noqa: BLE001 - one line, never the page
        _log.debug("could not read the last run's record: {}", exc)
        return None

    stats = record.get("stats") if isinstance(record.get("stats"), dict) else {}

    def number(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def moment(value: Any) -> Optional[float]:
        try:
            return float(value) if value else None
        except (TypeError, ValueError):
            return None

    seen = number(stats.get("seen"))
    found: dict[str, Any] = {
        "owner": str(record.get("owner") or ""),
        "started_at": moment(record.get("started_at")),
        "stopped_at": moment(record.get("updated_at")) or moment(record.get("started_at")),
        "seen": seen,
        "indexed": number(stats.get("indexed")),
        "walk_complete": bool(stats.get("walk_complete", False)),
        "not_reached": None,
    }

    roots = record.get("roots")
    if isinstance(roots, list) and roots:
        try:
            from app.index.scan import SCAN_STATE_KEY, saved_total

            total = saved_total(store.get_state(SCAN_STATE_KEY, "") or "", roots)
        except Exception as exc:                 # noqa: BLE001 - a count, not the finding
            _log.debug("no scan total for the unfinished run: {}", exc)
            total = 0
        # **A lower bound, and worded as one.** `seen` counts files the walker
        # had handed on, and the few still queued behind the workers when it
        # stopped were not reached either - so "about" is the true word, and
        # a total below `seen` (a scan from before files were added) is no
        # count at all rather than a negative one.
        if total > seen:
            found["not_reached"] = total - seen
    return found


def read_part_read_archives(store: Any) -> list[dict[str, Any]]:
    """Mail archives a run stopped inside, which the next run carries on with.

    Work order `dates-live-log-and-interrupted-runs` 3c. **Interruption, not
    damage** - the "Mail archives partly read" row is damage, and is a
    different fact from a different record (`warned_by_code`). This one is the
    folder cursor an archive keeps until it has been read to the end
    (`ARCHIVE_RESUME_PREFIX`): present means "not finished yet", and the
    archive's completion marker is what removes it.

    `[{"path", "name"}]`, sorted by path. An archive that is no longer on disk
    is left out: nothing will ever carry on with it, and its cursor is simply
    an orphan (a reset clears it). Never raises.
    """
    import json
    import os

    try:
        state = store.all_state()
    except Exception as exc:                     # noqa: BLE001 - one row, never the page
        _log.debug("could not read the archive cursors: {}", exc)
        return []

    found: list[dict[str, Any]] = []
    for key, raw in state.items():
        if not str(key).startswith(ARCHIVE_RESUME_PREFIX):
            continue
        try:
            path = str(json.loads(raw).get("path") or "")
        except (TypeError, ValueError, AttributeError):
            continue
        if not path or not os.path.exists(path):
            continue
        found.append({"path": path, "name": os.path.basename(path) or path})
    return sorted(found, key=lambda entry: entry["path"].lower())
