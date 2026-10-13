r"""The Photos tab behaves as one app: one viewer per double-click, a View menu
that empties itself, one total in its summary, a header sort that holds.

Layer: L5

2026-10-12, the owner: "do the recommended to make it world class and friendly
and do it all". Each test pins one fault an audit of the Photos tab found in
the code, written to fail before its fix:

- a double-click opened the viewer twice (`doubleClicked` *and* `activated`);
- the window's View menu kept every Photos entry it was ever given;
- hiding and showing the info panel left it blank;
- the "of N" under the photos changed when a photo was clicked;
- a click on a Details heading was undone by the next read of the library;
- the viewer showed "…" on every step, its neighbours never asked for;
- a pile said "photo(s)" while counting faces;
- buttons on the naming pages had no icon, and a page had many primaries.
"""

from __future__ import annotations

import datetime as dt
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.storage.sqlite_store import PhotoRow, SqliteStore

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _library(store, tmp_path, count=3):
    from PIL import Image

    paths = []
    for n in range(count):
        path = tmp_path / f"photo{n}.jpg"
        Image.new("RGB", (64, 48), (40 * n, 90, 200)).save(path)
        paths.append(path)
        store.upsert_file(path=str(path), size_bytes=path.stat().st_size,
                          mtime_ns=path.stat().st_mtime_ns, source_kind="file")
    return paths


@pytest.fixture
def view(qapp, qtbot, store, tmp_path):
    from app.ui.photos_view import PhotosView

    _library(store, tmp_path)
    page = PhotosView(store)
    qtbot.addWidget(page)
    qtbot.waitUntil(lambda: page.browser.model.rowCount() == 3, timeout=15000)
    yield page
    page.shutdown()


def _row(n, name, when):
    ns = int(dt.datetime(when, 1, 1).timestamp() * 1e9)
    return PhotoRow(file_id=n, path=f"D:/Photos/{name}.jpg", ext="jpg", size_bytes=10,
                    mtime_ns=ns, taken_at_ns=ns, taken_is_hint=False, place=None,
                    people=(), faces=0, described=False, has_text=False, page_like=False,
                    status="SKIPPED", scanned=True, tags=())


def _browser(qapp):
    from app.ui.widgets.photo_browser import PhotoBrowser
    from app.ui.widgets.photo_thumbs import ThumbLoader

    return PhotoBrowser(ThumbLoader(None))


# -- 1. one viewer per double-click ------------------------------------------------------

def test_one_double_click_opens_the_viewer_once(qapp):
    browser = _browser(qapp)
    browser.set_rows([_row(1, "a", 2020)])
    opened = []
    browser.opened.connect(opened.append)
    index = browser.model.index(0, 0)
    # What Qt sends for one double-click on Windows: doubleClicked, then
    # activated (no style there activates on a single click).
    browser.grid.doubleClicked.emit(index)
    browser.grid.activated.emit(index)
    assert len(opened) == 1


def test_opening_a_photo_again_reuses_the_viewer_already_open(view):
    from app.ui.widgets.photo_viewer import PhotoViewer

    row = view.browser.model.row_at(0)
    view.view_photo(row)
    view.view_photo(row)
    viewers = [w for w in view.findChildren(PhotoViewer) if w.isVisible()]
    assert len(viewers) == 1
    for viewer in viewers:
        viewer.close()



def test_a_minimised_viewer_comes_back_when_a_photo_is_opened_again(view):
    """CodeRabbit on #68: a minimised viewer is still "visible", and raise_() does not restore it."""
    row = view.browser.model.row_at(0)
    view.view_photo(row)
    viewer = view._viewer
    viewer.showMinimized()
    if not viewer.isMinimized():
        pytest.skip("this platform plugin keeps no minimised state")
    view.view_photo(row)
    assert view._viewer is viewer and not viewer.isMinimized()
    viewer.close()

# -- 2. the window's View menu empties itself ---------------------------------------------

@pytest.mark.slow
def test_the_view_button_hands_the_window_a_menu_of_its_own(view):
    from PySide6.QtWidgets import QMenu

    holder = QMenu()
    built = view.view_button.menu_for(holder)
    assert isinstance(built, QMenu)
    labels = [a.text() for a in built.actions() if a.text()]
    assert labels[:4] == ["Details", "Small thumbnails", "Medium thumbnails",
                          "Large thumbnails"]


def test_the_window_view_menu_does_not_pile_up_on_photos(view):
    from PySide6.QtWidgets import QMenu

    from app.ui.shell import MainWindow

    window = SimpleNamespace(_current_view=lambda: view)
    menu = QMenu()
    MainWindow._fill_view_menu(window, menu)
    first = len(menu.actions())
    assert first > 0
    for _ in range(3):
        MainWindow._fill_view_menu(window, menu)
    assert len(menu.actions()) == first


# -- 4. the info panel shows the current photo when it comes back -------------------------

def test_showing_the_info_panel_again_shows_the_current_photo(view):
    view.show()
    view.show_info(False, remember=False)
    second = view.browser.model.row_at(1)
    view.browser.select_path(str(second.path))
    view.show_info(True, remember=False)
    assert view.info._row is not None and view.info._row.path == second.path


# -- 5. one total in the summary ------------------------------------------------------------

def test_the_summary_total_does_not_change_when_a_photo_is_clicked(view):
    rows = view.browser.model.rows()
    # A scope that differs from the whole library, as with mail pictures left out.
    view._drawn((rows, "newest", len(rows), 7), (), view._arranging)
    before = view.summary.text()
    assert "of 7" in before
    view.browser.select_path(str(rows[0].path))
    assert "of 7" in view.summary.text(), view.summary.text()


# -- 6. a heading click holds -----------------------------------------------------------------

