r"""The window's error box: shown, never waited for, and one at a time.

Layer: L5

**Why it does not block.** `QMessageBox.exec()` runs an event loop of its own until
somebody clicks. Most errors reach the window from somewhere nobody pressed - a worker's
`failed` signal, a page that re-reads the index on a timer, the poll that watches for
another process's run - and an `exec()` there freezes the caller inside a nested loop
while every other timer keeps firing into it. A second failure then opens a second box on
top of the first, and so on (2026-10-13: a test window's background read failed with
ERR_DB_BUSY, its box waited for a click that never came, and the run lock's poll kept
running inside it until the test timed out and the process died).

So the box is opened with `open()`: still modal to the window, still in front, still the
same words, but the call returns at once and the event loop that is already running
carries on. **One box at a time**: an error that arrives while one is up waits behind it,
and an error already showing or already waiting is not queued again - a read that fails
every 45 seconds says so once, not once a minute until somebody comes back.

`confirm.confirm` is the other kind of box and keeps `exec()`: a question has to be
answered before the action it guards goes on, and it is only ever asked from a click.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget

__all__ = ["TITLE", "show_error"]

TITLE = "Something needs attention"


def _words(error: Any) -> tuple[str, str, str]:
    return (str(getattr(error, "message", str(error))),
            str(getattr(error, "suggestion", "") or ""),
            str(getattr(error, "details", None) or ""))


def _showing(parent: QWidget) -> QMessageBox | None:
    """The box `parent` has up now, or None - a closed or deleted one counts as none."""
    box = getattr(parent, "_error_box", None)
    if box is None:
        return None
    try:
        return box if box.isVisible() else None
    except RuntimeError:                 # its C++ half was deleted on close
        return None


def show_error(parent: QWidget, error: Any) -> QMessageBox | None:
    """Put `error` in front of the person without waiting for them to read it.

    Every error shows what happened, the fix, and a working button. Returns the box
    opened, or None when the error waits behind (or repeats) the one already up.
    """
    words = _words(error)
    waiting = getattr(parent, "_errors_waiting", None)
    if waiting is None:
        waiting = parent._errors_waiting = []
    current = _showing(parent)
    if current is not None:
        if words != getattr(current, "_words", None) and all(
                words != _words(queued) for queued in waiting):
            waiting.append(error)
        return None

    text, fix, details = words
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(TITLE)
    box.setText(text)
    box.setInformativeText(fix)
    if details:
        box.setDetailedText(details)
    box._words = words                   # what is up, so a repeat is recognised
    box.setWindowModality(Qt.WindowModality.WindowModal)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

    def closed(_result: int) -> None:
        if getattr(parent, "_error_box", None) is box:
            parent._error_box = None
        if waiting:
            show_error(parent, waiting.pop(0))

    box.finished.connect(closed)
    parent._error_box = box
    box.open()
    return box
