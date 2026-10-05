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


class MacList(QListWidget):
    """A list that treats Enter as macOS does: no `activated`, key not taken."""

    def keyPressEvent(self, event):                  # noqa: N802 - Qt's name
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.ignore()
            return
        super().keyPressEvent(event)


def _listing(cls):
    widget = cls()
    widget.addItems(["boiler-quote.txt", "boiler-notes.md"])
    opened: list = []
    widget.activated.connect(lambda index: opened.append(index.row()))
    return widget, opened


@pytest.fixture()
def listing():
    app = QApplication.instance() or QApplication([])
    widget, opened = _listing(MacList)
    yield app, widget, opened
    widget.deleteLater()
    app.processEvents()


def _press(key, modifiers=Qt.KeyboardModifier.NoModifier) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers)


def test_enter_on_the_current_line_says_activated_once(listing):
    _app, widget, opened = listing
    widget.setCurrentRow(1)
    handled = enter_key.EnterActivates().eventFilter(widget, _press(Qt.Key.Key_Return))
    assert handled is True, "delivered by the filter, so not delivered again"
    assert opened == [1]
    assert enter_key.EnterActivates().eventFilter(widget, _press(Qt.Key.Key_Enter)) is True
    assert opened == [1, 1]


def test_a_list_that_says_activated_itself_is_not_made_to_say_it_twice():
    """What Qt does on Windows, written out so it is the same test everywhere."""
    app = QApplication.instance() or QApplication([])

    class SaysItItself(QListWidget):
        def keyPressEvent(self, event):              # noqa: N802 - Qt's name
            self.activated.emit(self.currentIndex())
            event.ignore()

    widget, opened = _listing(SaysItItself)
    widget.setCurrentRow(0)
    assert enter_key.EnterActivates().eventFilter(widget, _press(Qt.Key.Key_Return)) is True
    assert opened == [0]
    widget.deleteLater()
    app.processEvents()


def test_a_page_that_hears_enter_through_its_own_filter_still_hears_it():
    """The Code page and Files: `installEventFilter` on the table. A filter on
    the application runs first, and the first version here kept the key."""
    from PyQt6.QtCore import QObject

    app = QApplication.instance() or QApplication([])
    heard: list = []

    class Page(QObject):
        def eventFilter(self, watched, event):       # noqa: N802 - Qt's name
            if event.type() == QEvent.Type.KeyPress:
                heard.append(event.key())
                return True
            return False

    widget, opened = _listing(MacList)
    page = Page()
    widget.installEventFilter(page)
    widget.setCurrentRow(0)
    watcher = enter_key.EnterActivates()
    app.installEventFilter(watcher)
    try:
        QApplication.sendEvent(widget, _press(Qt.Key.Key_Return))
    finally:
        app.removeEventFilter(watcher)
    assert heard == [Qt.Key.Key_Return], "once"
    assert opened == [], "the page opened it; `activated` would open it again"
    widget.deleteLater()
    app.processEvents()


def test_a_page_around_the_list_that_takes_enter_is_enough():
    """Mail: the page's own `keyPressEvent` opens the message."""
    from PyQt6.QtWidgets import QVBoxLayout, QWidget

    app = QApplication.instance() or QApplication([])
    heard: list = []

    class Page(QWidget):
        def keyPressEvent(self, event):              # noqa: N802 - Qt's name
            heard.append(event.key())
            event.accept()

    page = Page()
    widget, opened = _listing(MacList)
    QVBoxLayout(page).addWidget(widget)
    widget.setCurrentRow(0)
    assert enter_key.EnterActivates().eventFilter(widget, _press(Qt.Key.Key_Return)) is True
    assert heard == [Qt.Key.Key_Return] and opened == []
    page.deleteLater()
    app.processEvents()


def test_a_list_that_reads_its_own_keys_is_left_to_it():
    """The timeline gives Enter its own meaning in `keyPressEvent` and takes
    the key. Sending `activated` as well broke its tests on macOS."""
    app = QApplication.instance() or QApplication([])
    heard: list = []

    class OwnKeys(QListWidget):
        def keyPressEvent(self, event):              # noqa: N802 - Qt's name
            heard.append(event.key())

    widget, opened = _listing(OwnKeys)
    widget.setCurrentRow(0)
    enter_key.EnterActivates().eventFilter(widget, _press(Qt.Key.Key_Return))
    assert heard == [Qt.Key.Key_Return], "it heard the key, once"
    assert opened == [], "it was not told `activated` behind its back"
    widget.deleteLater()
    app.processEvents()


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
