r"""Shared fixtures for pytest-qt scenario tests (order 0m S1a - the harness).

Layer: L0

**One real `MainWindow`, built once per test module and kept for its whole
lifetime.** The same rule `test_window_opens.py` and `test_ui_redesign_qt.py`
already state, for the same reason: building and tearing `MainWindow` down
repeatedly in one process crashes inside Qt - a window owns threads, timers
and a tray icon, and Python's garbage collector does not destroy the C++
side in the order Qt expects.

`qtbot` (pytest-qt) is requested separately, function-scoped, by each test
that drives it - it never has to build the window itself, only send it
keystrokes and clicks. A module-scoped fixture and a function-scoped one can
coexist freely as long as neither is built *from* the other.

**Real store, real engine, real window - only the embedding model is a
stub.** `SearchEngine`'s `embedder`/`vectors` are duck-typed (see
`app/storage/filters.py`'s own note on the same technique), so a fake that
raises on `.embed()` keeps every scenario keyword-only and offline without
touching `MainWindow`, `SearchView` or the presenter at all - the same
`_NoVectors`/`_NoModel` idiom `test_mini_search.py` already uses, repeated
here because `app/search/vector.py` catches exactly that exception and
degrades to keyword-only rather than crashing (`app/search/vector.py:99-112`).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

import os                                                       # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


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

#: A handful of real, keyword-findable documents. Chosen so every scenario
#: below has something real to search for: "barnsley" for a plain
#: type-and-see-results journey, "invoice" for the `/` popup, one `pdf`
#: extension for a `type:` filter scenario.
GUI_DOCUMENTS = (
    ("barnsley-report.txt", "txt", "the barnsley site survey report"),
    ("newcastle-invoice.pdf", "pdf", "newcastle invoice and receipt bundle"),
    ("leeds-notes.txt", "txt", "leeds commissioning notes, draft"),
)


class _NoVectors:
    def search(self, *_a, **_k):
        return []


class _NoModel:
    def embed(self, _t):
        raise RuntimeError("no model - gui scenario tests stay keyword-only")

    def embed_all(self, _t):
        raise RuntimeError("no model - gui scenario tests stay keyword-only")

    def warm_up(self) -> None:
        pass


class _FakeReranker:
    """Enough of `app.search.reranker.Reranker` for `_rerank_toggled` to have
    something real to flip. `enabled` is the one attribute `shell.py`'s
    handler ever touches (`reranker.enabled = bool(enabled)`)."""

    def __init__(self) -> None:
        self.enabled = False

    def warm_up(self) -> None:
        pass


@pytest.fixture(scope="module")
def gui_mainwindow(tmp_path_factory):
    """`(app, window, store, engine)` - see the module docstring."""
    from PyQt6.QtWidgets import QApplication

    from app.core.config import load_settings
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path_factory.mktemp("gui")
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance() or QApplication([])
    store = SqliteStore(settings.fts_db).connect()
    for name, ext, text in GUI_DOCUMENTS:
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext=ext, size_bytes=1,
            mtime_ns=1, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])

    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()
    engine = SearchEngine(store, _NoVectors(), _NoModel(), reranker=_FakeReranker())
    window = MainWindow(settings, store, vectors, engine, debug=False)
    for _ in range(5):
        app.processEvents()

    yield app, window, store, engine

    # Deliberately not closing the window - see `test_window_opens.py`. The
    # process is ending anyway, and tearing it down is what crashes.
    engine.close()
    store.close()
    vectors.close()


def gui_pump(app, n: int = 5) -> None:
    for _ in range(n):
        app.processEvents()


def gui_row_count(results_view) -> int:
    """`ResultsView` has no public row count - it is a `QListView` over a
    private `_model`, not a `QTableWidget`. Reaching into `_rows` is the same
    thing `test_ui_redesign_qt.py` already does for `rail._buttons`."""
    return len(results_view._rows)


def gui_select_row(results_view, row: int) -> None:
    results_view._list.setCurrentIndex(results_view._model.index(row, 0))
