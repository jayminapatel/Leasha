r"""IA-11 (UX review 2026-10-14): the owner can take a page off the rail, and put it back.

Layer: L5

Right-click a page button: "Hide from the rail" (not on Search; Indexing and Settings have
no menu). "Show hidden pages" - on the same menu and in the View menu - brings them all
back. Remembered beside the rail order (`ui:rail_hidden`). A hidden page still opens from
the Go menu, and the keys that number the pages (`Rail.page_order`) skip it.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.ui.state_writes import settle_before_run                        # noqa: E402
from tests.unit.conftest import gui_pump                                 # noqa: E402

pytestmark = pytest.mark.gui


@pytest.fixture
def rail(gui_mainwindow):
    app, window, store, _engine = gui_mainwindow
    gui_pump(app, 4)
    window.reset_page_order()
    window.show_hidden_pages()
    yield app, window, store
    window.show_hidden_pages()
    window.reset_page_order()
    window._show(window.search_view)
    gui_pump(app, 2)


def _index(window, title):
    return [window.rail.tabText(i) for i in range(window.rail.count())].index(title)


@pytest.mark.slow
def test_a_hidden_page_leaves_the_rail_and_the_numbered_order(rail):
    _app, window, store = rail
    from app.ui.widgets import rail_order

    assert window.rail.hide_page(_index(window, "Tidy"))
    assert "Tidy" not in window.rail.page_order()
    assert "Tidy" in window.rail.all_page_order()
    assert window.rail._buttons[_index(window, "Tidy")].isHidden()
    assert window.rail.hidden_pages() == ["Tidy"]
    settle_before_run(5000)
    assert store.get_state(rail_order.HIDDEN_KEY, "") == "Tidy"


def test_search_cannot_be_hidden_and_settings_has_no_menu(rail):
    _app, window, _store = rail
    assert not window.rail.hideable(_index(window, "Search"))
    assert not window.rail.hide_page(_index(window, "Search"))
    assert not window.rail.hideable(_index(window, "Settings"))
    assert "Search" in window.rail.page_order()


def test_a_hidden_page_still_opens_from_the_go_menu(rail):
    app, window, _store = rail
    window.rail.hide_page(_index(window, "Tidy"))
    go = next(a for a in window._menu_actions if a.text() == "Tidy")
    go.trigger()
    gui_pump(app, 2)
    assert window.rail.currentIndex() == window._tab_index[window.tidy_view]


def test_show_hidden_pages_puts_every_one_back_and_forgets(rail):
    _app, window, store = rail
    from app.ui.widgets import rail_order

    window.rail.hide_page(_index(window, "Tidy"))
    window.rail.hide_page(_index(window, "Browse"))
    item = next(a for a in window._menu_actions if a.text() == "Show hidden pages")
    item.trigger()
    assert window.rail.hidden_pages() == []
    assert not window.rail._buttons[_index(window, "Tidy")].isHidden()
    settle_before_run(5000)
    assert store.get_state(rail_order.HIDDEN_KEY, "") == ""


def test_the_hidden_pages_come_back_hidden_after_a_restart(rail):
    _app, window, store = rail
    from app.ui.widgets import rail_order

    store.set_state(rail_order.HIDDEN_KEY, "Chat|Search|Nonsense")
    rail_order.restore(window)
    # Search can never be hidden; a title with no page is ignored.
    assert window.rail.hidden_pages() == ["Chat"]


def test_moving_steps_over_a_hidden_page(rail):
    _app, window, _store = rail
    from app.ui.widgets.rail_order import move_down

    # The owner's order: Search, Mail, Photos ... - hide Photos, then Mail moves past it.
    window.rail.hide_page(_index(window, "Photos"))
    assert move_down(window.rail, _index(window, "Mail"))
    order = window.rail.all_page_order()
    assert order.index("Mail") > order.index("Files")
    assert window.rail.page_order()[:3] == ["Search", "Files", "Mail"]


def test_up_and_down_on_the_rail_step_over_a_hidden_page(rail):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    app, window, _store = rail
    window.rail.hide_page(_index(window, "Mail"))
    window._show(window.search_view)
    window.rail._column_key(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down,
                                      Qt.KeyboardModifier.NoModifier))
    gui_pump(app, 1)
    assert window.rail.tabText(window.rail.currentIndex()) == "Photos"


def test_the_rail_menu_offers_hide_and_show_hidden(rail, monkeypatch):
    _app, window, _store = rail
    from app.ui.widgets import menus, rail_order

    built = []
    monkeypatch.setattr(menus, "show", lambda menu, *a, **k: built.append(menu))
    button = window.rail._buttons[_index(window, "Tidy")]
    rail_order._menu(window.rail, button, button.rect().center())
    words = {a.text(): a for a in built[-1].actions() if a.text()}
    assert words["Hide from the rail"].isEnabled()
    assert not words["Show hidden pages"].isEnabled()
    words["Hide from the rail"].trigger()
    assert window.rail.hidden_pages() == ["Tidy"]
    search = window.rail._buttons[_index(window, "Search")]
    rail_order._menu(window.rail, search, search.rect().center())
    words = {a.text(): a for a in built[-1].actions() if a.text()}
    assert not words["Hide from the rail"].isEnabled()
    assert words["Show hidden pages"].isEnabled()
