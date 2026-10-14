r"""The thumbnail grid. Work order 0h §3a.

Layer: L5 widget.

**Off by default, worker-decoded, never a crash on a broken photo** - the
three things the work order names explicitly, and the three things this file
checks against the real widget rather than trusting the docstrings that say
so. `test_ui_never_blocks.py` already proves the *shape* of the worker
delegation (`_load_thumbnails` is in its explicit parametrize list) and that
nothing here ever builds a `QPixmap` straight from a path string; this file
proves the *behaviour* - a photo really gets decoded, off the interface
thread, and a photo that cannot be decoded really leaves its placeholder in
place rather than raising.
"""

from __future__ import annotations

import os
import time
from typing import NamedTuple

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage                                  # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

from app.ui.thumbnail_loader import (                           # noqa: E402
    IMAGE_RESULT_EXTS, decode_thumbnail, is_image_result,
)
from app.ui.widgets.thumbnail_grid import (                     # noqa: E402
    GRID_ENABLED_KEY, ThumbnailGrid, enabled_checkbox,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class _Row(NamedTuple):
    path: str
    ext: str = ""


def _png(tmp_path, name: str = "photo.png", *, width: int = 400, height: int = 100):
    """A small, real PNG - wide by default, so a 90° rotation is checkable."""
    path = tmp_path / name
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    image.save(str(path))
    return path


def _pump(qapp, tries: int = 60) -> None:
    """Spin the loop so queued worker signals land. No condition to wait for
    on the H4 path - a decode that fails leaves nothing to observe changing -
    so this runs a bounded number of ticks rather than polling forever."""
    for _ in range(tries):
        qapp.processEvents()
        time.sleep(0.02)


def _settle(qapp, until, tries: int = 60) -> bool:
    for _ in range(tries):
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# is_image_result / IMAGE_RESULT_EXTS
# ---------------------------------------------------------------------------

def test_a_photo_extension_is_recognised():
    assert is_image_result("jpg")
    assert is_image_result(".PNG")            # dotted, upper-case - as typed
    assert "heic" in IMAGE_RESULT_EXTS


def test_a_non_photo_extension_is_not():
    assert not is_image_result("pdf")
    assert not is_image_result("txt")
    assert not is_image_result("")


def test_svg_is_excluded_even_though_ocr_reads_it():
    r"""`OcrExtractor.extensions` includes `.svg` (OCR can read text baked
    into one), but CLIP never embeds it - work order 0h §1a is the vision
    tower over ladder-passed *raster* images - so the grid must not offer a
    placeholder tile for every diagram in a result set."""
    assert not is_image_result("svg")


# ---------------------------------------------------------------------------
# decode_thumbnail - orientation and scaling, on a real file
# ---------------------------------------------------------------------------

def test_decode_thumbnail_scales_down_and_keeps_aspect(tmp_path):
    path = _png(tmp_path, width=400, height=100)
    image = decode_thumbnail(str(path), edge=200)
    assert image is not None
    assert max(image.width(), image.height()) <= 200
    # 4:1 aspect kept, within a pixel for integer rounding.
    assert abs(image.width() / image.height() - 4.0) < 0.1


def test_decode_thumbnail_applies_exif_orientation(tmp_path, monkeypatch):
    r"""A camera's orientation 6 means "rotate 90° clockwise to display
    upright" - a *wide* photo tagged that way must come back *tall*.
    `app.extract.exif.read_orientation` existed and was called from nowhere
    before this order; this is the first thing that calls it."""
    path = _png(tmp_path, width=400, height=100)
    monkeypatch.setattr("app.extract.exif.read_orientation", lambda _p: 6)
    image = decode_thumbnail(str(path), edge=200)
    assert image is not None
    assert image.height() > image.width()


def test_decode_thumbnail_returns_none_for_an_unreadable_file(tmp_path):
    """H4: an unreadable photo is a placeholder, never a crash - `None` is
    the ordinary answer, not an exception."""
    broken = tmp_path / "not_really_a.jpg"
    broken.write_bytes(b"this is not image data")
    assert decode_thumbnail(str(broken)) is None


def test_decode_thumbnail_returns_none_for_a_missing_file(tmp_path):
    assert decode_thumbnail(str(tmp_path / "does_not_exist.png")) is None


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ThumbnailGrid - what shows, and how it gets decoded
#
# Dated note, 2026-10-14 (review MS-6): the grid is drawn by the Photos page's
# pipeline now (`search_photos.py`: `ThumbLoader`, `PhotoModel`, the fade-in
# delegate), so the tests of its own cache, pool and generation stamp gave way
# to these, which ask the same questions of the new pipeline.
# ---------------------------------------------------------------------------

def _shown(grid, qapp):
    grid.resize(600, 400)
    grid.show()
    qapp.processEvents()


def test_show_rows_keeps_only_photos(qapp, tmp_path):
    grid = ThumbnailGrid()
    png = _png(tmp_path)
    rows = [_Row(str(png), "png"), _Row("notes.txt", "txt"), _Row("diagram.svg", "svg")]
    grid.show_rows(rows)
    assert grid.model.rowCount() == 1
    assert [row.ext for row in grid.image_rows()] == ["png"]


def test_image_rows_preserves_order_for_the_lightbox(qapp, tmp_path):
    r"""Work order 0h §3b: the grid's own ordering is the lightbox's sibling
    list, so it has to survive filtering in the order the result set had."""
    grid = ThumbnailGrid()
    rows = [_Row(str(tmp_path / f"{i}.jpg"), "jpg") for i in range(5)]
    grid.show_rows(rows)
    assert grid.image_rows() == rows


def test_one_picture_found_twice_is_one_cell(qapp, tmp_path):
    """Found by its words and by its look: two rows, one picture."""
    grid = ThumbnailGrid()
    path = str(tmp_path / "beach.jpg")
    grid.show_rows([_Row(path, "jpg"), _Row(path, "jpg")])
    assert grid.model.rowCount() == 1


def test_a_thumbnail_is_made_off_the_ui_thread_and_painted(qapp, tmp_path):
    grid = ThumbnailGrid()
    _shown(grid, qapp)
    png = _png(tmp_path, "painted.png")
    grid.show_rows([_Row(str(png), "png")])
    loader = grid.model._thumbs
    assert _settle(qapp, lambda: loader.pixmap(str(png)) is not None, tries=150), \
        "the thumbnail never arrived"
    grid.close()


@pytest.mark.slow
def test_a_photo_that_fails_to_decode_keeps_its_placeholder(qapp, tmp_path):
    """H4: never a crash, and never an empty cell either - the grey tile stays."""
    from PySide6.QtCore import Qt

    grid = ThumbnailGrid()
    _shown(grid, qapp)
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a real jpeg")
    grid.show_rows([_Row(str(broken), "jpg")])
    loader = grid.model._thumbs
    assert _settle(qapp, lambda: str(broken) in loader._failed, tries=150)
    decoration = grid.model.data(grid.model.index(0, 0), Qt.ItemDataRole.DecorationRole)
    assert decoration.cacheKey() == grid.model.placeholder.cacheKey()
    grid.close()


@pytest.fixture()
def asked(monkeypatch):
    """Every thumbnail the Search pictures' loader is asked for, without decoding."""
    from app.ui.widgets import photo_thumbs

    seen: list = []
    monkeypatch.setattr(photo_thumbs.ThumbLoader, "request",
                        lambda self, path, size, mtime: seen.append(path))
    return seen


def test_a_hidden_grid_asks_for_nothing_until_shown(qapp, asked, tmp_path):
    """`rows_changed` fires on every repaint of the list, and the grid is off
    by default - so a grid nobody sees must make no thumbnails."""
    grid = ThumbnailGrid()
    rows = [_Row(str(tmp_path / "a.jpg"), "jpg"), _Row(str(tmp_path / "b.jpg"), "jpg")]
    grid.show_rows(rows)
    _pump(qapp, tries=5)
    assert asked == []
    _shown(grid, qapp)
    _pump(qapp, tries=5)
    assert sorted(set(asked)) == sorted(row.path for row in rows)
    grid.close()


def test_only_the_cells_on_screen_and_one_screen_ahead_are_asked_for(qapp, asked, tmp_path):
    """Hundreds of photos in one set used to be queued at once."""
    grid = ThumbnailGrid()
    grid.resize(520, 300)
    grid.show()
    rows = [_Row(str(tmp_path / f"{n}.jpg"), "jpg") for n in range(300)]
    grid.show_rows(rows)
    _pump(qapp, tries=10)
    assert asked, "the cells on screen were asked for"
    assert len(set(asked)) < 60, f"{len(set(asked))} asked for - the whole set, not a screen"
    grid.close()


def test_the_same_photos_again_do_not_reset_the_grid(qapp, tmp_path):
    """A tier swap, a details redraw, a chevron: the same photos, nothing new."""
    grid = ThumbnailGrid()
    resets: list = []
    grid.model.modelReset.connect(lambda: resets.append(1))
    path = str(tmp_path / "a.jpg")
    grid.show_rows([_Row(path, "jpg")])
    grid.show_rows([_Row(path, "jpg"), _Row("notes.txt", "txt")])
    assert len(resets) == 1


def test_decodes_run_on_the_search_pictures_own_pool(qapp):
    """Fifty photos used to queue on the global pool ahead of the next search
    and every Open."""
    from PySide6.QtCore import QThreadPool

    from app.ui.widgets.search_photos import DECODE_THREADS, decode_pool

    grid = ThumbnailGrid()
    pool = grid.model._thumbs._pool
    assert pool is decode_pool() and pool is not QThreadPool.globalInstance()
    assert pool.maxThreadCount() == DECODE_THREADS


def test_the_placeholder_is_the_photos_pages_grey_tile(qapp):
    """MS-6: not the generic file icon the grid showed while it waited."""
    from app.ui.widgets.face_crops import blank_tile
    from app.ui.widgets.thumbnail_grid import THUMB_CELL

    grid = ThumbnailGrid()
    assert grid.model.placeholder.cacheKey() == blank_tile(THUMB_CELL - 20).cacheKey()


def test_the_thumbnails_go_in_a_folder_of_their_own(tmp_path):
    from types import SimpleNamespace

    from app.ui.widgets.search_photos import _thumbs_dir

    store = SimpleNamespace(db_path=str(tmp_path / "leasha.db"))
    assert _thumbs_dir(store) == tmp_path / "thumbs" / "search"
    assert _thumbs_dir(None) is None


# ---------------------------------------------------------------------------
# Review 2026-10-14, MS-5: nothing to show says so
# ---------------------------------------------------------------------------

def test_a_set_with_no_pictures_shows_the_empty_state(qapp, tmp_path):
    from app.ui.widgets.thumbnail_grid import NO_PHOTOS

    grid = ThumbnailGrid()
    grid.show_rows([_Row("notes.txt", "txt")])
    assert grid._pages.currentWidget() is grid.empty
    assert grid.empty.text() == NO_PHOTOS
    grid.show_rows([_Row(str(tmp_path / "a.jpg"), "jpg")])
    assert grid._pages.currentWidget() is grid._list


# ---------------------------------------------------------------------------
# Review 2026-10-14, MS-2: the box's keys reach the grid
# ---------------------------------------------------------------------------

def test_down_from_the_box_selects_then_moves_and_enter_opens_the_lightbox(qapp, tmp_path):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    grid = ThumbnailGrid()
    _shown(grid, qapp)
    rows = [_Row(str(tmp_path / f"{n}.jpg"), "jpg") for n in range(6)]
    grid.show_rows(rows)
    qapp.processEvents()
    down = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier)
    grid.forward_key(down)
    assert grid.current_row() == rows[0], "the first press lands on the first photo"
    grid.forward_key(down)
    assert grid.current_row() == rows[1]
    opened: list = []
    grid.opened.connect(lambda row, siblings: opened.append((row, siblings)))
    assert grid.open_current() is True
    assert opened == [(rows[1], rows)]
    grid.close()


# ---------------------------------------------------------------------------
# The off switch - off by default, work order §3a
# ---------------------------------------------------------------------------

def test_enabled_checkbox_defaults_off_with_no_store(qapp):
    box = enabled_checkbox(None, on_toggle=lambda _checked: None)
    assert not box.isChecked()


def test_enabled_checkbox_reads_a_stored_on_state(qapp):
    from types import SimpleNamespace

    store = SimpleNamespace(get_state=lambda key, default=None: "on")
    box = enabled_checkbox(store, on_toggle=lambda _checked: None)
    assert box.isChecked()


def test_enabled_checkbox_uses_its_own_key(qapp):
    seen: list = []
    store_stub = type("Store", (), {
        "get_state": lambda self, key, default=None: seen.append(key) or default,
    })()
    enabled_checkbox(store_stub, on_toggle=lambda _checked: None)
    assert seen == [GRID_ENABLED_KEY]
