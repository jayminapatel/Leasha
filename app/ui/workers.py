"""Background work, so the UI thread never does I/O.

Layer: L5

Non-negotiable #6: *the UI thread never does I/O.* Every search, every index run
and every file open goes through here, onto a `QThreadPool`, and comes back as a
signal. A 4GB PST or a cold ANN probe on the UI thread means a frozen window,
and a frozen window is indistinguishable from a crashed one.

**Every worker catches everything.** An exception escaping a `QRunnable` does not
propagate anywhere useful - Qt logs it and the task simply vanishes, leaving the
UI waiting forever for a signal that will never arrive. So each worker converts
whatever escapes into an `AppError` and emits it, which is the same contract
every other layer honours.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from app.core.errors import AppError, to_app_error
from app.core.logging import logger
from app.core.osbridge import hidden_console_flags, new_console_flags

__all__ = [
    "CallableWorker",
    "open_async",
    "open_at_line",
    "open_at_line_async",
    "open_row_async",
    "IndexWorker",
    "SearchWorker",
    "WorkerSignals",
    "run",
    "stop_timers",
]

_log = logger.bind(component="ui.workers")

#: How long an index run waits for queued UI state writes before it starts
#: reading settings (see `IndexWorker.run`). A keyed upsert takes milliseconds;
#: this is the ceiling for one stuck behind another process's transaction.
SETTLED_STATE_WAIT_MS = 5_000


#: Codes that mean "the window is closing", not "something went wrong".
#:
#: Three ERR_UNEXPECTED tracebacks appeared on every exit, each telling the
#: owner "This is a bug... send the log file". Closing a window is not a bug,
#: and printing that it is buries the errors that are.
_SHUTDOWN_CODES = frozenset({"ERR_SHUTTING_DOWN"})

#: Substrings of the *detail* that mean the same thing, for the races that
#: surface as a plain RuntimeError from the standard library before any of this
#: application's code can label them.
_SHUTDOWN_DETAILS = (
    "cannot schedule new futures after shutdown",
    # **The short form, deliberately.** This matched the full sentence
    # "used before connect(), or after close()", which SqliteStore's main guard
    # says - but VectorStore said only "used before connect()." and SqliteStore
    # has a second path that says the same. So a perfectly ordinary window close
    # printed ERR_UNEXPECTED with "This is a bug... send the log file" and a
    # traceback, which is exactly what this list exists to prevent.
    #
    # Matching the shorter fragment covers every variant, and there is no case
    # where "used before connect()" in a detail line means something a person
    # needs to act on: a genuine use-before-connect is a programming error that
    # fails on the first run, in a test, not in the field.
    "used before connect()",
    "was closed while a worker was using it",
)


def _is_shutdown(error: Any) -> bool:
    if getattr(error, "code", "") in _SHUTDOWN_CODES:
        return True
    detail = str(getattr(error, "details", "") or "")
    return any(fragment in detail for fragment in _SHUTDOWN_DETAILS)

#: Every worker handed to a QThreadPool, until it reports itself done.
#:
#: **Without this the application crashes.** `QThreadPool.start()` takes
#: ownership of the `QRunnable` on the C++ side, but nothing on the Python side
#: holds the `WorkerSignals` QObject it carries. As soon as the local variable
#: at the call site goes out of scope, Python collects it, sip deletes the
#: underlying C++ object, and the worker - still running on its thread - dies
#: emitting into a corpse:
#:
#:     RuntimeError: wrapped C/C++ object of type WorkerSignals has been deleted
#:
#: It only bites when a search outlives the function that started it, which is
#: exactly what happens when the machine is busy - so it looks intermittent and
#: unrelated to anything.
_IN_FLIGHT: set[Any] = set()


def _retain(worker: Any) -> Any:
    """Hold a worker alive for as long as its thread might still touch it."""
    _IN_FLIGHT.add(worker)
    worker.signals.done.connect(lambda: _IN_FLIGHT.discard(worker))
    return worker


def stop_timers(view: Any, *names: str) -> None:
    """Stop a view's debounce timers and stale anything still in flight.

    **Called from `closeEvent`, before anything is torn down.** A timer that
    fires during teardown starts a query against a store that is being closed,
    which arrives as a traceback telling the owner to send the log file.
    Nothing is wrong; the work simply should not have begun.

    Here rather than three near-identical copies in three views - the third one
    is where the divergence starts, and a view that quietly stops stopping its
    timer would put those tracebacks straight back.
    """
    # Bumping the generation is what makes a result that lands anyway get
    # dropped: every view checks it before drawing.
    if hasattr(view, "_generation"):
        view._generation += 1
    if hasattr(view, "_shown_generation"):
        view._shown_generation += 1
    # **A named timer that does not exist is a mistake, not an absence.**
    # `search_view` asked for `_typing_timer`, `_idle_timer` and `_timer`; its
    # timers are called `_interim_timer` and `_full_timer`. Every name missed,
    # `getattr(..., None)` returned None three times, and `shutdown()` stopped
    # nothing at all - which is exactly the shutdown race it exists to prevent,
    # hidden behind a call that looked correct at both ends.
    #
    # Stopping every timer the view owns is what makes the argument list an
    # optimisation rather than a promise, so a rename cannot silently disarm it.
    stopped = 0
    for name in names:
        timer = getattr(view, name, None)
        if timer is not None:
            timer.stop()
            stopped += 1

    for attribute in vars(view):
        if not attribute.endswith("_timer") or attribute in names:
            continue
        timer = getattr(view, attribute, None)
        if timer is not None and hasattr(timer, "stop"):
            timer.stop()
            stopped += 1

    if not stopped:
        _log.debug("{} had no timers to stop", type(view).__name__)


def run(pool: Any, worker: Any) -> Any:
    """Start a worker on `pool`, keeping it alive until it finishes.

    Always use this rather than `pool.start(worker)` directly.
    """
    _retain(worker)
    pool.start(worker)
    return worker


def _emit(signals: Any, name: str, *args: Any) -> None:
    """Emit `signals.<name>`, unless Qt has already torn the object down.

    At shutdown the C++ side goes first, and a thread still finishing its work
    emits into nothing. The window is closing and nobody is listening, so that is
    not worth a traceback - but an unhandled `RuntimeError` inside a `QRunnable`
    produces three nested ones and looks exactly like a crash.

    **The signal is looked up by name, inside the try.** That is the whole point
    of the two-argument form, and it is not a style choice. The first version
    took the bound signal directly - `_emit(self.signals.finished, result)` -
    which cannot work: the attribute is evaluated at the *call site*, before
    `_emit` is entered, so once sip has deleted the underlying object the
    `RuntimeError` is raised while building the arguments, outside the
    try/except written to catch it.

    The guard was therefore never reached even once, and the proof was a
    traceback whose caret pointed at the argument rather than at `emit`:

        File "app/ui/workers.py", line 223, in run
            _emit(self.signals.finished, result)
                  ^^^^^^^^^^^^^^^^^^^^^
        RuntimeError: wrapped C/C++ object of type WorkerSignals has been deleted

    The `except` clause then tried `self.signals.failed` and the `finally` tried
    `self.signals.done`, each failing the same way - so one dead object produced
    three nested tracebacks, which is exactly what this was written to prevent.

    Never pass a bound signal to this function.
    """
    try:
        getattr(signals, name).emit(*args)
    except RuntimeError:
        pass
    except Exception as exc:
        # A slot that raises must not take the worker down with it, and must not
        # skip the `done` signal that releases the worker from `_IN_FLIGHT`.
        _log.debug("emitting {} failed: {}", name, exc)


class WorkerSignals(QObject):
    """Signals are on a QObject because QRunnable is not one."""

    finished = pyqtSignal(object)      # the result, whatever it is
    failed = pyqtSignal(object)        # an AppError, never a bare exception
    progress = pyqtSignal(object)      # partial state, for long runs
    done = pyqtSignal()                # always, success or failure


def open_async(path: str, *, reveal: bool = False,
               on_error: Any = None, component: str = "ui.open") -> None:
    r"""Hand a path to Explorer on a worker thread. Never blocks the UI.

    `open_in_explorer` shells out, and on a network share or a sleeping
    external drive that is seconds of a frozen window - which is why its own
    docstring forbids calling it on the UI thread. It was called there anyway
    from `files_view`, and `shell._open_path` had already grown the correct
    version for search results.

    One function, so the third caller cannot get it wrong. It returns an
    `AppError` rather than raising, so the *result* is what carries a problem
    and `failed` is reserved for something genuinely unexpected - both are
    routed to `on_error`.
    """
    from PyQt6.QtCore import QThreadPool

    if not path:
        return
    worker = CallableWorker(open_in_explorer, path, select=reveal,
                            component=component)
    if on_error is not None:
        worker.signals.finished.connect(
            lambda error: on_error(error) if error is not None else None)
        worker.signals.failed.connect(on_error)
    run(QThreadPool.globalInstance(), worker)


def open_attachment_async(store: Any, path: str, cache_path: Any, *,
                          on_error: Any = None, on_note: Any = None,
                          opener: Callable[..., Any] | None = None) -> None:
    """Save a read-only copy of an attachment or zip member and open it. 2026-10-04.

    The owner: "build the open on attachment, save a copy and open it". The
    copy is written by `tasks.save_attachment_copy` and opened in its own
    program, both on this worker. A problem comes back as an `AppError` with
    the way out; success as a note that the copy is not the original.
    """
    from pathlib import Path

    from PyQt6.QtCore import QThreadPool

    from app.ui.attachment_open import zip_member_of
    from app.ui.tasks import save_attachment_copy

    if not path:
        return
    where = "the zip" if zip_member_of(path)[0] else "the email"

    def save_and_open() -> Any:
        from app.core.errors import AppErrorException

        try:
            target = save_attachment_copy(store, path, cache_path)
        except AppErrorException as exc:
            return exc.error
        error = (opener or open_in_explorer)(str(target), select=False)
        return error if isinstance(error, AppError) else target

    worker = CallableWorker(save_and_open, component="ui.attachment_open")

    def finished(result: Any) -> None:
        if isinstance(result, AppError):
            if on_error is not None:
                on_error(result)
        elif on_note is not None and result is not None:
            on_note(f"Opened a copy of '{Path(result).name}' from {where}. "
                    f"Changes to it are not saved back to {where}.")

    worker.signals.finished.connect(finished)
    if on_error is not None:
        worker.signals.failed.connect(on_error)
    run(QThreadPool.globalInstance(), worker)


def open_media_at(path: str, seconds: Any) -> Any:
    """Worker body for `open_media_async`: an `AppError` or a sentence or None."""
    from app.core import media_open

    outcome = media_open.open_at(path, seconds)
    return outcome.error if outcome.error is not None else (outcome.note or None)


def open_media_async(path: str, seconds: Any, *, on_error: Any = None,
                     on_note: Any = None, component: str = "ui.open") -> None:
    r"""Open a recording at the moment a result is about. Never blocks the UI.

    Work order 202626270515. The same worker discipline as `open_async` (the
    player lookup is stat calls and the launch a process start), with one more
    thing to say: when no installed player can be told where to start, the file
    is opened plainly and `on_note` is handed the sentence that says *which
    moment* - so a result that cannot seek still tells the person where to look.
    """
    from PyQt6.QtCore import QThreadPool

    if not path:
        return
    worker = CallableWorker(open_media_at, path, seconds, component=component)

    def _done(result: Any) -> None:
        if isinstance(result, AppError):
            if on_error is not None:
                on_error(result)
        elif result and on_note is not None:
            on_note(str(result))

    worker.signals.finished.connect(_done)
    if on_error is not None:
        worker.signals.failed.connect(on_error)
    run(QThreadPool.globalInstance(), worker)


def editor_creationflags(command: Any) -> int:
    """How Windows should start this editor command. Order 0y §2c.

    A window editor gets **no console**: VS Code's `code` is a small console
    program that starts the real window, and from `pythonw.exe` it would flash a
    black box. An editor that lives in a terminal (`editors.TERMINAL_EDITORS`)
    gets **a console of its own**, or it would run where nobody can see it.
    """
    from app.ui.editors import TERMINAL_EDITORS

    program = str(command[0]) if command else ""
    stem = program.replace("\\", "/").rpartition("/")[2].lower()
    stem = stem.rpartition(".")[0] if "." in stem else stem
    return new_console_flags() if stem in TERMINAL_EDITORS else hidden_console_flags()


def start_editor(command: Any) -> None:
    """Start an editor and do not wait for it. **Worker thread only.**"""
    subprocess.Popen(list(command), shell=False,
                     creationflags=editor_creationflags(command))


def open_at_line(path: str, line: Any, *, choice: str = "auto", custom: str = "",
                 launch: Any = None, fallback: Any = None) -> Any:
    r"""Open a code result in the person's editor, at its line. Order 0y §2c.

    Worker body for `open_at_line_async`. Returns an `AppError`, or a sentence
    worth saying, or None - the same contract as `open_media_at`.

    `choice` and `custom` are the two settings (`CODE_EDITOR`,
    `CODE_EDITOR_COMMAND`); `editors.command_for` turns them into a command.
    When there is no command - nothing installed, or the person chose "None" -
    the file opens in its usual program, which is what Enter did before. When
    nothing was *found* that is said, with the line: somebody who pressed Enter
    on line 512 is owed the number if they land on line 1.

    `launch` and `fallback` are injected by the tests, which never start a
    real editor.
    """
    from pathlib import Path

    from app.ui import editors

    start = launch if launch is not None else start_editor
    plain = fallback if fallback is not None else open_in_explorer
    number = max(1, int(line or 1))
    try:
        exists = Path(path).exists()
    except OSError:
        exists = False
    if not exists:
        # One wording for a file that has gone, whichever way it was opened.
        return open_in_explorer(path, select=False)

    wanted = str(choice or editors.AUTO).strip().lower()
    own = str(custom or "").strip()
    command = (None if (wanted == editors.NONE and not own)
               else editors.command_for(wanted, path, number, custom=own))
    note = None
    if command:
        try:
            start(command)
            return None
        except Exception as exc:                    # noqa: BLE001 - fall back to a plain open
            _log.warning("the editor would not start ({}: {}); opening plainly",
                         type(exc).__name__, exc)
            note = (f"{editors.label_for(command)} would not start, so this opened "
                    f"in its usual program. What you searched for is at line {number}.")
    elif wanted != editors.NONE:
        note = (f"No code editor was found, so this opened in its usual program. "
                f"What you searched for is at line {number}. Choose an editor in "
                f"Settings, under “Opening code results”.")

    error = plain(path, select=False)
    return error if error is not None else note


def open_at_line_async(path: str, line: Any, *, choice: str = "auto", custom: str = "",
                       on_error: Any = None, on_note: Any = None,
                       component: str = "ui.open") -> None:
    """`open_at_line` on a worker. Never blocks the UI (non-negotiable #5).

    Finding the editor is stat calls and starting it is a process start - the
    same two costs `open_async` keeps off the interface thread.
    """
    from PyQt6.QtCore import QThreadPool

    if not path:
        return
    worker = CallableWorker(open_at_line, path, line, choice=choice, custom=custom,
                            component=component)

    def _done(result: Any) -> None:
        if isinstance(result, AppError):
            if on_error is not None:
                on_error(result)
        elif result and on_note is not None:
            on_note(str(result))

    worker.signals.finished.connect(_done)
    if on_error is not None:
        worker.signals.failed.connect(on_error)
    run(QThreadPool.globalInstance(), worker)


def open_row_async(store: Any, row: Any, *, reveal: bool = False,
                   on_error: Any = None, component: str = "ui.open") -> None:
    r"""`open_async`, for a result row rather than a bare path.

    Offline Media §1b/3a/3c: `row.path` for one on a catalogued volume
    (`row.volume_id is not None`) is never a real filesystem path - it is the
    letter-free key `volume_synthetic_path` builds - and needs resolving
    through the volume's *current* mount point first, which is a live
    Windows volume check and so never the interface thread. An ordinary row
    goes straight to `open_async`, unchanged. One function so a third caller
    (`files_view`, after `shell._open_volume_result`) cannot get it wrong.
    """
    if row is None:
        return
    if getattr(row, "volume_id", None) is None:
        open_async(str(getattr(row, "path", "") or ""), reveal=reveal,
                  on_error=on_error, component=component)
        return

    from PyQt6.QtCore import QThreadPool
    from app.ui.tasks import resolve_open_path

    def _resolve_and_open() -> Any:
        return open_in_explorer(resolve_open_path(store, row), select=reveal)

    worker = CallableWorker(_resolve_and_open, component=component)
    if on_error is not None:
        worker.signals.finished.connect(
            lambda error: on_error(error) if error is not None else None)
        worker.signals.failed.connect(on_error)
    run(QThreadPool.globalInstance(), worker)


class CallableWorker(QRunnable):
    """Run any callable off the UI thread and emit the result.

    `report_progress=True` hands the work function an `on_progress(stage)`
    keyword it can call zero or more times before returning - each call
    re-emits `signals.progress` on this worker. Off by default: most
    callables here are one query and have nothing worth staging, and adding
    the keyword unconditionally would break every existing call site that
    does not expect it.
    """

    def __init__(self, work: Callable[..., Any], *args: Any, component: str = "ui",
                report_progress: bool = False, **kwargs: Any):
        super().__init__()
        self._work = work
        self._args = args
        self._kwargs = kwargs
        self._component = component
        self._report_progress = report_progress
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            if self._report_progress:
                self._kwargs["on_progress"] = lambda stage: _emit(self.signals, "progress", stage)
            _emit(self.signals, "finished", self._work(*self._args, **self._kwargs))
        except Exception as exc:
            error = to_app_error(exc, self._component)
            if _is_shutdown(error):
                _log.debug("{} abandoned during shutdown", self._component)
                return
            _log.bind(error_code=error.code).error("{}", error.render())
            _emit(self.signals, "failed", error)
        finally:
            _emit(self.signals, "done")


class SearchWorker(QRunnable):
    """One search, at one tier.

    Carries `generation` - a monotonic counter the view increments on every
    keystroke - so a slow result that arrives after the person has typed more can
    be recognised and dropped. Without it, an old search landing late overwrites
    a newer one and the results flicker backwards, which looks like a bug in the
    ranking rather than a race.
    """

    def __init__(self, engine: Any, query: str, *, tier: str, generation: int, **options: Any):
        super().__init__()
        self._engine = engine
        self._query = query
        self._tier = tier
        self._options = options
        self.generation = generation
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            # **The recognised filters are applied here, on the worker** -
            # `presenter.auto_filters` reads the store's senders and file types,
            # which is I/O the interface thread may not do. Present only when
            # the surface's policy applies them (`search_options`), and taken
            # off the options, because the engine has no such argument and must
            # not: it may not know a sentence was read at all.
            options = dict(self._options)
            query, applied = self._query, ()
            if "declined" in options:
                from app.ui.presenter.search import auto_filters

                query, applied = auto_filters(
                    getattr(self._engine, "store", None), self._query,
                    options.get("policy"), options.pop("declined"))
            if self._tier == "interim":
                # The interim tier takes a scope too but not a rerank flag, so
                # the options cannot simply be forwarded whole.
                response = self._engine.interim(
                    query, scope=options.get("scope", "all")
                )
            else:
                response = self._engine.search(query, **options)
            # Set on every response, cached or not, so a chip can never be
            # carried over from the search that happened to fill the cache.
            response.applied = applied
            _emit(self.signals, "finished", (self.generation, response))
        except Exception as exc:
            error = to_app_error(exc, "ui.search")
            if _is_shutdown(error):
                # Expected, and not the user's problem. Logged at debug so it
                # is still findable, and never emitted - `failed` puts a dialog
                # in front of somebody who has already clicked the close button.
                _log.debug("search abandoned during shutdown")
                return
            _log.bind(error_code=error.code).error("{}", error.render())
            _emit(self.signals, "failed", error)
        finally:
            _emit(self.signals, "done")


class IndexWorker(QRunnable):
    """A whole index run, reporting progress as it goes.

    Holds the `Pipeline` so the UI can call `request_stop()` on it - which is a
    clean stop that preserves everything written, not a kill.
    """

    def __init__(self, pipeline: Any):
        super().__init__()
        self.pipeline = pipeline
        self.signals = WorkerSignals()

    def stop(self) -> None:
        self.pipeline.request_stop()

    def run(self) -> None:
        r"""**The run lock is held here, for exactly the length of the run.**

        Not by the window, which is the whole point of splitting it out of
        `SingleInstance`: a window that is merely open is a reader, and holding
        a writer's lock for its lifetime is what stopped `app.cli index` from
        running at all. Taken on this thread rather than on the UI thread so
        that waiting on it - if the CLI got there first - cannot freeze the
        window; the failure arrives as an ordinary `failed` signal.
        """
        from contextlib import nullcontext

        from app.core.priority import background_thread
        from app.core.run_lock import GUI, IndexRunLock

        def progress(payload: Any) -> None:
            # **A copy, made here on the run's thread.** The live `IndexStats`
            # is being written by the walker, the extraction threads and the
            # consumer while the window reads it to paint - see `snapshot`.
            snap = getattr(payload, "snapshot", None)
            _emit(self.signals, "progress", snap() if callable(snap) else payload)

        limits = getattr(getattr(self.pipeline, "config", None), "limits", None)
        polite = bool(getattr(limits, "low_priority", True))
        # **Settings saved a moment ago land before the run reads them.** UI
        # state writes are queued (`app.ui.state_writes`, bug 3a), and the run
        # reads some of them itself - the archive modes, the cloud-content
        # folders. Change one and press Start straight away, and without this
        # wait the run could read the value from before the change. Waited for
        # here, on the run's own thread, so the window never waits with it;
        # bounded, because a write stuck behind a CLI run must not hold this
        # one forever - the run then reads what is committed, as it always did.
        from app.ui import state_writes

        if not state_writes.settle_before_run(SETTLED_STATE_WAIT_MS):
            _log.warning("starting the index run with a settings save still queued "
                         "after {} ms; the run reads what is already saved",
                         SETTLED_STATE_WAIT_MS)
        # Work order 0x §2. **A run in a child process takes the lock itself**
        # (`app.index.child_run.ChildIndexRun`): it is an ordinary
        # `app.cli index`, and it could not take the lock if this thread held
        # it. Nor is this thread lowered for it - it only waits on the child's
        # output, and a waiting thread at the lowest priority would read the
        # child's progress late on a busy machine. The child lowers itself,
        # the way `app.cli index` always has.
        in_child = bool(getattr(self.pipeline, "takes_its_own_run_lock", False))
        try:
            with (nullcontext() if in_child
                  else IndexRunLock(getattr(self.pipeline, "store", None), owner=GUI)):
                # **This thread, and every thread the run starts, lowers itself
                # - the process does not.** The window is in this process, and
                # lowering the process lowered the window with it. See
                # `app.core.priority`.
                with background_thread(polite and not in_child):
                    self.pipeline.thread_priority_only = polite
                    stats = self.pipeline.run(on_progress=progress)
            _emit(self.signals, "finished", stats)
        except Exception as exc:
            error = to_app_error(exc, "ui.index")
            _log.bind(error_code=error.code).error("{}", error.render())
            _emit(self.signals, "failed", error)
        finally:
            _emit(self.signals, "done")


def open_in_explorer(path: str, *, select: bool = True) -> AppError | None:
    """Open a file, or its folder with the file selected. Returns an error or None.

    Windows only in the useful sense; falls back to a plain open elsewhere so
    development on another platform is not blocked.

    **Never call this on the UI thread.** `explorer /select,` takes a few
    hundred milliseconds to start, and `target.exists()` is a stat that can
    block for seconds on a network share or a drive that has spun down. Both
    were happening inline, which is why opening a result felt slow when the
    work itself is nearly free - see `MainWindow._open_result`.
    """
    from pathlib import Path

    from app.core.errors import make_error

    target = Path(path)
    if not target.exists():
        return make_error(
            "ERR_FILE_CORRUPT", "ui.open", path=str(target),
            suggestion="The file has moved or been deleted since it was indexed. "
                       "Re-index this folder to update the results.",
            details="Not found on disk.",
        )

    try:
        # Order 0x section 1b (2026-09-27): the platform-specific part lives in
        # `app.core.osbridge` now. On Windows it sends exactly what this
        # function always sent (`explorer /select,` or `os.startfile`); on a
        # Mac it uses Finder's `open -R`; elsewhere `xdg-open` as before.
        from app.core.osbridge import show_in_file_manager

        show_in_file_manager(target, select=select)
        return None
    except Exception as exc:
        return to_app_error(exc, "ui.open", path=str(target))


def recent_searches_async(store: Any, on_ready: Callable,
                          settings: Any = None) -> None:
    r"""What this person searched for before, off the interface thread.

    **A read of `searches` is still a read.** `test_no_store_call_outside_a_
    worker` caught §2e's first version asking the store for this while a box
    was being focused - which is a freeze waiting for a busy index, on the
    keystroke where somebody is least willing to wait. The rules that turn
    rows into a short list live in `first_contact`, which needs no store and
    no window.

    `on_ready` is handed a tuple of strings, newest first. A failure is
    swallowed by the worker, so the box simply has no list - which is what
    this should cost.
    """
    from PyQt6.QtCore import QThreadPool

    from app.ui.first_contact import FETCH_MULTIPLE, RECENT_LIMIT, rows_for

    def fetch() -> tuple:
        rows = store.recent_searches(limit=RECENT_LIMIT * FETCH_MULTIPLE)
        return tuple(rows_for(rows, settings))

    worker = CallableWorker(fetch, component="ui.search.recent")
    worker.signals.finished.connect(on_ready)
    run(QThreadPool.globalInstance(), worker)


def saved_searches_async(store: Any, on_ready: Callable) -> None:
    r"""The saved searches, off the interface thread. Adoptions §3.

    **Fetched once and kept, because it is read on the search path.** The list
    is what `saved.expand_saved` resolves `saved:invoices` against, and that
    happens between a keystroke and a search - so the caller holds the answer
    and refreshes it when it changes, rather than asking the database in the
    one place this project has a standing rule against asking it.

    `on_ready` is handed a tuple of `SavedSearch`, most-run first. A failure
    is swallowed, which costs the saved-search list and nothing else.
    """
    from PyQt6.QtCore import QThreadPool

    from app.search.saved import ordered

    def fetch() -> tuple:
        return ordered(store.saved_searches())

    worker = CallableWorker(fetch, component="ui.search.saved")
    worker.signals.finished.connect(on_ready)
    run(QThreadPool.globalInstance(), worker)


def change_saved_search_async(store: Any, action: str, args: tuple, on_done: Callable) -> None:
    """Rename or delete a saved search, then hand back the refreshed list.

    The same one-worker shape as `save_search_async`, for the same reason: the
    write and the re-read must not race, or the list somebody is looking at
    would still show the search they just deleted.
    """
    from PyQt6.QtCore import QThreadPool

    from app.search.saved import ordered

    method = {"rename": store.rename_saved_search, "delete": store.delete_saved_search}[action]

    def write() -> tuple:
        method(*args)
        return ordered(store.saved_searches())

    worker = CallableWorker(write, component="ui.search.saved")
    worker.signals.finished.connect(on_done)
    run(QThreadPool.globalInstance(), worker)


def save_search_async(store: Any, name: str, query: str, scope: str,
                      on_done: Callable) -> None:
    """Store a named search, then hand back the refreshed list.

    **One worker for the write and the re-read.** Two would race: the list
    could come back from a read that started before the write committed, and
    the search somebody just saved would be missing from the menu they saved
    it in. That is the kind of bug that gets reported as "it did not save".
    """
    from PyQt6.QtCore import QThreadPool

    from app.search.saved import ordered

    def write() -> tuple:
        store.save_search(name, query, scope)
        return ordered(store.saved_searches())

    worker = CallableWorker(write, component="ui.search.saved")
    worker.signals.finished.connect(on_done)
    run(QThreadPool.globalInstance(), worker)


def record_open_async(engine: Any, search_id: Any, chunk_id: Any) -> None:
    """Record that a result was opened, off the interface thread.

    **Extracted from `search_view` so every view can use it**, and because
    `search_view.py` was at 249 of the 250 code lines the presenter guard
    allows - a view at its limit is a view that starts pushing logic somewhere
    worse.

    `record_open` is a database *write*. It was running on the UI thread
    between the double-click and the file opening, so a busy or locked index
    made opening a result feel slow for a reason that had nothing to do with
    opening it.

    It once took four arguments where three were wanted, so every click raised
    a TypeError inside the worker; the worker caught it, as it must, and
    `record_open` swallows failures because a click is never worth blocking on.
    Two safety nets in a row turned a wrong call into silence, and Layer 10's
    table was empty by construction. That is the argument for
    `test_a_worker_is_called_with_arguments_it_accepts`, not for removing
    either net.
    """
    from PyQt6.QtCore import QThreadPool

    from app.ui.tasks import record_open

    worker = CallableWorker(record_open, engine, search_id, chunk_id,
                            component="ui.search.record")
    run(QThreadPool.globalInstance(), worker)


def filter_offers_async(store: Any, sentence: str, preferences: Any,
                        on_done: Callable, applied: Any = ()) -> None:
    """Read the filters a typed sentence contains, off the interface thread.

    Here for the reason `decorate_results_async` is: it is a store query, and
    every results handler that wants it is a view held short.
    """
    from PyQt6.QtCore import QThreadPool

    from app.ui.tasks import filter_offer_notices

    worker = CallableWorker(filter_offer_notices, store, sentence, preferences,
                            tuple(applied or ()), component="ui.search.offers")
    worker.signals.finished.connect(on_done)
    run(QThreadPool.globalInstance(), worker)


def decorate_results_async(store: Any, results: Any, on_done: Callable) -> None:
    """Fetch mail subtitles and missing-file marks off the interface thread.

    `mail_details` is a SQLite query and `missing_paths` is one filesystem stat
    per result. Both were running in the handler that paints results - the
    comment above `mail_details` said "one query, not fifty", and nobody had
    asked the prior question of whether it belonged on this thread at all.

    Here for the same reason as `record_open_async`: every view that shows
    results wants it, and the view it came from was at the 250-line limit.
    """
    from PyQt6.QtCore import QThreadPool

    from app.ui.tasks import decorate_results

    worker = CallableWorker(decorate_results, store, list(results),
                            component="ui.search.decorate")
    worker.signals.finished.connect(on_done)
    run(QThreadPool.globalInstance(), worker)
