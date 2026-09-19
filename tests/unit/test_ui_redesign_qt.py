r"""UI Redesign (work order 202626160950) - the Qt half of §9.

Needs PyQt6 and pytest-qt (offscreen): §9b the rail, §9e the chip widget,
§9f toasts, §9g the delegate, §9h keyboard-only, §9m responsiveness under
indexing. Skips itself where PyQt6 is absent - the Linux sandbox this order
was built in - and is run on the Windows venv before an item is ticked:

    venv\Scripts\python.exe -m pytest tests/unit/test_ui_redesign_qt.py -v

**One window, shared**, for the reason `test_window_opens.py` gives: building
and tearing `MainWindow` down repeatedly in one process crashes inside Qt.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.unit.test_window_opens import ENV, _Engine  # noqa: E402


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    from PyQt6.QtWidgets import QApplication

    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path_factory.mktemp("redesign")
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)
    app = QApplication.instance() or QApplication([])
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()
    built = MainWindow(settings, store, vectors, _Engine(store), debug=False)
    for _ in range(5):
        app.processEvents()
    yield app, built, store
    store.close()
    vectors.close()


def _pump(app, n: int = 5) -> None:
    for _ in range(n):
        app.processEvents()


# ---------------------------------------------------------------------------
# §9b - the rail
# ---------------------------------------------------------------------------

def test_the_central_widget_is_the_rail_and_there_is_no_status_bar(window):
    from PyQt6.QtWidgets import QStatusBar, QTabWidget

    from app.ui.widgets.rail import Rail
    from app.ui.widgets.space_table import SpaceTables
    from app.ui.widgets.spreadsheet_view import SpreadsheetView
    app, built, _ = window
    assert isinstance(built.centralWidget(), Rail)
    # SpreadsheetView (Workspace §4b) legitimately subclasses QTabWidget for
    # its own sheet tabs - only a QTabWidget acting as page navigation (what
    # the redesign replaced with Rail) should trip this check.
    # The Space Report's tables (order 0n 3a) are the same kind of exception:
    # one tab per question the report answers, inside the Reports page.
    def _inside_space_tables(widget) -> bool:
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, SpaceTables):
                return True
            parent = parent.parentWidget()
        return False

    nav_tab_widgets = [w for w in built.findChildren(QTabWidget)
                       if not isinstance(w, SpreadsheetView)
                       and not _inside_space_tables(w)]
    assert nav_tab_widgets == []
    assert built.findChild(QStatusBar) is None


def test_every_page_is_reachable_by_click_and_by_index(window):
    app, built, _ = window
    rail = built.rail
    titles = [rail.tabText(i) for i in range(rail.count())]
    assert titles == ["Search", "Files", "Mail", "Code", "Chat", "Offline",
                      "Reports", "Indexing", "Settings"]
    for index in range(rail.count()):
        rail.setCurrentIndex(index)
        _pump(app)
        assert rail.currentIndex() == index
        assert rail.stack.currentIndex() == index
    # The buttons: one per page except Indexing (the pill).
    assert sorted(rail._buttons) == [i for i in range(rail.count())
                                     if rail.tabText(i) != "Indexing"]
    for index, button in rail._buttons.items():
        button.click()
        _pump(app)
        assert rail.currentIndex() == index
        assert button.text() == rail.tabText(index)
        assert button.accessibleName() or button.text()


def test_the_pill_opens_indexing_and_ctrl_i_still_lands_there(window):
    app, built, _ = window
    rail = built.rail
    rail.setCurrentIndex(0)
    rail.pill.activated.emit()
    _pump(app)
    assert rail.tabText(rail.currentIndex()) == "Indexing"
    rail.setCurrentIndex(0)
    built._show(built.indexing_view)
    assert rail.tabText(rail.currentIndex()) == "Indexing"


def test_the_pill_paints_from_plain_data(window):
    app, built, _ = window
    built.indexing_view.progressed.emit("running", 120, 120, 1000, False, False, "")
    assert built.rail.pill.headline.text() == "Indexing"
    assert "120" in built.rail.pill.detail.text()
    built.indexing_view.totals_shown.emit(39306)
    built.indexing_view.progressed.emit("finished", 500, 1, 1, False, False, "")
    assert built.rail.pill.headline.text() == "Up to date"
    assert "39,306" in built.rail.pill.detail.text()


def test_keyboard_moves_the_rail(window):
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    app, built, _ = window
    rail = built.rail
    rail.setCurrentIndex(0)
    rail.column.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down,
                                        Qt.KeyboardModifier.NoModifier))
    assert rail.currentIndex() == 1
    rail.column.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up,
                                        Qt.KeyboardModifier.NoModifier))
    assert rail.currentIndex() == 0


def test_the_last_page_is_remembered_under_ui_page(window):
    app, built, store = window
    built.rail.setCurrentIndex(1)
    _pump(app)
    assert store.get_state("ui:page", "") == "Files"
    built.rail.setCurrentIndex(0)
    store.set_state("ui:page", "Reports")
    built._restore_last_page()
    assert built.rail.tabText(built.rail.currentIndex()) == "Reports"
    built.rail.setCurrentIndex(0)


def test_the_menu_bar_exists_with_the_shortcuts_it_shows(window):
    from PyQt6.QtWidgets import QMenuBar
    app, built, _ = window
    bar = built.findChild(QMenuBar)
    assert bar is not None
    titles = [a.text().replace("&", "") for a in bar.actions()]
    assert titles == ["File", "Edit", "View", "Go", "Help"]
    shown = {a.shortcut().toString() for a in built._menu_actions if not a.shortcut().isEmpty()}
    assert {"F5", "Ctrl+K", "Ctrl+I", "Ctrl+P", "Ctrl+M", "Ctrl+E", "Ctrl+Shift+P", "Ctrl+Q"} <= shown


# ---------------------------------------------------------------------------
# §3 / §9e - the Search page
# ---------------------------------------------------------------------------

def test_the_page_opens_on_the_home_state_and_compacts_on_typing(window):
    app, built, _ = window
    view = built.search_view
    view.input.setText("")
    _pump(app)
    assert view.home.isVisibleTo(view) and not view.chips.isVisibleTo(view)
    view.input.setText("boiler")
    _pump(app)
    assert not view.home.isVisibleTo(view)
    assert view.input.property("empty_state") is False
    view.input.setText("")
    _pump(app)
    assert view.home.isVisibleTo(view)


def test_scope_is_segmented_and_keeps_the_combo_surface(window):
    from app.ui.widgets.search_bar import SCOPES, scope_value, select_scope
    from app.ui.widgets.segmented import SegmentedControl
    app, built, _ = window
    scope = built.search_view.scope
    assert isinstance(scope, SegmentedControl)
    assert [scope.button(i).text() for i in range(scope.count())] == [s[0] for s in SCOPES]
    select_scope(scope, "mail")
    assert scope_value(scope) == "mail"
    select_scope(scope, "all")


def test_typing_a_filter_shows_a_chip_and_removing_it_edits_the_box(window):
    app, built, _ = window
    view = built.search_view
    view.input.setText("boiler /type pdf")
    _pump(app)
    assert view.chips.labels() == ["type: pdf"]
    from PyQt6.QtWidgets import QToolButton
    chip = view.chips.findChild(QToolButton, "chip")
    dispatched = []
    original = view._dispatch
    view._dispatch = lambda tier: dispatched.append(tier)
    try:
        chip.click()
        _pump(app)
        assert view.input.text() == "boiler"
        assert view.chips.labels() == []
    finally:
        view._dispatch = original
    view.input.setText("")


def test_interpret_and_rerank_live_in_the_more_menu_with_their_labels(window):
    from PyQt6.QtGui import QAction
    app, built, _ = window
    view = built.search_view
    texts = [a.text() for a in view.more_menu.actions() if isinstance(a, QAction) and a.text()]
    assert "Interpret" in texts and "Rerank" in texts and "Drag results out" in texts
    assert view.interpret_button.toolTip().startswith("Turn a sentence into a search query")
    assert view.rerank_toggle.isCheckable()


def test_icon_toggles_mirror_the_hidden_checkboxes(window):
    app, built, _ = window
    view = built.search_view
    boxes = view.split.switches.boxes
    for key in ("pinned", "timeline", "grid"):
        toggle = view.toggles[key]
        assert toggle.toolTip().startswith(boxes[key].text())
        assert toggle.accessibleName() == boxes[key].text()
        before = boxes[key].isChecked()
        toggle.setChecked(not before)
        assert boxes[key].isChecked() == (not before)
        toggle.setChecked(before)
    assert not view.split.switches.isVisibleTo(view)


# ---------------------------------------------------------------------------
# §9f - toasts
# ---------------------------------------------------------------------------

def test_a_notify_shows_a_toast_queues_a_second_and_announces(window):
    app, built, _ = window
    built.toast.clear()
    built.notify("First thing", 1000)
    assert built.toast.current_text() == "First thing"
    built.notify("Second thing", 1000, level="warning")
    assert built.toast.current_text() == "First thing"
    assert built.toast.pending() == 1
    assert built.toast.announcer.text() == "First thing"
    built.toast.clear()
    assert built.toast.current_text() == ""


# ---------------------------------------------------------------------------
# §9g - the delegate
# ---------------------------------------------------------------------------

def _option(width: int):
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QStyleOptionViewItem
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, width, 200)
    option.font = QFont()
    return option


def test_skeleton_rows_are_as_tall_as_a_real_group_row(window):
    from PyQt6.QtGui import QStandardItem, QStandardItemModel
    from app.ui.result_delegate import ROLE_PAYLOAD, ResultDelegate, Skeleton
    app, built, _ = window
    delegate = ResultDelegate()
    model = QStandardItemModel()
    item = QStandardItem()
    item.setData(Skeleton(0), ROLE_PAYLOAD)
    model.appendRow(item)
    hint = delegate.sizeHint(_option(600), model.index(0, 0))
    assert hint.height() > 30
    # Painting a skeleton must not raise.
    from PyQt6.QtGui import QPainter, QPixmap
    pixmap = QPixmap(600, hint.height())
    painter = QPainter(pixmap)
    delegate.paint(painter, _option(600), model.index(0, 0))
    painter.end()


def test_badge_colours_come_from_the_palette(window):
    from app.ui.kind_badge import badge_token
    from app.ui.theme import theme_colours
    colours = theme_colours()
    for kind in ("pdf", "email", "py", "jpg"):
        assert badge_token(kind) in colours


# ---------------------------------------------------------------------------
# §9m - responsiveness under indexing (owner, 2026-09-16)
# ---------------------------------------------------------------------------

def test_the_window_stays_instant_while_progress_floods_in(window):
    """A stand-in for a busy indexer: 200 progress ticks in a burst. A rail
    switch, a keystroke and a toast must each land within one loop turn."""
    from PyQt6.QtCore import QElapsedTimer
    app, built, _ = window
    clock = QElapsedTimer()
    clock.start()
    for n in range(200):
        built.indexing_view.progressed.emit("running", n, n, 200, False, False, "")
    built.rail.setCurrentIndex(1)
    built.search_view.input.setText("x")
    built.notify("still here", 500)
    _pump(app, 2)
    assert built.rail.currentIndex() == 1
    assert built.toast.current_text() == "still here"
    assert clock.elapsed() < 1500, f"{clock.elapsed()}ms for 200 ticks and three actions"
    built.search_view.input.setText("")
    built.rail.setCurrentIndex(0)
    built.toast.clear()
