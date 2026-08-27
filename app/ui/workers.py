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

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from app.core.errors import AppError, to_app_error
from app.core.logging import logger

__all__ = [
    "CallableWorker",
    "open_async",
    "IndexWorker",
    "SearchWorker",
    "WorkerSignals",
    "run",
    "stop_timers",
]

_log = logger.bind(component="ui.workers")


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


class CallableWorker(QRunnable):
    """Run any callable off the UI thread and emit the result."""

    def __init__(self, work: Callable[..., Any], *args: Any, component: str = "ui", **kwargs: Any):
        super().__init__()
        self._work = work
        self._args = args
        self._kwargs = kwargs
        self._component = component
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
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
            if self._tier == "interim":
                # The interim tier takes a scope too but not a rerank flag, so
                # the options cannot simply be forwarded whole.
                response = self._engine.interim(
                    self._query, scope=self._options.get("scope", "all")
                )
            else:
                response = self._engine.search(self._query, **self._options)
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
        from app.core.run_lock import GUI, IndexRunLock

        try:
            with IndexRunLock(getattr(self.pipeline, "store", None), owner=GUI):
                stats = self.pipeline.run(
                    on_progress=lambda payload: _emit(self.signals, "progress", payload)
                )
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
    import subprocess
    import sys
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
        if sys.platform == "win32":
            if select:
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                import os

                os.startfile(str(target))        # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(target if not select else target.parent)])
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

    from app.ui.presenter import record_open

    worker = CallableWorker(record_open, engine, search_id, chunk_id,
                            component="ui.search.record")
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

    from app.ui.presenter import decorate_results

    worker = CallableWorker(decorate_results, store, list(results),
                            component="ui.search.decorate")
    worker.signals.finished.connect(on_done)
    run(QThreadPool.globalInstance(), worker)
