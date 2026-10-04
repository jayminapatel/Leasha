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


@pytest.fixture(scope="session", autouse=True)
def no_models_listed_or_loaded_unasked():
    """2026-10-04: a test window never lists Ollama's models or loads a chat model
    ahead of a question (`chat_controller.BACKGROUND_MODELS`) - the test env names
    the real `127.0.0.1:11434`, and a test must not need, or reach, a real Ollama.
    A test of those features turns it back on, or hands in a `menu_factory`."""
    from app.ui.controllers import chat_controller

    chat_controller.BACKGROUND_MODELS = False
    yield


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
    #
    # **But hidden, before its store closes.** A test that `show()`s it (the
    # Chat tab's fixture does) left it visible, and in Qt 6 any later
    # `app.quit()` in the same process - `test_later.py` pumps with one - asks
    # every *visible* window to close. That ran this window's `closeEvent`, whose
    # geometry save hit the store closed two lines below, and the
    # ERR_UNEXPECTED traceback failed whichever test happened to be running.
    # A hidden window is not asked.
    window.hide()
    engine.close()
    store.close()
    vectors.close()


@pytest.fixture(scope="module", autouse=True)
def windows_left_open_stop_watching_the_run_lock():
    r"""A window a finished test module leaves open stops polling the run lock.

    **Kept open, as `gui_mainwindow` explains - but not still asking.** Every
    `MainWindow` runs `_watch_timer`, which every four seconds sends a worker to
    `run_lock.is_indexing`, and that probe *takes the machine-wide index mutex*
    for an instant. Windows built by one module are never torn down, so on a
    full run a few dozen of them were still probing all through every later
    module - measured on Linux, 22 by `test_media_open.py`, from a spy on the
    live top-level widgets after each test (2026-09-29).

    On Windows the mutex name is machine-wide, so a `lock_dir` isolates
    nothing: an `IndexRunLock` or an `is_indexing` in `test_run_lock.py` that
    landed inside one of those probes was refused as "another process", one
    test at a time. `IndexRunLock` now waits a probe out
    (`run_lock.CONTENTION_WAIT_S`); a bare `is_indexing` cannot, because a
    probe is exactly what it is. So the probing stops with the module that
    built the window: its tests are over, and nothing after it may depend on a
    timer it cannot see. Only this timer - it is the one that reaches outside
    the process - and only stopped, never closed or deleted, for the reason in
    `gui_mainwindow`.

    Module-scoped and autouse, so it is torn down after the module's own
    fixtures: a `gui_mainwindow` has closed its store by then, and a poll
    started against a closed store is one more thing this ends.
    """
    yield
    from PyQt6.QtWidgets import QApplication

    if QApplication.instance() is None:
        return
    for widget in QApplication.topLevelWidgets():
        timer = getattr(widget, "_watch_timer", None)
        if timer is None:
            continue
        try:
            timer.stop()
        except RuntimeError:             # its C++ side is already gone
            pass


@pytest.fixture
def no_leaked_widgets():
    r"""Delete the top-level widgets *this test* built, and nothing else.

    **Opt-in, per test, and deliberately not autouse.** 2026-09-20: see the dated note
    in `test_no_application_stylesheet.py` for why the blanket version was not added.
    The short form: the leak is real and large - `test_tuning_screen.py` leaves 6
    top-level widgets and about 120 widgets behind on *every* test, measured - but the
    one walk that made it fatal is gone, and `gui_mainwindow` above says in as many
    words that tearing a `MainWindow` down mid-process is itself a crash.

    **Why the snapshot is what makes this safe.** pytest builds higher-scoped fixtures
    first, so by the time a function-scoped fixture runs, any module-scoped window -
    `gui_mainwindow`'s, or a module's own - already exists and is in `before`. Only
    widgets that appear afterwards are touched, so a fixture's window can never be
    taken out from under the tests that still need it.

    **What it still cannot know** is whether the test stashed one of its widgets in a
    module-level global for a later test to use. That is the case that turns a passing
    suite red, and it is why this is opt-in: a module adopts it after its own file
    passes with it, rather than all 349 files being changed at once on the strength of
    an argument.

        def test_something(no_leaked_widgets):
            ...
    """
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    # **The widgets, kept alive, not just their ids.** A widget Qt owns but
    # Python holds no reference to (a leaked menu, an earlier test's window)
    # gets a brand-new wrapper object from every `topLevelWidgets()` call. A
    # set of ids let those wrappers be freed at once, and the widgets this
    # test then built were given the same memory - the same ids - so they
    # looked as if they had been there before and were never deleted. That
    # only happens in a long run with such widgets about, which is why
    # `test_worker_signal_owner.py` failed in the full suite and passed alone.
    # Holding each wrapper in the dict keeps its id taken until the sweep.
    before = {} if app is None else {id(w): w for w in QApplication.topLevelWidgets()}
    yield
    if app is None:
        return
    for widget in list(QApplication.topLevelWidgets()):
        if id(widget) in before:
            continue
        widget.close()
        widget.deleteLater()
    # **`processEvents()` is not enough, and this is the trap worth knowing.**
    # `deleteLater` posts a `DeferredDelete`, and Qt deliberately holds those back until
    # the event loop *that posted them* returns - `processEvents` does not deliver them.
    # The obvious `deleteLater(); processEvents()` teardown therefore frees nothing at
    # all while looking exactly as though it does; `test_worker_signal_owner.py` pins
    # this, because it is the shape anyone writing this fixture reaches for first.
    from PyQt6.QtCore import QEvent

    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


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
