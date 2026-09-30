r"""Watching the indexed folders, so a file saved a moment ago can be found.

Layer: L3

Work order 0z, item F1. Until this existed the index only learned about a new
or changed file when somebody ran the indexer: a report saved at 14:02 was not
findable until the next run walked the whole tree again. This module is told
by the operating system when something under an indexed folder changes, waits
for the writing to stop, and puts just those files through the ordinary
pipeline - no walk of the tree.

(Not to be confused with `file_watch.py`, which is the time limit on one file
*while it is being read*. This one watches folders for changes.)

## Four parts, each testable on its own

1. **A source per folder** says "something happened at this path".
   `NativeSource` is told by Windows (`ReadDirectoryChangesW`, in
   `app/core/osbridge/dirwatch.py` - one handle covers the whole tree below a
   folder). `PollingSource` is for every other system and for a folder Windows
   will not watch: it lists the folder every so often and compares with last
   time, never more than about a tenth of the time (`POLL_DUTY`).
2. **`ChangeBuffer`** gathers those and decides when a path is ready. One save
   is several events (an editor writes a temporary file, deletes the original,
   renames one over the other), so a path is left alone until nothing has
   happened to it for `QUIET_S`; a path that never goes quiet is taken after
   `MAX_WAIT_S` anyway.
3. **`BatchIndexer`** puts a ready batch into the index. It asks the disk what
   is true *now* - the events only say where to look - and then uses the
   pipeline's own per-file path (`PipelineConfig.candidate_source`), so a
   watched file is classified, read, chunked, embedded and written by exactly
   the code an ordinary run uses. There is no second indexer.
4. **`FolderWatcher`** owns the threads: one per source, and one that hands
   ready batches to the indexer and deals with "not now".

## What is never lost

* **An overflow is a rescan, never silence.** Windows gives up when changes
  arrive faster than they are read (a large copy) and returns nothing at all.
  That, a folder that came back after being unreachable, and more than
  `MAX_PATHS_PER_ROOT` paths waiting under one folder all become "look at the
  whole of this folder": the pipeline walks it, and its clean-up pass removes
  what has gone - limited to that folder (`PipelineConfig.prune_under`).
* **A batch that cannot be applied is kept.** While an index run holds the run
  lock (`app/core/run_lock.py` - one writer at a time), while the machine is
  on battery and the owner's setting says to wait, or after an error, the
  batch goes back in the buffer and is tried again later.
* **One bad path never stops the watch** (non-negotiable 3). A file that
  cannot be read is recorded in the skip ledger by the pipeline like any
  other; a folder that cannot be watched is reported (`ERR_WATCH_FOLDER`) and
  tried again every `REOPEN_S` while the others carry on.

## What it deliberately does not do

* **It never reads a cloud placeholder** (`walker.PathRules`), whatever the
  folder's opt-in: that download budget belongs to a run somebody started.
* **It leaves mailboxes to the ordinary run.** A `.pst` or `.ost` that Outlook
  has open changes every few seconds, and re-reading a multi-gigabyte mailbox
  each time would hold the run lock all day. A mailbox that is *new* is
  recorded by name, so it can be found; its messages wait for the next run.
* **It does not look for what changed while it was not running.** Starting
  the watch does not walk anything. That is what an ordinary run is for, and
  the schedule on the Indexing page can start one when the window opens.
* **It never writes to the watched folders** (non-negotiable 10). Removing a
  file "from the index" removes rows and vectors, nothing on disk.

## Threads

Sources and the dispatcher are daemon threads started by `FolderWatcher.start`
and ended by `stop`. No Qt anywhere: the window runs all of this in a process
of its own through `app.cli watch` and reads what it prints.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

from app.core.errors import AppError, AppErrorException, make_error, to_app_error
from app.core.logging import logger
from app.core.osbridge import dirwatch
from app.core.osbridge.pathnames import path_key
from app.core.run_lock import FOLDER_WATCH, INDEX_MUTEX_NAME, IndexRunLock

__all__ = [
    "BACKEND_AUTO", "BACKEND_NATIVE", "BACKEND_POLL",
    "Batch", "BatchIndexer", "BatchResult", "Change", "ChangeBuffer",
    "FolderWatcher", "IndexBusy", "NativeSource", "PollingSource",
    "QUIET_S", "MAX_WAIT_S", "MAX_PATHS_PER_ROOT", "POLL_S",
    "live_roots",
]

log = logger.bind(component="index.folder_watch")

#: Seconds a path must be left alone before it is indexed. A constant
#: (non-negotiable 11): long enough for an editor's save (temporary file,
#: delete, rename - all within a fraction of a second) to finish, and equal to
#: the walker's own `RECENT_EDIT_WINDOW_S`, so a file is normally past the
#: window in which its timestamp cannot be trusted. Only a measured editor
#: that takes longer between the steps of one save would change it.
QUIET_S = 2.0
#: Seconds after which a path that never goes quiet is taken anyway - a log
#: file written every second, a long download. It is read as it stands and
#: read again when it next changes.
MAX_WAIT_S = 15.0
#: Paths waiting under one folder before they are dropped in favour of one
#: "look at the whole folder". Past a couple of thousand, a walk that stats
#: every file costs less than remembering each path, and the list cannot grow
#: without limit while a long index run holds the lock.
MAX_PATHS_PER_ROOT = 2_000
#: How often the dispatcher looks for a ready batch.
TICK_S = 0.25
#: Seconds before trying again when an index run holds the lock.
BUSY_RETRY_S = 20.0
#: Seconds before trying again after an error, doubling each time up to
#: `ERROR_RETRY_MAX_S`. Never given up on: see "What is never lost".
ERROR_RETRY_S = 30.0
ERROR_RETRY_MAX_S = 600.0
#: Seconds before a file that was locked by the program saving it is tried
#: again, and how many times. Word keeps a document locked while it is open;
#: after this many tries it is left in the skip ledger as locked, which every
#: ordinary run retries.
LOCKED_RETRY_S = 30.0
LOCKED_RETRIES = 4
#: Seconds between attempts to watch a folder that could not be opened.
REOPEN_S = 30.0
#: How long `NativeSource` waits in one read before checking it should stop.
READ_WAIT_S = 1.0
#: Seconds between two listings of a folder that is compared rather than
#: watched, at the least.
POLL_S = 30.0
#: ...and the most of its time a comparing source may spend listing: after a
#: listing that took 4 seconds it waits at least 40. This is what keeps the
#: fallback cheap on a tree of any size, at the price of noticing later.
POLL_DUTY = 0.1
#: Seconds with nothing to do before the meaning model is let go of, so an
#: idle watch is not holding a few hundred megabytes it is not using.
MODEL_IDLE_S = 600.0

BACKEND_AUTO = "auto"
BACKEND_NATIVE = "native"
BACKEND_POLL = "poll"


class IndexBusy(Exception):
    """The batch cannot be applied right now; keep it and try again later.

    `reason` is a sentence for the status line: who holds the lock, or what
    the machine is waiting for.
    """

    def __init__(self, reason: str, *, retry_s: float = BUSY_RETRY_S) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_s = retry_s


# ---------------------------------------------------------------------------
# What is waiting
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    """One path to look at, under one indexed folder.

    The disk is asked what is there when the batch is applied; the two flags
    are only what the disk cannot say afterwards.
    """

    root: Path
    path: Path
    #: It was created or renamed to this name. For a folder that means its
    #: contents are new to the index and must be walked; a folder that was
    #: merely *modified* (something inside it changed) needs nothing.
    new: bool = False
    #: A rename away from this name was seen. If a file still answers to the
    #: name afterwards, only the letter case changed - see `_exact_name`.
    renamed_from: bool = False
    #: How many times this path has been put back because it was locked.
    attempts: int = 0


@dataclass
class Batch:
    """What `ChangeBuffer.take` hands over: paths, and folders to look at whole."""

    changes: list[Change] = field(default_factory=list)
    rescan: list[Path] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.changes or self.rescan)

    def __len__(self) -> int:
        return len(self.changes) + len(self.rescan)


@dataclass
class _Waiting:
    change: Change
    first_at: float
    last_at: float
    #: Not handed over before this moment (a locked file waiting its turn).
    not_before: float = 0.0


@dataclass
class _Rescan:
    root: Path
    first_at: float
    last_at: float
    not_before: float = 0.0


class _Stopping(Exception):
    """A listing was abandoned because the watch is stopping."""


def _root_key(root: Any) -> str:
    return str(root).rstrip("\\/").lower()


class ChangeBuffer:
    """Paths that have changed, gathered until each has gone quiet.

    Safe from any thread. Time comes from `clock`, so every rule here is
    tested without waiting.
    """

    def __init__(self, *, quiet_s: float = QUIET_S, max_wait_s: float = MAX_WAIT_S,
                 max_paths: int = MAX_PATHS_PER_ROOT,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.quiet_s = float(quiet_s)
        self.max_wait_s = float(max_wait_s)
        self.max_paths = int(max_paths)
        self._clock = clock
        self._lock = threading.Lock()
        self._paths: dict[str, _Waiting] = {}
        self._per_root: dict[str, int] = {}
        #: Folders to look at in full, by `_root_key`.
        self._rescan: dict[str, _Rescan] = {}
        #: Nothing at all is handed over before this moment (`restore(hold=True)`).
        self._hold_until = 0.0
        #: How many times a folder's waiting paths became one rescan.
        self.overflows = 0

    # -- putting things in -------------------------------------------------------

    def add(self, root: Any, path: Any, *, new: bool = False,
            renamed_from: bool = False, attempts: int = 0) -> None:
        """Something happened at `path`, which is under the folder `root`."""
        now = self._clock()
        with self._lock:
            self._add(Change(Path(root), Path(path), new, renamed_from, attempts), now)

    def _add(self, change: Change, now: float) -> None:
        root_key = _root_key(change.root)
        rescan = self._rescan.get(root_key)
        if rescan is not None:
            # The whole folder is going to be looked at; this only says it is
            # still busy, so the look waits for it to go quiet.
            rescan.last_at = now
            return
        key = path_key(change.path)
        waiting = self._paths.get(key)
        if waiting is not None:
            old = waiting.change
            waiting.change = replace(
                old, new=old.new or change.new,
                renamed_from=old.renamed_from or change.renamed_from,
                attempts=max(old.attempts, change.attempts))
            waiting.last_at = now
            return
        self._paths[key] = _Waiting(change, now, now)
        count = self._per_root.get(root_key, 0) + 1
        self._per_root[root_key] = count
        if count > self.max_paths:
            self._overflow(change.root, now)

    def overflow(self, root: Any) -> None:
        """Changes under `root` were missed: look at the whole folder."""
        with self._lock:
            self._overflow(Path(root), self._clock())

    def _overflow(self, root: Path, now: float) -> None:
        root_key = _root_key(root)
        for key in [key for key, waiting in self._paths.items()
                    if _root_key(waiting.change.root) == root_key]:
            del self._paths[key]
        self._per_root.pop(root_key, None)
        known = self._rescan.get(root_key)
        if known is None:
            self._rescan[root_key] = _Rescan(root, now, now)
            self.overflows += 1
        else:
            known.last_at = now

    def restore(self, batch: Batch, *, delay_s: float = 0.0,
                hold: bool = False) -> None:
        """Put back a batch that could not be applied, to be tried again.

        Each thing in it has already waited its turn, so it is ready again as
        soon as `delay_s` has passed. `hold=True` makes *everything* wait that
        long - for a reason that is not about these paths (an index run holds
        the lock, the last attempt failed). Newer events for the same path are
        kept: the flags are merged.
        """
        now = self._clock()
        with self._lock:
            ripe = now - self.max_wait_s          # already waited its turn once
            until = now + max(0.0, float(delay_s))
            for root in batch.rescan:
                self._overflow(Path(root), now)
                rescan = self._rescan[_root_key(root)]
                rescan.first_at = min(rescan.first_at, ripe)
                rescan.not_before = max(rescan.not_before, until)
            for change in batch.changes:
                self._add(change, now)
                waiting = self._paths.get(path_key(change.path))
                if waiting is not None:
                    waiting.first_at = min(waiting.first_at, ripe)
                    waiting.not_before = max(waiting.not_before, until)
            if hold and delay_s > 0:
                self._hold_until = max(self._hold_until, until)

    # -- taking things out -------------------------------------------------------

    def _ripe(self, entry: Any, now: float) -> bool:
        if now < entry.not_before:
            return False
        return ((now - entry.last_at) >= self.quiet_s
                or (now - entry.first_at) >= self.max_wait_s)

    def take(self) -> Optional[Batch]:
        """Everything that is ready, removed from the buffer; None if nothing is."""
        now = self._clock()
        with self._lock:
            if now < self._hold_until:
                return None
            batch = Batch()
            for key in [key for key, rescan in self._rescan.items()
                        if self._ripe(rescan, now)]:
                batch.rescan.append(self._rescan.pop(key).root)
            for key in [key for key, waiting in self._paths.items()
                        if self._ripe(waiting, now)]:
                waiting = self._paths.pop(key)
                batch.changes.append(waiting.change)
                root_key = _root_key(waiting.change.root)
                left = self._per_root.get(root_key, 1) - 1
                if left > 0:
                    self._per_root[root_key] = left
                else:
                    self._per_root.pop(root_key, None)
            return batch if batch else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._paths) + len(self._rescan)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class _Source:
    """One folder's changes, fed into a `ChangeBuffer` from a thread of its own."""

    backend = ""

    def __init__(self, root: Any, sink: ChangeBuffer, *,
                 rules: Callable[[], Any], stop: threading.Event,
                 on_problem: Optional[Callable[[Path, Optional[AppError]], None]] = None,
                 ) -> None:
        self.root = Path(root)
        self.sink = sink
        self._rules = rules
        self._stop = stop
        self._on_problem = on_problem
        #: The error this folder is currently failing with, or None.
        self.problem: Optional[AppError] = None
        #: Events handed to the buffer, and events dropped as excluded.
        self.seen = 0
        self.ignored = 0
        self.thread: Optional[threading.Thread] = None
        #: Set once changes from now on will be noticed: the folder is open
        #: (native) or has been listed once (comparison).
        self.ready = threading.Event()

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run_guarded, name=f"folder-watch-{self.root.name}",
            daemon=True)
        self.thread.start()

    def close(self) -> None:
        """Let go of anything held. Called by `FolderWatcher.stop`."""

    def _run_guarded(self) -> None:
        try:
            self.run()
        except Exception as exc:                 # noqa: BLE001 - one folder, not the watch
            self._report(to_app_error(exc, "index.folder_watch"))
            log.error("the watch on {} ended: {}", self.root, exc)

    def run(self) -> None:                       # pragma: no cover - overridden
        raise NotImplementedError

    def _report(self, problem: Optional[AppError]) -> None:
        changed = (problem is None) != (self.problem is None) or (
            problem is not None and self.problem is not None
            and problem.message != self.problem.message)
        self.problem = problem
        if changed and self._on_problem is not None:
            try:
                self._on_problem(self.root, problem)
            except Exception as exc:             # noqa: BLE001 - reporting never stops a source
                log.debug("could not report a folder watch problem: {}", exc)

    def _lost(self, reason: str) -> None:
        self._report(make_error("ERR_WATCH_FOLDER", "index.folder_watch",
                                path=str(self.root), reason=reason))

    def _offer(self, path: Path, *, new: bool = False, renamed_from: bool = False) -> None:
        """One path into the buffer, unless the walker would never reach it."""
        try:
            if self._rules().excluded(self.root, path):
                self.ignored += 1
                return
        except Exception as exc:                 # noqa: BLE001 - when unsure, look
            log.debug("could not check {} against the exclusions: {}", path, exc)
        self.seen += 1
        self.sink.add(self.root, path, new=new, renamed_from=renamed_from)


