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
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from app.core.errors import AppError, to_app_error
from app.core.logging import logger
from app.core.osbridge import hidden_console_flags, new_console_flags

__all__ = [
    "CallableWorker",
    "OpenContext",
    "copy_path_async",
    "open_async",
    "open_at_line",
    "open_row_async",
    "set_open_context",
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


@dataclass
class OpenContext:
    """What the window lends every Open and Show in folder. 2026-10-04.

    Set once by `MainWindow` (`set_open_context`); every field optional, so a
    page tested on its own still opens, with what it passes itself. `editor`
    and `search_id` are read at the click, so a choice just made in Settings
    and the search on screen now are the ones used.
    """

    store: Any = None
    cache_path: Any = None
    engine: Any = None
    editor: Callable[[], tuple] | None = None
    search_id: Callable[[], Any] | None = None
    on_error: Callable[[Any], Any] | None = None
    on_note: Callable[[str], Any] | None = None
    search_inside: Callable[[str], Any] | None = None


#: The window's, or an empty one without a window - as in a test of one view.
_CONTEXT = OpenContext()


def set_open_context(context: OpenContext | None) -> None:
    """Lend every open the window's store, settings and toasts; None on close.

    Replaces `route_through_window` (2026-10-04, the same day): that sent an
    attachment from another page *back* to the window's route. There is one
    route now, `open_row_async`, and the window only lends it what it knows.
    """
    global _CONTEXT
    _CONTEXT = context if context is not None else OpenContext()


def open_async(path: str, *, reveal: bool = False, on_error: Any = None,
               component: str = "ui.open") -> None:
    """`open_row_async` for a caller with a path and no row - the log, a chip."""
    if path:
        from app.ui.presenter.opening import Place

        open_row_async(None, Place(str(path)), reveal=reveal, on_error=on_error,
                       component=component)


def open_row_async(store: Any, row: Any, *, reveal: bool = False, on_error: Any = None,
                   on_note: Any = None, search_inside: Any = None,
                   component: str = "ui.open") -> None:
    r"""Open, or show in its folder, one row - **the** route. Never blocks the UI.

    2026-10-04, the owner: "where ever possible the same code should run for
    functions so they are all consistent and standard". Every page's Open and
    Show in folder - Search, Files, Mail, Code, Chat, the timeline, the mini
    search, a pinned window, the lightbox - comes here with its ROW, so a
    catalogued drive is resolved, a recording opens at its moment, code at its
    line, an attachment or zip member from a read-only copy, a message in
    Outlook, and the open is recorded for ranking, the same way from all of
    them. The decision is `presenter.opening.plan_for`; the work, on a worker,
    is `tasks.open_target`. A path alone is wrapped (`open_async`).

    `on_error`, `on_note` and `search_inside` are the caller's when given,
    else the window's (`OpenContext`) - a caller's own error route is kept.
    A web address goes to the browser here: it is not a file.
    """
    from PyQt6.QtCore import QThreadPool

    from app.ui.presenter.opening import Place, SearchInside, is_web, key_of
    from app.ui.tasks import open_target

    if row is None:
        return
    row = Place(row) if isinstance(row, str) else row
    key = key_of(row)
    if not key:
        return
    context = _CONTEXT
    if is_web(key):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(key))
        return
    errors = on_error or context.on_error
    notes = on_note or context.on_note
    inside = search_inside or context.search_inside
    worker = CallableWorker(
        open_target, store if store is not None else context.store, row, reveal=reveal,
        cache_path=context.cache_path, engine=context.engine,
        editor=context.editor() if context.editor else ("auto", ""),
        search_id=context.search_id() if context.search_id else None,
        component=component)

    def landed(result: Any) -> None:
        if isinstance(result, AppError):
            if errors is not None:
                errors(result)
        elif isinstance(result, SearchInside):
            if inside is not None:
                inside(result.path)
        elif result and notes is not None:
            notes(str(result))

    worker.signals.finished.connect(landed)
    if errors is not None:
        worker.signals.failed.connect(errors)
    run(QThreadPool.globalInstance(), worker)


def copy_path_async(row: Any, *, store: Any = None) -> None:
    """Put a row's real path on the clipboard. 2026-10-04.

    A file on a catalogued drive is copied as its path on the drive's current
    letter, resolved on a worker - Search and Files used to copy the internal
    `leasha-volume://` key while the timeline copied the real one. A drive that
    is not plugged in copies the key, without an error. Anything else is
    copied as it is, at once.
    """
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtGui import QGuiApplication

    from app.ui.presenter.opening import key_of
    from app.ui.tasks import resolve_open_path

    key = key_of(row)

    def put(text: Any) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(str(text or key))

    if getattr(row, "volume_id", None) is None:
        put(key)
        return
    reader = store if store is not None else _CONTEXT.store

    def resolved() -> str:
        try:
            return resolve_open_path(reader, row)
        except Exception:                        # noqa: BLE001 - not plugged in: the key
            return key

    worker = CallableWorker(resolved, component="ui.copy_path")
    worker.signals.finished.connect(put)
    run(QThreadPool.globalInstance(), worker)


def open_media_at(path: str, seconds: Any) -> Any:
    """A recording at its moment (`tasks.open_target`): an `AppError`, a sentence or None."""
    from app.core import media_open

    outcome = media_open.open_at(path, seconds)
    return outcome.error if outcome.error is not None else (outcome.note or None)


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

    Worker body, reached from `tasks.open_target`. Returns an `AppError`, or a sentence
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

    Here because every view that shows
    results wants it, and the view it came from was at the 250-line limit.
    """
    from PyQt6.QtCore import QThreadPool

    from app.ui.tasks import decorate_results

    worker = CallableWorker(decorate_results, store, list(results),
                            component="ui.search.decorate")
    worker.signals.finished.connect(on_done)
    run(QThreadPool.globalInstance(), worker)
