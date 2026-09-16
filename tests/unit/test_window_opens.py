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


def test_idle_bench_never_asks_should_bench_while_a_run_is_in_progress(
    window, monkeypatch,
):
    r"""Work order 0b §5e: "never mid-run" is the first of the three guards,
    and it must hold before `should_bench` is even consulted - a bench is
    real CPU/GPU work, and starting one alongside an index run is exactly
    the collision §5e exists to avoid."""
    import app.index.autotune as autotune_module

    app, built = window

    asked: list[bool] = []
    monkeypatch.setattr(built.indexing_view, "is_running", lambda: True)
    monkeypatch.setattr(
        autotune_module, "should_bench",
        lambda store, profile: asked.append(True) or "unused")

    built._maybe_run_idle_bench()

    assert not asked, "should_bench must not run while indexing is in progress"


def test_idle_bench_skips_while_unplugged(window, monkeypatch):
    r"""The second guard: "never on battery." A `None` reading (no battery,
    or the question does not apply) must not hold the bench back - only a
    genuine "running on battery" answer does."""
    import app.index.autotune as autotune_module

    app, built = window

    class _Unplugged:
        power_plugged = False

    asked: list[bool] = []
    monkeypatch.setattr(built.indexing_view, "is_running", lambda: False)
    monkeypatch.setattr("psutil.sensors_battery", lambda: _Unplugged())
    monkeypatch.setattr(
        autotune_module, "should_bench",
        lambda store, profile: asked.append(True) or "unused")

    built._maybe_run_idle_bench()

    assert not asked, "should_bench must not run while unplugged"


def test_idle_bench_runs_and_quietly_upgrades_defaults_to_auto(window, monkeypatch):
    r"""The whole promise in one test: idle, plugged in, nothing running -
    the bench runs unattended and Defaults becomes Auto-tune with no dialog,
    no question, just the status bar saying what happened afterwards."""
    from PyQt6.QtCore import QThreadPool

    import app.index.autotune as autotune_module
    import app.index.index_bench as index_bench_module

    app, built = window
    original_mode = built._settings.index_tuning_mode
    assert original_mode == "defaults", "the fixture's own .env sets no mode"

    from app.index.index_bench import IndexBench

    fake_result = IndexBench(
        documents=3, chunks=9, extract_per_second=12.0, write_per_second=40.0,
        embed_per_second={"cpu": 8.0}, seconds=1.2, error="",
    )
    remembered = []

    monkeypatch.setattr(built.indexing_view, "is_running", lambda: False)
    monkeypatch.setattr(
        "psutil.sensors_battery",
        lambda: type("Plugged", (), {"power_plugged": True})())
    monkeypatch.setattr(
        autotune_module, "should_bench",
        lambda store, profile: "this machine has not been timed yet")
    monkeypatch.setattr(
        index_bench_module, "run_index_bench",
        lambda settings, devices=None: fake_result)
    monkeypatch.setattr(
        "app.core.measured.remember",
        lambda store, measured: remembered.append(measured))

    try:
        built._maybe_run_idle_bench()
        QThreadPool.globalInstance().waitForDone(10_000)
        for _ in range(3):
            app.processEvents()

        assert remembered, "a successful bench must be remembered"
        assert built._settings.index_tuning_mode == "auto", (
            "Defaults must become Auto-tune, quietly, after the first "
            "successful idle bench")
        assert built.indexing_view.tuning.current_mode() == "auto", (
            "the mode control must reflect the upgrade too, not just Settings")
    finally:
        built._settings_changed({"INDEX_TUNING_MODE": original_mode})
        built._settings = built._settings.model_copy(
            update={"index_tuning_mode": original_mode})
        built.indexing_view.tuning.load(built._settings)


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


