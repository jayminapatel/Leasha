r"""How an indexer running in its own process tells the window what it is doing.

Layer: L3

Work order 0x §2a. **The indexer can run as a child process of the window**
(`app/index/child_run.py` starts and watches it). The two share nothing but a
pair of pipes: the child's standard output, which it writes events to, and its
standard input, which the window writes commands to. This module is the
language they speak - both halves of it, so the two ends cannot drift apart.

**Why lines of JSON.** One JSON object per line is the simplest format that is
still unambiguous: a reader knows an event is complete when it sees the end of
the line, and anything that is not a JSON object (a stray print from a
library, a half-written line from a process that died) is recognisably not an
event and can be ignored rather than misread. It needs no library, it reads the
same on Windows and macOS, and a person can watch it by running
`app.cli index --events jsonl` in a terminal.

The events, each a JSON object with an `"event"` key and `"t"` (the sender's
`time.monotonic()` when it wrote the line):

* `hello` - the first line: the protocol version and the child's process id.
* `progress` - a faithful copy of `IndexStats` (see `encode_stats`) plus the
  lines of the run's activity log recorded since the last event.
* `heartbeat` - at least once a second, even when nothing else is happening,
  so a silent child can be told from a stuck pipe.
* `finished` - the last line: the final `IndexStats`, or a structured
  `AppError` when the run could not be done at all.

The commands, one word per line: `pause`, `resume`, `stop`.

**Serialised generically, on purpose.** Other work (order 0x §3) is adding
fields to `IndexStats` - progress inside an archive, one line per worker - at
the same time as this was written. So nothing here lists the fields: every
dataclass field is turned into a JSON-safe value by its type (`to_json_safe`)
and put back by name (`StatsRebuilder`). A field added tomorrow crosses the
pipe without anybody touching this file. Two things are special, and only
two: the activity log, which is sent as "what is new since last time" rather
than whole, and the two clock fields (`current_since`, `recent`), which are
moved onto the window's own clock (see `_shift_clocks`).

Qt-free and store-free: both the child (`app/cli/index.py`) and the window's
supervisor use it, and its tests need neither a display nor a database.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import math
import threading
import time
from pathlib import PurePath
from typing import Any, Callable, Optional, TextIO

from app.core.logging import logger

__all__ = [
    "COMMANDS", "COMMAND_PAUSE", "COMMAND_RESUME", "COMMAND_STOP",
    "EVENT_FINISHED", "EVENT_HEARTBEAT", "EVENT_HELLO", "EVENT_PROGRESS",
    "EventWriter", "HEARTBEAT_S", "PROGRESS_MIN_S", "PROTOCOL",
    "StatsRebuilder", "encode_stats", "event_line", "from_json_safe", "parse_command",
    "parse_line", "to_json_safe",
]

_log = logger.bind(component="index.run_events")

#: Bumped if an event changes shape in a way an older reader would misread.
#: The window checks it on `hello` and says so rather than drawing nonsense.
PROTOCOL = 1

EVENT_HELLO = "hello"
EVENT_PROGRESS = "progress"
EVENT_HEARTBEAT = "heartbeat"
EVENT_FINISHED = "finished"

COMMAND_PAUSE = "pause"
COMMAND_RESUME = "resume"
COMMAND_STOP = "stop"
COMMANDS = frozenset({COMMAND_PAUSE, COMMAND_RESUME, COMMAND_STOP})

#: Seconds between heartbeats. The order asks for at least one a second; half
#: a second leaves room for a busy machine to be late without the window ever
#: seeing a gap. Fixed, not a setting (non-negotiable #11): nobody would tune
#: it, and it only decides how quickly a dead pipe is noticed.
HEARTBEAT_S = 0.5

#: The fastest the child sends progress. **The same number as the Indexing
#: page's paint throttle** (`indexing_view.PROGRESS_PAINT_MIN_S`, 0.25 s), for
#: the same reason: a person cannot read more than a few updates a second, and
#: every event is a line to write, a line to read and a stats object to build.
#: Kept here as its own constant because this layer may not import the window.
#: A change of phase or of pause state is sent at once, whatever this says.
PROGRESS_MIN_S = 0.25

#: The marker a structured value carries so it can be rebuilt as the right
#: kind of object on the other side. Only `AppError` is rebuilt today.
_TYPE_KEY = "__type__"

#: Fields of `IndexStats` that hold `time.monotonic()` readings. Monotonic
#: clocks are not promised to agree between two processes, so these are moved
#: onto the reader's clock (`_shift_clocks`). A new field holding a monotonic
#: time must be added here, or its "n seconds ago" would be computed against
#: the wrong clock.
MONOTONIC_FIELDS = ("current_since",)
#: Fields holding a list of `(monotonic, ...)` samples - the windowed rates.
MONOTONIC_SAMPLE_FIELDS = ("recent",)


# ---------------------------------------------------------------------------
# Values -> JSON, generically
# ---------------------------------------------------------------------------

def to_json_safe(value: Any) -> Any:
    r"""Turn any value `IndexStats` might hold into something `json.dumps` takes.

    By type, never by field name, so a field nobody has told this module about
    still crosses the pipe:

    * numbers, text, `True`/`False` and `None` pass through (a float that is
      not a number - NaN, infinity - becomes `None`, because JSON cannot say
      it and a crash in the middle of a progress tick is worse than a blank);
    * dictionaries keep their shape, with every key turned into text;
    * lists, tuples and sets become lists (JSON has only the one kind);
    * a path becomes its text;
    * a pydantic model (an `AppError`) becomes its fields plus a type marker;
    * a dataclass becomes a dictionary of its fields; an enum becomes its value;
    * anything else becomes its `str()`, which is lossy but never raises.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json_safe(item) for item in value]
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, enum.Enum):
        return to_json_safe(value.value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            found = dump(mode="json")
            if isinstance(found, dict):
                return {_TYPE_KEY: type(value).__name__, **to_json_safe(found)}
        except Exception:                        # noqa: BLE001 - fall through to str
            pass
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {spec.name: to_json_safe(getattr(value, spec.name, None))
                for spec in dataclasses.fields(value)}
    return str(value)


def from_json_safe(value: Any) -> Any:
    """The reverse of `to_json_safe`, as far as it can be reversed.

    An `AppError` comes back as an `AppError`. A list whose items are all
    lists comes back as a list of tuples, because the only such field
    `IndexStats` has (`recent`) holds tuples and JSON cannot say "tuple".
    Everything else stays the plain value JSON gave.
    """
    if isinstance(value, dict):
        kind = value.get(_TYPE_KEY)
        if kind == "AppError":
            from app.core.errors import AppError

            fields = {key: item for key, item in value.items() if key != _TYPE_KEY}
            try:
                return AppError.model_validate(fields)
            except Exception:                    # noqa: BLE001 - keep the words
                _log.debug("an AppError from the indexer could not be rebuilt")
        return {key: from_json_safe(item) for key, item in value.items() if key != _TYPE_KEY}
    if isinstance(value, list):
        items = [from_json_safe(item) for item in value]
        if items and all(isinstance(item, list) for item in items):
            return [tuple(item) for item in items]
        return items
    return value


def encode_stats(stats: Any, *, since_seq: int = 0) -> tuple[dict[str, Any], int]:
    r"""`IndexStats` as a JSON-safe dictionary, and the activity sequence sent.

    Every dataclass field except `activity` is copied through `to_json_safe`.
    The activity log goes as `{"run", "last_seq", "entries"}` holding only
    the entries newer than `since_seq`: a run tells a few hundred lines in
    all, and sending the whole buffer on every tick would send each line
    hundreds of times. Returns the newest sequence number included, which the
    caller passes back as `since_seq` next time.

    **Read from a snapshot**, never the live object - `IndexStats.snapshot`
    exists because the run's threads mutate it while a reader walks it.
    """
    out: dict[str, Any] = {}
    for spec in dataclasses.fields(stats):
        if spec.name == "activity":
            continue
        try:
            out[spec.name] = to_json_safe(getattr(stats, spec.name))
        except Exception:                        # noqa: BLE001 - one field, never the tick
            out[spec.name] = None
    last = int(since_seq)
    log = getattr(stats, "activity", None)
    if log is not None:
        entries = log.since(int(since_seq))
        if entries:
            last = int(entries[-1].seq)
        out["activity"] = {
            "run": int(getattr(log, "run", 0) or 0),
            "last_seq": int(getattr(log, "last_seq", last) or last),
            "entries": [[int(e.seq), float(e.at), str(e.kind), str(e.text),
                         int(e.size), str(e.detail)] for e in entries],
        }
    return out, last


def event_line(event: str, **payload: Any) -> str:
    """One event as one line of JSON, without the newline.

    `ensure_ascii` so the line is plain ASCII whatever a file name holds: a
    Windows pipe read with the wrong code page cannot then mangle it, and a
    person watching in a legacy console sees escapes rather than garbage.
    """
    body = {"event": event, "t": time.monotonic(), **payload}
    return json.dumps(body, ensure_ascii=True, separators=(",", ":"),
                      allow_nan=False, default=str)


def parse_line(text: Any) -> Optional[dict[str, Any]]:
    """One line from the child, as a dictionary - or None if it is not an event.

    **Never raises.** A line that is not JSON, not an object, or has no
    `"event"` is somebody else's output (a library printing to the console,
    the tail of a process that died mid-write) and is ignored, not misread.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = str(text or "").strip()
    if not text.startswith("{"):
        return None
    try:
        found = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(found, dict) or not isinstance(found.get("event"), str):
        return None
    return found


def parse_command(text: Any) -> Optional[str]:
    """One line from the window, as a known command word - or None."""
    word = str(text or "").strip().lower()
    return word if word in COMMANDS else None


# ---------------------------------------------------------------------------
# The window's side: events -> IndexStats again
# ---------------------------------------------------------------------------

def _shift_clocks(stats: Any, shift: float) -> None:
    r"""Move the monotonic readings in `stats` onto this process's clock.

    `time.monotonic()` counts from a point the operating system chooses, and
    Python does not promise that two processes share it. The page computes
    "n seconds on this file" as *its* `monotonic()` minus `current_since`, so a
    reading from the child's clock must be moved by the difference between
    the two clocks first. `shift` is (reader's clock when the line arrived)
    minus (child's clock when it wrote the line), which also absorbs the few
    milliseconds the line spent in the pipe.
    """
    if not shift:
        return
    for name in MONOTONIC_FIELDS:
        value = getattr(stats, name, None)
        if isinstance(value, (int, float)) and value:
            setattr(stats, name, float(value) + shift)
    for name in MONOTONIC_SAMPLE_FIELDS:
        samples = getattr(stats, name, None)
        if isinstance(samples, list):
            moved = []
            for sample in samples:
                if isinstance(sample, (list, tuple)) and sample and isinstance(
                        sample[0], (int, float)):
                    moved.append((float(sample[0]) + shift, *tuple(sample[1:])))
                else:
                    moved.append(sample)
            setattr(stats, name, moved)


class StatsRebuilder:
    r"""Turns `progress` and `finished` events back into `IndexStats` objects.

    One per child run. The window's code - the presenter, the Indexing page,
    the run log, `autotune.learn` - was written against a real `IndexStats`,
    methods and properties included (`files_per_minute`,
    `recent_files_per_minute`, `snapshot`, `activity.since`). So a real one is
    built for every event, with each field set by name from the payload; a
    field the payload lacks keeps its default, and a key the dataclass lacks
    is ignored.

    **The activity log is kept here, across events.** Each event carries only
    the lines that are new, and they are adopted into one window-side
    `ActivityLog` (with the child's own sequence numbers), so every stats
    object handed out holds the whole story so far, as a frozen copy -
    exactly what `IndexStats.snapshot` hands the page for a run in this
    process. A new run token from the child starts a fresh log.
    """

    def __init__(self) -> None:
        from app.index.activity import ActivityLog

        self._log = ActivityLog()
        self._child_run: Optional[int] = None

    def rebuild(self, payload: Any, *, sent_at: Any = None,
                received_at: Optional[float] = None) -> Any:
        """An `IndexStats` from one event's `stats` dictionary. Never raises
        for a malformed field; a completely unusable payload gives defaults."""
        from app.index.activity import ActivityEntry, ActivityLog
        from app.index.pipeline import IndexStats

        stats = IndexStats()
        # **A rebuilt stats is a snapshot: plain data, no live parts.** A new
        # IndexStats carries a live `WorkerBoard` (order 0x §3c), and
        # `snapshot()` - which `IndexWorker` calls on every tick - would
        # refresh `workers` from that empty board, wiping the reader lines
        # that came across the pipe and stamping `last_activity` with the
        # window's "now". `IndexStats.snapshot` sets the same `None` on its
        # own copies, for the same reason.
        if hasattr(stats, "board"):
            stats.board = None
        data = payload if isinstance(payload, dict) else {}
        names = {spec.name for spec in dataclasses.fields(IndexStats)}
        for key, raw in data.items():
            if key == "activity" or key not in names:
                continue
            try:
                setattr(stats, key, from_json_safe(raw))
            except Exception:                    # noqa: BLE001 - one field, never the tick
                _log.debug("could not set {} from the indexer's event", key)

        activity = data.get("activity")
        if isinstance(activity, dict):
            run = activity.get("run")
            if run != self._child_run:
                # A different ActivityLog in the child means a different run:
                # start the window's copy afresh, so the page clears its log.
                self._child_run = run
                self._log = ActivityLog()
            for row in activity.get("entries") or ():
                try:
                    seq, at, kind, text, size, detail = row
                    self._log.adopt(ActivityEntry(int(seq), float(at), str(kind),
                                                  str(text), int(size), str(detail)))
                except (TypeError, ValueError):
                    continue
        stats.activity = self._log.copy()

        if isinstance(sent_at, (int, float)):
            now = time.monotonic() if received_at is None else float(received_at)
            _shift_clocks(stats, now - float(sent_at))
        return stats


# ---------------------------------------------------------------------------
# The child's side: IndexStats -> lines on standard output
# ---------------------------------------------------------------------------

class EventWriter:
    r"""Writes the child's events to its standard output, on a thread of its own.

    **The pipeline never waits for the window.** A pipe holds only a few
    kilobytes; if the window is slow to read - busy, or its reading thread
    starved - a `write` on a full pipe blocks until it catches up. Written
    from the pipeline's own callback, that would stall indexing behind the
    window, which is the opposite of the reason for having two processes. So
    `offer` only notes "there is newer progress" and returns at once, and
    this thread does the snapshot, the JSON and the write.

    **Progress is coalesced, never queued.** If three ticks arrive while one
    line is being written, the next line carries the newest state, and the
    activity lines from all three (they are sent as "everything since the last
    line", so none is lost). Sent at most every `PROGRESS_MIN_S`, except that a
    change of phase or of pause state goes at once, and the last tick before a
    quiet stretch is always sent (the thread wakes for it) - so the page never
    shows a state older than a fraction of a second.

    A heartbeat goes every `HEARTBEAT_S` whatever else is happening.

    `on_broken` is called once if a write fails - the window has gone and the
    pipe is closed - so the run can stop instead of indexing for nobody.
    """

    def __init__(self, stream: TextIO, *, heartbeat_s: float = HEARTBEAT_S,
                 progress_min_s: float = PROGRESS_MIN_S,
                 on_broken: Optional[Callable[[], None]] = None) -> None:
        self._stream = stream
        self._heartbeat_s = float(heartbeat_s)
        self._progress_min_s = float(progress_min_s)
        self._on_broken = on_broken
        #: Guards everything below that both threads touch.
        self._lock = threading.Lock()
        #: One line at a time on the stream, from whichever thread writes.
        self._write_lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._latest: Any = None             # the live stats object last offered
        self._pending = False                # newer than the last line written
        self._urgent = False
        self._seen_state: Optional[tuple] = None
        self._sent_seq = 0
        self._last_progress = 0.0
        self._last_line = 0.0
        self._broken = False
        self._thread: Optional[threading.Thread] = None

    # -- the pipeline's thread ------------------------------------------------

    def start(self) -> None:
        """Say hello and start the writing thread."""
        import os

        self._write(event_line(EVENT_HELLO, protocol=PROTOCOL, pid=os.getpid()))
        self._thread = threading.Thread(target=self._loop, name="index-events",
                                        daemon=True)
        self._thread.start()

    def offer(self, stats: Any) -> None:
        """The pipeline's `on_progress`. Notes the stats and returns at once.

        Cheap on purpose: four attribute reads and a flag. The snapshot is
        taken on the writing thread, when a line is actually due.
        """
        state = (getattr(stats, "phase", ""), bool(getattr(stats, "paused", False)),
                 bool(getattr(stats, "paused_by_person", False)),
                 bool(getattr(stats, "walk_complete", False)))
        with self._lock:
            self._latest = stats
            self._pending = True
            if state != self._seen_state:
                self._seen_state = state
                self._urgent = True
        self._wake.set()

    def finish(self, *, stats: Any = None, error: Any = None,
               exit_code: int = 0) -> None:
        """Stop the thread and write the last line: final stats, or the error."""
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        payload: dict[str, Any] = {"exit": int(exit_code)}
        if stats is not None:
            snap = _snapshot(stats)
            encoded, self._sent_seq = encode_stats(snap, since_seq=self._sent_seq)
            payload["stats"] = encoded
        if error is not None:
            payload["error"] = to_json_safe(error)
        self._write(event_line(EVENT_FINISHED, **payload))

    @property
    def broken(self) -> bool:
        """True once a write has failed: nobody is reading any more."""
        return self._broken

    # -- the writing thread ---------------------------------------------------

    def _loop(self) -> None:
        while not self._stopping.is_set():
            now = time.monotonic()
            with self._lock:
                pending, urgent = self._pending, self._urgent
            due_progress = pending and (
                urgent or now - self._last_progress >= self._progress_min_s)
            if due_progress:
                self._send_progress()
            elif now - self._last_line >= self._heartbeat_s:
                self._write(event_line(EVENT_HEARTBEAT))
            # Sleep until the next thing that could be due: the throttle
            # opening for a waiting tick, or the next heartbeat.
            now = time.monotonic()
            wait = self._heartbeat_s - (now - self._last_line)
            with self._lock:
                if self._pending:
                    wait = min(wait, self._progress_min_s - (now - self._last_progress))
            self._wake.wait(max(0.01, wait))
            self._wake.clear()

    def _send_progress(self) -> None:
        with self._lock:
            live = self._latest
            self._pending = False
            self._urgent = False
        if live is None:
            return
        try:
            snap = _snapshot(live)
            encoded, sent = encode_stats(snap, since_seq=self._sent_seq)
            line = event_line(EVENT_PROGRESS, stats=encoded)
        except Exception as exc:                 # noqa: BLE001 - a tick, never the run
            _log.debug("a progress event could not be built: {}", exc)
            return
        self._sent_seq = sent
        self._last_progress = time.monotonic()
        self._write(line)

    def _write(self, line: str) -> None:
        if self._broken:
            return
        with self._write_lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
                self._last_line = time.monotonic()
            except (OSError, ValueError) as exc:
                # BrokenPipeError is an OSError; ValueError is a closed stream.
                self._broken = True
                _log.info("the window is no longer reading the indexer's events: {}", exc)
                callback = self._on_broken
                if callback is not None:
                    try:
                        callback()
                    except Exception:            # noqa: BLE001 - never from here
                        pass


def _snapshot(stats: Any) -> Any:
    """`stats.snapshot()` when it has one (a real `IndexStats`), else itself."""
    snap = getattr(stats, "snapshot", None)
    return snap() if callable(snap) else stats
