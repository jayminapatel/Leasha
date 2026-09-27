r"""What each reader is doing right now, and when anything last moved.

Layer: L3

Work order 0x sections 3a, 3c and 3d. `IndexStats` used to keep **one**
`current` file name, written by whichever extraction thread wrote last - so
with four readers busy the page showed one name that flickered between four
files, and said nothing about the other three. This module gives each reader
its own line (a *slot*), and gives the run a heartbeat: the moment anything
last moved, so the page can say "working, last activity 2 s ago" and, when
nothing has moved for a while, say so plainly.

## The three pieces

* **Stage codes** (`STAGE_*`, `STAGES`). Short keys for what a reader, the
  writer or the embedder is doing. The reader-level ones come from
  `app/extract/progress.py` - a reader (Layer 2) cannot import this layer -
  and the rest are defined here. The words for every code live in the
  presenter (`app/ui/presenter/live_progress.py`, `STAGE_WORDS`).
* **`WorkerSlot`**. One per extraction thread: which file, since when, which
  stage, how many documents so far, and the reader's own frame stack from
  `app.extract.progress` (message 812 of 2,000, and so on).
* **`WorkerBoard`**. The set of slots for one run, and the heartbeat. It turns
  the live slots into plain dictionaries when a snapshot is taken - never
  before - and notices whether anything changed since the last snapshot.

## Why the heartbeat is worked out at snapshot time

The obvious way to keep "last activity" is to stamp the time whenever
anything happens. On an archive of 100,000 messages that is 100,000 calls to
the clock for a number read once a second. Instead the board remembers a
small *signature* of the run at each snapshot - counters, and each slot's
file and position - and moves `last_activity` forward only when the
signature differs from last time. The readers pay nothing extra, and the
resolution is the snapshot interval (about a second, see
`pipeline.HEARTBEAT_SECONDS`), which is all "2 s ago" needs.

## Thread safety, in one paragraph

Slots are opened and closed under a lock (once per worker thread, so the
lock costs nothing). Everything written per file or per message is a plain
attribute store, which in CPython cannot be seen half-done. The snapshot
copies the slot table with one `list(...)` call and each frame stack with
another, then reads plain values - the worst it can see is a counter one
message stale. Every value it produces is an `int`, `float`, `str`, `None`,
`list` or `dict`, so a snapshot goes to JSON as it is: the indexer is moving
into its own process (0x section 2) and streams its progress as JSON lines.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from app.extract import progress as reader_progress
from app.extract.progress import (
    STAGE_ATTACHMENTS,
    STAGE_FOLDER,
    STAGE_MESSAGES,
    STAGE_OCR,
    STAGE_OPENING,
    STAGE_ZIP,
)

__all__ = [
    "STAGE_FINDING", "STAGE_READING", "STAGE_OPENING", "STAGE_FOLDER",
    "STAGE_MESSAGES", "STAGE_ATTACHMENTS", "STAGE_ZIP", "STAGE_OCR",
    "STAGE_CHUNKING", "STAGE_EMBEDDING", "STAGE_WRITING", "STAGE_SAVING_RESUME",
    "STAGES", "WorkerSlot", "WorkerBoard",
]

#: The walker is still finding files. A run-level stage: the walker is its own
#: thread and has no slot, so the presenter shows it from `walk_complete`.
STAGE_FINDING = "finding"
#: A reader has a file open and no finer stage has been said - an ordinary
#: document, or the moment before an archive reader opens its first frame.
STAGE_READING = "reading"
#: The text that came out of a file is being cut into passages. Runs on the
#: extraction thread, straight after each document is read.
STAGE_CHUNKING = "chunking"
#: A batch of passages is being turned into vectors, "batch n of m". On the
#: feeder thread; see `IndexStats.embed_batch`.
STAGE_EMBEDDING = "embedding"
#: Rows are being written to the index. On the consumer thread.
STAGE_WRITING = "writing"
#: An archive's resume point is being saved, which waits for the embedder to
#: finish what it was handed first. On the consumer thread.
STAGE_SAVING_RESUME = "saving_resume"

#: Every stage, in the order a person meets them in a run. Work order 0x 3a's
#: fixed list: finding files, opening an archive, reading a folder, reading
#: messages, extracting attachments, reading inside a zip, OCR, chunking,
#: embedding, writing, saving the resume point - plus plain "reading" for a
#: file that is not an archive at all.
STAGES = (
    STAGE_FINDING, STAGE_READING, STAGE_OPENING, STAGE_FOLDER, STAGE_MESSAGES,
    STAGE_ATTACHMENTS, STAGE_ZIP, STAGE_OCR, STAGE_CHUNKING, STAGE_EMBEDDING,
    STAGE_WRITING, STAGE_SAVING_RESUME,
)


class WorkerSlot:
    """One extraction thread's line on the page.

    Written only by the thread that owns it, read by whoever takes a
    snapshot. `__slots__` so the per-document stores (`item`, `stage`) are as
    cheap as an attribute store can be.

    * `id` - a small number, 1, 2, 3..., stable for the thread's life. A
      string in the snapshot, because JSON object keys are strings.
    * `file` / `path` - the file's name and its whole path; "" when idle.
    * `started_at` - wall-clock `time.time()` when this file was opened.
      Wall clock rather than `monotonic` because the snapshot may be read in
      another process, and only the wall clock means the same thing there.
    * `stage` - a `STAGE_*` code.
    * `item` - documents produced from this file so far.
    * `frames` - the reader's frame stack, shared with `app.extract.progress`
      through `attach`, so the reader writes it and this slot reads it.
    """

    __slots__ = ("id", "file", "path", "started_at", "stage", "item", "frames")

    def __init__(self, slot_id: int) -> None:
        self.id = slot_id
        self.file = ""
        self.path = ""
        self.started_at = 0.0
        self.stage = ""
        self.item = 0
        self.frames: list[reader_progress.Frame] = []

    def begin(self, path: Any) -> None:
        """A new file. Called once per file, never per message."""
        # Anything a reader left behind (a generator abandoned mid-archive and
        # not yet collected) belongs to the previous file, not to this one.
        self.frames.clear()
        self.item = 0
        self.stage = STAGE_READING
        self.started_at = time.time()
        self.path = str(path)
        self.file = getattr(path, "name", "") or self.path

    def end(self) -> None:
        """The file is finished, however it finished. The slot goes idle."""
        self.frames.clear()
        self.file = ""
        self.path = ""
        self.stage = ""
        self.item = 0
        self.started_at = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Plain values for a snapshot. Reads, never writes."""
        return {
            "file": self.file,
            "path": self.path,
            "started_at": self.started_at,
            "stage": self.stage,
            "item": self.item,
            "inner": reader_progress.trail(self.frames),
        }


