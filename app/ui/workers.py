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

from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from app.core.errors import AppError, to_app_error
from app.core.logging import logger

__all__ = [
    "WorkerSignals", "CallableWorker", "SearchWorker", "IndexWorker", "run",
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
    "used before connect(), or after close()",
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
    for name in names or ("_timer",):
        timer = getattr(view, name, None)
        if timer is not None:
            timer.stop()


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
    except Exception as exc:                     # noqa: BLE001
        # A slot that raises must not take the worker down with it, and must not
        # skip the `done` signal that releases the worker from `_IN_FLIGHT`.
        _log.debug("emitting {} failed: {}", name, exc)


class WorkerSignals(QObject):
    """Signals are on a QObject because QRunnable is not one."""

    finished = pyqtSignal(object)      # the result, whatever it is
    failed = pyqtSignal(object)        # an AppError, never a bare exception
    progress = pyqtSignal(object)      # partial state, for long runs
    done = pyqtSignal()                # always, success or failure


class CallableWorker(QRunnable):
    """Run any callable off the UI thread and emit the result."""

    def __init__(self, work: Callable[..., Any], *args: Any, component: str = "ui", **kwargs: Any):
        super().__init__()
        self._work = work
        self._args = args
        self._kwargs = kwargs
        self._component = component
        self.signals = WorkerSignals()

    def run(self) -> None:                       # noqa: D102 - Qt's entry point
        try:
            _emit(self.signals, "finished", self._work(*self._args, **self._kwargs))
        except Exception as exc:                 # noqa: BLE001 - the boundary; see module docstring
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

    def run(self) -> None:                       # noqa: D102
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
        except Exception as exc:                 # noqa: BLE001
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

    def run(self) -> None:                       # noqa: D102
        try:
            stats = self.pipeline.run(
                on_progress=lambda payload: _emit(self.signals, "progress", payload)
            )
            _emit(self.signals, "finished", stats)
        except Exception as exc:                 # noqa: BLE001
            error = to_app_error(exc, "ui.index")
            _log.bind(error_code=error.code).error("{}", error.render())
            _emit(self.signals, "failed", error)
        finally:
            _emit(self.signals, "done")


def open_in_explorer(path: str, *, select: bool = True) -> Optional[AppError]:
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
    except Exception as exc:                     # noqa: BLE001
        return to_app_error(exc, "ui.open", path=str(target))
