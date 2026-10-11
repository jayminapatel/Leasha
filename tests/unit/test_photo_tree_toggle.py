r"""2026-10-11, the owner: "the tree view in photos i like it can we have a option to
switch it on and off ideally a rich icon and in view menu".

Layer: L5
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.gui


@pytest.fixture
def photos(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.storage.sqlite_store import SqliteStore
    from app.ui.photos_view import PhotosView

    with SqliteStore(tmp_path / "p.db") as store:
        view = PhotosView(store)
        view.show()
        yield view, store
        view.shutdown()


def test_the_icon_hides_and_shows_the_tree_and_it_is_remembered(photos):
    view, store = photos
    assert view.sidebar.isVisible() and view.tree_button.isChecked()
    view.tree_button.click()
    assert not view.sidebar.isVisible() and not view.tree_button.isChecked()
    from app.ui.state_writes import settle_before_run

    settle_before_run(5000)                    # the queued write (`save_state`)
    assert store.get_state("ui:photos_tree") == "0"


def test_the_view_menu_has_the_switch_and_keeps_the_icon_in_step(photos):
    from PySide6.QtWidgets import QMenu

    from app.ui.widgets.photo_page_parts import fill_view_menu

    view, _store = photos
    menu = QMenu()
    fill_view_menu(menu, view)
    entry = next(a for a in menu.actions() if a.text() == "People, years and places")
    assert entry.isCheckable() and entry.isChecked()
    entry.trigger()
    assert not view.sidebar.isVisible()
    assert not view.tree_button.isChecked(), "the icon did not follow the menu"