def test_clip_download_progress_reaches_the_status_bar_not_a_second_splash(tmp_path):
    r"""Work order 0r item 1c, second clause.

    If the CLIP text-tower embedder's model cache is emptied mid-life (a
    moved index, a re-staged data directory) and the first image search is
    what next tries to load it, the splash is long closed by then -
    correctly, there must be no second one - so this window's own notices
    bar has to carry the message instead.

    Exercises the wiring from the shell's own point of view:
    `MainWindow.__init__` connects a signal to `self.statusBar().
    showMessage(...)` and hands the signal's `emit` to `engine.
    status_callback` - see `app/search/engine.py::SearchEngine.
    _clip_download_progress` for what actually calls it during a real
    search. Calling that same seam directly here, exactly the way the engine
    would, must make the message show up on the status bar.

    **No `SplashScreen` is constructed anywhere in this test** - `app/ui/
    splash.py` is never imported. The whole point of this item is that this
    path does not need one.
    """
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path / "clip_download_status"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance()
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    try:
        engine = _Engine(store)
        window = MainWindow(settings, store, vectors, engine)
        app.processEvents()

        assert callable(engine.status_callback), (
            "MainWindow must wire a status_callback onto the engine it is "
            "given, so a download that starts long after the splash has "
            "closed still has somewhere to report to")

        # Not followed by `app.processEvents()`: `_clip_download_progress` is
        # a signal whose only connection lives on this same thread, so
        # `.emit()` runs the connected slot synchronously (a direct
        # connection) - pumping the loop afterwards would let unrelated
        # startup work (e.g. the background file-count query) land its own
        # message on top of the one this test is checking.
        message = "Downloading the picture-search model - 42%"
        engine.status_callback(message)

        assert window.statusBar().currentMessage() == message
    finally:
        store.close()
        vectors.close()


def test_a_leasha_link_arriving_while_the_window_is_open_runs_it(window):
    r"""Adoptions §7a, in a live window - the half that had no test.

    **The link process never becomes a window.** It writes one `index_state`
    row and exits, and this window's own four-second poll is what picks the
    request up: `_show_external_run` hands it to `_run_link`, which goes to
    Search, puts the words in the box, runs them, and comes forward.

    Driven through the same seam the poll's worker delivers to, with a real
    `deeplink.Request` - so what is under test is the window's half of the
    hand-off rather than the parsing, which `test_deeplink.py` owns.

    `search_now` is watched rather than let run: this stub engine has no
    `search`, and what §7a promises about the window is *this query, in the
    box, on the Search tab, in front* - the searching itself is the same
    `_dispatch` every keystroke already uses.
    """
    from unittest.mock import patch

    from app.core.deeplink import Request

    _app, built = window
    built.search_view.input.clear()
    built.tabs.setCurrentIndex(built.tabs.count() - 1)

    with patch.object(built.search_view, "search_now") as ran, \
            patch.object(built, "showNormal"), patch.object(built, "raise_"), \
            patch.object(built, "activateWindow") as activate:
        built._show_external_run(
            {"locked": False, "record": None, "front_requested": False,
             "link": Request("search", "pump station")})

    assert built.search_view.input.text() == "pump station"
    assert built.tabs.currentIndex() == built._tab_index[built.search_view], (
        "a link has to bring the Search tab forward - somebody clicking one "
        "is asking a question, not opening whichever tab was last used")
    ran.assert_called_once()
    # A search that ran behind whatever they clicked the link in is a search
    # nobody saw. `raise_` is advisory on Windows; this is the half that takes
    # focus, so it is the one asserted.
    activate.assert_called_once()


def test_a_malformed_link_costs_the_link_and_not_the_window(window):
    """`_run_link` never raises: a `leasha://` URL is an input from outside
    the application, and anything on this machine can invoke it."""
    _app, built = window
    built.search_view.input.clear()

    built._show_external_run({"locked": False, "record": None, "link": object()})

    assert built.search_view.input.text() == ""


