r"""What an index run is doing, as a short timestamped story.

Layer: L3

Work order 0w §2a. **The Indexing page could say how far a run had got, never
what it was doing.** Its only list was the run's notices, plain strings with no
time, so "it has sat on the same file for ten minutes" and "it has been
pausing for the battery all afternoon" looked the same from the page. The
application log has the answer, but that log is every component's commentary
at once, in a pane on another page - not the run's own account of itself.

So the run keeps its own: a few hundred `ActivityEntry` rows, each a time, a
kind and a line of data, recorded at the moments worth telling - a phase
starting, a large file started, a pause and why, a warning, a notice. The
words live in the presenter (`app/ui/presenter/activity.py`); this module only
stores what happened and when, so the wording can change without touching
the pipeline.

**Bounded, and free when nobody reads it.** A `deque(maxlen=...)` drops the
oldest line as a new one arrives, so a run of days holds the same few hundred
entries as a run of minutes. Recording is one lock and one append at an event
that is already rare - dozens a run, not one per file - and nothing is
formatted until somebody asks for a line.

**Thread-safe, because four kinds of thread write it.** The walker's governor
check reports pauses, extraction workers report large files, the consumer
reports warnings and notices, and the interface thread reports the person's
own pause. One lock covers the sequence counter and the append together, so
sequence numbers are strictly increasing in the order entries were stored and
`since` never misses one.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from pathlib import Path
from typing import NamedTuple, Optional

__all__ = [
    "ACTIVITY_LIMIT",
    "ActivityEntry",
    "ActivityLog",
    "KIND_ARCHIVE",
    "KIND_FINISHED",
    "KIND_LARGE_FILE",
    "KIND_NOTICE",
    "KIND_PAUSE",
    "KIND_PHASE",
    "KIND_RESUME",
    "KIND_STOPPING",
    "KIND_WARNING",
    "LARGE_CONTAINER_BYTES",
    "LARGE_FILE_BYTES",
    "large_file_kind",
]

#: Entries kept per run. The page shows fewer than this; the rest is headroom
#: for a burst between two paints. Fixed rather than tunable (non-negotiable
#: #11): a run records dozens of entries, not thousands, so nobody would ever
#: move it. Evidence that would change it: a real run whose story needed more
#: than this to tell between two glances at the page.
ACTIVITY_LIMIT = 500

#: The kinds. **Keys, not words** - the presenter owns every sentence.
KIND_PHASE = "phase"          # text: a `pipeline.PHASE_*` key
KIND_LARGE_FILE = "large_file"  # text: file name; size; detail: see `large_file_kind`
KIND_PAUSE = "pause"          # text: the governor's reason; detail: its cause
KIND_RESUME = "resume"        # text: ""
KIND_WARNING = "warning"      # text: a plain sentence, already worded
KIND_NOTICE = "notice"        # text: the notice, exactly as `IndexStats.notices` holds it
KIND_STOPPING = "stopping"    # text: ""
KIND_FINISHED = "finished"    # text: "" for a whole run, "stopped" for one that was not
#: 0w 3b/3c. text: the archive's file name; detail: "resumed" (carrying on at
#: the folder an earlier run reached) or "part_read" (this run stopped in it).
KIND_ARCHIVE = "archive"

#: **What counts as a large file**, and so earns a line of its own when a
#: worker starts it. The point of the line is the one file that holds a worker
#: for minutes, which is what made a run look hung: a mail archive, a zip, or
#: a video or recording, whose cost grows with what is inside rather than with
#: anything the walk can see - those from `LARGE_CONTAINER_BYTES`. Anything
#: else only from `LARGE_FILE_BYTES`, because an ordinary document that big is
#: unusual enough to be worth a line and small ones are the thousands a
#: minute nobody wants listed.
#:
#: Fixed constants, not settings (non-negotiable #11): the only effect of
#: either is how chatty the log is. Chosen from the shape of the problem, not
#: measured - a 25MB archive is a few thousand messages and tens of seconds;
#: below that a file is gone before anybody could read the line. Evidence that
#: would change them: a real run log where the page sat on a file for minutes
#: with no line saying so (lower), or where the log was mostly these (higher).
LARGE_CONTAINER_BYTES = 25 * 1024 * 1024
LARGE_FILE_BYTES = 100 * 1024 * 1024

#: Kept here rather than imported from the extractors, because the pipeline
#: asks for every file a worker starts and importing `email_pst` or `media`
#: for a set of suffixes would load far more than a set of suffixes.
#: `tests/unit/test_activity_log.py` holds them to the extractors' own lists.
MAIL_ARCHIVE_EXTENSIONS = frozenset({".pst", ".ost", ".mbox"})
ARCHIVE_EXTENSIONS = frozenset({".zip", ".jar", ".nupkg", ".whl"})
RECORDING_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".mpg", ".mpeg",
    ".3gp", ".flv", ".m2ts",
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".oga", ".opus", ".aac", ".wma",
})

#: One token per `ActivityLog`, so a reader can tell a new run from more of
#: the same one without comparing contents.
_RUNS = itertools.count(1)


def large_file_kind(path: Path, size_bytes: int) -> str:
    """`"mail"`, `"archive"`, `"recording"`, `"file"`, or "" for not large.

    Pure and cheap: a suffix and a comparison, asked once per file started.
    """
    size = int(size_bytes or 0)
    suffix = Path(path).suffix.lower()
    if size >= LARGE_CONTAINER_BYTES:
        if suffix in MAIL_ARCHIVE_EXTENSIONS:
            return "mail"
        if suffix in ARCHIVE_EXTENSIONS:
            return "archive"
        if suffix in RECORDING_EXTENSIONS:
            return "recording"
    if size >= LARGE_FILE_BYTES:
        return "file"
    return ""


class ActivityEntry(NamedTuple):
    """One thing the run did. `at` is wall-clock (`time.time()`), because
    the only use of it is to be read as a time of day."""

    seq: int
    at: float
    kind: str
    text: str = ""
    size: int = 0
    detail: str = ""


class ActivityLog:
    """The run's bounded, thread-safe story. See the module docstring."""

    def __init__(self, limit: int = ACTIVITY_LIMIT) -> None:
        #: Re-entrant, because `IndexStats.add_notice` holds it across its
        #: own append and the `record` that follows.
        self.lock = threading.RLock()
        self._entries: deque[ActivityEntry] = deque(maxlen=max(1, int(limit)))
        self._seq = 0
        #: Which run this is. Carried by every copy, so a view can clear itself
        #: when a new run's first tick arrives and not otherwise.
        self.run = next(_RUNS)
        #: The last copy handed out, and the sequence it was taken at. A
        #: snapshot is taken on every progress tick; most ticks add nothing
        #: here, and those get the same frozen copy back for nothing.
        self._frozen: Optional[ActivityLog] = None
        self._frozen_at = -1

    def record(self, kind: str, text: str = "", *, size: int = 0,
               detail: str = "", at: Optional[float] = None) -> None:
        """Add one entry. **Never raises** - a log line is never worth a run."""
        try:
            stamp = time.time() if at is None else float(at)
            with self.lock:
                self._seq += 1
                self._entries.append(ActivityEntry(
                    self._seq, stamp, str(kind), str(text or ""),
                    int(size or 0), str(detail or "")))
        except Exception:                        # noqa: BLE001 - see docstring
            return

    def adopt(self, entry: ActivityEntry) -> None:
        """Store an entry that was recorded **in another process**, as it was.

        Work order 0x §2. When the indexer runs as a child process, its log
        lives over there and reaches the window a few lines at a time, inside
        each progress event (`app/index/run_events.py`). The window keeps its
        own copy of the story by adopting those lines here - with the child's
        own sequence number and time, not new ones - so `since` and every
        reader of it behave exactly as they do for a run in this process.

        An entry no newer than the last one held is ignored: the same line
        can never be told twice, whatever order a reader asks in. Never
        raises, like `record`.
        """
        try:
            with self.lock:
                if int(entry.seq) <= self._seq:
                    return
                self._seq = int(entry.seq)
                self._entries.append(entry)
        except Exception:                        # noqa: BLE001 - see `record`
            return

    @property
    def last_seq(self) -> int:
        """The sequence number of the newest entry ever recorded, or 0."""
        return self._seq

    def entries(self) -> list[ActivityEntry]:
        """Everything still held, oldest first."""
        with self.lock:
            return list(self._entries)

    def since(self, seq: int) -> list[ActivityEntry]:
        """Entries newer than `seq`, oldest first.

        **From the end, not the start.** A reader asks every paint and there
        is usually nothing or one new line, so walking back from the newest
        costs a comparison or two rather than the whole buffer. Entries that
        have already been dropped for age are simply not returned: a reader
        that fell further behind than the buffer holds gets the newest
        `limit`, which is the right answer for a live view.
        """
        with self.lock:
            if seq >= self._seq:
                return []
            newer: list[ActivityEntry] = []
            for entry in reversed(self._entries):
                if entry.seq <= seq:
                    break
                newer.append(entry)
        newer.reverse()
        return newer

    def copy(self) -> "ActivityLog":
        """A frozen copy for `IndexStats.snapshot`. Same `run`, same entries.

        Nothing records into a copy - it is what the interface reads while
        the live one carries on.
        """
        with self.lock:
            if self._frozen is not None and self._frozen_at == self._seq:
                return self._frozen
            clone = ActivityLog.__new__(ActivityLog)
            clone.lock = threading.RLock()
            clone._entries = deque(self._entries, maxlen=self._entries.maxlen)
            clone._seq = self._seq
            clone.run = self.run
            clone._frozen, clone._frozen_at = None, -1
            self._frozen, self._frozen_at = clone, self._seq
            return clone

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"ActivityLog(run={self.run}, entries={len(self._entries)}, seq={self._seq})"
