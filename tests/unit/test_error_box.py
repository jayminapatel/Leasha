"""The window's error box is shown, never waited for, and one at a time.

Layer: L5 (pytest-qt, no `MainWindow` - `_show_error` is called on a plain widget)

2026-10-13: a background read failed (ERR_DB_BUSY) and `MainWindow._show_error`
opened its box with `exec()`. That ran a nested event loop until a click that never
came; the run lock's poll kept firing inside it, and the suite's process died on the
timeout. `widgets.error_box.show_error` opens the box with `open()` instead - same
words, still modal to the window - so the caller returns and the event loop it is
already in carries on.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget

from app.ui.later import later
from app.ui.shell import MainWindow
from app.ui.widgets.error_box import TITLE, show_error

pytestmark = pytest.mark.gui


def _error(message="The index did not answer.", suggestion="Wait a minute and try again.",
           details=None):
    return SimpleNamespace(message=message, suggestion=suggestion, details=details)


@pytest.fixture
def parent(qtbot, monkeypatch):
    """A plain window to own the box, and an `exec` that fails the test if reached."""
    def never(_box):
        raise AssertionError("the error box waited for a click (exec)")

    monkeypatch.setattr(QMessageBox, "exec", never)
    widget = QWidget()
    qtbot.addWidget(widget)
    yield widget
    box = getattr(widget, "_error_box", None)
    if box is not None:
        try:
            box.close()
        except RuntimeError:
            pass


def _boxes(widget):
    return [b for b in widget.findChildren(QMessageBox) if b.isVisible()]


def test_the_window_shows_its_error_without_waiting_and_with_the_same_words(parent, qtbot):
    MainWindow._show_error(parent, _error(details="Waited 30 s for the lock."))

    (box,) = _boxes(parent)
    assert box.windowTitle() == TITLE == "Something needs attention"
    assert box.text() == "The index did not answer."
    assert box.informativeText() == "Wait a minute and try again."
    assert box.detailedText() == "Waited 30 s for the lock."
    assert box.icon() == QMessageBox.Icon.Warning
    assert box.windowModality() == Qt.WindowModality.WindowModal


def test_an_error_from_a_timer_lets_the_timer_return(parent, qtbot):
    """The fault itself: a callback on the event loop that shows an error comes back."""
    returned: list = []

    def tick():
        MainWindow._show_error(parent, _error())
        returned.append(True)

    later(parent, 0, tick)
    qtbot.waitUntil(lambda: bool(returned), timeout=5_000)
    assert len(_boxes(parent)) == 1


def test_a_repeat_is_not_stacked_and_a_different_error_waits_its_turn(parent, qtbot):
    first = show_error(parent, _error())
    assert first is not None
    assert show_error(parent, _error()) is None             # the same words again
    assert show_error(parent, _error("Another thing.")) is None
    assert show_error(parent, _error("Another thing.")) is None
    assert len(_boxes(parent)) == 1, "a box stacked on a box"

    first.done(0)                                            # the person clicks OK
    qtbot.waitUntil(lambda: [b.text() for b in _boxes(parent)] == ["Another thing."],
                    timeout=5_000)
    parent._error_box.done(0)
    qtbot.waitUntil(lambda: not _boxes(parent), timeout=5_000)
    assert parent._errors_waiting == []


def test_a_closed_box_lets_the_next_error_open_one(parent, qtbot):
    first = show_error(parent, _error())
    first.done(0)
    qtbot.waitUntil(lambda: not _boxes(parent), timeout=5_000)
    assert show_error(parent, _error()) is not None