class NativeSource(_Source):
    """A folder the operating system reports on (`osbridge.dirwatch`)."""

    backend = BACKEND_NATIVE

    def __init__(self, root: Any, sink: ChangeBuffer, *,
                 rules: Callable[[], Any], stop: threading.Event,
                 on_problem: Optional[Callable[[Path, Optional[AppError]], None]] = None,
                 open_watch: Optional[Callable[[str], Any]] = None,
                 reopen_s: float = REOPEN_S) -> None:
        super().__init__(root, sink, rules=rules, stop=stop, on_problem=on_problem)
        self._open = open_watch or dirwatch.DirectoryWatch
        self._reopen_s = reopen_s
        self._watch: Any = None

    def close(self) -> None:
        watch = self._watch
        if watch is not None:
            try:
                watch.close()
            except Exception:                    # noqa: BLE001 - closing never raises
                pass

    def run(self) -> None:
        opened_before = False
        while not self._stop.is_set():
            try:
                self._watch = self._open(str(self.root))
            except OSError as exc:
                self._lost(_os_reason(exc, self.root))
                self._stop.wait(self._reopen_s)
                continue
            self._report(None)
            self.ready.set()
            if opened_before:
                # Whatever happened while the folder could not be watched
                # was not seen. Look at all of it.
                self.sink.overflow(self.root)
            opened_before = True
            try:
                self._read_until_lost()
            finally:
                self.close()
                self._watch = None

    def _read_until_lost(self) -> None:
        while not self._stop.is_set():
            try:
                records = self._watch.read(READ_WAIT_S)
            except OSError as exc:
                if not self._stop.is_set():
                    self._lost(_os_reason(exc, self.root))
                    self._stop.wait(self._reopen_s)
                return
            if records is None:
                # A renamed folder keeps its handle but not its name: events
                # would be joined to a path that no longer exists.
                if not os.path.isdir(self.root):
                    self._lost("the folder is no longer there")
                    self._stop.wait(self._reopen_s)
                    return
                continue
            for action, name in records:
                if action == dirwatch.OVERFLOW:
                    log.info("changes under {} came faster than they could be "
                             "read; the whole folder will be looked at", self.root)
                    self.sink.overflow(self.root)
                    continue
                self._offer(
                    self.root / name,
                    new=action in (dirwatch.ADDED, dirwatch.RENAMED_TO),
                    renamed_from=action == dirwatch.RENAMED_FROM)


