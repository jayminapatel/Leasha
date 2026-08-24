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

__all__ = ["WorkerSignals", "CallableWorker", "SearchWorker", "IndexWorker"]

_log = logger.bind(component="ui.workers")


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
            self.signals.finished.emit(self._work(*self._args, **self._kwargs))
        except Exception as exc:                 # noqa: BLE001 - the boundary; see module docstring
            error = to_app_error(exc, self._component)
            _log.bind(error_code=error.code).error("{}", error.render())
            self.signals.failed.emit(error)
        finally:
            self.signals.done.emit()


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
                response = self._engine.interim(self._query)
            else:
                response = self._engine.search(self._query, **self._options)
            self.signals.finished.emit((self.generation, response))
        except Exception as exc:                 # noqa: BLE001
            error = to_app_error(exc, "ui.search")
            _log.bind(error_code=error.code).error("{}", error.render())
            self.signals.failed.emit(error)
        finally:
            self.signals.done.emit()


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
            stats = self.pipeline.run(on_progress=self.signals.progress.emit)
            self.signals.finished.emit(stats)
        except Exception as exc:                 # noqa: BLE001
            error = to_app_error(exc, "ui.index")
            _log.bind(error_code=error.code).error("{}", error.render())
            self.signals.failed.emit(error)
        finally:
            self.signals.done.emit()


def open_in_explorer(path: str, *, select: bool = True) -> Optional[AppError]:
    """Open a file, or its folder with the file selected. Returns an error or None.

    Windows only in the useful sense; falls back to a plain open elsewhere so
    development on another platform is not blocked.
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
