"""Closing the main window closes the application's other windows too.

Layer: L5

Work order `202626191300` §6d. **Why the event loop can outlive `closeEvent`.**
Qt quits when the *last visible top-level window* closes, and it counts every
window, not only the main one. `PreviewWindow` (the pop-out), `LogWindow` and the
mini-search palette are all top-level widgets with no parent, so any one of them
still visible keeps the loop running after the main window has gone - with the
main window hidden and its stores about to close.

**Measured 2026-09-20, plain PySide6 6.x offscreen, no application code:** closing
a main window with a second visible top-level widget left `exec()` running until
something else called `quit()` (3.0 s, the test's own timer); without one it
returned in 0.2 s. So this is one reproducible mechanism, and closing every other
window first removes it.

**What it does not explain.** The live incident (9 hours between `closing` and the
event loop returning, 2026-09-17) had no visible pop-out. A second mechanism is
also real and reproduced: deleting an object that *owns* a `QThreadPool` blocks
the main thread in `~QThreadPool` until its run finishes (6.0 s for a 6 s job).
`MainWindow` is not `WA_DeleteOnClose` and the view's pool is never deleted inside
`exec()`, so that is not shown to be the live cause either. What is left is
guarded, not cured: the exit watchdog (`exit_watchdog.py`) still logs every
thread's stack at 30 s and ends the process at 300 s, and the next occurrence
names itself.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from app.core.logging import logger

__all__ = ["close_other_windows", "wait_off_window"]

_log = logger.bind(component="ui.close_windows")


def close_other_windows(main: Any) -> int:
    """Close every visible top-level window except `main` (and other main windows). Returns how many.

    Each is asked with `close()`, not hidden or deleted, so its own `closeEvent`
    runs - a pop-out remembers its geometry there. Never raises: a window that
    will not close is logged and left, because closing the main window must go on.
    """
    from PySide6.QtWidgets import QApplication, QMainWindow

    closed = 0
    for widget in list(QApplication.topLevelWidgets()):
        # Another `QMainWindow` is a second application window, not a pop-out: the
        # app builds exactly one, so it is only ever a test's (whose earlier
        # window is still shown, with its store already closed).
        if widget is main or isinstance(widget, QMainWindow) or not widget.isVisible():
            continue
        try:
            widget.close()
            closed += 1
        except Exception as exc:                          # noqa: BLE001
            _log.warning("could not close {}: {}", type(widget).__name__, exc)
    if closed:
        _log.info("closed {} other window(s) so the application can quit", closed)
    return closed


def wait_off_window(action: Callable[[], Any], *, name: str = "closing-wait",
                    slice_ms: int = 50) -> Any:
    r"""Run `action` on a thread of its own and wait for it with the window's events still served.

    2026-10-15, the owner's rule that the window never freezes or goes "Not responding". The lag
    monitor logged the window's thread 5 s inside `subprocess.wait` at close on 2026-10-14
    (`closeEvent` -> `end_all_children` -> `ChildIndexRun.shutdown` -> `_force_end`): an indexing
    child given its grace, then terminated, with the window's thread blocked in
    `WaitForSingleObject` for all of it. **The wait is kept, only moved.** `closeEvent` must not
    close the stores while a child process can still write to them (work order 0x section 2c),
    so this returns only once `action` has - however long that takes - and anything it raised
    is raised here. What changes is whose thread sits in the wait: the action's own, while this
    one serves paint and timer events in `slice_ms` slices, the same trade `MainWindow.
    _drain_workers` makes for the thread pools. User input is excluded for the reason given
    there: a click into a closing window must not start new work against stores about to shut.
    Without a `QApplication` (a script, a test) it simply waits.
    """
    from PySide6.QtCore import QEventLoop
    from PySide6.QtWidgets import QApplication

    done = threading.Event()
    box: dict[str, Any] = {}

    def body() -> None:
        try:
            box["value"] = action()
        except BaseException as exc:                     # noqa: BLE001 - handed back below
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=body, name=name, daemon=True).start()
    application = QApplication.instance()
    while not done.wait(max(1, int(slice_ms)) / 1000.0):
        if application is not None:
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 10)
    if "error" in box:
        raise box["error"]
    return box.get("value")
