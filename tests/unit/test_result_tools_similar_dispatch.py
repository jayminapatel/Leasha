r"""Work order 0h §2d: "more like this" dispatches to the right backend.

`result_tools._wire_similar` used to route every right-click "more like
this" to `SearchEngine.similar_to` unconditionally - correct for a text
result, silently wrong for a photo (it reads the text `VectorStore`, so a
photo row either finds nothing or, on an id coincidence, returns an
unrelated passage's neighbours). Two independent, concurrent sessions each
built one half of the real fix and named the other's missing half as a gap;
once both existed, closing it was a one-line dispatch on `row.ext`. These
tests pin that dispatch down directly, at the smallest scope that can prove
it, rather than only through a full `ResultsView`/`ThumbnailGrid` build.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QApplication

from app.ui.widgets.result_tools import _wire_similar


class _Row:
    def __init__(self, chunk_id: int, ext: str) -> None:
        self.chunk_id = chunk_id
        self.ext = ext


def _fake_view_class():
    from PyQt6.QtCore import QObject

    class _Fake(QObject):
        similar_requested = pyqtSignal(object)

        def show_results(self, *args, **kwargs) -> None:
            pass

    return _Fake


class _FakeEngine:
    def __init__(self) -> None:
        self.similar_to_calls: list[int] = []
        self.find_similar_images_calls: list[int] = []

    def similar_to(self, chunk_id: int, *, limit: int = 20):
        self.similar_to_calls.append(chunk_id)
        return _empty_response()

    def find_similar_images(self, file_id: int, *, limit: int = 20):
        self.find_similar_images_calls.append(file_id)
        return _empty_response()


def _empty_response():
    class _Response:
        results: list = []

    return _Response()


@pytest.fixture()
def app():
    return QApplication.instance() or QApplication([])


def _pump(app, seconds: float = 1.0) -> None:
    import time

    from PyQt6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(int(seconds * 1000))
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()


def test_a_text_row_calls_similar_to(app):
    Fake = _fake_view_class()
    results, grid = Fake(), Fake()
    engine = _FakeEngine()

    _wire_similar(results=results, grid=grid, engine=engine, on_error=None)

    row = _Row(chunk_id=7, ext="pdf")
    results.similar_requested.emit(row)
    _pump(app)

    assert engine.similar_to_calls == [7]
    assert engine.find_similar_images_calls == []


def test_a_photo_row_calls_find_similar_images(app):
    Fake = _fake_view_class()
    results, grid = Fake(), Fake()
    engine = _FakeEngine()

    _wire_similar(results=results, grid=grid, engine=engine, on_error=None)

    row = _Row(chunk_id=42, ext="jpg")
    results.similar_requested.emit(row)
    _pump(app)

    assert engine.find_similar_images_calls == [42]
    assert engine.similar_to_calls == []


def test_the_grid_signal_dispatches_the_same_way(app):
    """`grid.similar_requested` is the thumbnail-grid's own copy of the same
    signal (a photo pinned out of the grid, not the list) - must dispatch
    identically, not just the list's."""
    Fake = _fake_view_class()
    results, grid = Fake(), Fake()
    engine = _FakeEngine()

    _wire_similar(results=results, grid=grid, engine=engine, on_error=None)

    row = _Row(chunk_id=9, ext="png")
    grid.similar_requested.emit(row)
    _pump(app)

    assert engine.find_similar_images_calls == [9]
