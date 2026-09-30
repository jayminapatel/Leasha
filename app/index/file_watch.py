r"""A time limit per file, and Force skip: one stuck file costs one file.

Layer: L3

Work order 0z, lane B. The owner, on indexing: "especially as the pst scanning
is way slow ... and is not reliable, this has to have a robust design". Reader
processes (`app/index/read_process.py`) already make a reader that *crashes*
cost one file. A reader that *hangs* - a damaged PDF that sends the parser
round a loop, a 3GB corrupt zip, a mailbox that stops yielding messages - used
to hold its extraction thread for the rest of the run, and with it a quarter
of the run's reading.

## What is limited, and how

Only **reader time** counts: the seconds an extraction thread spends inside
the reader, waiting for its next document. Time spent handing a document to a
busy writer, or held by a pause, is not the file's fault and is not counted.

The limit depends on the kind of file (`limit_kind`):

* **Text and code** (`QUICK_READERS`): `INDEX_FILE_TIME_LIMIT_S` in total.
* **Everything else that reads one document** - PDFs, Office files, e-books,
  single mail messages: ten times that (`LONG_FACTOR`), in total.
* **Mailboxes and archives** (`STALL_READERS`: `.pst`/`.ost`, `.mbox`, `.olm`,
  `.zip`): **no total limit at all.** A 30GB `.pst` can rightly take hours.
  What is limited instead is *no progress*: no new document and no movement in
  the reader's own position (`app.extract.progress` frames, `frame.n` and
  `frame.beat`) for `INDEX_STALL_LIMIT_S`.
* **Video and recordings** (`UNLIMITED_READERS`): no limit. Transcription
  runs at a few times real time and says nothing while it works; any fixed
  limit would cut off a long recording that was working. Force skip still
  applies to them.

0 switches a limit off.

## What happens when a limit is reached

The file is recorded as skipped with `ERR_FILE_TIMEOUT` - settled like every
other skip, so the next run does not read it again unless it changes - and the
thread moves on to its next file. How the thread is freed depends on where
the file was being read:

* **In a reader process** (0x §5b): that process is ended. The thread was only
  waiting on its pipe, so it sees the pipe close at once, records the file, and
  starts a fresh process for its next file - the `ReaderProcess._abandon`
  pattern, triggered from outside.
* **In the thread itself**: a thread cannot be ended from outside. What can be
  done is to raise an exception *in* it (`PyThreadState_SetAsyncExc`), which
  Python delivers the next time that thread runs a line of Python. Measured
  (`tests/unit/test_file_watch.py`): a reader looping in Python - the usual
  shape of a parser gone round in circles - is interrupted within a fraction
  of a second and the same thread carries on with its next file.
  **A reader blocked inside native code cannot be interrupted this way** - a
  read from a network drive that never returns, a C library stuck in its own
  loop - because Python only delivers the exception when control comes back.
  For that case, if the thread has not let go within `GRACE_S`, the watchdog
  records the file itself, **leaves the stuck thread behind**, and the
  pipeline starts a replacement thread (`Pipeline._replace_worker`), so the
  run keeps its full number of readers. The stuck thread holds its memory
  until the native call returns or the run ends; if it ever does return, it
  sees it was replaced and ends quietly without taking more work.

## Force skip

The Indexing page's per-reader Force skip does exactly what a limit does, for
that one reader's current file, straight away, with the reason naming the
person. `Watchdog.request_skip(slot)`; the in-process run calls it through
`Pipeline.force_skip`, the separate-process run through the `skip <slot>`
command on the child's standard input (`run_events.COMMAND_SKIP`).

## Threads and locking

Each extraction thread owns one `FileWatch`. The thread brackets every
`next()` into its reader with `enter()`/`leave()`; the watchdog thread reads
the same object once every `TICK_S`. Both take the watch's own lock, which is
what makes "raise an exception in the thread" safe: the watchdog only does it
while the thread is between `enter` and `leave`, and `leave` clears anything
still pending before the thread goes back to its own code. Two lock round
trips per document - microseconds - against a document's milliseconds.
"""

from __future__ import annotations

import ctypes
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.errors import AppError, make_error
from app.core.logging import logger

log = logger.bind(component="index.file_watch")

