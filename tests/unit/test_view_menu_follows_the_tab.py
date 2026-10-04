r"""View, in the menu bar, shows the options of the tab in front; each tab's
View control is an icon beside Preview.

Layer: L5 (pytest-qt, `gui_mainwindow`)

2026-10-04, the owner: "the view in each tab should be in the view in the menu
and should be dynamic i.e. content changes depending on the tab you are in.. if
the view has to stay on each tab it should be a icon similar to preview
consistent across all".
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt                                           # noqa: E402

from app.ui import view_options                                       # noqa: E402

pytestmark = pytest.mark.gui


def _view_menu(window):
    return next(a.menu() for a in window.menuBar().actions()
                if a.text().replace("&", "") == "View")


def _texts(menu):
    return [a.text().replace("&", "") for a in menu.actions() if a.text()]


def test_the_menu_shows_the_options_of_the_tab_in_front(gui_mainwindow):
    _app, window, _store, _engine = gui_mainwindow
    menu = _view_menu(window)

    window._show(window.files_view)
    window._fill_view_menu(menu)
    on_files = _texts(menu)
    assert on_files[0] == "Preview pane"
    assert "Columns" in on_files, "the tab's own options follow"
    own = [a.text() for a in window.files_view.view_button.menu_for(window).actions()
           if a.text() and a.text().replace("&", "") != "Preview pane"]
    assert [t for t in _texts(menu)[1:]] == [t.replace("&", "") for t in own],         "the tab's own options, in its own order, Preview pane once"

    window._show(window.mail_view)
    window._fill_view_menu(menu)
    assert _texts(menu) != on_files, "Mail's columns, not Files'"
    assert _texts(menu).count("Preview pane") == 1, "the old tab's options are gone"

    indexing = getattr(window, "indexing_view", None)
    if indexing is not None:
        window._show(indexing)
        window._fill_view_menu(menu)
        assert _texts(menu) == ["Preview pane"]


def test_a_choice_in_the_menu_is_the_tabs_choice(gui_mainwindow):
    _app, window, _store, _engine = gui_mainwindow
    window._show(window.files_view)
    menu = _view_menu(window)
    window._fill_view_menu(menu)
    button = window.files_view.view_button
    before = button.prefs
    column = next(a for a in window._view_menu_built.actions()
                  if a.isCheckable() and a.isEnabled() and a.text() and a.isChecked())
    column.trigger()
    assert button.prefs != before, "the tab's preferences changed"
    column.trigger()


@pytest.mark.parametrize("name", ["files_view", "mail_view", "code_view", "search_view"])
def test_every_tabs_view_control_is_an_icon_like_preview(gui_mainwindow, name):
    _app, window, _store, _engine = gui_mainwindow
    view = getattr(window, name, None)
    if view is None:
        pytest.skip(f"no {name} in this window")
    button = view.view_button
    assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    assert not button.icon().isNull()
    assert button.icon_name == view_options.VIEW_ICON
    assert button.text() == "View" and button.toolTip(), "still named and explained"
    assert view.toggles.get("view") is button, "retinted with the toggles on a theme change"
