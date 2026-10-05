"""Enter opens a list's current line on every system.

2026-10-05. On macOS Qt does not send `activated` for Enter (it starts editing;
"open" is Cmd+O), so Enter on a search result did nothing on a Mac. The filter
is forced on here, since this suite mostly runs on Windows, where Qt sends the
signal itself - which is also why it is installed only on macOS.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QEvent, Qt  # noqa: E402
from PyQt6.QtGui import QKeyEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QListWidget  # noqa: E402

from app.ui import enter_key  # noqa: E402


@pytest.fixture()
def listing():
    app = QApplication.instance() or QApplication([])
    widget = QListWidget()
    widget.addItems(["boiler-quote.txt", "boiler-notes.md"])
    opened: list = []
    widget.activated.connect(lambda index: opened.append(index.row()))
    yield app, widget, opened
    widget.deleteLater()
    app.processEvents()


def _press(key, modifiers=Qt.KeyboardModifier.NoModifier) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers)


def test_enter_on_the_current_line_says_activated_once(listing):
    _app, widget, opened = listing
    widget.setCurrentRow(1)
    handled = enter_key.EnterActivates().eventFilter(widget, _press(Qt.Key.Key_Return))
    assert handled is True, "taken, so the list does not also start editing the line"
    assert opened == [1]
    assert enter_key.EnterActivates().eventFilter(widget, _press(Qt.Key.Key_Enter)) is True
    assert opened == [1, 1]


def test_other_keys_other_widgets_and_no_line_are_left_alone(listing):
    app, widget, opened = listing
    watcher = enter_key.EnterActivates()
    assert watcher.eventFilter(widget, _press(Qt.Key.Key_Return)) is False, "no current line"
    widget.setCurrentRow(0)
    assert watcher.eventFilter(widget, _press(Qt.Key.Key_Down)) is False
    assert watcher.eventFilter(app, _press(Qt.Key.Key_Return)) is False, "not a list"
    # Ctrl+Enter is "show in folder" on the Search page: its own meaning stays.
    assert watcher.eventFilter(widget, _press(
        Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)) is False
    assert opened == []


def test_it_is_installed_only_on_a_mac(monkeypatch):
    monkeypatch.setattr(enter_key, "_installed", None)
    monkeypatch.setattr(enter_key, "is_macos", lambda: False)
    assert enter_key.install() is None
    monkeypatch.setattr(enter_key, "is_macos", lambda: True)
    app = QApplication.instance() or QApplication([])
    watcher = enter_key.install(app)
    try:
        assert isinstance(watcher, enter_key.EnterActivates)
        assert enter_key.install(app) is watcher, "once, however often it is asked for"
    finally:
        app.removeEventFilter(watcher)


def test_the_window_asks_for_it():
    from pathlib import Path

    assert "enter_key.install()" in (Path(__file__).resolve().parents[2]
                                     / "app" / "ui" / "shell.py").read_text(encoding="utf-8")