__all__ = [
    "FileCancelled",
    "FileTimedOut",
    "FileWatch",
    "GRACE_S",
    "LIMIT_LONG",
    "LIMIT_NONE",
    "LIMIT_QUICK",
    "LIMIT_STALL",
    "LONG_FACTOR",
    "ORPHANED",
    "QUICK_READERS",
    "STALL_READERS",
    "TICK_S",
    "UNLIMITED_READERS",
    "Watchdog",
    "duration_words",
    "limit_kind",
]

#: How often the watchdog looks. A constant (non-negotiable 11): limits are
#: whole seconds to minutes, so looking more often changes nothing a person
#: could notice, and a Force skip acts within this.
TICK_S = 0.5
#: How long a thread has to let go of a file after being told to, before the
#: watchdog records the file itself and the pipeline replaces the thread. A
#: constant: a thread running Python lets go in milliseconds (measured), so
#: this only ever decides how long a thread stuck in native code is waited for.
GRACE_S = 5.0
#: Documents, e-books, PDFs and Office files get this many times the limit for
#: text and code. A constant rather than a third setting (non-negotiable 11:
#: every setting must justify itself): the ratio reflects how much more work a
#: parser does per byte, which is not a thing anybody has a view on.
LONG_FACTOR = 10

LIMIT_QUICK = "quick"
LIMIT_LONG = "long"
LIMIT_STALL = "stall"
LIMIT_NONE = "none"

#: Readers of plain text and source code. Named by class, as
#: `read_process.PROCESS_READERS` is; `test_file_watch.py` checks every name
#: is still a registered reader.
QUICK_READERS = frozenset({"PlainTextExtractor"})
#: Readers of containers that can rightly take hours, and that report their
#: position as they go. Judged on progress, never on total time.
STALL_READERS = frozenset({
    "PstExtractor", "MboxExtractor", "OlmExtractor", "ArchiveExtractor",
})
#: Readers with no limit at all - see the module docstring.
UNLIMITED_READERS = frozenset({"VideoExtractor", "AudioExtractor"})

#: `FileWatch.settle` returns this for a thread that was replaced.
ORPHANED = "orphaned"


class FileCancelled(BaseException):
    """Raised on an extraction thread whose file was timed out or skipped.

    A `BaseException`, like `FileTimedOut`, so a reader's own
    `except Exception` cannot swallow it.
    """


class FileTimedOut(BaseException):
    """What the watchdog raises *inside* a stuck in-process reader.

    A `BaseException` so that a reader's `except Exception: continue` - which
    several have, to survive one bad message - does not swallow it and carry
    on looping.
    """


def limit_kind(path: Any) -> str:
    """Which limit applies to reading `path`: one of the `LIMIT_*` codes."""
    try:
        from app.extract.base import extractor_for

        extractor = extractor_for(Path(path))
    except Exception:                               # noqa: BLE001 - a lookup, not a read
        extractor = None
    name = type(extractor).__name__ if extractor is not None else ""
    if name in UNLIMITED_READERS:
        return LIMIT_NONE
    if name in STALL_READERS:
        return LIMIT_STALL
    if name in QUICK_READERS:
        return LIMIT_QUICK
    return LIMIT_LONG


def _factor(value: Any) -> float:
    """A usable multiple of the limit: a positive number, else 1."""
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return 1.0
    return factor if factor > 0 else 1.0


def duration_words(seconds: float) -> str:
    """`45 s`, `3 min 20 s`, `2 h 5 min`. For the error message, not the UI."""
    total = max(0, int(round(float(seconds))))
    if total < 60:
        return f"{total} s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


# ---------------------------------------------------------------------------
# Raising an exception in another thread (CPython)
# ---------------------------------------------------------------------------

def _set_async_exc(thread_id: int, exc: Optional[type]) -> bool:
    """Ask `thread_id` to raise `exc` at its next Python line; None clears.

    CPython's own `PyThreadState_SetAsyncExc`. Returns False where it is
    missing (another interpreter), so the caller falls back to replacing the
    thread rather than raising.
    """
    try:
        setter = ctypes.pythonapi.PyThreadState_SetAsyncExc
    except AttributeError:                          # pragma: no cover - not CPython
        return False
    target = ctypes.c_ulong(thread_id)
    changed = setter(target, ctypes.py_object(exc) if exc is not None else None)
    if changed > 1:                                 # pragma: no cover - cannot happen
        setter(target, None)
        return False
    return changed == 1


