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

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QImage                                  # noqa: E402
from PyQt6.QtWidgets import QApplication                        # noqa: E402

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
# ThumbnailGrid - what shows, and how it gets decoded
# ---------------------------------------------------------------------------

def test_show_rows_keeps_only_photos(qapp, tmp_path):
    grid = ThumbnailGrid()
    png = _png(tmp_path)
    rows = [_Row(str(png), "png"), _Row("notes.txt", "txt"), _Row("diagram.svg", "svg")]
    grid.show_rows(rows)
    assert grid._list.count() == 1
    assert [row.ext for row in grid.image_rows()] == ["png"]


def test_image_rows_preserves_order_for_the_lightbox(qapp, tmp_path):
    r"""Work order 0h §3b: the grid's own ordering is the lightbox's sibling
    list, so it has to survive filtering in the order the result set had."""
    grid = ThumbnailGrid()
    rows = [_Row(f"{i}.jpg", "jpg") for i in range(5)]
    grid.show_rows(rows)
    assert grid.image_rows() == rows


def test_a_thumbnail_is_decoded_off_the_ui_thread_and_painted(qapp, tmp_path):
    # Dated note, 2026-10-04, code review: shown first - a hidden grid
    # decodes nothing (`test_a_hidden_grid_decodes_nothing_until_shown`).
    grid = ThumbnailGrid()
    grid.show()
    png = _png(tmp_path)
    grid.show_rows([_Row(str(png), "png")])
    placeholder_key = grid._list.item(0).icon().cacheKey()

    landed = _settle(
        qapp, lambda: grid._list.item(0).icon().cacheKey() != placeholder_key)
    assert landed, "the decoded thumbnail never replaced the placeholder icon"


def test_a_photo_that_fails_to_decode_keeps_its_placeholder(qapp, tmp_path):
    """H4: never a crash, and never an empty cell either - the placeholder
    icon set in `show_rows` stays exactly where it was."""
    grid = ThumbnailGrid()
    grid.show()                                 # dated note, 2026-10-04: see above
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a real jpeg")
    grid.show_rows([_Row(str(broken), "jpg")])
    placeholder_key = grid._list.item(0).icon().cacheKey()

    _pump(qapp)                                 # let the failed decode land
    assert grid._list.item(0).icon().cacheKey() == placeholder_key


def test_a_second_show_rows_drops_a_stale_decode(qapp, tmp_path):
    r"""The generation stamp: a slow decode for a photo that has since
    scrolled out of an *earlier* result set must never paint over a cell
    that now belongs to something else - the same discipline `render_page`'s
    callers already use."""
    grid = ThumbnailGrid()
    first = _png(tmp_path, "first.png")
    grid.show_rows([_Row(str(first), "png")])
    first_generation = grid._generation

    grid.show_rows([])                          # a new, empty result set
    # A late arrival stamped with the old generation must be ignored.
    grid._thumbnail_ready(0, QImage(10, 10, QImage.Format.Format_RGB32), first_generation)
    assert grid._list.count() == 0


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


# ---------------------------------------------------------------------------
# 2026-10-04, code review: decode only while shown, once, on a pool of its own
# ---------------------------------------------------------------------------

@pytest.fixture()
def decodes(monkeypatch):
    """Every path handed to the decoder, recorded; the grids' cache emptied."""
    import threading

    from app.ui.widgets import thumbnail_grid

    seen: list = []
    thumbnail_grid._CACHE.clear()

    def fake(path, **_kwargs):
        seen.append((path, threading.current_thread() is threading.main_thread()))
        return QImage(8, 8, QImage.Format.Format_RGB32)

    monkeypatch.setattr(thumbnail_grid, "decode_thumbnail", fake)
    yield seen
    thumbnail_grid._CACHE.clear()


def _wait_for_decodes(qapp):
    from app.ui.widgets.thumbnail_grid import decode_pool

    decode_pool().waitForDone(3_000)
    _pump(qapp, tries=5)


def test_a_hidden_grid_decodes_nothing_until_shown(qapp, decodes):
    """`rows_changed` fires on every repaint of the list, and the grid is off
    by default - so each repaint decoded every photo for a grid nobody saw."""
    grid = ThumbnailGrid()
    grid.show_rows([_Row("a.jpg", "jpg"), _Row("b.jpg", "jpg")])
    _wait_for_decodes(qapp)
    assert decodes == []

    grid.show()
    _wait_for_decodes(qapp)
    assert sorted(decodes) == [("a.jpg", False), ("b.jpg", False)], "on a worker, once each"
    grid.close()


def test_the_same_photos_again_are_not_decoded_or_redrawn_again(qapp, decodes):
    """A tier swap, a details redraw, a chevron: the same photos, nothing new."""
    grid = ThumbnailGrid()
    grid.show()
    rows = [_Row("a.jpg", "jpg")]
    grid.show_rows(rows)
    _wait_for_decodes(qapp)
    first = grid._list.item(0)
    grid.show_rows([_Row("a.jpg", "jpg"), _Row("notes.txt", "txt")])
    _wait_for_decodes(qapp)
    assert decodes == [("a.jpg", False)]
    assert grid._list.item(0) is first, "the cell was not rebuilt"
    grid.close()


def test_a_photo_decoded_once_comes_from_the_cache_after(qapp, decodes):
    grid = ThumbnailGrid()
    grid.show()
    grid.show_rows([_Row("a.jpg", "jpg")])
    _wait_for_decodes(qapp)
    grid.show_rows([])
    other = ThumbnailGrid()
    other.show()
    other.show_rows([_Row("a.jpg", "jpg")])
    _wait_for_decodes(qapp)
    assert decodes == [("a.jpg", False)], "the second time is the cache"
    grid.close()
    other.close()


def test_the_cache_key_is_the_rows_path_mtime_and_size_without_a_stat():
    from app.ui.widgets.thumbnail_grid import cache_key

    class Row(NamedTuple):
        path: str
        mtime_ns: int
        size_bytes: int

    assert cache_key(Row("a.jpg", 5, 9)) == ("a.jpg", 5, 9)
    assert cache_key(Row("a.jpg", 6, 9)) != cache_key(Row("a.jpg", 5, 9)), "changed file"


def test_decodes_queue_on_the_grids_own_pool_and_are_taken_back(qapp, monkeypatch):
    """Fifty photos used to queue on the global pool ahead of the next search
    and every Open, and a new result set cancelled none of them."""
    import threading

    from PyQt6.QtCore import QThreadPool

    from app.ui.widgets import thumbnail_grid

    thumbnail_grid._CACHE.clear()
    gate = threading.Event()
    started: list = []

    def slow(path, **_kwargs):
        started.append(path)
        gate.wait(5)
        return None

    monkeypatch.setattr(thumbnail_grid, "decode_thumbnail", slow)
    grid = ThumbnailGrid()
    grid.show()
    assert grid._pool is not QThreadPool.globalInstance()
    grid.show_rows([_Row(f"{n}.jpg", "jpg") for n in range(8)])
    for _ in range(100):
        if len(started) >= thumbnail_grid.DECODE_THREADS:
            break
        time.sleep(0.01)
    grid.show_rows([])                          # a new result set
    gate.set()
    _wait_for_decodes(qapp)
    assert len(started) == thumbnail_grid.DECODE_THREADS, "the queued six never started"
    assert grid._queued == []
    grid.close()