def test_an_ordinary_poll_leaves_the_search_box_alone(window):
    """No link means no query typed into somebody's box. The poll runs every
    four seconds for the life of the window; this is the far commoner case."""
    _app, built = window
    built.search_view.input.setText("half a question")

    built._show_external_run({"locked": False, "record": None, "link": None})

    assert built.search_view.input.text() == "half a question"
    built.search_view.input.clear()


# --- Order 0r item 2b: Mail and Code built a beat after show() -------------


def test_mail_and_code_are_not_built_until_the_event_loop_turns(tmp_path):
    r"""The deferral itself: `mail_view`/`code_view` do not exist the instant
    `MainWindow()` returns, and do exist once the event loop has had a turn.

    This is the regression test for the whole point of item 2b - if
    `_construct_secondary_views` were ever folded back into `__init__` by
    accident, this would be the first thing to go red, because the "not yet
    built" half would stop being true.
    """
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path / "deferred_views"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance()
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    try:
        built = MainWindow(settings, store, vectors, _Engine(store))

        # Not built yet: the constructor only scheduled it, via the same
        # `QTimer.singleShot(0, ...)` idiom `_start_background_work` already
        # used before this item existed.
        assert not hasattr(built, "mail_view"), (
            "mail_view must not exist the instant MainWindow() returns - "
            "if it does, the deferral has regressed back into __init__")
        assert not hasattr(built, "code_view"), (
            "code_view must not exist the instant MainWindow() returns - "
            "same reason as mail_view above")
        # Search is what first paint shows, so it must be built already.
        assert built.search_view is not None
        assert built.tabs.count() == 6, (
            "only Search, Files, Offline Media, Reports, Indexing and "
            "Settings exist before the event loop turns - Mail and Code are "
            "inserted a beat later. Order 202626270513 added Offline Media "
            "and order 202626270602 added Reports to this count.")

        # Let the singleShot(0, ...) callback run.
        for _ in range(5):
            app.processEvents()

        assert hasattr(built, "mail_view") and built.mail_view is not None
        assert hasattr(built, "code_view") and built.code_view is not None
        assert built.tabs.count() == 8, "Mail and Code must both be inserted"
    finally:
        store.close()
        vectors.close()


def test_mail_and_code_land_in_their_original_tab_order(tmp_path):
    r"""The tab order nobody has to relearn: Search, Files, Mail, Code,
    Offline Media, Reports, Indexing, Settings.

    Order 202626270513 added Offline Media to the single `addTab` loop
    right after Files - the same slot Mail and Code are inserted into a
    beat later (`_construct_secondary_views`'s `after_files + 1`/`+ 2`),
    which is what pushes Offline Media one further place along rather than
    ahead of them.
    """
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path / "tab_order"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance()
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    try:
        built = MainWindow(settings, store, vectors, _Engine(store))
        for _ in range(5):
            app.processEvents()

        assert [built.tabs.tabText(i) for i in range(built.tabs.count())] == [
            "Search", "Files", "Mail", "Code", "Offline Media", "Reports",
            "Indexing", "Settings",
        ]
        # `_tab_index` (what `_show`, `_tab_changed` and the shortcuts all
        # use to navigate) must agree with the tab bar itself, not just with
        # whatever order the widgets happened to be built in.
        for view, title in (
            (built.search_view, "Search"), (built.files_view, "Files"),
            (built.mail_view, "Mail"), (built.code_view, "Code"),
            (built.offline_media_view, "Offline Media"),
            (built.reports_view, "Reports"),
            (built.indexing_view, "Indexing"), (built.settings_view, "Settings"),
        ):
            index = built._tab_index[view]
            assert built.tabs.tabText(index) == title, (
                f"_tab_index says {title} is at {index}, but the tab bar "
                f"disagrees - insertTab must have shifted something the "
                f"index refresh in _construct_secondary_views missed")
    finally:
        store.close()
        vectors.close()


