r"""A run started with no total counts the corpus itself, in the background.

Layer: L5

Reported live: *"the file count was way less than it should have been ... it
seemed stuck, it was ever at 680."* A GUI-started run over folders nobody had
run the Scan button on had no denominator, and `_scan_total` reusing a stale
scan of a different-sized corpus made the ETA claim "done" the moment the
walker's own count passed it - see `test_long_run_reporting.
test_a_scan_that_undercounted_does_not_make_the_eta_say_done`.

`_start_indexing` now starts the same walk the Scan button runs, on its own,
whenever `_scan_total` comes back empty - so nobody has to remember to click
it first, and `IndexingView.update_total_estimate` feeds the result to
whichever run is still going. These tests exercise the wiring: the resolve
step and the index worker itself are replaced with spies, exactly as
`test_start_indexing_resolves_off_thread.py` already does for the same window.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from app.core.config import load_settings
from app.index.resolve import Resolved
from app.storage.sqlite_store import SqliteStore
from app.storage.vector_store import VectorStore
from app.ui.shell import MainWindow

ENV = """\
DATA_PATH={d}
VECTOR_PATH={d}/vectors
FTS_DB={d}/fts/knowledge.db
CACHE_PATH={d}/cache
MODEL_CACHE={d}/models
STATE_PATH={d}/state
PROJECT_PATH={d}
LOG_PATH={d}/logs

EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
RERANK_MODEL=BAAI/bge-reranker-base
RERANK_ENABLED=false

OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mistral

MIN_FREE_GB=1
REQUIRED_FREE_GB=1
"""


class _Engine:
    def __init__(self, store) -> None:
        self.store = store

    def warm_up(self) -> None:
        pass

    def close(self) -> None:
        pass


def _window(tmp_path):
    from PyQt6.QtWidgets import QApplication

    root = tmp_path / "window"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance() or QApplication([])
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()
    built = MainWindow(settings, store, vectors, _Engine(store))
    return app, built, store, vectors


def _pump(app, ms: int = 5_000) -> None:
    from PyQt6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(ms)
    for _ in range(5):
        app.processEvents()


def _fast_resolve(monkeypatch):
    import app.index.resolve as resolve_module

    monkeypatch.setattr(
        resolve_module, "resolve_for_run",
        lambda settings, store: Resolved(workers=1, onnx_threads=1, embed_batch=8),
    )


# ---------------------------------------------------------------------------
# No scan for these folders: a background count starts on its own
# ---------------------------------------------------------------------------

def test_a_run_with_no_saved_scan_starts_one_in_the_background(tmp_path, monkeypatch):
    app, built, store, vectors = _window(tmp_path)
    _fast_resolve(monkeypatch)
    built.indexing_view.start = lambda pipeline, **kw: None
    started: list = []
    monkeypatch.setattr(built, "_start_background_scan", lambda roots: started.append(roots))
    try:
        folder = tmp_path / "corpus"
        folder.mkdir()
        built._start_indexing(roots=[str(folder)])
        _pump(app)

        assert started == [[str(folder)]], (
            "a run over folders nobody has scanned must count them itself"
        )
    finally:
        store.close()
        vectors.close()


def test_a_run_with_a_matching_saved_scan_does_not_rescan(tmp_path, monkeypatch):
    import json

    from app.index.scan import SCAN_STATE_KEY

    app, built, store, vectors = _window(tmp_path)
    _fast_resolve(monkeypatch)
    built.indexing_view.start = lambda pipeline, **kw: None
    started: list = []
    monkeypatch.setattr(built, "_start_background_scan", lambda roots: started.append(roots))
    try:
        folder = tmp_path / "corpus"
        folder.mkdir()
        store.set_states({SCAN_STATE_KEY: json.dumps({
            "at": 0, "roots": [str(folder)], "files": 42, "bytes": 1000,
        })})

        built._start_indexing(roots=[str(folder)])
        _pump(app)

        assert started == [], (
            "a folder already counted by a matching scan must not be counted twice"
        )
    finally:
        store.close()
        vectors.close()


# ---------------------------------------------------------------------------
# The background scan itself: off-thread, and feeds the running view
# ---------------------------------------------------------------------------

def test_the_background_scan_result_reaches_the_running_view(tmp_path, monkeypatch):
    import app.ui.shell as shell_module

    app, built, store, vectors = _window(tmp_path)
    monkeypatch.setattr(
        shell_module, "_scan_and_save",
        lambda store, roots: {"files": 12_345, "bytes": 999},
    )
    updates: list = []
    monkeypatch.setattr(built.indexing_view, "update_total_estimate", updates.append)
    try:
        folder = tmp_path / "corpus"
        folder.mkdir()
        built._start_background_scan([str(folder)])
        _pump(app)

        assert updates == [12_345]
    finally:
        store.close()
        vectors.close()


def test_a_scan_already_in_flight_is_not_started_twice(tmp_path, monkeypatch):
    """The Scan button and this automatic count share one signal: a disabled
    button means somebody - this or the button - is already counting."""
    import app.ui.shell as shell_module

    app, built, store, vectors = _window(tmp_path)
    ran: list = []
    monkeypatch.setattr(
        shell_module, "_scan_and_save",
        lambda store, roots: ran.append(roots) or {"files": 1, "bytes": 1},
    )
    built.indexing_view.scan_button.setEnabled(False)
    try:
        built._start_background_scan([str(tmp_path)])
        _pump(app)

        assert ran == [], "a scan already running must not be started a second time"
    finally:
        built.indexing_view.scan_button.setEnabled(True)
        store.close()
        vectors.close()


def test_the_background_scan_is_dispatched_to_a_worker_rather_than_inline():
    """Mirrors `test_start_indexing_resolves_off_thread`'s own technique: the
    source shape is what actually decides whether this can reach the UI
    thread, not a thread-identity assertion that can pass by accident."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "app" / "ui" / "shell.py"
           ).read_text(encoding="utf-8")
    body = text.split("def _start_background_scan(")[1].split("\n    def ")[0]

    assert "CallableWorker(_scan_and_save" in body
    assert "run(QThreadPool.globalInstance(), worker)" in body