class WorkerBoard:
    """Every reader's slot for one run, and the run's heartbeat.

    One per `IndexStats`, created with it. **Deliberately not a dataclass
    field**: it holds live objects, and `IndexStats`' fields are what gets
    copied into snapshots and serialised. The plain results it produces are
    what go on the fields (`IndexStats.workers`, `last_activity`).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: dict[int, WorkerSlot] = {}
        self._next_id = 1
        self._signature: Any = None

    def open_slot(self) -> WorkerSlot:
        """A slot for the calling extraction thread. Once per thread.

        The lowest free number is reused, so a worker that ends and one that
        starts in its place (§6g grows the pool mid-run) keep the lines on the
        page numbered 1..N rather than climbing for ever.
        """
        with self._lock:
            slot_id = 1
            while slot_id in self._slots:
                slot_id += 1
            slot = WorkerSlot(slot_id)
            self._slots[slot_id] = slot
            return slot

    def close_slot(self, slot: WorkerSlot) -> None:
        """The thread is ending; its line goes."""
        with self._lock:
            if self._slots.get(slot.id) is slot:
                del self._slots[slot.id]

    def workers(self) -> dict[str, dict[str, Any]]:
        """Every slot as plain values, keyed "1", "2"..., in number order."""
        with self._lock:
            slots = sorted(self._slots.items())
        return {str(slot_id): slot.as_dict() for slot_id, slot in slots}

    def moved(self, signature: Any) -> bool:
        """True if `signature` differs from the one seen last time.

        Called once per snapshot, from one thread (the run's own), so it needs
        no lock of its own.
        """
        if signature == self._signature:
            return False
        self._signature = signature
        return True


def signature(counters: tuple, workers: dict[str, dict[str, Any]]) -> tuple:
    """A small, comparable summary of "where the run is", for the heartbeat.

    Counters (documents, files seen, chunks, vectors and so on) catch the
    consumer, walker and embedder moving; each worker's file, item count,
    stage and innermost position catch a reader moving inside one file.
    """
    parts = []
    for key, worker in workers.items():
        inner = worker.get("inner") or []
        last = inner[-1] if inner else {}
        parts.append((key, worker.get("file"), worker.get("item"),
                      worker.get("stage"), last.get("n"), last.get("where"),
                      last.get("stage"), last.get("detail"),
                      len(inner)))
    return (counters, tuple(parts))


def now_wall() -> float:
    """The wall clock, in one place so tests can see what is used."""
    return time.time()


def plain_copy(workers: Optional[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """A copy of a `workers` map deep enough that nobody shares a list.

    Used by `IndexStats.snapshot`: the generic shallow copy there would leave
    each worker's dict (and its `inner` list) shared between the live stats
    and the snapshot handed to the window.
    """
    out: dict[str, dict[str, Any]] = {}
    for key, worker in (workers or {}).items():
        copied = dict(worker)
        copied["inner"] = [dict(frame) for frame in worker.get("inner") or []]
        out[key] = copied
    return out
