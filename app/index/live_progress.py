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

* **Lanes** (`Lane`, `LaneBoard`, 2026-10-14, "nothing silent"). Every
  other thread a run uses - the walker, the picture worker, the meaning
  model's feeder, the writer, the run's own thread doing its passes, and the
  photo passes beside the meaning model - has a line too, in plain words:
  what it is doing, on which file, how many so far. Before this a run that
  was only looking for faces showed idle readers and read as stuck. The
  readers keep their slots exactly as they were.

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
    "STAGE_CHUNKING", "STAGE_HANDING_OVER", "STAGE_EMBEDDING", "STAGE_WRITING",
    "STAGE_SAVING_RESUME",
    "STAGES", "WorkerSlot", "WorkerBoard",
    "LANE_WALKER", "LANE_PICTURES", "LANE_MEANING", "LANE_WRITER", "LANE_PASS",
    "LANE_BESIDE", "LANE_FACES", "LANE_DESCRIBE", "LANE_PICTURE_TEXT", "LANE_CARDS",
    "LANES", "LANE_LABELS", "Lane", "LaneBoard", "NULL_LANE",
    "PHASE_WORDS",
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
#: A reader has finished its document and is waiting to hand it to the writer,
#: because the writer's queue is full (it is usually embedding a batch). Set by
#: `pipeline._offer` only while the hand-over actually waits. 2026-10-05: before
#: this, three readers sat on a small `.md` "for 30 s" and looked stuck.
STAGE_HANDING_OVER = "handing_over"
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
    STAGE_ATTACHMENTS, STAGE_ZIP, STAGE_OCR, STAGE_CHUNKING, STAGE_HANDING_OVER,
    STAGE_EMBEDDING,
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
        #: 2026-10-14: the run's other threads, beside the readers' slots.
        self.lane_board = LaneBoard()
        #: 2026-10-14: a callable giving the cockpit's resource readings as
        #: plain values, set by the run (`Pipeline._resources_reading`); None
        #: leaves `IndexStats.resources` as it is.
        self.resources: Any = None

    def lane(self, lane_id: str) -> "Lane":
        """The run's lane `lane_id` (a `LANE_*`), opened on first use."""
        return self.lane_board.lane(lane_id)

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


def signature(counters: tuple, workers: dict[str, dict[str, Any]],
              lanes: Optional[dict[str, dict[str, Any]]] = None) -> tuple:
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
                      # Order 0z lane C: a mail archive's reader moves `beat`
                      # for every item, attachments and failures included.
                      last.get("beat"),
                      len(inner)))
    # 2026-10-14: the lanes too, so a run that is only grouping faces or
    # describing photos never reads as "no progress".
    return (counters, tuple(parts), lane_signature(lanes))


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


# ---------------------------------------------------------------------------
# Lanes: every other thread of a run, in plain words (2026-10-14)
# ---------------------------------------------------------------------------

#: What the detail line says while the run is in a stretch it cannot count,
#: keyed by `pipeline.PHASE_*`. A phase absent from here - `reading`, or none
#: yet - is drawn from the counts as usual.
#:
#: **Every one of these used to be a bar that did not move.** Loading the
#: model, catching up on a previous run, building the vector index at the end:
#: minutes each on a large index, with no tick at all, so the page showed
#: whatever it showed last and the run was taken for a hang. A busy bar with a
#: sentence beside it is the honest picture - something is happening, this is
#: what, and nobody can say how long.
#:
#: **Lives here, not in the presenter**, since 2026-10-14: the "Run's end"
#: lane says these same words for the same stretch, and this layer cannot
#: import the presenter. `app/ui/presenter/indexing.py` re-exports it. The
#: ellipsis is escaped because Python source here is ASCII.
PHASE_WORDS: dict[str, str] = {
    "model": "Getting the search model ready\u2026",
    "word_index_check": "Checking the word index\u2026",
    "catch_up": "Finishing what the last run left undone\u2026",
    "planning": "Working out which folders to read\u2026",
    # 2026-09-29, `read_order` "newest": the whole walk before the first read.
    # The count is the headline's "files seen", climbing as it goes.
    "scanning": "Finding files, to read the newest first\u2026",
    "media": "Reading videos and recordings\u2026",
    # 2026-10-04: Florence-2 tags, moved to the end of a run.
    "photo_tags": "Describing the photos that have no text in them\u2026",
    "picture_text": "Reading any text in the pictures, last of all\u2026",
    # 2026-10-12, order smarter-answers 2c.
    "doc_cards": "Writing a card for each document - what it is, who and when\u2026",
    # 2026-10-08: text first - every file is findable by its words already.
    "meaning": "Words are searchable now. Working out what they mean\u2026",
    "tidying": "Tidying up the index\u2026",
    "vector_index": "Organising the index so searches stay quick\u2026",
    "word_index": "Tidying the word index so searches stay quick\u2026",
}

