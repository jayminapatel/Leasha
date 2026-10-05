r"""Order 202626270602 (0n) section 4b - the doors into the Life Timeline.

Layer: L5

"Entry points: from the Reports section ('Browse your timeline') AND from any
result's date ('see everything from this month' in the context menu) AND
composing with the existing timeline strip (strip click -> this view,
pre-filtered)." Each is pressed here, in the real window where it matters,
because a widget that works alone is exactly how a feature ships unreachable.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt                               # noqa: E402
from app.ui.widgets.timeline_host import REPORT_KEY  # noqa: E402 - the list's key role
from PySide6.QtWidgets import QMenu                                  # noqa: E402

from app.ui.controllers.timeline_controller import NO_DATE         # noqa: E402
from app.ui.widgets.file_menu import FileActions, build_menu       # noqa: E402
from tests.unit.conftest import gui_pump, gui_row_count, gui_select_row   # noqa: E402
from tests.unit.timeline_env import NS, add_file, add_mail, noon   # noqa: E402

pytestmark = pytest.mark.gui


class _Row:
    """All the strip reads of a result: when it was modified."""

    def __init__(self, mtime_ns):
        self.mtime_ns = mtime_ns


def timeline_row(view) -> int:
    return [view.list.item(i).data(REPORT_KEY) for i in range(view.list.count())].index("timeline")


# ---------------------------------------------------------------------------
# Door 1: Reports -> "Browse your timeline"
# ---------------------------------------------------------------------------

def test_the_reports_page_lists_the_timeline_and_choosing_it_gives_it_the_pane(qtbot, tmp_path):
    from app.storage.sqlite_store import SqliteStore
    from app.ui.reports_view import ReportsView

    with SqliteStore(tmp_path / "t.db") as store:
        view = ReportsView(store)
        qtbot.addWidget(view)
        view.resize(1000, 700)
        view.show()
        titles = [view.list.item(i).text() for i in range(view.list.count())]
        assert "Browse your timeline" in titles
        item = view.list.item(titles.index("Browse your timeline"))
        assert "in the order it happened" in item.toolTip()
        view.list.setCurrentRow(timeline_row(view))
        assert view.timeline.isVisibleTo(view) and not view.export.isVisibleTo(view)
        assert not view.body.isVisibleTo(view) and not view.space_table.isVisibleTo(view)
        view.list.setCurrentRow(0)                                   # back to a document
        assert not view.timeline.isVisibleTo(view) and view.export.isVisibleTo(view)
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(5000)


# ---------------------------------------------------------------------------
# Door 2: a result's menu - "See everything from this month"
# ---------------------------------------------------------------------------

def test_the_menu_offers_the_month_only_where_a_caller_can_honour_it(qtbot):
    from PySide6.QtWidgets import QWidget

    parent = QWidget()
    qtbot.addWidget(parent)
    asked = []
    with_it = build_menu(parent, "C:/x.txt", FileActions(same_period=lambda: asked.append(1)))
    names = [a.text() for a in with_it.actions()]
    assert "See everything from this month" in names
    action = with_it.actions()[names.index("See everything from this month")]
    assert "timeline" in action.toolTip().lower() and "wherever" in action.toolTip()
    action.trigger()
    assert asked == [1]
    without = build_menu(parent, "C:/x.txt", FileActions())
    assert "See everything from this month" not in [a.text() for a in without.actions()]


@pytest.fixture
def dated_window(gui_mainwindow, qtbot):
    """The real window with a photograph shot in June 2015 (copied in 2019), a
    message sent that month, and a file with no date at all."""
    app, window, store, _engine = gui_mainwindow
    ids = {
        "photo": add_file(store, "C:/photos/lake.jpg", mtime=noon(2019, 3, 1), taken=noon(2015, 6, 10)),
        "letter": add_file(store, "C:/letters/to-the-bank.docx", mtime=noon(2015, 6, 20)),
        "mail": add_mail(store, "Wedding plans", sent=noon(2015, 6, 25), container_mtime=noon(2021, 3, 3)),
        "undated": add_file(store, "C:/misc/odd.txt", mtime=0),
    }
    with store.write() as conn:
        conn.execute("UPDATE files SET indexed_at = 1700000000 + id WHERE indexed_at IS NULL")
    window.resize(1100, 700)
    window.show()
    gui_pump(app)
    return app, window, store, ids


def test_choosing_it_on_a_search_result_opens_the_month_that_file_is_really_from(dated_window, qtbot, monkeypatch):
    r"""A photograph's row carries its copy date (2019); the timeline opens at the
    month the camera says (June 2015)."""
    app, window, store, ids = dated_window
    results = window.search_view.results
    window._show(window.search_view)
    window.search_view.input.setText("barnsley")
    qtbot.waitUntil(lambda: gui_row_count(results) > 0, timeout=10000)
    row = results._rows[0]
    heard = []
    results.period_requested.connect(heard.append)

    def choose_the_month(menu, *_args):
        names = [a.text() for a in menu.actions()]
        assert "See everything from this month" in names
        menu.actions()[names.index("See everything from this month")].trigger()

    monkeypatch.setattr(QMenu, "exec", choose_the_month)
    gui_select_row(results, 0)
    index = results._model.index(0, 0)
    point = results._list.viewport().mapTo(results._list, results._list.visualRect(index).center())
    results._on_context_menu(point)
    assert [r.file_id for r in heard] == [row.file_id]
    # The menu path opens the timeline too (at whatever month that file's date is).
    qtbot.waitUntil(lambda: window.rail.currentIndex() == window._tab_index[window.reports_view],
                    timeout=10000)
    # ...and the window's answer, for a file whose truthful date is June 2015 (asked
    # only after the first answer has landed, so the two cannot arrive out of order):
    window._show(window.search_view)
    window.timeline_ctl.browse_period(ids["photo"])
    timeline = window.reports_view.timeline
    qtbot.waitUntil(lambda: timeline.heading.text() == "June 2015", timeout=10000)
    assert window.rail.currentIndex() == window._tab_index[window.reports_view]
    qtbot.waitUntil(lambda: timeline._period is not None and not timeline._loading
                    and timeline.list.block_count() > 0, timeout=10000)
    shown = {f.head.file_id for r in range(timeline.list.block_count())
             for f in timeline.list.block_at(r).folds}
    assert {ids["photo"], ids["letter"], ids["mail"]} <= shown
    assert ids["undated"] not in shown


def test_a_message_opens_the_month_it_was_sent_not_the_month_its_archive_was_saved(dated_window, qtbot):
    _app, window, _store, ids = dated_window
    # Start from somewhere else, so the wait below is for *this* answer and not for
    # a "June 2015" heading a previous test left behind (which made the next test see
    # the Reports page arrive late).
    window._show(window.search_view)
    window.mail_view.period_requested.emit(ids["mail"])            # what the Mail tab's menu emits
    timeline = window.reports_view.timeline
    qtbot.waitUntil(lambda: window.rail.currentIndex() == window._tab_index[window.reports_view],
                    timeout=10000)
    qtbot.waitUntil(lambda: timeline.heading.text() == "June 2015", timeout=10000)


def test_a_file_with_no_date_says_so_instead_of_opening_the_wrong_month(dated_window, qtbot, monkeypatch):
    _app, window, _store, ids = dated_window
    window._show(window.search_view)
    said = []
    monkeypatch.setattr(window, "notify", lambda text, *a, **k: said.append(text))
    window.timeline_ctl.browse_period(ids["undated"])
    qtbot.waitUntil(lambda: bool(said), timeout=10000)
    assert said == [NO_DATE]
    assert window.rail.currentIndex() == window._tab_index[window.search_view]


# ---------------------------------------------------------------------------
# Door 3: the timeline strip above the results
# ---------------------------------------------------------------------------

def test_a_right_click_on_a_strip_period_opens_the_timeline_on_that_period(dated_window, qtbot, monkeypatch):
    from app.ui.widgets.timeline_strip import TimelineStrip

    _app, window, _store, _ids = dated_window
    strip = window.search_view.split.timeline
    assert isinstance(strip, TimelineStrip)

    strip.set_rows([_Row(noon(2015, 6, 10)), _Row(noon(2016, 1, 10))])
    button = strip._layout.itemAt(0).widget()
    assert "Right-click to browse everything from this period." in button.toolTip()
    assert "Click to filter the search to this period." in button.toolTip()      # unchanged, still there
    heard = []
    strip.browse_requested.connect(lambda a, b: heard.append((a, b)))
    monkeypatch.setattr(QMenu, "exec", lambda menu, *_a: menu.actions()[0].trigger())
    button.customContextMenuRequested.emit(QPoint(2, 2))
    assert heard and heard[0][0].startswith("2015")
    timeline = window.reports_view.timeline
    qtbot.waitUntil(lambda: timeline._period is not None, timeout=10000)
    assert window.rail.currentIndex() == window._tab_index[window.reports_view]
    assert timeline.picker.range_from.text() == heard[0][0]


def test_a_left_click_on_the_strip_still_only_filters_the_search(dated_window, qtbot):
    r"""The strip's own behaviour must not have moved: click = a filter typed
    into the search box, not a change of page."""
    _app, window, _store, _ids = dated_window
    window._show(window.search_view)
    strip = window.search_view.split.timeline

    strip.set_rows([_Row(noon(2015, 6, 10)), _Row(noon(2016, 1, 10))])
    chosen = []
    strip.filter_chosen.connect(chosen.append)
    strip._layout.itemAt(0).widget().click()
    assert chosen and chosen[0].startswith("after:")
    assert window.rail.currentIndex() == window._tab_index[window.search_view]


# ---------------------------------------------------------------------------
# And out again: opening what the timeline shows
# ---------------------------------------------------------------------------

def test_opening_an_item_on_a_drive_that_is_not_plugged_in_says_what_to_do(dated_window, qtbot, monkeypatch):
    r"""The shell's own opener resolves a catalogued file through the drive's
    *current* mount point and says so when it is not there."""
    _app, window, store, _ids = dated_window
    volume = store.upsert_volume("guid-entry-drawer", kind="drive", name="Drawer WD", status="OFFLINE")
    file_id = add_file(store, "leasha-volume://9/Holiday/beach.jpg", mtime=noon(2020, 1, 1),
                       taken=noon(2015, 6, 15), volume_id=volume, relative_path="Holiday/beach.jpg")
    from app.reports.timeline import Period, timeline_page

    page = timeline_page(store, Period.month(2015, 6), connected={})
    entry = next(e for e in page.entries if e.file_id == file_id)
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    window.reports_view.opened.emit(entry)
    qtbot.waitUntil(lambda: bool(errors), timeout=10000)
    assert "not plugged in" in errors[0].suggestion


def test_the_timeline_page_is_read_only_for_the_whole_window_session(dated_window, qtbot):
    _app, window, store, _ids = dated_window
    before = store.conn.execute("SELECT COUNT(*), MAX(mtime_ns), SUM(size_bytes) FROM files").fetchone()[:]
    window.timeline_ctl.browse_range("2015-01-01", "2015-12-31")
    timeline = window.reports_view.timeline
    qtbot.waitUntil(lambda: timeline._period is not None and not timeline._loading
                    and timeline.list.block_count() > 0, timeout=10000)
    assert store.conn.execute(
        "SELECT COUNT(*), MAX(mtime_ns), SUM(size_bytes) FROM files").fetchone()[:] == before
