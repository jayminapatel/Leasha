"""Every entry in a page's own list carries an icon. 2026-10-04.

Layer: L5

The owner, looking at the regrabbed pictures: "what gets read does not have a
icon on the indexing page .. similarly there are no icons for the reports on
the report page". *What gets read* was added on 1 October without a row in
`CategoryNav.ICONS`; the Reports list never had any. §0.3: icons wherever
possible - and a list where one entry has none looks like a fault.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from tests.unit.conftest import gui_pump  # noqa: E402

pytestmark = pytest.mark.gui


def _qt():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.mark.slow
def test_every_indexing_and_settings_category_has_an_icon(gui_mainwindow):
    from app.ui.widgets.category_nav import CategoryNav

    _app, window, _store, _engine = gui_mainwindow
    names = (window.indexing_view._nav.category_names()
             + window.settings_view._nav.category_names())
    missing = [n for n in names if n not in CategoryNav.ICONS]
    assert not missing, f"no icon for {missing}"


def _side_rows(side):
    """Every entry line of a `SideList` (its section headings are not entries)."""
    rows = []
    for t in range(side.topLevelItemCount()):
        top = side.topLevelItem(t)
        rows += [top.child(c) for c in range(top.childCount())]
    return rows


def test_every_tidy_entry_has_an_icon_once_the_page_is_tinted():
    """1q: the Reports list became Tidy's `SideList` - every entry still has its icon."""
    from app.ui.theme import theme_colours
    from app.ui.tidy_view import TidyView

    _qt()
    view = TidyView(None)
    view.retint(theme_colours())
    rows = _side_rows(view.list)
    assert len(rows) == len(view.list.entries()) == 8
    bare = [r.text(0) for r in rows if r.icon(0).isNull()]
    assert not bare, f"no icon on {bare}"


@pytest.mark.slow
def test_the_window_tints_the_tidy_list_with_the_rest(gui_mainwindow):
    from PySide6.QtGui import QIcon

    app, window, _store, _engine = gui_mainwindow
    for row in _side_rows(window.tidy_view.list):
        row.setIcon(0, QIcon())                                # blank them
    window._apply_theme()
    gui_pump(app)
    rows = _side_rows(window.tidy_view.list)
    assert rows and all(not row.icon(0).isNull() for row in rows), "the theme change did not redraw them"
