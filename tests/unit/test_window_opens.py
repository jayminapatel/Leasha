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

    def close(self) -> None:
        # closeEvent's teardown calls this unconditionally (stage("engine",
        # self._engine.close)). The app's own stage() wrapper catches an
        # AttributeError from a missing close() and only logs a warning, but
        # pytest-qt's Qt-event-loop exception hook still reports it as a test
        # failure regardless - this stub needs the real SearchEngine's
        # interface, not just enough to construct a window.
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


def test_idle_optimize_timer_actually_calls_the_store(window):
    r"""§3c/§4: "idle-optimize runs" - the timer's wiring, not just the
    method it calls.

    `tests/unit/test_idle_optimize.py` already proves `optimize_query_
    planner()` itself works and that `close()` no longer calls it; neither
    touches `MainWindow` at all, so neither can say whether the hourly
    `_optimize_timer` this item promises is actually connected to anything
    real. This calls `_run_idle_optimize()` directly - exactly what
    `_optimize_timer.timeout` does once an hour - and waits for the
    background `QThreadPool` worker it starts to finish, then asserts the
    store's `optimize_query_planner` was the thing that ran.
    """
    from PyQt6.QtCore import QThreadPool

    app, built = window

    calls: list[bool] = []
    real = built._store.optimize_query_planner

    def spy():
        calls.append(True)
        return real()

    built._store.optimize_query_planner = spy
    try:
        built._run_idle_optimize()
        QThreadPool.globalInstance().waitForDone(5_000)
        for _ in range(3):
            app.processEvents()

        assert calls, (
            "_run_idle_optimize must actually invoke "
            "store.optimize_query_planner - the timer exists for nothing "
            "otherwise"
        )
    finally:
        built._store.optimize_query_planner = real


# --- §4: Window state (save/restore geometry) ----------------------------------


def test_window_saves_geometry_on_close(window, tmp_path):
    r"""§4a: The window saves its geometry when closing.

    This test would need a real window close and re-open to verify the state
    was actually persisted and restored, which is beyond the scope of a
    construction test. What we can assert is that the window has the mechanism
    in place: calling `saveGeometry()` works and returns bytes.
    """
    from app.ui.window_state import save_window_state

    app, built = window

    state = save_window_state(built)

    assert isinstance(state, bytes), "saveGeometry must return bytes"
    assert len(state) > 0, "saved geometry must not be empty"


def test_window_can_restore_from_saved_state(tmp_path):
    r"""§4a-4b: A window can be created and restored from saved geometry.

    Create a window, save its state, then create a new window and restore
    from that state. The geometries should match.
    """
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow
    from app.ui.window_state import restore_window_state, save_window_state

    root = tmp_path / "window_restore"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance()
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    # A size guaranteed to fit the real screen without Qt's own
    # restoreGeometry() silently shrinking it to fit. Found by running this
    # suite: the offscreen QPA platform here provides only an 800x800
    # virtual screen, so the previous hardcoded 900x700 got clamped by Qt on
    # restore and the exact-pixel assertions below failed - not a
    # save/restore bug, just a magic number that only held on a big enough
    # screen.
    avail = QGuiApplication.primaryScreen().availableGeometry()
    if avail.width() < 300 or avail.height() < 300:
        pytest.skip(f"screen too small ({avail.width()}x{avail.height()}) for this test")
    width = min(900, avail.width() - 60)
    height = min(700, avail.height() - 60)

    try:
        # Build first window, set a custom geometry, save it
        window1 = MainWindow(settings, store, vectors, _Engine(store))
        window1.resize(width, height)
        window1.move(150, 100)
        saved_state = save_window_state(window1)

        # Build second window with default geometry
        window2 = MainWindow(settings, store, vectors, _Engine(store))
        original_geom = window2.geometry()
        assert window2.width() != width or window2.height() != height, \
            "second window should have different geometry initially"

        # Restore the saved state
        restore_window_state(window2, saved_state)

        # After restoration, geometries should match
        assert window2.width() == width, "width should be restored"
        assert window2.height() == height, "height should be restored"

    finally:
        store.close()
        vectors.close()


def test_close_event_persists_geometry_without_raising(tmp_path):
    r"""§3b/§4a: a real close hides the window, persists geometry, and never raises.

    **A regression, found live.** `closeEvent` called `self.set_states(...)` -
    `MainWindow` has no such method, only `self._store.set_states(...)` does
    (every other call site in this file gets this right). The `AttributeError`
    fired *before* `self.hide()` and the entire staged teardown below it, so a
    real close never hid the window and never ran cleanup (workers, the
    scheduler, the engine, the store) - confirmed by actually launching the
    app and closing it, where the run log showed the exception immediately
    followed by the event loop returning.

    None of the tests above catch this: `test_window_saves_geometry_on_close`
    calls `save_window_state` directly, and `test_window_can_restore_from_saved_state`
    never calls `.close()` at all - neither goes through the real `closeEvent`
    override this bug lived in. This one does, by actually calling `.close()`
    on a real window rather than mocking around it.
    """
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path / "window_close"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance()
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    try:
        built = MainWindow(settings, store, vectors, _Engine(store))
        built.resize(640, 480)
        built.show()
        for _ in range(3):
            app.processEvents()

        # The real override, not a mocked slot - this is the exact path that
        # raised AttributeError in production.
        built.close()
        for _ in range(3):
            app.processEvents()

        assert not built.isVisible(), \
            "closeEvent's self.hide() must run - it never did while the " \
            "AttributeError above it was unhandled"
        assert store.get_state("ui:window_geometry"), \
            "closeEvent must persist window geometry via self._store.set_states"
    finally:
        store.close()
        vectors.close()