# ---------------------------------------------------------------------------
# One extraction thread's file
# ---------------------------------------------------------------------------

class FileWatch:
    """What one extraction thread is reading, and for how long.

    Written by the thread (`begin`, `enter`, `leave`, `settle`, `end`), read and
    acted on by the watchdog (`Watchdog._check`). Everything under `lock`.
    """

    def __init__(self, *, slot: Any = None, reader: Any = None,
                 thread_id: Optional[int] = None) -> None:
        self.lock = threading.Lock()
        #: The thread's `WorkerSlot` (its line on the page), or None.
        self.slot = slot
        #: The thread's `ReaderProcess`, or None when files are read in-process.
        self.reader = reader
        self.thread_id = thread_id if thread_id is not None else threading.get_ident()
        #: Bumped for every file, so a skip asked for one file never lands on
        #: the next.
        self.token = 0
        self.candidate: Any = None
        self.digest: Optional[str] = None
        self.kind = LIMIT_NONE
        #: Order 0z F3: how many times the usual limit this one file is given.
        #: 1 for every file except one being retried with a longer limit
        #: (`app/index/timed_out_retry.py`).
        self.factor = 1.0
        self.started = 0.0
        #: `time.monotonic()` when the current `next()` began; 0 outside one.
        self.reading_since = 0.0
        #: Reader seconds in finished `next()` calls for this file.
        self.read_s = 0.0
        self.documents = 0
        self.last_progress = 0.0
        self.last_signature: Any = None
        #: The `ERR_FILE_TIMEOUT` this file will be recorded with, once decided.
        self.cancel: Optional[AppError] = None
        #: `time.monotonic()` when the thread was told to let go; 0 before.
        self.acted_at = 0.0
        #: True once an exception was raised into the thread for this file.
        self.injected = False
        #: The token a Force skip was asked for, or -1.
        self.skip_token = -1
        self.skip_by = ""
        #: True once the watchdog has recorded this file itself and the
        #: pipeline has replaced the thread.
        self.orphaned = False
        #: 2026-09-30. Set by the pipeline while it reads this file: a call
        #: returning what the reader has read and not yet handed on, so a
        #: mailbox that is cut off keeps its last message
        #: (`Pipeline._kept_in_hand`). Callable from the watchdog's thread,
        #: for a reader stuck where its own thread cannot be reached.
        self.in_hand: Optional[Callable[[], list]] = None

    # -- the extraction thread's side ------------------------------------------

    def begin(self, candidate: Any, digest: Optional[str], kind: str,
              factor: float = 1.0) -> None:
        now = time.monotonic()
        with self.lock:
            self.token += 1
            self.candidate = candidate
            self.digest = digest
            self.kind = kind
            self.factor = _factor(factor)
            self.started = now
            self.reading_since = 0.0
            self.read_s = 0.0
            self.documents = 0
            self.last_progress = now
            self.last_signature = None
            self.cancel = None
            self.acted_at = 0.0
            self.injected = False
            self.in_hand = None

    def enter(self) -> None:
        """About to ask the reader for its next document."""
        with self.lock:
            if self.orphaned or self.cancel is not None:
                raise FileCancelled()
            self.reading_since = time.monotonic()

    def leave(self, *, finished: bool = False) -> None:
        """The reader answered. Raises `FileCancelled` if the file was cancelled.

        `finished` is True when the reader said it had nothing more: the file
        is complete, so a limit reached in the same instant is ignored rather
        than throwing away a whole file's work - unless the thread was already
        replaced, when it must go regardless.
        """
        with self.lock:
            self._stop_clock()
            if self.injected:
                # Nothing raised in the thread may escape into its own code.
                _set_async_exc(self.thread_id, None)
                self.injected = False
            if self.orphaned:
                raise FileCancelled()
            if not finished:
                self.documents += 1
                if self.cancel is not None:
                    raise FileCancelled()

    def settle(self) -> Any:
        """After the reader raised: `ORPHANED`, the cancel error, or None.

        None means the exception was the reader's own, to be recorded as it
        always was.
        """
        with self.lock:
            self._stop_clock()
            if self.injected:
                _set_async_exc(self.thread_id, None)
                self.injected = False
            if self.orphaned:
                return ORPHANED
            return self.cancel

    def end(self) -> None:
        """The file is finished, however it finished."""
        with self.lock:
            self._stop_clock()
            if self.injected:
                _set_async_exc(self.thread_id, None)
                self.injected = False
            self.token += 1
            self.candidate = None
            self.cancel = None
            self.in_hand = None

    def _stop_clock(self) -> None:
        if self.reading_since:
            self.read_s += time.monotonic() - self.reading_since
            self.reading_since = 0.0

    # -- the watchdog's side ---------------------------------------------------

    def reader_seconds(self, now: float) -> float:
        running = (now - self.reading_since) if self.reading_since else 0.0
        return self.read_s + running

    def signature(self) -> tuple:
        """Where the reader is: documents so far and its frames' positions.

        `beat` as well as `n` (order 0z audit, 2026-09-30). Inside one message
        of a `.pst`, `n` stands still while the attachments are read, and an
        attachment that is skipped, held, a duplicate or unreadable hands over
        no document - so a message with a long run of those looked like "no
        progress" and the whole archive could be cut off while it was working.
        `Frame.beat` rises for every item, and was added for exactly this.
        """
        frames = getattr(self.slot, "frames", None) or []
        return (self.documents, getattr(self.slot, "item", 0),
                tuple((getattr(f, "n", 0), getattr(f, "where", ""),
                       getattr(f, "beat", 0))
                      for f in list(frames)))