#: Lane ids. A fixed set, so a page can give each its own row in a fixed
#: order (`LANES`) rather than reflowing as threads come and go.
LANE_WALKER = "walker"
LANE_PICTURES = "pictures"
LANE_MEANING = "meaning"
LANE_WRITER = "writer"
#: The run's own thread outside the reading: the catch-up at the start
#: (`_run_enrichment_drains`) and every pass at the end.
LANE_PASS = "pass"
#: The photo passes beside the meaning model - present only while that
#: thread lives (`Pipeline._start_pictures_beside_meaning`).
LANE_BESIDE = "beside"
#: 2026-10-14, order section 5b: the run's end passes side by side under the
#: conductor (`app/index/conductor.py`), each on its own line. Present only
#: while that pass's thread lives; "pass" keeps the sequential tail (tidying,
#: the vector and word indexes) and every pass when the conductor is off.
LANE_FACES = "faces"
LANE_DESCRIBE = "describe"
LANE_PICTURE_TEXT = "picture_text"
LANE_CARDS = "cards"

LANES = (LANE_WALKER, LANE_PICTURES, LANE_MEANING, LANE_WRITER, LANE_PASS,
         LANE_BESIDE, LANE_FACES, LANE_DESCRIBE, LANE_PICTURE_TEXT, LANE_CARDS)
LANE_LABELS: dict[str, str] = {
    LANE_WALKER: "Finding files",
    LANE_PICTURES: "Pictures",
    LANE_MEANING: "Meaning",
    LANE_WRITER: "Writing",
    LANE_PASS: "Run's end",
    LANE_BESIDE: "Pictures beside meaning",
    LANE_FACES: "Faces",
    LANE_DESCRIBE: "Describing photos",
    LANE_PICTURE_TEXT: "Text in pictures",
    LANE_CARDS: "Document cards",
}

#: A lane's `state`. "waiting" is blocked on something outside the lane (an
#: empty or full queue); "idle" has nothing in hand; "done" will do nothing
#: more this run.
LANE_WORKING = "working"
LANE_IDLE = "idle"
LANE_WAITING = "waiting"
LANE_PAUSED = "paused"
LANE_DONE = "done"

#: A lane's `device`: where its model runs, in the page's words.
DEVICE_CPU = "processor"
DEVICE_GPU = "graphics card"

#: A lane's `task` for the work no `PHASE_WORDS` sentence names. The passes
#: that do have a phase (describing photos, reading text from pictures, the
#: document cards, tidying) say that phase's own sentence instead, so the
#: same stretch is never called two things on one page.
TASK_FINDING = "Finding files"
TASK_PICTURE_SEARCH = "Picture search"
TASK_DUPLICATE = "Duplicate fingerprint"
TASK_FACES = "Looking for faces"
TASK_VIDEO = "Video pictures"
TASK_SAVING_PICTURES = "Saving picture search"
TASK_GROUPING_FACES = "Grouping faces"
TASK_FACE_BACKFILL = "Looking for faces in photos read before"
TASK_DESCRIBING = "Describing photos"
TASK_MEANING = "Making text searchable by meaning"
TASK_WRITING = "Writing"
TASK_SAVING_RESUME = "Saving where to resume from"

#: What `Lane.set` leaves alone when a keyword is not given.
_KEEP: Any = object()


