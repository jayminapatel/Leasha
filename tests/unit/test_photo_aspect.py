r"""MS-12 (UX review 2026-10-14): the Photos page's optional "Aspect" view.

Layer: L5

Rows of photos at their own shape, each row filling the width - off unless chosen; the
square grid stays the default. The row arithmetic is `presenter.photo_rows` (Qt-free);
the grid lays itself out on those rows (`widgets.photo_aspect`).
"""

from __future__ import annotations

import os

import pytest

from app.ui.presenter.photo_rows import (LAST_ROW_STRETCH, MAX_ASPECT, MIN_ASPECT, ROW_HEIGHT,
                                         UNKNOWN_ASPECT, clamp_aspect, justify, row_breaks)


# -- the arithmetic, no Qt ----------------------------------------------------------------

def _rows_of(sizes, breaks):
    return [sizes[first:last + 1] for first, last in breaks]


def test_every_full_row_fills_the_width_exactly_at_one_height():
    aspects = [1.5, 0.75, 1.0, 1.333, 2.0, 0.66, 1.5, 1.5, 1.0, 0.8, 3.0, 1.2]
    width, gap = 900, 4
    sizes = justify(aspects, width, 180, gap)
    assert len(sizes) == len(aspects)
    rows = _rows_of(sizes, row_breaks(aspects, width, 180, gap))
    for row in rows[:-1]:
        assert sum(w for w, _h in row) + gap * (len(row) - 1) == width
        assert len({h for _w, h in row}) == 1


def test_each_photo_keeps_its_own_shape():
    sizes = justify([2.0, 1.0, 0.5, 1.0, 1.0, 1.0, 1.0], 800, 200)
    for (w, h), aspect in zip(sizes, [2.0, 1.0, 0.5, 1.0, 1.0, 1.0, 1.0], strict=True):
        assert abs(w / h - aspect) < 0.05, (w, h, aspect)


def test_full_rows_are_no_taller_than_aimed_for():
    aspects = [1.333] * 40
    sizes = justify(aspects, 1000, ROW_HEIGHT, 4)
    rows = _rows_of(sizes, row_breaks(aspects, 1000, ROW_HEIGHT, 4))
    for row in rows[:-1]:
        assert row[0][1] <= ROW_HEIGHT


def test_a_short_last_row_stays_at_the_height_and_ragged():
    sizes = justify([1.0] * 5 + [1.0], 1000, 200)       # five fill a row, one is left over
    *_full, last = sizes
    assert last == (200, 200)


def test_a_nearly_full_last_row_is_filled():
    # Two of shape 2.3 at height 200 are 920 of 1000: filling needs 1.087x, within the limit.
    assert 1000 / 920 < LAST_ROW_STRETCH
    sizes = justify([2.3, 2.3], 1000, 200)
    assert sum(w for w, _h in sizes) == 1000


def test_a_panorama_wider_than_the_page_is_a_row_of_its_own():
    sizes = justify([1.0, 4.0, 1.0], 500, 200)
    breaks = row_breaks([1.0, 4.0, 1.0], 500, 200)
    assert breaks[0] == (0, 1)                          # 200 + 800 >= 500: the row ends there
    assert sum(w for w, _h in sizes[:2]) == 500


def test_shapes_unknown_or_extreme_are_held_to_sensible_ones():
    assert clamp_aspect(None) == UNKNOWN_ASPECT
    assert clamp_aspect("x") == UNKNOWN_ASPECT
    assert clamp_aspect(0) == UNKNOWN_ASPECT
    assert clamp_aspect(float("nan")) == UNKNOWN_ASPECT
    assert clamp_aspect(20.0) == MAX_ASPECT
    assert clamp_aspect(0.01) == MIN_ASPECT


def test_nothing_to_lay_out_and_a_tiny_width_never_fail():
    assert justify([], 800) == []
    sizes = justify([1.0, 1.0], 1, 180)
    assert all(w >= 1 and h >= 1 for w, h in sizes)


# -- on the grid --------------------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _Thumbs:
    """A `ThumbLoader` stand-in with shapes."""

    def __init__(self, aspects):
        from PySide6.QtCore import QObject, Signal

        class _Signals(QObject):
            ready = Signal(str)

        self._signals = _Signals()
        self.ready = self._signals.ready
        self.aspects = dict(aspects)
        self.keep = None

    def pixmap(self, path):
        return None

    def request(self, path, size, mtime):
        pass

    def forget_waiting(self):
        pass

    def aspect(self, path):
        return self.aspects.get(path)

    def shaped(self, path):
        return None

    def set_keep_shape(self, on):
        self.keep = on


