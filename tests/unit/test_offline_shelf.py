r"""Order 1i (2026-10-11): Offline is the Indexing page's last shelf, not a rail entry.

Layer: L5

The owner: Offline is upkeep, not finding, so it joins the page that keeps the
index current, and the timeline ("Browse") takes its rail slot. It stays fully
manual, and its list is read when its **shelf** is shown - not when the
Indexing page is, and never on a timer.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.unit.conftest import gui_pump                                 # noqa: E402

pytestmark = pytest.mark.gui


@pytest.fixture
def reads(gui_mainwindow, monkeypatch):
    app, window, *_ = gui_mainwindow
    gui_pump(app, 4)
    counted: list[int] = []
    monkeypatch.setattr(window.offline_media_view, "refresh", lambda: counted.append(1))
    yield app, window, counted
    window.indexing_view._nav.show_category(window.indexing_view._nav.category_names()[0])
    window._show(window.search_view)
    gui_pump(app, 2)


def test_offline_is_the_indexing_pages_last_shelf_and_not_on_the_rail(gui_mainwindow):
    from app.ui.widgets.indexing_layout import CATEGORY_OFFLINE

    app, window, *_ = gui_mainwindow
    gui_pump(app, 4)
    nav = window.indexing_view._nav
    assert nav.category_names()[-1] == CATEGORY_OFFLINE == "Offline"
    assert nav.page(CATEGORY_OFFLINE) is window.offline_media_view
    titles = [window.rail.tabText(i) for i in range(window.rail.count())]
    assert "Offline" not in titles
    assert window.offline_media_view not in window._tab_index


def test_its_list_is_read_when_the_shelf_is_shown_and_not_before(reads):
    app, window, counted = reads
    nav = window.indexing_view._nav
    nav.show_category(nav.category_names()[0])
    window._show(window.search_view)
    gui_pump(app, 2)
    counted.clear()

    window._show(window.indexing_view)                  # the page, on another shelf
    gui_pump(app, 2)
    assert counted == [], "the Offline list was read without its shelf being shown"

    nav.show_category("Offline")                        # the shelf chosen
    gui_pump(app, 2)
    assert counted == [1]

    window._show(window.search_view)                    # away and back, shelf still chosen
    window._show(window.indexing_view)
    gui_pump(app, 2)
    assert counted == [1, 1]


def test_go_offline_opens_the_indexing_page_on_its_offline_shelf(reads):
    app, window, counted = reads
    window._show(window.search_view)
    gui_pump(app, 2)
    counted.clear()
    window._show_offline()
    gui_pump(app, 2)
    assert window.rail.currentIndex() == window._tab_index[window.indexing_view]
    assert window.indexing_view._nav.current_category() == "Offline"
    assert counted, "the shelf came forward without its list being read"