class PollingSource(_Source):
    """A folder that is listed and compared with last time.

    For every system but Windows, and for a folder Windows will not watch. It
    sees files only - a folder has no entry of its own - so a folder that
    appears is reported as its files, and one that goes as its files going.
    """

    backend = BACKEND_POLL

    def __init__(self, root: Any, sink: ChangeBuffer, *,
                 rules: Callable[[], Any], stop: threading.Event,
                 on_problem: Optional[Callable[[Path, Optional[AppError]], None]] = None,
                 interval_s: float = POLL_S, duty: float = POLL_DUTY,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(root, sink, rules=rules, stop=stop, on_problem=on_problem)
        self.interval_s = float(interval_s)
        self.duty = float(duty)
        self._clock = clock
        #: `path_key -> (path, mtime_ns, size)` from the last complete listing.
        self._known: Optional[dict[str, tuple[str, int, int]]] = None
        #: How long the last listing took, for `POLL_DUTY` and the measurement.
        self.last_scan_s = 0.0
        self.scans = 0

    def run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            wait = max(self.interval_s, self.last_scan_s / max(self.duty, 0.001))
            self._stop.wait(wait)

    def poll(self) -> int:
        """List the folder once and report what differs. Returns how many did.

        The first listing reports nothing: it is what later ones are compared
        with. A listing that could not be finished is thrown away whole - a
        drive that vanished half-way through must not look like a thousand
        deleted files.
        """
        started = self._clock()
        try:
            found = self._snapshot()
        except _Stopping:
            return 0
        except OSError as exc:
            self._lost(_os_reason(exc, self.root))
            return 0
        finally:
            self.last_scan_s = self._clock() - started
        self.scans += 1
        self._report(None)
        self.ready.set()
        known, self._known = self._known, found
        if known is None:
            return 0
        changed = 0
        for key, (path, mtime_ns, size) in found.items():
            before = known.get(key)
            if before is None:
                self._offer(Path(path), new=True)
                changed += 1
            elif before[1] != mtime_ns or before[2] != size:
                self._offer(Path(path))
                changed += 1
        for key, (path, _mtime, _size) in known.items():
            if key not in found:
                self._offer(Path(path))
                changed += 1
        return changed

    def _snapshot(self) -> dict[str, tuple[str, int, int]]:
        """Every file the walker would reach under the folder, without opening one."""
        rules = self._rules()
        if not os.path.isdir(self.root):
            raise FileNotFoundError(2, "the folder is no longer there", str(self.root))
        found: dict[str, tuple[str, int, int]] = {}
        pending = [self.root]
        while pending:
            if self._stop.is_set():
                raise _Stopping()
            folder = pending.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                if folder == self.root:
                    raise
                continue                       # one unreadable sub-folder
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not rules.excluded(self.root, path, is_dir=True):
                            pending.append(path)
                        continue
                    if rules.excluded(self.root, path):
                        continue
                    stat = entry.stat()
                except OSError:
                    continue                   # gone between the listing and the stat
                found[path_key(path)] = (entry.path, stat.st_mtime_ns, stat.st_size)
        return found


def _os_reason(exc: OSError, root: Path) -> str:
    if isinstance(exc, FileNotFoundError) or not os.path.isdir(root):
        return "the folder is not there (moved, renamed, or its drive is not connected)"
    if isinstance(exc, PermissionError):
        return "Windows refused access to it"
    return str(exc.strerror or exc) or type(exc).__name__


# ---------------------------------------------------------------------------
# Putting a batch into the index
# ---------------------------------------------------------------------------

@dataclass
class BatchResult:
    """What one batch came to. Plain numbers, for the status line and the log."""

    #: Documents written (a changed file is one; a folder that appeared, many).
    indexed: int = 0
    #: Rows removed because their file has gone.
    removed: int = 0
    #: Files the pipeline looked at and found already up to date.
    unchanged: int = 0
    #: Files recorded as skipped (unreadable, locked, no reader...).
    skipped: int = 0
    #: Paths that needed nothing, decided before the run lock was taken.
    ignored: int = 0
    #: Mailboxes left for the ordinary run - see the module notes.
    mailboxes_left: int = 0
    #: Folders looked at in full after an overflow.
    rescanned: int = 0
    seconds: float = 0.0
    #: Locked files to be offered again later.
    retry: list[Change] = field(default_factory=list)
    #: Folders that should be looked at in full but are not there right now.
    rescan_later: list[Path] = field(default_factory=list)
    #: The names of up to five files that were indexed, for the status line.
    names: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "indexed": self.indexed, "removed": self.removed,
            "unchanged": self.unchanged, "skipped": self.skipped,
            "ignored": self.ignored, "mailboxes_left": self.mailboxes_left,
            "rescanned": self.rescanned, "seconds": round(self.seconds, 3),
            "retry": len(self.retry), "names": list(self.names),
        }