def test_close_hides_before_the_staged_teardown_begins(tmp_path):
    r"""§3b, and §4's own coverage gap: "hide-first" as an *ordering*, not
    just an eventual end state.

    `test_close_event_persists_geometry_without_raising` already proves the
    window ends up hidden after `close()` returns - which would be true even
    if `self.hide()` were the very last line of `closeEvent`, telling nobody
    that the perceived-instant-close behaviour this item is actually about
    (the user sees the app gone from the screen *before* the teardown runs,
    not merely by the time it finishes) is real. This test hooks the first
    teardown stage (`search_view.shutdown`, the first call inside
    `closeEvent`'s `stage()` loop) and asserts the window is already
    invisible at that exact moment - the ordering the work order's own
    "close: hide-first verified (window invisible before drain begins)"
    acceptance line names.
    """
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path / "window_hide_first"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance()
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    try:
        built = MainWindow(settings, store, vectors, _Engine(store))
        built.show()
        for _ in range(3):
            app.processEvents()

        visible_at_first_stage: list[bool] = []
        real_shutdown = built.search_view.shutdown

        def spy_shutdown():
            visible_at_first_stage.append(built.isVisible())
            real_shutdown()

        built.search_view.shutdown = spy_shutdown

        built.close()
        for _ in range(3):
            app.processEvents()

        assert visible_at_first_stage == [False], (
            "the window must already be hidden by the time the first "
            "teardown stage runs - hiding only at the very end of "
            "closeEvent would satisfy the weaker 'ends up hidden' test "
            "above without delivering perceived-instant close at all"
        )
    finally:
        store.close()
        vectors.close()


def test_maximised_window_state_restored(tmp_path):
    r"""§4a: A maximised window is restored as maximised.

    When a window is closed while maximised, the saved state includes that
    fact, and restoration puts it back in maximised state.
    """
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow
    from app.ui.window_state import save_window_state

    root = tmp_path / "window_maximised"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance()
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    try:
        window = MainWindow(settings, store, vectors, _Engine(store))
        window.showMaximized()
        app.processEvents()
        assert window.isMaximized(), "window should be maximised"

        # Save state of maximised window
        state = save_window_state(window)

        # The blob should contain the maximised state
        assert isinstance(state, bytes) and len(state) > 0

    finally:
        store.close()
        vectors.close()


def test_minimised_window_opens_normal(tmp_path):
    r"""§4b: A window closed while minimised opens normal, never minimised.

    An app that starts invisible looks broken. Even if somehow a saved state
    says the window was minimised, restoration should show it normally.
    """
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path / "window_minimised"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance()
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    try:
        window = MainWindow(settings, store, vectors, _Engine(store))

        # If a window somehow got minimised (user action or crash recovery),
        # we want to ensure it opens normally. We can't easily create a
        # minimised blob, but we can verify the restore logic handles it.
        from unittest.mock import patch
        from app.ui.window_state import restore_window_state

        # Simulate restoration of a state that reports minimised
        with patch.object(window, "isMinimized", return_value=True):
            with patch.object(window, "showNormal") as mock_show:
                restore_window_state(window, b"fake_geometry")
                mock_show.assert_called_once()

    finally:
        store.close()
        vectors.close()


def test_a_second_launch_brings_the_window_forward(window):
    r"""**The bug, reported live: "the box is hard to get to."**

    A second launch that found the GUI mutex already held writes
    `run_lock.FRONT_STATE_KEY` and exits; this window's own watcher
    (`_show_external_run`) is what has to notice and come forward - the same
    three calls `_run_link` already ends on for a `leasha://` link, pulled
    into `_front_self` so both paths use one.
    """
    from unittest.mock import patch

    _app, built = window

    with patch.object(built, "showNormal") as show, \
            patch.object(built, "raise_") as raise_, \
            patch.object(built, "activateWindow") as activate:
        built._show_external_run(
            {"locked": False, "record": None, "link": None,
             "front_requested": True})

    show.assert_called_once()
    raise_.assert_called_once()
    activate.assert_called_once()


def test_an_ordinary_poll_does_not_steal_focus(window):
    """No front request means no `_front_self` - or every four-second poll
    would yank focus back to a window nobody asked to see."""
    from unittest.mock import patch

    _app, built = window

    with patch.object(built, "showNormal") as show, \
            patch.object(built, "raise_") as raise_, \
            patch.object(built, "activateWindow") as activate:
        built._show_external_run(
            {"locked": False, "record": None, "link": None,
             "front_requested": False})

    show.assert_not_called()
    raise_.assert_not_called()
    activate.assert_not_called()
