r"""Order 1m: the rail's pages in any order - dragged, or moved from a page button's menu.

Layer: L5

2026-10-11, the owner: "is there a way they can be dragged in any order ?".
Indexing and Settings stay where they are; the order is remembered.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.unit.conftest import gui_pump                                 # noqa: E402

pytestmark = pytest.mark.gui


@pytest.fixture
def rail(gui_mainwindow):
    from app.ui.widgets import rail_order

    app, window, store, _engine = gui_mainwindow
    gui_pump(app, 4)
    window.reset_page_order()
    yield app, window, store
    window.reset_page_order()
    window._show(window.search_view)
    gui_pump(app, 2)
    assert window.rail.page_order() == list(rail_order.DEFAULT_ORDER)


def _titles(window):
    return [window.rail.tabText(i) for i in range(window.rail.count())]


def _index(window, title):
    return _titles(window).index(title)


@pytest.mark.slow
def test_the_default_order_is_the_owners(rail):
    from app.ui.widgets.rail_order import DEFAULT_ORDER

    _app, window, _store = rail
    assert window.rail.page_order() == list(DEFAULT_ORDER)
    assert _titles(window)[-2:] == ["Indexing", "Settings"]


def test_moving_a_page_keeps_every_index_true_and_the_page_in_front(rail):
    app, window, _store = rail
    window._show(window.mail_view)
    assert window.rail.moveTab(_index(window, "Mail"), _index(window, "Files"))
    gui_pump(app, 2)
    assert window.rail.page_order()[:4] == ["Search", "Photos", "Files", "Mail"]
    assert window.rail.currentIndex() == window._tab_index[window.mail_view]
    for view, at in window._tab_index.items():
        assert window.rail.widget(at) is window._tab_wrapped.get(view, view)


def test_indexing_and_settings_do_not_move_or_take_a_page(rail):
    _app, window, _store = rail
    assert not window.rail.moveTab(_index(window, "Settings"), 0)
    assert not window.rail.moveTab(_index(window, "Mail"), _index(window, "Settings"))
    assert not window.rail.moveTab(_index(window, "Indexing"), 1)


def test_the_menu_moves_up_and_down(rail):
    from app.ui.widgets.rail_order import move_down, move_up

    _app, window, _store = rail
    assert move_down(window.rail, _index(window, "Search"))
    assert window.rail.page_order()[:2] == ["Mail", "Search"]
    assert move_up(window.rail, _index(window, "Search"))
    assert not move_up(window.rail, _index(window, "Search")), "the first page moved up"
    assert not move_down(window.rail, _index(window, "Tidy")), "the last page moved down"


def test_the_order_is_remembered_and_restored_and_reset_forgets_it(rail):
    from app.ui.state_writes import settle_before_run
    from app.ui.widgets import rail_order

    app, window, store = rail
    window.rail.moveTab(_index(window, "Chat"), _index(window, "Search"))
    settle_before_run(5000)
    saved = store.get_state(rail_order.STATE_KEY, "")
    assert saved.split("|")[:2] == ["Chat", "Search"]

    window.reset_page_order()
    settle_before_run(5000)
    assert store.get_state(rail_order.STATE_KEY, "") == ""
    store.set_state(rail_order.STATE_KEY, "Reports|Mail|Nonsense")
    rail_order.restore(window)
    gui_pump(app, 2)
    assert window.rail.page_order()[:2] == ["Tidy", "Mail"]
    assert window.rail.tabText(window._tab_index[window.tidy_view]) == "Tidy"


def test_a_page_dropped_on_another_lands_there(rail):
    from PySide6.QtCore import QMimeData, QPointF, Qt
    from PySide6.QtGui import QDropEvent

    from app.ui.widgets.rail_order import MIME

    app, window, _store = rail
    window.resize(1100, 900)
    window.show()
    gui_pump(app, 6)
    source = _index(window, "Tidy")
    target_button = window.rail._buttons[_index(window, "Mail")]
    data = QMimeData()
    data.setData(MIME, str(source).encode("ascii"))
    where = QPointF(target_button.geometry().center())
    drop = QDropEvent(where, Qt.DropAction.MoveAction, data, Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier)
    assert window.rail._drop_target.eventFilter(window.rail.column, drop)
    assert window.rail.page_order()[:2] == ["Search", "Tidy"]


def test_every_page_button_has_the_move_menu(rail):
    from PySide6.QtCore import QPoint, Qt

    _app, window, _store = rail
    button = window.rail._buttons[_index(window, "Mail")]
    assert button.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    button.customContextMenuRequested.emit(QPoint(4, 4))
    menu = window.rail.last_menu
    assert [a.text() for a in menu.actions() if a.text()] == [
        "Move up", "Move down", "Reset page order"]
    menu.close()