def _exact_name(path: Path) -> bool:
    """Is there an entry spelled exactly like `path.name` in its folder?

    Windows answers "yes, it exists" for `Report.docx` when the file is now
    `report.docx`. After a rename away from a name, that is the difference
    between "renamed to something else" and "only the case changed".
    """
    try:
        return path.name in os.listdir(path.parent)
    except OSError:
        return False


class BatchIndexer:
    r"""Applies one `Batch` to the index through the ordinary pipeline.

    `config(roots)` returns the `PipelineConfig` an ordinary run over those
    folders would use (the command line passes `build_pipeline_config`); this
    narrows it to the batch. `embedder()` builds the meaning model - called at
    the first batch that needs it and again after `MODEL_IDLE_S` of nothing to
    do, never at construction, so a watch that sees no changes loads nothing.

    Call it with a batch: returns a `BatchResult`, raises `IndexBusy` when the
    batch must wait, and lets any other failure out for the caller to report.
    """

    def __init__(self, store: Any, vectors: Any, *,
                 embedder: Callable[[], Any],
                 config: Callable[[list[Path]], Any],
                 image_embedder: Any = None, image_vectors: Any = None,
                 owner: str = FOLDER_WATCH, lock_dir: Any = None,
                 lock_name: str = INDEX_MUTEX_NAME,
                 probe: Optional[Callable[[], Any]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.vectors = vectors
        self._embedder_factory = embedder
        self._config = config
        self.image_embedder = image_embedder
        self.image_vectors = image_vectors
        self.owner = owner
        self._lock_dir = lock_dir
        #: The run lock's name. Only a test passes another, so it cannot meet
        #: a real index run on the machine it is running on.
        self._lock_name = lock_name
        self._probe = probe
        self._clock = clock
        self._embedder: Any = None
        self._last_used = clock()
        #: Set while a pipeline is running, so `stop` can end it cleanly.
        self._pipeline: Any = None
        self._stopping = False
        #: Totals since this object was built, for `app.cli watch`'s last line.
        self.batches = 0
        self.total_indexed = 0
        self.total_removed = 0

    # -- the model -------------------------------------------------------------

    def idle(self) -> None:
        """Nothing is waiting. Let go of the model if it has not been needed
        for `MODEL_IDLE_S`."""
        if self._embedder is not None and (
                self._clock() - self._last_used) >= MODEL_IDLE_S:
            log.debug("the meaning model was not needed for {:.0f}s; letting it go",
                      MODEL_IDLE_S)
            self._embedder = None

    def stop(self) -> None:
        """Ask a batch in progress to finish the file in hand and end."""
        self._stopping = True
        pipeline = self._pipeline
        if pipeline is not None:
            try:
                pipeline.request_stop()
            except Exception as exc:             # noqa: BLE001
                log.debug("could not stop the batch in progress: {}", exc)

    # -- one batch -------------------------------------------------------------

    def __call__(self, batch: Batch) -> BatchResult:
        from app.index.walker import PathRules

        started = self._clock()
        result = BatchResult()
        roots = sorted({Path(change.root) for change in batch.changes}
                       | {Path(root) for root in batch.rescan}, key=str)
        if not roots:
            return result
        config = self._config(list(roots))
        rules = PathRules(config.walk)
        plan = self._plan(batch, rules, result)
        if not plan.anything:
            result.seconds = self._clock() - started
            return result

        self._may_start(config)
        try:
            lock = IndexRunLock(self.store, owner=self.owner, name=self._lock_name,
                                lock_dir=self._lock_dir).acquire()
        except AppErrorException as exc:
            if exc.error.code == "ERR_INDEX_RUNNING":
                raise IndexBusy(exc.error.message) from exc
            raise
        try:
            self._apply(plan, config, result)
        finally:
            lock.release()
            self._last_used = self._clock()
        self.batches += 1
        self.total_indexed += result.indexed
        self.total_removed += result.removed
        result.seconds = self._clock() - started
        return result

    def _may_start(self, config: Any) -> None:
        """Wait outside the lock, not inside it.

        The pipeline's own governor pauses a run that is on battery or short
        of disk - while holding the run lock, which would refuse the owner's
        own Start for as long as the laptop stays unplugged. Asked here first,
        the batch simply waits in the buffer.
        """
        from app.index.resources import SystemProbe, verdict

        try:
            limits = config.resolved_limits()
            snapshot = (self._probe or SystemProbe(
                lambda: getattr(self.vectors, "uri", None)).read)()
            found = verdict(snapshot, limits)
        except Exception as exc:                 # noqa: BLE001 - cannot tell: carry on
            log.debug("could not read the machine before a batch: {}", exc)
            return
        if found.action != "run" and found.cause in ("battery", "disk"):
            raise IndexBusy(found.reason, retry_s=60.0)

    # -- deciding what the batch really is ----------------------------------------

    @dataclass
    class _Plan:
        files: list = field(default_factory=list)        # Candidates
        changes: dict = field(default_factory=dict)      # path_key -> Change
        subtrees: list = field(default_factory=list)     # (root, folder)
        rescan: list = field(default_factory=list)       # roots
        gone: list = field(default_factory=list)         # file ids to remove

        @property
        def anything(self) -> bool:
            return bool(self.files or self.subtrees or self.rescan or self.gone)

    def _plan(self, batch: Batch, rules: Any, result: BatchResult) -> "_Plan":
        """Ask the disk and the index what each path in the batch needs.

        Read-only, and done before the run lock is taken: most events need
        nothing (a folder whose contents changed, a file touched and left the
        same, an attribute flipped), and a batch of only those never takes
        the lock at all.
        """
        from app.index.walker import STREAMED_MAILBOXES

        plan = self._Plan()
        rescan_keys = set()
        for root in batch.rescan:
            if os.path.isdir(root):
                plan.rescan.append(Path(root))
                rescan_keys.add(_root_key(root))
            else:
                # Not there right now: nothing can be said about its files.
                # Kept, so it is looked at when it comes back.
                result.rescan_later.append(Path(root))
        roots_there: dict[str, bool] = {}
        for change in batch.changes:
            root_key = _root_key(change.root)
            if root_key in rescan_keys:
                continue                        # the rescan covers it
            if root_key not in roots_there:
                roots_there[root_key] = os.path.isdir(change.root)
            if not roots_there[root_key]:
                # The folder itself is unreachable. "Missing" means nothing.
                result.ignored += 1
                continue
            self._plan_one(change, rules, plan, result, STREAMED_MAILBOXES)
        return plan

    def _plan_one(self, change: Change, rules: Any, plan: "_Plan",
                  result: BatchResult, mailboxes: frozenset) -> None:
        path = change.path
        try:
            is_dir = path.is_dir()
            exists = is_dir or path.exists()
        except OSError:
            is_dir = exists = False
        if exists and change.renamed_from and not _exact_name(path):
            exists = is_dir = False             # only the case of the name changed
        if not exists:
            if rules.excluded(change.root, path):
                result.ignored += 1
                return
            ids = self.store.file_ids_at_or_under(str(path))
            if ids:
                plan.gone.extend(ids)
            else:
                result.ignored += 1
            return
        if is_dir:
            if change.new and not rules.excluded(change.root, path, is_dir=True):
                plan.subtrees.append((change.root, path))
            else:
                result.ignored += 1
            return
        candidate = rules.candidate(change.root, path)
        if candidate is None:
            result.ignored += 1
            return
        record = self.store.get_file(str(path))
        if candidate.ext in mailboxes:
            if record is not None:
                result.mailboxes_left += 1
                return
            # New to the index: findable by name now, read by the next run.
            candidate = replace(candidate, readable=False)
        if change.attempts:
            candidate = replace(candidate, retry=True)
        elif not _needs_work(candidate, record):
            result.ignored += 1
            return
        plan.files.append(candidate)
        plan.changes[path_key(path)] = change

    # -- applying it -----------------------------------------------------------------

    def _apply(self, plan: "_Plan", config: Any, result: BatchResult) -> None:
        from app.index.pipeline import Pipeline

        if self._embedder is None:
            self._embedder = self._embedder_factory()
        light = self._narrow(config, plan)
        pipeline = Pipeline(
            self.store, self.vectors, self._embedder, light,
            image_embedder=self.image_embedder, image_vectors=self.image_vectors)
        pipeline.run_owner = self.owner
        if plan.gone:
            # Before the run, so a file renamed from A to B leaves one row.
            result.removed += pipeline.forget_files(plan.gone)
        if not (plan.files or plan.subtrees or plan.rescan):
            return
        self._pipeline = pipeline
        try:
            if self._stopping:
                raise IndexBusy("the watch is stopping", retry_s=0.0)
            stats = pipeline.run()
        finally:
            self._pipeline = None
        if stats.stopped_early is not None:
            raise AppErrorException(stats.stopped_early)
        if getattr(pipeline, "_interrupted", False):
            raise IndexBusy("the update was stopped before it finished")
        result.indexed += int(stats.indexed)
        result.unchanged += int(stats.unchanged)
        result.skipped += int(stats.skipped)
        result.removed += int(stats.deleted)
        result.rescanned += len(plan.rescan)
        self._after(plan, result)

    def _narrow(self, config: Any, plan: "_Plan") -> Any:
        """The ordinary run's configuration, narrowed to this batch."""
        walk = replace(
            config.walk,
            # Never a download: see `walker.PathRules`.
            include_cloud=False, cloud_content_roots=frozenset())
        return replace(
            config,
            walk=walk,
            candidate_source=_CandidateSource(plan.files, plan.subtrees, plan.rescan,
                                              self.store),
            light=True,
            # Rows under a rescanned folder that are no longer on disk go;
            # nothing outside it is looked at.
            prune_missing=bool(plan.rescan),
            prune_under=tuple(str(root) for root in plan.rescan) or None,
            # An archive folder's "walked once" record belongs to a full walk.
            archives=False, recheck_archives=False,
            # A handful of files: no reader processes to start and end per
            # batch, no sort before reading, and the word index kept live so
            # the file is findable the moment it is written.
            read_processes=False, read_order="found", bulk_fts="off",
            worker_ceiling=0,
            workers=(config.workers if (plan.rescan or plan.subtrees)
                     else min(max(1, config.worker_count()), 2)),
            retry_locked=False, retry_skipped=False, force=False,
            pause_file=None,
        )

    def _after(self, plan: "_Plan", result: BatchResult) -> None:
        """Which files to offer again (locked), and a few names for the status."""
        from app.storage.sqlite_store import FileStatus

        for candidate in plan.files:
            try:
                record = self.store.get_file(str(candidate.path))
            except Exception:                    # noqa: BLE001 - a status, not the work
                continue
            if record is None:
                continue
            change = plan.changes.get(path_key(candidate.path))
            if (getattr(record, "skip_code", None) == "ERR_FILE_LOCKED"
                    and change is not None and change.attempts < LOCKED_RETRIES):
                result.retry.append(replace(change, attempts=change.attempts + 1))
            elif len(result.names) < 5 and record.status == FileStatus.INDEXED:
                result.names.append(candidate.path.name)


def _needs_work(candidate: Any, record: Any) -> bool:
    """Could the pipeline do anything with this file? Conservative: when in
    doubt, yes - `Pipeline._classify` makes the real decision.

    No, only when the index already has a settled row with this exact size
    and date and the file was not written in the last moment (the window in
    which a timestamp cannot be trusted - `walker.RECENT_EDIT_WINDOW_S`).
    """
    from app.index.walker import _modified_recently
    from app.storage.sqlite_store import FileStatus

    if record is None:
        return True
    settled = (FileStatus.INDEXED, FileStatus.NAME_ONLY, FileStatus.SKIPPED,
               FileStatus.FAILED)
    if record.status not in settled:
        return True
    if record.mtime_ns != candidate.mtime_ns or record.size_bytes != candidate.size_bytes:
        return True
    if (record.status == FileStatus.SKIPPED and record.skip_code == "ERR_CLOUD_ONLY"
            and not candidate.is_cloud_placeholder):
        return True                             # it has been downloaded since
    if record.status == FileStatus.NAME_ONLY and candidate.readable:
        return True                             # a reader exists for it now
    return bool(candidate.readable and _modified_recently(candidate))


class _CandidateSource:
    """`PipelineConfig.candidate_source` for one batch.

    Yields the batch's files, then walks the folders that appeared and the
    folders to be looked at in full - with `walker.walk` itself, so they get
    the walk's own exclusions, placeholder handling and repository detection.
    """

    def __init__(self, files: Sequence[Any], subtrees: Sequence[tuple[Path, Path]],
                 rescan: Sequence[Path], store: Any) -> None:
        self.files = list(files)
        self.subtrees = list(subtrees)
        self.rescan = list(rescan)
        self.store = store

    def __call__(self, walk_config: Any, seen: set) -> Iterator[Any]:
        from app.index.walker import STREAMED_MAILBOXES, enclosing_repo, repo_kind_at, walk

        sink = walk_config.repo_sink
        looked: set[str] = set()

        def note_repo(folder: Path, ceiling: Path) -> None:
            # A walk finds a repository by passing its `.git`; a single file
            # passes nothing, so its repository is looked for upwards, once
            # per folder, no higher than the indexed folder.
            if sink is None or str(folder) in looked:
                return
            looked.add(str(folder))
            try:
                found = enclosing_repo(folder, ceiling=ceiling)
            except OSError:
                return
            if found is not None:
                sink.setdefault(str(found), repo_kind_at(found) or "work")

        roots = [Path(root) for root in walk_config.roots]
        for candidate in self.files:
            key = path_key(candidate.path)
            if key in seen:
                continue
            seen.add(key)
            note_repo(candidate.path.parent, _root_of(candidate.path, roots))
            yield candidate
        for root, folder in self.subtrees:
            note_repo(folder.parent, Path(root))
            yield from self._walk(walk_config, folder, seen, STREAMED_MAILBOXES, walk)
        for root in self.rescan:
            yield from self._walk(walk_config, root, seen, STREAMED_MAILBOXES, walk)

    def _walk(self, walk_config: Any, folder: Path, seen: set,
              mailboxes: frozenset, walk: Callable[..., Iterable[Any]]) -> Iterator[Any]:
        for candidate in walk(replace(walk_config, roots=[folder]), seen):
            if candidate.ext in mailboxes and candidate.readable:
                try:
                    known = self.store.get_file(str(candidate.path)) is not None
                except Exception:                # noqa: BLE001 - the pipeline decides
                    known = True
                if known:
                    continue                     # left for the ordinary run
                candidate = replace(candidate, readable=False)
            yield candidate


def _root_of(path: Path, roots: Sequence[Path]) -> Path:
    """The indexed folder `path` is under - the longest, if several are."""
    text = str(path).lower()
    best: Optional[Path] = None
    for root in roots:
        prefix = str(root).lower().rstrip("\\/")
        if text.startswith(prefix) and (best is None or len(str(root)) > len(str(best))):
            best = root
    return best if best is not None else path.parent


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------

def live_roots(roots: Iterable[Any], store: Any = None) -> list[Path]:
    """The folders to watch: the indexed folders, less those marked Archive.

    A folder the owner marked as an archive is one they have said does not
    change (`app/index/archives.py`); it keeps its own, cheaper checks and is
    not watched. Never raises: an unreadable setting means every folder is
    live, which watches more rather than less.
    """
    chosen = [Path(root) for root in roots if str(root).strip()]
    if store is None:
        return chosen
    try:
        from app.index.archives import ARCHIVE, MODE_STATE_KEY, load_modes, normalise

        modes = load_modes(store.get_state(MODE_STATE_KEY, "") or "")
    except Exception as exc:                     # noqa: BLE001 - see the docstring
        log.debug("could not read which folders are archives: {}", exc)
        return chosen
    return [root for root in chosen if modes.get(normalise(root)) != ARCHIVE]


class FolderWatcher:
    r"""Watches `roots` and hands ready batches to `apply`.

        watcher = FolderWatcher(roots, apply=BatchIndexer(...), rules=rules)
        watcher.start()
        ...
        watcher.stop()

    `rules()` returns the `walker.PathRules` for the folders, so a source can
    drop an excluded path the moment it is reported. `on_event(kind, data)` is
    told what happens, in plain data, from the watcher's own threads:

    * `"watching"` - `{"root", "backend"}`, once per folder when it starts;
    * `"ready"` - `{"folders"}`, once every folder is really being noticed;
    * `"problem"` - `{"root", "error"}` (an `AppError`), or `error=None` when
      the folder is being watched again;
    * `"pending"` - `{"count"}`, when changes are waiting to go quiet;
    * `"busy"` - `{"reason", "count"}`, a batch put back to wait;
    * `"updated"` - a `BatchResult.as_dict()`, after a batch that did something;
    * `"error"` - `{"error", "count"}`, a batch that failed and was put back.

    `backend` is `auto` (the system's notifications where they exist, a
    comparison otherwise), `native` or `poll`.
    """

    def __init__(self, roots: Iterable[Any], *,
                 apply: Callable[[Batch], BatchResult],
                 rules: Callable[[], Any],
                 backend: str = BACKEND_AUTO,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 buffer: Optional[ChangeBuffer] = None,
                 tick_s: float = TICK_S, poll_s: float = POLL_S,
                 open_watch: Optional[Callable[[str], Any]] = None) -> None:
        self.roots = [Path(root) for root in roots]
        self.apply = apply
        self._rules = rules
        self.backend = backend
        self._on_event = on_event
        # `is None`, not `or`: an empty buffer has length 0 and is falsy.
        self.buffer = buffer if buffer is not None else ChangeBuffer()
        self._tick_s = float(tick_s)
        self._poll_s = float(poll_s)
        self._open_watch = open_watch
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.sources: list[_Source] = []
        self._error_wait = ERROR_RETRY_S
        self._said_pending = 0
        #: Batches applied, and the last result, for callers that ask.
        self.batches = 0
        self.last_result: Optional[BatchResult] = None
        self.last_error: Optional[AppError] = None

    # -- life ----------------------------------------------------------------------

    def _use_native(self) -> bool:
        if self.backend == BACKEND_POLL:
            return False
        if self._open_watch is not None:
            return True
        return dirwatch.native_available()

    def start(self) -> "FolderWatcher":
        if self._thread is not None:
            return self
        self._stop.clear()
        native = self._use_native()
        for root in self.roots:
            if native:
                source: _Source = NativeSource(
                    root, self.buffer, rules=self._rules, stop=self._stop,
                    on_problem=self._problem, open_watch=self._open_watch)
            else:
                source = PollingSource(
                    root, self.buffer, rules=self._rules, stop=self._stop,
                    on_problem=self._problem, interval_s=self._poll_s)
            self.sources.append(source)
            source.start()
            self._say("watching", {"root": str(root), "backend": source.backend})
        self._thread = threading.Thread(
            target=self._dispatch, name="folder-watch-dispatch", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout_s: float = 10.0) -> None:
        """Stop watching. A batch in progress finishes the file in hand first."""
        self._stop.set()
        stopper = getattr(self.apply, "stop", None)
        if stopper is not None:
            try:
                stopper()
            except Exception as exc:             # noqa: BLE001 - stopping never raises
                log.debug("could not stop the batch in progress: {}", exc)
        for source in self.sources:
            source.close()
        deadline = time.monotonic() + max(0.0, timeout_s)
        threads = [source.thread for source in self.sources] + [self._thread]
        for thread in threads:
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._thread = None
        self.sources = []

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait_ready(self, timeout_s: float = 30.0) -> bool:
        """Wait until every folder is being noticed. False if one is not yet
        (it cannot be opened, or a first listing of a large tree is still
        going) - the watch carries on either way."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        for source in list(self.sources):
            if not source.ready.wait(max(0.0, deadline - time.monotonic())):
                return False
        return True

    def problems(self) -> dict[str, AppError]:
        """Folders that cannot be watched right now, and why."""
        return {str(source.root): source.problem for source in self.sources
                if source.problem is not None}

    # -- the dispatcher ------------------------------------------------------------

    def _dispatch(self) -> None:
        said_ready = False
        while not self._stop.wait(self._tick_s):
            if not said_ready and all(s.ready.is_set() for s in self.sources):
                said_ready = True
                self._say("ready", {"folders": len(self.sources)})
            try:
                self.step()
            except Exception as exc:             # noqa: BLE001 - the watch must not die
                log.error("the folder watch could not take a step: {}", exc)

    def step(self) -> Optional[BatchResult]:
        """Apply one ready batch, if there is one. Called every `TICK_S`, and
        by tests. Never raises for a batch that fails: it is put back."""
        batch = self.buffer.take()
        if batch is None:
            waiting = len(self.buffer)
            if waiting != self._said_pending:
                self._said_pending = waiting
                if waiting:
                    self._say("pending", {"count": waiting})
            if not waiting:
                idle = getattr(self.apply, "idle", None)
                if idle is not None:
                    idle()
            return None
        self._said_pending = 0
        try:
            result = self.apply(batch)
        except IndexBusy as busy:
            self.buffer.restore(batch, delay_s=busy.retry_s, hold=True)
            self._say("busy", {"reason": busy.reason, "count": len(batch)})
            return None
        except Exception as exc:                 # noqa: BLE001 - reported, kept, retried
            error = _update_error(exc, len(batch))
            self.last_error = error
            self.buffer.restore(batch, delay_s=self._error_wait, hold=True)
            self._error_wait = min(self._error_wait * 2, ERROR_RETRY_MAX_S)
            log.warning("{}", error.render())
            self._say("error", {"error": error, "count": len(batch)})
            return None
        self._error_wait = ERROR_RETRY_S
        self.last_error = None
        self.batches += 1
        self.last_result = result
        if result.retry:
            self.buffer.restore(Batch(changes=list(result.retry)),
                                delay_s=LOCKED_RETRY_S)
        if result.rescan_later:
            self.buffer.restore(Batch(rescan=list(result.rescan_later)),
                                delay_s=REOPEN_S)
        if result.indexed or result.removed or result.skipped or result.rescanned:
            self._say("updated", result.as_dict())
        return result

    # -- saying what happened ---------------------------------------------------------

    def _problem(self, root: Path, error: Optional[AppError]) -> None:
        if error is not None:
            log.warning("{}", error.render())
        else:
            log.info("watching {} again", root)
        self._say("problem", {"root": str(root), "error": error})

    def _say(self, kind: str, data: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, data)
        except Exception as exc:                 # noqa: BLE001 - a listener never stops the watch
            log.debug("a folder watch listener failed on {!r}: {}", kind, exc)


def _update_error(exc: BaseException, count: int) -> AppError:
    """`ERR_WATCH_UPDATE`, carrying the cause's own words and detail."""
    cause = to_app_error(exc, "index.folder_watch")
    return make_error(
        "ERR_WATCH_UPDATE", "index.folder_watch", count=count,
        reason=cause.message.rstrip("."),
        details=f"{cause.code}: {cause.details or cause.message}")
