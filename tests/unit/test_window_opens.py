r"""The window is built, for real, and survives a turn of the event loop.

Layer: L5

**Nothing had ever constructed `MainWindow`.** Every other UI test builds one
view, or greps a source file, and the suite was green while the application
could not open at all:

    UnboundLocalError: cannot access local variable 'QTimer'
    NameError: name '_read_external_run' is not defined

Two faults, both in `MainWindow.__init__`, both found by the owner typing
`leasha` and neither by 2,000 passing tests. That is not a gap in what the tests
assert - it is a gap in what they *run*.

The `QTimer` one is worth stating in full because it is a trap rather than a
typo. `__init__` used `QTimer` at line 207; four hundred lines later, still
inside the same function, sat a redundant `from PyQt6.QtCore import QTimer`.
Python binds names per **function**, not per line, so that import made `QTimer`
local for the whole of `__init__` and the earlier use referred to a variable
that did not exist yet. The import had been harmless for months and became fatal
the moment somebody used the same name earlier in the function.

`test_ui_never_blocks.test_no_function_reimports_a_name_the_module_already_has`
now refuses that shape anywhere in `app/`. This file is the other half: it
proves the thing actually starts.

**Deliberately not a screenshot test.** It cannot tell whether the window looks
right - that needs eyes, and the owner's are the ones that matter. What it can
tell is whether every name resolves, every signal connects, every widget builds
and nothing throws on the first turns of the event loop, which is the whole of
what these two bugs were.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PyQt6")


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
    """Enough of a `SearchEngine` to build a window around.

    The real one loads two ONNX models. Nothing here searches; what is under
    test is whether the window assembles, so paying 130MB of downloads for it
    would make this the test nobody runs.
    """

    def __init__(self, store) -> None:
        self.store = store

    def warm_up(self) -> None:
        pass


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    r"""**One window, shared.** Building four crashed the interpreter.

    Constructing and tearing down `MainWindow` repeatedly in one process
    segfaults inside Qt - a window owns threads, timers and a tray icon, and
    Python's garbage collector does not destroy the C++ side in the order Qt
    expects. That is a property of the test harness rather than of the
    application, which builds exactly one window and keeps it.

    Module-scoped, so the expensive part happens once and each test below is an
    assertion about the same live window.
    """
    from PyQt6.QtWidgets import QApplication

    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path_factory.mktemp("window")
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance() or QApplication([])
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()
    built = MainWindow(settings, store, vectors, _Engine(store), debug=False)

    yield app, built

    # Deliberately not closing the window: see the docstring. The process is
    # ending anyway, and tearing it down is what crashes.
    store.close()
    vectors.close()


def test_the_window_can_be_constructed(window):
    r"""**The test that would have caught both.**

    `MainWindow.__init__` is four hundred lines wiring twenty widgets to each
    other. Every name it resolves, every signal it connects and every import it
    depends on is exercised by building it once - and nothing ever did.
    """
    _app, built = window

    assert built is not None
    assert built.isEnabled()


def test_the_window_survives_the_first_turns_of_the_event_loop(window):
    r"""Construction is not the whole of starting.

    `__init__` defers work with `QTimer.singleShot(0, ...)` - the background
    warm-up, and now the poll for a run another process is doing. Those fire on
    the first turn of the loop, which is *after* everything a construction-only
    test would see.
    """
    app, _built = window

    for _ in range(5):
        app.processEvents()


def test_every_tab_can_be_selected(window):
    r"""The Files tab crashed once and was never reproducible offscreen.

    This does not prove it cannot again - a crash inside Qt's own paint pass
    needs a real display - but selecting each page runs its `showEvent`, its
    first layout and any timer it starts, which is where three of this
    application's UI bugs have actually lived.
    """
    from PyQt6.QtWidgets import QTabWidget

    app, built = window
    tabs = built.findChild(QTabWidget)
    assert tabs is not None and tabs.count() > 0, "no tabs were built"

    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        app.processEvents()