class Lane:
    """One non-reader thread's line: what it is doing, on what, how far.

    Written by the thread that owns the lane (for "pass", whichever pass is
    running on the run's thread), read by the snapshot. Each write takes the
    board's lock - one uncontended acquire, a few attribute stores and at most
    one `now_wall()` - so the writer, which calls it once per document, pays
    well under a microsecond for it.
    """

    __slots__ = ("id", "label", "state", "task", "item", "path", "started_at",
                 "done", "total", "device", "_lock")

    def __init__(self, lane_id: str, lock: Any) -> None:
        self.id = lane_id
        self.label = LANE_LABELS.get(lane_id, lane_id)
        self.state = LANE_IDLE
        self.task = ""
        self.item = ""
        self.path = ""
        self.started_at = 0.0
        self.done = 0
        self.total = 0
        self.device = ""
        self._lock = lock

    def set(self, *, task: Any = _KEEP, item: Any = _KEEP, path: Any = _KEEP,
            state: str = LANE_WORKING, total: Any = _KEEP, done: Any = _KEEP,
            device: Any = _KEEP) -> None:
        """Say what the lane is doing now. Unnamed fields keep their value.

        `started_at` moves only when the task, the item or the state changes,
        so a lane that repeats the same words does not look as if it had just
        begun. `item` may be a `Path` (its name is kept, and the whole path
        when no `path` is given) or a string kept as it is ("batch 3 of 9").
        """
        if item is not _KEEP and not isinstance(item, str):
            if path is _KEEP:
                path = str(item)
            item = getattr(item, "name", "") or str(item)
        with self._lock:
            moved = ((task is not _KEEP and task != self.task)
                     or (item is not _KEEP and item != self.item)
                     or state != self.state)
            if task is not _KEEP:
                self.task = str(task)
            if item is not _KEEP:
                self.item = str(item)
            if path is not _KEEP:
                self.path = str(path)
            if total is not _KEEP:
                self.total = int(total or 0)
            if done is not _KEEP:
                self.done = int(done or 0)
            if device is not _KEEP:
                self.device = str(device or "")
            if moved or not self.started_at:
                self.started_at = now_wall() if state == LANE_WORKING else 0.0
            self.state = state

    def tick(self, n: int = 1) -> None:
        """`n` more done this run. No clock."""
        with self._lock:
            self.done += n

    def idle(self, state: str = LANE_IDLE) -> None:
        """Nothing in hand: task, item and start cleared, counts kept."""
        with self._lock:
            self.state = state
            self.task = ""
            self.item = ""
            self.path = ""
            self.started_at = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Plain values for a snapshot. Called under the board's lock."""
        return {
            "lane": self.id, "label": self.label, "state": self.state,
            "task": self.task, "item": self.item, "path": self.path,
            "started_at": self.started_at, "done": self.done,
            "total": self.total, "device": self.device,
        }


class _NullLane:
    """A lane that goes nowhere, for code running with no board (a pipeline a
    test built without `run()`, a snapshot). The same calls, no effect."""

    __slots__ = ()

    def set(self, **_kwargs: Any) -> None:
        return None

    def tick(self, n: int = 1) -> None:
        return None

    def idle(self, state: str = LANE_IDLE) -> None:
        return None


NULL_LANE: Any = _NullLane()


class LaneBoard:
    """Every lane of one run. Owned by the run's `WorkerBoard`.

    One lock for all of them: a lane write is a handful of stores, so the
    lock is held for less time than a second lock would cost to take, and
    the snapshot reads every lane consistently under it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lanes: dict[str, Lane] = {}

    def lane(self, lane_id: str) -> Lane:
        """The lane, opened on first use. Callers that loop keep the result."""
        lane = self._lanes.get(lane_id)
        if lane is not None:
            return lane
        with self._lock:
            lane = self._lanes.get(lane_id)
            if lane is None:
                lane = Lane(lane_id, self._lock)
                self._lanes[lane_id] = lane
            return lane

    def drop(self, lane_id: str) -> None:
        """The lane's thread has ended and its row should go."""
        with self._lock:
            self._lanes.pop(lane_id, None)

    def lanes(self) -> dict[str, dict[str, Any]]:
        """Every open lane as plain values, in `LANES` order."""
        with self._lock:
            ordered = sorted(self._lanes.items(),
                             key=lambda kv: (LANES.index(kv[0]) if kv[0] in LANES
                                             else len(LANES), kv[0]))
            return {lane_id: lane.as_dict() for lane_id, lane in ordered}


def lane_signature(lanes: Optional[dict[str, dict[str, Any]]]) -> tuple:
    """The lanes' share of the heartbeat's signature: a run that is only
    looking for faces moves here and nowhere else."""
    return tuple((key, lane.get("state"), lane.get("task"), lane.get("item"),
                  lane.get("done"), lane.get("total"))
                 for key, lane in (lanes or {}).items())


def plain_lanes(lanes: Optional[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """A copy of a `lanes` map with no dict shared - for `IndexStats.snapshot`."""
    return {key: dict(lane) for key, lane in (lanes or {}).items()}