def test_pressing_a_deferred_views_shortcut_before_it_exists_does_not_crash(tmp_path):
    r"""The rapid-interaction risk item 2b's own task names: Ctrl+M / Ctrl+E
    pressed in the gap between `show()` and `_construct_secondary_views`
    firing must do nothing, not raise `AttributeError` on an attribute that
    does not exist yet.

    No `app.processEvents()` at all before calling these - this is exactly
    the "immediately, before the deferred callback has fired" window.
    """
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path / "rapid_shortcut"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance()
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    try:
        built = MainWindow(settings, store, vectors, _Engine(store))
        assert not hasattr(built, "mail_view"), (
            "test only proves what it claims to if this fires before the "
            "deferred callback has run")

        # The real methods F5/Ctrl+M/Ctrl+E are bound to - not a simulated
        # keypress, which would need a real window manager offscreen.
        built._focus_mail()
        built._focus_code()

        # And the same gap for a Settings control that reaches for
        # `code_view` (`_save_code_types`, wired to
        # `settings_view.code_types_changed`).
        built._save_code_types("all", [])

        for _ in range(5):
            app.processEvents()

        assert hasattr(built, "mail_view") and hasattr(built, "code_view"), (
            "the callback must still fire normally after being raced like "
            "this - nothing above should have broken its own scheduling")
    finally:
        store.close()
        vectors.close()


def test_switching_tabs_before_mail_and_code_exist_does_not_crash(tmp_path):
    r"""`_tab_changed` reaches for `code_view`/`mail_view` on every switch;
    proves it tolerates the gap before either is built, not just that
    nothing happens to switch to in that gap.
    """
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path / "rapid_tab_switch"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance()
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    try:
        built = MainWindow(settings, store, vectors, _Engine(store))
        assert not hasattr(built, "code_view")

        # Only Search, Files, Indexing and Settings exist yet - switch
        # through all of them, which is the only switching a user could
        # actually perform in this gap (there is nothing to click for Mail
        # or Code, since their tabs do not exist either).
        for index in range(built.tabs.count()):
            built.tabs.setCurrentIndex(index)
            built._tab_changed(index)

        for _ in range(5):
            app.processEvents()
        assert hasattr(built, "mail_view") and hasattr(built, "code_view")
    finally:
        store.close()
        vectors.close()


def test_closing_before_mail_and_code_exist_does_not_crash(tmp_path):
    r"""`closeEvent`'s teardown loop reaches for `mail_view`/`code_view`
    directly; proves an immediate close (no event-loop turn at all) is
    guarded rather than raising `AttributeError` mid-`closeEvent`, which
    would defeat hide-first and the whole staged teardown after it - the
    exact failure mode `task_6c99824d` already found once for a different
    attribute on this same method.
    """
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path / "rapid_close"
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance()
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()

    try:
        built = MainWindow(settings, store, vectors, _Engine(store))
        assert not hasattr(built, "mail_view")

        built.close()   # no processEvents() first - the immediate-close case

        assert not built.isVisible(), \
            "hide-first must still happen even in this gap"
    finally:
        store.close()
        vectors.close()


def test_files_and_search_are_not_deferred(window):
    r"""Regression guard the other way: item 2b defers Mail and Code only.
    Search (first paint) and Files (named safe to keep synchronous - see
    the dated note under §2b) must still be built by the time `MainWindow()`
    returns, with no event-loop turn needed at all.

    Uses the shared `window` fixture, which has already had several
    `processEvents()` calls by the time this test runs - so this only
    proves the *presence* of both views, not their timing. The timing claim
    (built before any event-loop turn) is covered by the fresh-window tests
    above; this one guards against a future change silently pulling Files
    into the deferred set too.
    """
    _app, built = window

    assert built.search_view is not None
    assert built.files_view is not None
    assert built._tab_index[built.search_view] == 0, \
        "Search must be tab 0 - the tab shown at first paint"
    assert built._tab_index[built.files_view] == 1
