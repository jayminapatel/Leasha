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

from typing import Any

from app.core.logging import logger

__all__ = ["close_other_windows"]

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
