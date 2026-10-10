r"""The order a run reads files in: the folders chosen first, then newest first.

Layer: L3

**Scan, then sort, then read.** The walk and the cheap half of the change check
run first and produce a *work list*: every file that needs reading, with the
facts the walk already knows about it (size, date, priority). Nothing is read
while the list is being made. The list is then sorted and handed to the readers
in that order:

1. the folders the person marked "Index this folder first", in their order;
2. then the most recent, newest first - documents and mail archives mixed, each
   by its own date (a `.pst` is dated by its file's mtime, like anything else);
3. within one month of dates, small before large, so a single 4GB archive
   dated today does not hold back the three hundred letters dated today.

Before this, the walk streamed straight into a **bounded** priority queue
(`PipelineConfig.queue_size`, 256), so priorities only ever reordered the next
256 files. A folder marked "first" that happened to be walked last was read
last. Sorting the whole list is the only way "first" can mean first.

**Why the cheap half only.** A file never seen before is hashed by the change
check (`walker.has_changed`), and a hash is a full read. Hashing the whole corpus
before sorting would read every byte twice and put the first searchable file
behind the last hashed one. So the scan asks the question the row can answer
from `stat()` alone - is this file settled? - and the hash is taken when the
file's turn comes, just before it is queued, exactly where it always was.

**Kept on disk when large.** A `Candidate` in memory is about 760 bytes
(measured, 100,000 of them under `tracemalloc`), so a million would be most of
a gigabyte. Up to `SPILL_AT` entries stay in a list; past that the whole list
goes to a throwaway SQLite file beside the index and SQLite's own external
sort orders it, in bounded memory.

**Resume follows from the change check.** Nothing here is a cursor: an
interrupted run is resumed by walking again, when every file already read is
settled and drops out of the list. What is left sorts into the same order,
because the key is made from the file's own facts plus the walk's order - so
the next run carries on from where this one stopped.

"As found" (`ORDER_FOUND`) is the behaviour before this: the walk streams into
the queue, and a folder chosen first is favoured only within the queue's window.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import fields
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from app.index.walker import Candidate, HashDeferred

__all__ = [
    "ORDER_NEWEST",
    "ORDER_FOUND",
    "ORDERS",
    "DEFAULT_ORDER",
    "BUCKET_NS",
    "SPILL_AT",
    "FIRST_FOLDERS_STATE_KEY",
    "WorkList",
    "sort_key",
    "normalise_order",
    "load_first_folders",
    "dump_first_folders",
    "candidate_fields",
]

#: Newest first, mail and files mixed, after the folders chosen first.
ORDER_NEWEST = "newest"
#: The walk's own order, streamed: how every run behaved before this module.
ORDER_FOUND = "found"
ORDERS = (ORDER_NEWEST, ORDER_FOUND)
DEFAULT_ORDER = ORDER_NEWEST

#: One date bucket: thirty days. Within a bucket, small files go first.
#:
#: **A constant, not a setting** (non-negotiable 11). A month is the grain a
#: person remembers things by ("the letter from last month"), and nobody would
#: know what to set it to instead. Finer buckets approach pure date order,
#: which lets one huge file dated a minute later hold back everything
#: else from that day; coarser buckets approach pure size order, which reads
#: 2014's thumbnails before this week's reports. Evidence that would change
#: it: a measured corpus where the first searchable thousand is visibly the
#: wrong thousand.
BUCKET_NS = 30 * 24 * 3600 * 1_000_000_000

#: Entries kept in memory before the list moves to disk.
#:
#: **A constant, not a setting.** At ~760 bytes a candidate this is ~15MB,
#: which no machine this runs on notices, and it keeps an incremental run with
#: a few thousand changes from creating a file at all. Past it, memory stays
#: flat however large the corpus - which is the whole point.
SPILL_AT = 20_000

#: Where the Indexing folder list keeps "Index this folder first", in order.
#: JSON, a list of folders as the person added them. In `index_state` beside
#: the folder list itself (`ui:roots`) and its per-folder modes, because it is a
#: fact about those rows rather than a machine-wide preference.
FIRST_FOLDERS_STATE_KEY = "ui:index_first_folders"

#: Rows go to disk in batches of this many. One `executemany` per batch.
_INSERT_BATCH = 5_000

#: The `Candidate` fields the spill file stores, one column each. Built from
#: the dataclass so a field added to `Candidate` without a column here fails
#: `test_read_order` rather than silently arriving as its default.
_CANDIDATE_COLUMNS = (
    "path", "size_bytes", "mtime_ns", "priority", "volume_id",
    "relative_path", "attributes", "flags", "readable", "retry",
)


def normalise_order(value: Any) -> str:
    """`newest` or `found`. Anything else is the default, never an error: an
    order is a preference about speed, and no value of it can lose a file."""
    text = str(value or "").strip().lower()
    return text if text in ORDERS else DEFAULT_ORDER


def sort_key(candidate: Candidate, sequence: int) -> tuple[int, int, int, int]:
    """`(priority, -month, size, sequence)`: ascending sorts it as wanted.

    `sequence` is the walk's own order, the tie-break that makes the order
    the same on every run over the same files.
    """
    return (int(candidate.priority), -(int(candidate.mtime_ns) // BUCKET_NS),
            int(candidate.size_bytes), int(sequence))


def load_first_folders(raw: str) -> list[str]:
    """The folders marked "first", in order, from the stored JSON. Never raises:
    an unreadable record means none are marked, which loses nothing - every
    folder is still read, only not first."""
    if not raw:
        return []
    try:
        record = json.loads(raw)
    except Exception:                              # noqa: BLE001 - see docstring
        return []
    if not isinstance(record, list):
        return []
    out: list[str] = []
    for folder in record:
        text = str(folder or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def dump_first_folders(folders: Iterable[str]) -> str:
    """The stored form. **Order kept**, unlike the cloud and archive records:
    here the order is the setting."""
    out: list[str] = []
    for folder in folders:
        text = str(folder or "").strip()
        if text and text not in out:
            out.append(text)
    return json.dumps(out)


class WorkList:
    """Everything a run will read, gathered first and handed back sorted.

    `add` during the scan; `sorted()` once, afterwards. `close` removes the
    spill file, and is safe to call more than once. Used from one thread only
    (the pipeline's walker thread).
    """

    def __init__(self, spill_dir: Optional[Path] = None, *,
                 spill_at: Optional[int] = None) -> None:
        self._spill_dir = Path(spill_dir) if spill_dir else None
        # Read at construction, not at import, so a test can lower it.
        self._spill_at = max(1, int(SPILL_AT if spill_at is None else spill_at))
        self._memory: list[tuple[tuple[int, int, int, int], Candidate, Any]] = []
        self._pending: list[tuple] = []
        self._db: Optional[sqlite3.Connection] = None
        self._db_path: Optional[Path] = None
        self._count = 0

    def __len__(self) -> int:
        return self._count

    @property
    def spilled(self) -> bool:
        """True once the list has moved to disk."""
        return self._db is not None

    @property
    def spill_path(self) -> Optional[Path]:
        return self._db_path

    def add(self, candidate: Candidate, decision: Any) -> None:
        """One file that needs reading, and what the scan decided about it."""
        key = sort_key(candidate, self._count)
        self._count += 1
        if self._db is None:
            self._memory.append((key, candidate, decision))
            if len(self._memory) > self._spill_at:
                self._spill()
            return
        self._pending.append(_row(key, candidate, decision))
        if len(self._pending) >= _INSERT_BATCH:
            self._flush()

    def sorted(self) -> Iterator[tuple[Candidate, Any]]:
        """Every entry, in reading order. Streams from disk when spilled."""
        if self._db is None:
            self._memory.sort(key=lambda entry: entry[0])
            memory, self._memory = self._memory, []
            for _key, candidate, decision in memory:
                yield candidate, decision
            return
        self._flush()
        cursor = self._db.execute(
            "SELECT " + ", ".join(_CANDIDATE_COLUMNS) + ", decision, has_decision "
            "FROM work ORDER BY k0, k1, k2, k3")
        while True:
            rows = cursor.fetchmany(1_000)
            if not rows:
                return
            for row in rows:
                yield _candidate_from(row), _decision_from(row[-2], row[-1])

    def close(self) -> None:
        """Drop the list and delete the spill file and its sidecars. Safe to
        call twice; a file that cannot be removed is left for the system."""
        self._memory = []
        self._pending = []
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None
        if self._db_path is not None:
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    os.remove(str(self._db_path) + suffix)
                except OSError:
                    pass
            self._db_path = None

    def __enter__(self) -> "WorkList":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- the disk half -------------------------------------------------------

    def _spill(self) -> None:
        """Past `SPILL_AT`: move everything gathered so far into a throwaway
        SQLite file and add to that from here on. Falls back to the system's
        temporary folder when the spill folder cannot be made."""
        folder = self._spill_dir
        if folder is not None:
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError:
                folder = None
        handle, name = tempfile.mkstemp(
            prefix="leasha-worklist-", suffix=".db",
            dir=str(folder) if folder is not None else None)
        os.close(handle)
        self._db_path = Path(name)
        # `isolation_level=None` and one explicit transaction per batch: the
        # file is thrown away at the end, so nothing here needs to survive a
        # crash - journaling and syncing would only slow the scan down.
        db = sqlite3.connect(name, isolation_level=None, check_same_thread=False)
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA synchronous=OFF")
        db.execute("PRAGMA temp_store=FILE")
        db.execute(
            "CREATE TABLE work (k0 INTEGER, k1 INTEGER, k2 INTEGER, k3 INTEGER, "
            "path TEXT, size_bytes INTEGER, mtime_ns INTEGER, priority INTEGER, "
            "volume_id INTEGER, relative_path TEXT, attributes INTEGER, "
            "flags INTEGER, readable INTEGER, retry INTEGER, decision TEXT, "
            "has_decision INTEGER)")
        self._db = db
        memory, self._memory = self._memory, []
        self._pending = [_row(key, candidate, decision)
                         for key, candidate, decision in memory]
        self._flush()

    def _flush(self) -> None:
        """Write the pending rows in one transaction. No-op in memory mode."""
        if self._db is None or not self._pending:
            return
        rows, self._pending = self._pending, []
        self._db.execute("BEGIN")
        self._db.executemany(
            "INSERT INTO work VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows)
        self._db.execute("COMMIT")


#: `has_decision` values in the spill file. A deferred hash (2026-10-10, W2)
#: has a value of its own: written as its bare `known_hash` under 1 it would
#: come back as a digest - the old hash, stored on the row after the contents
#: moved.
_NO_DECISION, _DECISION, _DEFERRED_HASH = 0, 1, 2


def _row(key: tuple[int, int, int, int], candidate: Candidate, decision: Any) -> tuple:
    # `decision` is None ("changed, no hash"), "" ("could not tell, read it"),
    # a hash, or a `HashDeferred`. None and "" mean different things
    # downstream, so whether there is one travels in its own column rather
    # than as a NULL.
    if isinstance(decision, HashDeferred):
        stored, kind = decision.known_hash, _DEFERRED_HASH
    elif decision is None:
        stored, kind = None, _NO_DECISION
    else:
        stored, kind = str(decision), _DECISION
    return (*key, str(candidate.path), candidate.size_bytes, candidate.mtime_ns,
            candidate.priority, candidate.volume_id, candidate.relative_path,
            candidate.attributes, candidate.flags, int(bool(candidate.readable)),
            int(bool(candidate.retry)), stored, kind)


def _decision_from(stored: Any, kind: Any) -> Any:
    """The decision back from its two spill columns - the reverse of `_row`."""
    if kind == _DEFERRED_HASH:
        return HashDeferred(str(stored))
    return stored if kind else None


def _candidate_from(row: tuple) -> Candidate:
    """A `Candidate` back from one spill row, column for column with `_row`."""
    return Candidate(
        path=Path(row[0]), size_bytes=row[1], mtime_ns=row[2], priority=row[3],
        volume_id=row[4], relative_path=row[5], attributes=row[6], flags=row[7],
        readable=bool(row[8]), retry=bool(row[9]))


def candidate_fields() -> tuple[str, ...]:
    """Every field `Candidate` has - for the test that holds the spill to it."""
    return tuple(f.name for f in fields(Candidate))
