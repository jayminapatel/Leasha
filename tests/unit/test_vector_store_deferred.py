"""Work order 0r item 2b: the vector store's connection can be made after the
window is on screen.

Importing LanceDB is the single largest cost between the splash and the window
(multi-second: it builds hundreds of pydantic models). A store built with
`deferred=True` therefore does nothing on `__enter__`; `warm()` connects on a
background thread, and *any* use before that finishes waits for it - so a search
typed the instant the window appears sees the same store it always did.
"""

from __future__ import annotations

import pytest

from app.core.errors import AppErrorException
from app.storage.vector_store import ImageVectorStore, VectorStore

pytest.importorskip("lancedb")

DIM = 8


def _batch(n=3):
    return list(range(1, n + 1)), list(range(1, n + 1)), [[0.1 * (i + 1)] * DIM for i in range(n)]


def test_deferred_enter_does_not_connect(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM, deferred=True) as store:
        assert store.connected is False


def test_warm_connects_in_the_background_and_wait_joins_it(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM, deferred=True) as store:
        store.warm()
        store.wait()
        assert store.connected is True
        assert store.deferred_error() is None


def test_first_use_without_warm_connects_on_demand(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM, deferred=True) as store:
        chunk_ids, file_ids, vectors = _batch()
        store.ensure_table()
        assert store.add(chunk_ids, file_ids, vectors) == 3
        assert store.count() == 3
        assert store.search([0.1] * DIM, k=2)


def test_use_right_after_warm_sees_the_existing_table(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM) as eager:
        eager.ensure_table()
        eager.add(*_batch())
    with VectorStore(tmp_path / "v", dim=DIM, deferred=True) as store:
        store.warm()
        assert store.exists is True          # waits for the connection, then answers
        assert store.count() == 3


def test_a_failed_deferred_connect_is_raised_at_first_use_and_reported(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM) as eager:
        eager.ensure_table()
        eager.add(*_batch())
    # Same directory, different width: the dimension check refuses it.
    with VectorStore(tmp_path / "v", dim=DIM + 4, deferred=True) as store:
        store.warm()
        store.wait()
        assert isinstance(store.deferred_error(), AppErrorException)
        with pytest.raises(AppErrorException):
            store.count()


def test_image_store_defers_too_and_close_after_warm_is_safe(tmp_path):
    with ImageVectorStore(tmp_path / "v", dim=DIM, deferred=True) as store:
        assert store.connected is False
        store.warm()
    # Leaving the block joined the connection before dropping it.
    assert store.connected is False


def test_non_deferred_store_is_unchanged(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        assert store.connected is True


# -- app.main: a store that will not open is reported once, on the window --------


class _FakeStore:
    def __init__(self, *, error=None, connected=False):
        self._error, self.connected = error, connected

    def deferred_error(self):
        return self._error


class _FakeWindow:
    def __init__(self, qtbot):
        from PySide6.QtWidgets import QWidget

        self.widget = QWidget()
        qtbot.addWidget(self.widget)
        self.shown = []

    def _show_error(self, error):
        self.shown.append(error)


def test_a_store_that_fails_to_open_is_shown_once_on_the_window(qtbot, monkeypatch):
    from app import main as app_main
    from PySide6.QtCore import QObject

    window = _FakeWindow(qtbot)
    monkeypatch.setattr(app_main, "log_app_error", lambda *_a, **_k: None)
    boom = AppErrorException(__import__("app.core.errors", fromlist=["make_error"]).make_error(
        "ERR_CONFIG_INVALID", "storage.vectors", key="VECTOR_PATH", reason="could not open"))
    # QTimer(parent) needs a QObject parent; the helper is given the window.
    app_main._watch_vector_connect(window.widget, (_FakeStore(error=boom),))
    window.widget._show_error = window._show_error
    qtbot.waitUntil(lambda: bool(window.shown), timeout=3000)
    qtbot.wait(600)
    assert len(window.shown) == 1
    assert isinstance(window.shown[0], QObject) is False


def test_nothing_is_shown_when_every_store_connects(qtbot):
    from app import main as app_main

    window = _FakeWindow(qtbot)
    window.widget._show_error = window._show_error
    app_main._watch_vector_connect(window.widget, (_FakeStore(connected=True),))
    qtbot.wait(600)
    assert window.shown == []


# -- work order 0r item 2b: the window asks for the assets folder ~40 times -----


def test_assets_dir_is_worked_out_once_not_per_icon(monkeypatch):
    from pathlib import Path

    from app.ui import tray

    tray.assets_dir()                        # warm: first answer may resolve
    calls = []
    real = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: (calls.append(1), real(self, *a, **k))[1])
    for _ in range(40):
        tray.assets_dir()
    assert calls == []


# -- a window opened before the index created the table -------------------------
#
# 1 October 2026: the window opened at 23:03, an index run created
# `chunks.lance` at 00:00, and every search for the next eight hours came back
# keyword-only - "Searching by meaning is off" - with nothing in the log but
# "no vector hits". `search` answered from the `_table is None` it saw at open
# and never looked again. Headless searches, which open the store fresh, were
# fine throughout, which is what made it look like a fault in the index.


def test_a_store_opened_before_the_table_existed_finds_rows_added_later(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM) as reader:
        assert reader.search([0.1] * DIM, k=2) == []          # nothing yet: normal
        with VectorStore(tmp_path / "v", dim=DIM) as writer:  # the index run
            writer.ensure_table()
            writer.add(*_batch())
        assert reader.count() == 3
        assert len(reader.search([0.1] * DIM, k=2)) == 2


def test_a_store_held_open_sees_rows_another_process_adds(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM) as first:
        first.ensure_table()
        first.add(*_batch(3))
    with VectorStore(tmp_path / "v", dim=DIM) as reader:
        assert reader.count() == 3
        with VectorStore(tmp_path / "v", dim=DIM) as writer:
            writer.add([4, 5], [4, 5], [[0.9] * DIM, [0.95] * DIM])
        assert reader.count() == 5
        assert len(reader.search([0.9] * DIM, k=10)) == 5
