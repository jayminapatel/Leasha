r"""`open_row_async` - Offline Media 1b/3a/3c's open resolution, shared.

Layer: L5

`shell._open_volume_result` proved the resolve-then-open pattern for search
results; `files_view.py` needed the identical thing for a row browsed to by
`/on` (order 202626270513 section 3c) and would otherwise have duplicated it
a second time. These tests prove the one function routes correctly rather
than re-proving `resolve_open_path`/`open_in_explorer` themselves, which
already have their own tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")


def _app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _pump(ms: int = 3_000) -> None:
    from PyQt6.QtCore import QThreadPool

    app = _app()
    QThreadPool.globalInstance().waitForDone(ms)
    for _ in range(5):
        app.processEvents()


def test_none_row_does_nothing(monkeypatch):
    from app.ui import workers

    called = []
    monkeypatch.setattr(workers, "open_async", lambda *a, **k: called.append((a, k)))

    workers.open_row_async(store=None, row=None)
    assert called == []


def test_an_ordinary_row_goes_straight_to_open_async(monkeypatch):
    """No `volume_id` at all: the pre-existing path, untouched."""
    from app.ui import workers

    called = []
    monkeypatch.setattr(
        workers, "open_async",
        lambda path, **k: called.append((path, k)))

    row = SimpleNamespace(path=r"D:\a\report.pdf", volume_id=None)
    workers.open_row_async(None, row, reveal=True, component="ui.test")

    assert len(called) == 1
    path, kwargs = called[0]
    assert path == r"D:\a\report.pdf"
    assert kwargs["reveal"] is True
    assert kwargs["component"] == "ui.test"


def test_a_volume_row_resolves_before_opening(monkeypatch):
    """A catalogued-volume row must never reach `open_async` with its
    synthetic path - it goes through `resolve_open_path` first, on a
    worker, exactly as `shell._open_volume_result` already does."""
    from app.ui import presenter, workers

    resolved_calls = []
    opened_calls = []

    def _fake_resolve(store, row):
        resolved_calls.append((store, row))
        return r"E:\reports\q3.txt"

    def _fake_open_in_explorer(path, *, select):
        opened_calls.append((path, select))
        return None

    monkeypatch.setattr(presenter, "resolve_open_path", _fake_resolve)
    monkeypatch.setattr(workers, "open_in_explorer", _fake_open_in_explorer)

    row = SimpleNamespace(
        path="leasha-volume://1/reports/q3.txt", volume_id=1,
        relative_path="reports/q3.txt",
    )
    store = object()
    errors = []
    workers.open_row_async(store, row, reveal=True, on_error=errors.append)
    _pump()

    assert resolved_calls == [(store, row)]
    assert opened_calls == [(r"E:\reports\q3.txt", True)]
    assert errors == [] or errors == [None]


def test_a_volume_row_that_cannot_resolve_reports_the_error(monkeypatch):
    """The volume is not connected: `resolve_open_path` raises (see its own
    docstring), and the worker's failure path is what the caller's
    `on_error` sees - never a silent nothing."""
    from app.core.errors import AppErrorException, make_error
    from app.ui import presenter, workers

    def _fake_resolve(store, row):
        raise AppErrorException(make_error(
            "ERR_FILE_CORRUPT", "ui.open",
            suggestion="Plug it in and try again.",
        ))

    monkeypatch.setattr(presenter, "resolve_open_path", _fake_resolve)

    row = SimpleNamespace(
        path="leasha-volume://1/reports/q3.txt", volume_id=1,
        relative_path="reports/q3.txt",
    )
    errors = []
    workers.open_row_async(object(), row, on_error=errors.append)
    _pump()

    assert len(errors) == 1
    assert errors[0].code == "ERR_FILE_CORRUPT"