# ---------------------------------------------------------------------------
# The watchdog
# ---------------------------------------------------------------------------

class Watchdog:
    """Every extraction thread's `FileWatch`, checked every `TICK_S`.

    `file_limit_s` is the limit for text and code (others get `LONG_FACTOR`
    times it); `stall_limit_s` is the no-progress limit for mailboxes and
    archives. 0 switches either off; Force skip works regardless.

    `on_orphan(watch)` is called, outside every lock, when a thread did not
    let go within `GRACE_S`: the pipeline records the file from
    `watch.cancel` and starts a replacement thread.
    """

    def __init__(self, *, file_limit_s: float = 0, stall_limit_s: float = 0,
                 on_orphan: Optional[Callable[[FileWatch], None]] = None,
                 grace_s: Optional[float] = None, tick_s: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.file_limit_s = max(0.0, float(file_limit_s or 0))
        self.stall_limit_s = max(0.0, float(stall_limit_s or 0))
        self.on_orphan = on_orphan
        # Read at construction rather than bound as defaults, so a test can
        # shorten the module's constants for a run it starts.
        self.grace_s = float(GRACE_S if grace_s is None else grace_s)
        self.tick_s = float(TICK_S if tick_s is None else tick_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._watches: list[FileWatch] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: How many files each way ended, for the log and the tests.
        self.timed_out = 0
        self.skipped_by_person = 0
        self.orphans = 0

    # -- membership ------------------------------------------------------------

    def add(self, watch: FileWatch) -> None:
        with self._lock:
            self._watches.append(watch)

    def remove(self, watch: FileWatch) -> None:
        with self._lock:
            if watch in self._watches:
                self._watches.remove(watch)

    # -- life ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="file-watchdog",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.check()
            except Exception as exc:                # noqa: BLE001 - the watchdog must not die
                log.warning("the file watchdog could not check the readers: {}", exc)

    # -- Force skip ------------------------------------------------------------

    def request_skip(self, slot_id: Any, *, by: str = "you") -> bool:
        """Skip the file reader `slot_id` has open now. False if it has none.

        Safe from any thread and never waits: it only marks the watch; the
        watchdog acts at its next look, within `TICK_S`.
        """
        try:
            wanted = int(str(slot_id).strip())
        except (TypeError, ValueError):
            return False
        with self._lock:
            watches = list(self._watches)
        for watch in watches:
            if getattr(watch.slot, "id", None) != wanted:
                continue
            with watch.lock:
                if watch.orphaned:
                    # A thread left behind keeps its old slot object, whose
                    # number its replacement now uses. Not the one meant.
                    continue
                if watch.candidate is None:
                    return False
                watch.skip_token = watch.token
                watch.skip_by = by
            return True
        return False

    # -- the look --------------------------------------------------------------

    def check(self) -> None:
        """One look at every thread. Called every `TICK_S`, and by tests."""
        with self._lock:
            watches = list(self._watches)
        orphans = []
        for watch in watches:
            if self._check(watch):
                orphans.append(watch)
        for watch in orphans:
            self.orphans += 1
            if self.on_orphan is not None:
                try:
                    self.on_orphan(watch)
                except Exception as exc:            # noqa: BLE001
                    log.error("could not replace a stuck reader: {}", exc)

    def _check(self, watch: FileWatch) -> bool:
        """True when `watch`'s thread has just been given up on."""
        now = self._clock()
        with watch.lock:
            if watch.candidate is None or watch.orphaned:
                return False
            signature = watch.signature()
            if not watch.reading_since or signature != watch.last_signature:
                watch.last_signature = signature
                watch.last_progress = now
            if watch.cancel is None:
                watch.cancel = self._verdict(watch, now)
                if watch.cancel is None:
                    return False
            if not watch.reading_since:
                # Not inside the reader (handing a document over, or held by a
                # pause): `enter` refuses the next `next()`, which is enough.
                return False
            if not watch.acted_at:
                watch.acted_at = now
                self._let_go(watch)
                return False
            if now - watch.acted_at < self.grace_s:
                return False
            # Told to let go `grace_s` ago and still inside the reader: stuck
            # in native code. Recorded here, and the thread is left behind.
            watch.orphaned = True
            if watch.injected:
                _set_async_exc(watch.thread_id, None)
                watch.injected = False
            return True

    def _verdict(self, watch: FileWatch, now: float) -> Optional[AppError]:
        """The error to record if this file must stop now, else None."""
        path = getattr(watch.candidate, "path", watch.candidate)
        took = duration_words(now - watch.started)
        if watch.skip_token == watch.token:
            self.skipped_by_person += 1
            # "you": the message is read by the person who pressed it.
            by = watch.skip_by or "you"
            reason = f"{by} pressed Force skip on the Indexing page"
            log.info("{} force-skipped by {} after {}", Path(str(path)).name, by, took)
            return make_error("ERR_FILE_TIMEOUT", "index.file_watch", path=str(path),
                              took=took, reason=reason,
                              details=f"Force skip, by {by}.")
        if not watch.reading_since:
            return None
        seconds = limit = usual = 0.0
        reason = ""
        # Order 0z F3: a file being retried is given `factor` times the usual
        # limit. 1 for every other file, which leaves each sentence below
        # exactly as it was.
        factor = watch.factor
        if watch.kind == LIMIT_STALL and self.stall_limit_s:
            usual = self.stall_limit_s
            seconds, limit = now - watch.last_progress, usual * factor
            reason = (f"nothing new was read from it for "
                      f"{duration_words(limit)}, the limit for a mailbox or archive")
        elif watch.kind == LIMIT_QUICK and self.file_limit_s:
            usual = self.file_limit_s
            seconds, limit = watch.reader_seconds(now), usual * factor
            reason = (f"reading it took longer than {duration_words(limit)}, "
                      "the limit for a text or code file")
        elif watch.kind == LIMIT_LONG and self.file_limit_s:
            usual = self.file_limit_s * LONG_FACTOR
            seconds, limit = watch.reader_seconds(now), usual * factor
            reason = (f"reading it took longer than {duration_words(limit)}, "
                      "the limit for a document of this kind")
        if not limit or seconds < limit:
            return None
        details = (f"limit={watch.kind} {limit:.0f}s; reader {seconds:.1f}s; "
                   f"documents so far {watch.documents}")
        if factor != 1.0:
            # The row says what this file was given, so a second timeout is
            # not read as "the retry changed nothing".
            reason += (f" on this retry ({factor:g} times the usual "
                       f"{duration_words(usual)})")
            details += f"; retry x{factor:g} of {usual:.0f}s"
        self.timed_out += 1
        log.warning("{} timed out ({} limit, {:.0f}s): {}",
                    Path(str(path)).name, watch.kind, limit, reason)
        return make_error(
            "ERR_FILE_TIMEOUT", "index.file_watch", path=str(path), took=took,
            reason=reason, details=details)

    def _let_go(self, watch: FileWatch) -> None:
        """Free the thread: end its reader process, or raise inside it."""
        reader = watch.reader
        if reader is not None and getattr(reader, "reading", False):
            reader.kill_child()
            return
        watch.injected = _set_async_exc(watch.thread_id, FileTimedOut)
