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

pytest.importorskip("PyQt6")

from tests.unit.conftest import gui_pump  # noqa: E402

pytestmark = pytest.mark.gui


def _qt():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_every_indexing_and_settings_category_has_an_icon(gui_mainwindow):
    from app.ui.widgets.category_nav import CategoryNav

    _app, window, _store, _engine = gui_mainwindow
    names = (window.indexing_view._nav.category_names()
             + window.settings_view._nav.category_names())
    missing = [n for n in names if n not in CategoryNav.ICONS]
    assert not missing, f"no icon for {missing}"


def test_every_report_has_an_icon_once_the_page_is_tinted():
    from app.ui.reports_view import REPORTS, ReportsView
    from app.ui.theme import theme_colours

    _qt()
    view = ReportsView(None)
    view.list.retint(theme_colours())
    rows = [view.list.item(i) for i in range(view.list.count())]
    assert [r.text() for r in rows] == [title for _k, title, _d in REPORTS]
    bare = [r.text() for r in rows if r.icon().isNull()]
    assert not bare, f"no icon on {bare}"


def test_the_window_tints_the_report_list_with_the_rest(gui_mainwindow):
    from PyQt6.QtGui import QIcon

    app, window, _store, _engine = gui_mainwindow
    rows = [window.reports_view.list.item(i) for i in range(window.reports_view.list.count())]
    for row in rows:
        row.setIcon(QIcon())                                   # blank them
    window._apply_theme()
    gui_pump(app)
    assert all(not row.icon().isNull() for row in rows), "the theme change did not redraw them"
