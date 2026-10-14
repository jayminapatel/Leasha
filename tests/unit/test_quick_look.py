r"""Space is quick look, everywhere - review 2026-10-14, MS-9 - and the photo strip, MS-5.

Layer: L5

Space meant the docked preview pane in the lists, the info panel in Photos and
the next photo in the viewer. Now: on a result list Space opens the current row
in a look window of its own and the next Space closes it; the docked pane stays
on Ctrl+Shift+P; a line with a tick box still ticks; in the Photos grid Space
opens the viewer and I is the info panel. The look window is stood in for here
(`preview_window.pop_out` is replaced), because what is tested is which row is
looked at and when the look goes - not the drawing of a document.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt                            # noqa: E402
from PySide6.QtGui import QKeyEvent                              # noqa: E402
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from app.search.engine import SearchResult                       # noqa: E402
from app.ui.widgets import preview_window                        # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _press(widget, key) -> None:
    for kind in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
        QApplication.sendEvent(widget, QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier))


def _result(n: int, ext: str = "pdf") -> SearchResult:
    return SearchResult(chunk_id=n, file_id=n, path=rf"D:\Archive\f{n}.{ext}", text="pump",
                        score=0.9, rank=n, ext=ext, mtime_ns=1)


@pytest.fixture()
def looks(monkeypatch):
    """Every look opened: `(row, siblings)`; the window is a plain widget."""
    opened: list = []

    def pop_out(row, **kw):
        window = QWidget()
        window.closed_with = kw["closed"]
        original = window.closeEvent

        def close_event(event, w=window):
            kw["closed"](w)
            original(event)

        window.closeEvent = close_event
        window.show()
        opened.append((row, kw["siblings"], window))
        return window

    monkeypatch.setattr(preview_window, "pop_out", pop_out)
    return opened


def _search_list(qapp, rows):
    """The Search list, wired as the page wires it: a docked pane beside it."""
    from app.ui.results_view import ResultsView
    from app.ui.widgets.preview import attach_preview

    page = QWidget()
    toggled: list = []
    page.view_button = SimpleNamespace(toggle_preview=lambda: toggled.append(1))
    results = ResultsView()
    _pane, split = attach_preview(results, lambda _r: None, lambda _e: None)
    QVBoxLayout(page).addWidget(split)
    page.resize(720, 360)
    page.show()
    results.show_results(rows, ["pump"])
    QApplication.processEvents()
    return page, results, toggled


def test_space_opens_a_quick_look_and_space_again_closes_it(qapp, looks) -> None:
    page, results, toggled = _search_list(qapp, [_result(1), _result(2)])
    results._list.setCurrentIndex(results._model.index(1, 0))
    _press(results._list, Qt.Key.Key_Space)
    assert [row.file_id for row, _s, _w in looks] == [2]
    window = looks[0][2]
    assert window.isVisible()
    _press(results._list, Qt.Key.Key_Space)
    assert not window.isVisible(), "the second Space put the look away"
    assert toggled == [], "the docked pane is Ctrl+Shift+P's now, not Space's"
    page.deleteLater()


def test_space_inside_the_look_closes_it(qapp, looks) -> None:
    page, results, _toggled = _search_list(qapp, [_result(1)])
    results._list.setCurrentIndex(results._model.index(0, 0))
    _press(results._list, Qt.Key.Key_Space)
    window = looks[0][2]
    window._quick_look_close.activated.emit()     # the window's own Space
    assert not window.isVisible()
    _press(results._list, Qt.Key.Key_Space)
    assert len(looks) == 2, "the next Space opens a fresh look"
    page.deleteLater()


def test_a_picture_looks_with_its_siblings(qapp, looks) -> None:
    rows = [_result(1, "jpg"), _result(2, "pdf"), _result(3, "jpg")]
    page, results, _toggled = _search_list(qapp, rows)
    results._list.setCurrentIndex(results._model.index(2, 0))
    _press(results._list, Qt.Key.Key_Space)
    row, siblings, _window = looks[0]
    assert row.file_id == 3 and [s.file_id for s in siblings] == [1, 3]
    page.deleteLater()


def test_nothing_current_opens_nothing(qapp, looks) -> None:
    page, results, _toggled = _search_list(qapp, [_result(1)])
    results._list.setCurrentIndex(results._model.index(-1, 0))
    _press(results._list, Qt.Key.Key_Space)
    assert looks == []
    page.deleteLater()


def test_a_tick_box_still_ticks_on_a_list_with_a_quick_look(qapp, looks) -> None:
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem

    from app.ui.widgets import list_keys
    from app.ui.widgets.quick_look import QuickLook

    page = QWidget()
    table = QTableWidget(1, 1)
    item = QTableWidgetItem("row")
    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
    item.setCheckState(Qt.CheckState.Unchecked)
    table.setItem(0, 0, item)
    QVBoxLayout(page).addWidget(table)
    page.quick_look = QuickLook(page, current=lambda: SimpleNamespace(path="D:/a.txt"))
    list_keys.install(table, preview=list_keys.PAGE_PREVIEW)
    page.show()
    table.setCurrentCell(0, 0)
    _press(table, Qt.Key.Key_Space)
    assert looks == []
    assert table.item(0, 0).checkState() == Qt.CheckState.Checked
    page.deleteLater()


def test_every_list_with_a_docked_pane_has_a_quick_look(qapp) -> None:
    """Files, Mail, Code, Browse and Tidy build their pane with `attach_preview`,
    which is where the look is made - one place, so none can be left out."""
    from app.ui.results_view import ResultsView
    from app.ui.widgets.preview import attach_preview
    from app.ui.widgets.quick_look import QuickLook

    _pane, split = attach_preview(ResultsView(), lambda _r: None, lambda _e: None)
    assert isinstance(split.quick_look, QuickLook)


# ---------------------------------------------------------------------------
# The Photos page: Space is the viewer, I the information panel
# ---------------------------------------------------------------------------

def test_photos_space_opens_the_viewer_and_i_the_info_panel(qapp, tmp_path) -> None:
    from app.storage.sqlite_store import PhotoRow
    from app.ui.widgets.photo_browser import PhotoBrowser
    from app.ui.widgets.photo_thumbs import ThumbLoader

    rows = [PhotoRow(file_id=n, path=f"D:/p/{n}.jpg", ext="jpg", size_bytes=1, mtime_ns=1,
                     taken_at_ns=n, taken_is_hint=False, place=None, people=(), faces=0,
                     described=False, has_text=False, page_like=False, status="SKIPPED",
                     scanned=True, tags=()) for n in range(3)]
    browser = PhotoBrowser(ThumbLoader(tmp_path))
    browser.set_rows(rows)
    browser.select_path(rows[1].path)
    viewed, info = [], []
    browser.opened.connect(viewed.append)
    browser.preview_requested.connect(lambda: info.append(1))
    _press(browser.grid, Qt.Key.Key_Space)
    assert viewed == [rows[1]] and info == []
    _press(browser.grid, Qt.Key.Key_I)
    assert info == [1] and viewed == [rows[1]]
    browser.deleteLater()


# ---------------------------------------------------------------------------
# The Search page's grid and strip: Space looks, in the lightbox
# ---------------------------------------------------------------------------

def _pane(qapp):
    from app.ui.widgets import result_tools

    results, _preview, outer = result_tools.build_results_pane(
        on_opened=lambda _r: None, on_reveal=lambda _r: None, on_reindex=lambda _r: None,
        on_error=lambda _e: None, store=None)
    outer.resize(900, 500)
    outer.show()
    return results, outer


def test_space_in_the_photo_grid_opens_and_closes_the_lightbox(qapp, looks) -> None:
    results, outer = _pane(qapp)
    rows = [_result(n, "jpg") for n in (1, 2, 3)]
    results.show_results(rows, [])
    outer.switches.boxes["grid"].setChecked(True)
    QApplication.processEvents()
    grid = outer.grid
    grid._list.setCurrentIndex(grid.model.index(1, 0))
    _press(grid._list, Qt.Key.Key_Space)
    row, siblings, window = looks[0]
    assert row.file_id == 2 and [s.file_id for s in siblings] == [1, 2, 3]
    _press(grid._list, Qt.Key.Key_Space)
    assert not window.isVisible()
    outer.switches.boxes["grid"].setChecked(False)
    outer.deleteLater()


def test_the_photo_strip_shows_the_pictures_of_a_mixed_set(qapp, looks) -> None:
    from app.ui.widgets.photo_strip import strip_heading

    results, outer = _pane(qapp)
    strip = outer.strip
    results.show_results([_result(1), _result(2, "jpg"), _result(3, "png")], [])
    QApplication.processEvents()
    assert strip.isVisible()
    assert [r.file_id for r in strip.image_rows()] == [2, 3]
    assert strip.heading.text() == strip_heading(2) == "Photos · 2 pictures"
    results.show_results([_result(4)], [])
    assert not strip.isVisible(), "no pictures, no strip"
    outer.deleteLater()


def test_the_strip_goes_while_the_grid_is_in_front(qapp) -> None:
    results, outer = _pane(qapp)
    results.show_results([_result(1), _result(2, "jpg")], [])
    outer.switches.boxes["grid"].setChecked(True)
    QApplication.processEvents()
    assert not outer.strip.isVisible()
    outer.switches.boxes["grid"].setChecked(False)
    QApplication.processEvents()
    assert outer.strip.isVisible()
    outer.deleteLater()


def test_a_click_on_the_strip_opens_one_lightbox_however_it_was_clicked(qapp, looks) -> None:
    results, outer = _pane(qapp)
    results.show_results([_result(1), _result(2, "jpg")], [])
    QApplication.processEvents()
    strip = outer.strip
    index = strip.model.index(0, 0)
    strip._list.clicked.emit(index)
    strip._list.activated.emit(index)             # a double-click is both
    assert [row.file_id for row, _s, _w in looks] == [2]
    outer.deleteLater()