def test_a_heading_sort_survives_the_next_read(qapp):
    from PySide6.QtCore import Qt

    from app.ui.presenter.photos import COLUMNS

    browser = _browser(qapp)
    rows = [_row(1, "b", 2021), _row(2, "c", 2020), _row(3, "a", 2019)]
    browser.set_rows(rows)
    name = next(n for n, (key, *_rest) in enumerate(COLUMNS) if key == "name")
    browser.table.sortByColumn(name, Qt.SortOrder.AscendingOrder)
    assert [r.file_id for r in browser.model.rows()] == [3, 1, 2]
    # The library read again: the page's worker sorts by the heading (CodeRabbit on #68 - never on the
    # window's thread), and the browser keeps the order it is handed.
    from app.ui.presenter.photos import arrange

    shown = arrange([*rows, _row(4, "d", 2018)], None, "", "newest", browser.model.heading_order())[0]
    browser.set_rows(shown)
    assert [r.file_id for r in browser.model.rows()] == [3, 1, 2, 4]
    browser.follow_order()                             # a sort chosen from the menu
    assert browser.model.heading_order() is None
    browser.set_rows(rows)
    assert [r.file_id for r in browser.model.rows()] == [1, 2, 3]


def test_set_rows_never_sorts_on_the_windows_thread(qapp, monkeypatch):
    """CodeRabbit on #68: with a heading held, every periodic read re-sorted the library on the UI thread."""
    from PySide6.QtCore import Qt

    from app.ui.presenter import photos as presenter
    from app.ui.widgets import photo_browser

    browser = _browser(qapp)
    rows = [_row(1, "b", 2021), _row(2, "c", 2020), _row(3, "a", 2019)]
    browser.set_rows(rows)
    browser.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    calls = []
    monkeypatch.setattr(photo_browser, "heading_sorted", lambda *a, **k: calls.append(a) or list(a[0]))
    monkeypatch.setattr(presenter, "heading_sorted", lambda *a, **k: calls.append(a) or list(a[0]))
    browser.set_rows(list(reversed(rows)))
    assert calls == [] and [r.file_id for r in browser.model.rows()] == [3, 2, 1]


def test_a_sort_from_the_menu_takes_over_from_the_heading(view, monkeypatch):
    called = []
    monkeypatch.setattr(view.browser, "follow_order", lambda: called.append(True))
    view.set_sort("oldest")
    assert called


# -- 7. the viewer's neighbours are asked for ahead ------------------------------------------

def test_the_viewer_asks_for_the_photos_either_side(view):
    from app.ui.widgets.photo_viewer import PhotoViewer

    middle = view.browser.model.row_at(1)
    view.view_photo(middle)
    viewer = next(w for w in view.findChildren(PhotoViewer) if w.isVisible())
    wanted = {str(view.browser.model.row_at(n).path) for n in (0, 2)}
    asked = set(viewer._asked) | set(viewer._decoded)
    assert wanted <= asked, asked
    assert view.browser.current_row().path == middle.path, "looking ahead moved nothing"
    viewer.close()


def test_the_viewer_promises_only_what_it_does():
    from app.ui.widgets import photo_viewer

    assert "click outside" not in " ".join(photo_viewer.__doc__.split())


# -- 8. a pile counts faces ----------------------------------------------------------------------

def test_a_pile_says_faces_not_photos():
    from app.ui.widgets.photo_tagger_page import _pile_label

    assert _pile_label(SimpleNamespace(name="", face_count=4), 2) == "Person 2 — 4 face(s)"
    assert _pile_label(SimpleNamespace(name="Jason", face_count=1), 1) == "Jason — 1 face(s)"


# -- 9 and 10. every naming button has an icon and a kind; one primary -------------------------

@pytest.mark.parametrize("label, expected", [
    ("Make a new person", ("user-plus", "secondary")),
    ("Move to", ("folder-input", "secondary")),
    ("Not Jason", ("x", "secondary")),
    ("Retry with a longer time limit", ("rotate-cw", "secondary")),
    ("Yes", ("check", "secondary")),
])
def test_the_naming_buttons_have_an_icon_and_a_kind(label, expected):
    from app.ui.widgets.buttons import lookup

    assert lookup(label) == expected


def test_the_stale_pile_buttons_are_gone_from_the_table():
    from app.ui.widgets.buttons import BUTTONS

    assert "Remove selected from this pile" not in BUTTONS
    assert "Move selected to a new pile" not in BUTTONS


def test_every_icon_is_named_once():
    from app.ui.widgets.icons import ICON_NAMES

    names = list(ICON_NAMES)
    assert len(names) == len(set(names)), sorted({n for n in names if names.count(n) > 1})


def test_a_heading_clicked_while_arranging_is_not_undone_by_the_result(qapp, monkeypatch):
    """CodeRabbit on #68: an arrangement started before a heading click came back in the old order."""
    from PySide6.QtCore import Qt

    from app.ui.widgets import photo_page_parts

    browser = _browser(qapp)
    rows = [_row(1, "b", 2021), _row(2, "c", 2020), _row(3, "a", 2019)]
    browser.set_rows(rows)
    owner = SimpleNamespace(browser=browser)
    landed: list = []

    def start(_owner, worker, finished, _failed):
        browser.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)     # the click, mid-arrangement
        finished((list(rows), "newest", 3, 3))

    monkeypatch.setattr(photo_page_parts, "_start", start)
    photo_page_parts.arrange_rows(owner, rows, None, "", "newest", finished=landed.append, failed=print)
    expected = photo_page_parts.__dict__.get("heading_sorted") or __import__(
        "app.ui.presenter.photos", fromlist=["x"]).heading_sorted
    assert [r.file_id for r in landed[0][0]] == [r.file_id for r in expected(rows, *browser.model.heading_order())]