def _photo_rows(count):
    from app.storage.sqlite_store import PhotoRow

    return [PhotoRow(file_id=n, path=f"p{n}.jpg", ext="jpg", size_bytes=1, mtime_ns=1,
                     taken_at_ns=None, taken_is_hint=False, place=None, people=(), faces=0,
                     described=False, has_text=False, page_like=False, status="INDEXED")
            for n in range(count)]


@pytest.fixture
def browser(qapp):
    from app.ui.widgets.photo_browser import PhotoBrowser

    shapes = [1.5, 0.75, 1.0, 1.333, 2.0, 0.66, 1.5, 1.5, 1.0, 0.8, 3.0, 1.2] * 3
    thumbs = _Thumbs({f"p{n}.jpg": a for n, a in enumerate(shapes)})
    widget = PhotoBrowser(thumbs)
    widget.resize(1000, 700)
    widget.show()
    widget.set_rows(_photo_rows(len(shapes)))
    qapp.processEvents()
    yield widget, thumbs, shapes
    widget.hide()
    widget.deleteLater()


def test_aspect_is_a_fifth_view_choice_and_not_the_default(browser):
    from app.ui.widgets.photo_browser import MODES

    widget, _thumbs, _shapes = browser
    assert [key for key, _label, _edge in MODES] == ["details", "small", "medium", "large", "aspect"]
    assert widget.mode == "medium"


def test_the_grid_lands_on_the_rows_the_arithmetic_chose(browser, qapp):
    widget, thumbs, shapes = browser
    widget.set_mode("aspect")
    qapp.processEvents()
    grid = widget.grid
    delegate = widget._aspect_delegate
    breaks = row_breaks(shapes, delegate.room(), ROW_HEIGHT, delegate.gap())
    assert thumbs.keep is True
    model = widget.model
    for first, last in breaks:
        tops = {grid.visualRect(model.index(n, 0)).top() for n in range(first, last + 1)}
        assert len(tops) == 1, (first, last, tops)          # one row on screen, too
        right = grid.visualRect(model.index(last, 0)).right()
        assert right < grid.viewport().width()
    assert len(breaks) > 1
    first_row = grid.visualRect(model.index(breaks[1][0], 0)).top()
    assert first_row > grid.visualRect(model.index(0, 0)).top()


def test_leaving_aspect_gives_back_the_square_grid(browser, qapp):
    from PySide6.QtWidgets import QListView

    widget, thumbs, _shapes = browser
    square = widget.grid.itemDelegate()
    widget.set_mode("aspect")
    widget.set_mode("medium")
    qapp.processEvents()
    grid = widget.grid
    assert grid.itemDelegate() is square
    assert grid.viewMode() == QListView.ViewMode.IconMode
    assert grid.movement() == QListView.Movement.Static
    assert grid.uniformItemSizes()
    assert thumbs.keep is False
    assert grid.gridSize().width() > 0


def test_a_shape_learned_later_lays_the_rows_out_again(browser, qapp):
    widget, thumbs, _shapes = browser
    widget.set_mode("aspect")
    before = widget._aspect_delegate.sizes()[0]
    thumbs.aspects["p0.jpg"] = 3.5
    thumbs.ready.emit("p0.jpg")
    widget._aspect_settle.timeout.emit()
    after = widget._aspect_delegate.sizes()[0]
    assert after != before
    assert abs(after[0] / after[1] - 3.5) < 0.1


def test_the_aspect_choice_is_offered_and_remembered(qapp, tmp_path):
    from PySide6.QtWidgets import QMenu

    from app.storage.sqlite_store import SqliteStore
    from app.ui.photos_view import PhotosView
    from app.ui.state_writes import settle_before_run
    from app.ui.widgets.photo_page_parts import STATE_KEYS, fill_view_menu

    store = SqliteStore(tmp_path / "x.db").connect()
    view = PhotosView(store)
    menu = QMenu()
    fill_view_menu(menu, view)
    choice = next(a for a in menu.actions() if a.text().startswith("Aspect"))
    choice.trigger()
    assert view.browser.mode == "aspect"
    settle_before_run(5000)
    assert store.get_state(STATE_KEYS["mode"]) == "aspect"
    again = PhotosView(store)
    assert again.browser.mode == "aspect"
    for page in (view, again):
        page.shutdown()
        page.deleteLater()
