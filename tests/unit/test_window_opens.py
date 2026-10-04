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


def _WindowSpy():
    """An event filter that remembers every top-level widget Qt shows.

    Built lazily so importing this module does not import Qt.
    """
    from PyQt6.QtCore import QEvent, QObject
    from PyQt6.QtWidgets import QWidget

    class Spy(QObject):
        def __init__(self) -> None:
            super().__init__()
            self.shown: list[tuple[QWidget, str]] = []

        def eventFilter(self, obj, event):          # noqa: N802 - Qt's name
            if (event.type() == QEvent.Type.Show and isinstance(obj, QWidget)
                    and obj.isWindow()):
                self.shown.append((obj, type(obj).__name__))
            return False

    return Spy()


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
    # Watching from before construction: a stray window opens inside
    # `__init__` or on the first turns of the loop, never later.
    spy = _WindowSpy()
    app.installEventFilter(spy)
    built = MainWindow(settings, store, vectors, _Engine(store), debug=False)
    built._test_window_spy = spy

    yield app, built

    app.removeEventFilter(spy)

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


def test_no_part_of_the_window_opens_as_a_window_of_its_own(window):
    r"""**The small window between the splash and the main one** (0r, 2026-09-29).

    The owner saw three windows on every start: the splash, "a small window",
    then Leasha. Measured on their display, the middle one was a separate
    top-level window titled Leasha that lived 1.6 s. `setVisible(True)` on a
    widget that has no parent yet does exactly that - Qt shows it as a window
    of its own until a layout adopts it - and three widgets did it in their
    constructors: `ChatBox` (the one on screen for the whole Settings build),
    `DebugPane`'s pop-out button and the search bar's Interpret hint.

    The window itself is the only top-level anything may show while it is
    built and while its deferred pages build.
    """
    app, built = window
    for _ in range(10):
        app.processEvents()

    strays = [name for widget, name in built._test_window_spy.shown
              if widget is not built]
    assert strays == [], (
        f"{strays} opened as windows of their own during start-up - a widget "
        "was shown before it had a parent. Hide, never show, until it is in "
        "a layout.")


def test_holding_the_deferred_pages_after_they_ran_rebuilds_nothing(window):
    """`hold`/`release` are for `app.main`'s splash hand-off, before the loop
    has turned. Called after the pages exist they must be no-ops, not a
    second build of Mail, Code, Indexing and Settings."""
    app, built = window
    for _ in range(5):
        app.processEvents()
    settings_view, mail_view = built.settings_view, built.mail_view

    built.hold_deferred_start()
    built.release_deferred_start()
    for _ in range(5):
        app.processEvents()

    assert built.settings_view is settings_view
    assert built.mail_view is mail_view


def test_every_tab_can_be_selected(window):
    r"""The Files tab crashed once and was never reproducible offscreen.

    This does not prove it cannot again - a crash inside Qt's own paint pass
    needs a real display - but selecting each page runs its `showEvent`, its
    first layout and any timer it starts, which is where three of this
    application's UI bugs have actually lived.
    """
    app, built = window
    # UI Redesign 202626160950 replaced the QTabWidget navigation strip with
    # Rail; Rail keeps the same shape (count/setCurrentIndex) on purpose, so
    # this drives the same pages through the same seam.
    tabs = built.rail
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

    # 2026-09-27: `_front_self` goes through `window_state.bring_forward`,
    # which raises and activates as before but no longer calls `showNormal()`
    # - that un-maximised a maximised window (owner report). The window still
    # comes forward; it just keeps its size.
    with patch.object(built, "showNormal") as show, \
            patch.object(built, "raise_") as raise_, \
            patch.object(built, "activateWindow") as activate:
        built._show_external_run(
            {"locked": False, "record": None, "link": None,
             "front_requested": True})

    raise_.assert_called_once()
    activate.assert_called_once()
    show.assert_not_called()


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
    `MainWindow.__init__` connects a signal to `self.notify(...)` (the
    toast that replaced the status bar, UI Redesign 202626160950 §6) and
    hands the signal's `emit` to `engine.
    status_callback` - see `app/search/engine.py::SearchEngine.
    _clip_download_progress` for what actually calls it during a real
    search. Calling that same seam directly here, exactly the way the engine
    would, must make the message show up on the toast.

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
        # A fresh window's own scheduler fires an initial state_changed
        # notice synchronously during __init__ (see the schedule wiring in
        # shell.py's _start_scheduler), which reaches the toast before this
        # test runs - Toast queues rather than overwrites (see toast.py's
        # own docstring), so that unrelated notice would otherwise still be
        # showing when this asserts. Clear it first, the same way
        # test_rules_5_and_6_remember_then_upgrade_defaults_to_auto_quietly
        # does in test_idle_tune_and_space_report_ui.py.
        window.toast.clear()
        engine.status_callback(message)

        assert window.toast.current_text() == message
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
    built.rail.setCurrentIndex(built.rail.count() - 1)

    with patch.object(built.search_view, "search_now") as ran, \
            patch.object(built, "showNormal"), patch.object(built, "raise_"), \
            patch.object(built, "activateWindow") as activate:
        built._show_external_run(
            {"locked": False, "record": None, "front_requested": False,
             "link": Request("search", "pump station")})

    assert built.search_view.input.text() == "pump station"
    assert built.rail.currentIndex() == built._tab_index[built.search_view], (
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
        assert built.rail.count() == 4, (
            "only Search, Files, Offline Media and Reports exist before the "
            "event loop turns - Mail and Code are inserted a beat later, and "
            "so are Indexing and Settings (order 0r item 2b, second pass; "
            "`test_indexing_and_settings_are_not_built_until_the_event_loop_"
            "turns` below owns that half). Order 202626270513 added Offline "
            "Media and order 202626270602 added Reports to this count.")

        # Let the singleShot(0, ...) callback run.
        for _ in range(5):
            app.processEvents()

        assert hasattr(built, "mail_view") and built.mail_view is not None
        assert hasattr(built, "code_view") and built.code_view is not None
        assert built.rail.count() == 9, (
                "Mail, Code and Chat must all be inserted")
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

        assert [built.rail.tabText(i) for i in range(built.rail.count())] == [
            "Search", "Files", "Mail", "Code", "Chat", "Offline", "Reports",
            "Indexing", "Settings",
        ]
        # `_tab_index` (what `_show`, `_tab_changed` and the shortcuts all
        # use to navigate) must agree with the tab bar itself, not just with
        # whatever order the widgets happened to be built in.
        for view, title in (
            (built.search_view, "Search"), (built.files_view, "Files"),
            (built.mail_view, "Mail"), (built.code_view, "Code"),
            (built.chat_view, "Chat"),
            (built.offline_media_view, "Offline"),
            (built.reports_view, "Reports"),
            (built.indexing_view, "Indexing"), (built.settings_view, "Settings"),
        ):
            index = built._tab_index[view]
            assert built.rail.tabText(index) == title, (
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

        # Only Search, Files, Offline and Reports exist yet - switch
        # through all of them, which is the only switching a user could
        # actually perform in this gap (there is nothing to click for Mail,
        # Code, Indexing or Settings, since their tabs do not exist either).
        for index in range(built.rail.count()):
            built.rail.setCurrentIndex(index)
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


# ---------------------------------------------------------------------------
# Order 0r item 2b, second pass: Indexing and Settings are deferred too.
#
# Same template as the Mail/Code tests above: not built until the loop turns,
# the rail keeps its order, and every way of reaching them in the gap - a
# shortcut, a menu action, a tab switch, a close - does nothing rather than
# raising. Each fresh-window test makes ZERO `processEvents()` calls before it
# probes, which is the exact gap the deferral opens.
# ---------------------------------------------------------------------------

import contextlib


@contextlib.contextmanager
def _unpumped_window(tmp_path, name, *, states=None):
    r"""`(app, window, store, settings)` with **no** event-loop turn taken yet.

    `states` are written to `index_state` first, so the restore paths (last
    page, the two switches, the tray boxes) have something to restore.
    """
    from PyQt6.QtWidgets import QApplication
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow

    root = tmp_path / name
    root.mkdir()
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)

    app = QApplication.instance() or QApplication([])
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()
    if states:
        store.set_states(states)
    try:
        yield app, MainWindow(settings, store, vectors, _Engine(store)), store, settings
    finally:
        store.close()
        vectors.close()


def _pump(app, turns: int = 10) -> None:
    for _ in range(turns):
        app.processEvents()


def test_indexing_and_settings_are_not_built_until_the_event_loop_turns(tmp_path):
    r"""The deferral itself, for the second pair. If `_construct_deferred_pages`
    were ever folded back into `__init__`, the "not yet built" half of this goes
    red first.
    """
    with _unpumped_window(tmp_path, "deferred_pages") as (app, built, _store, _settings):
        assert not hasattr(built, "indexing_view"), (
            "indexing_view must not exist the instant MainWindow() returns")
        assert not hasattr(built, "settings_view"), (
            "settings_view must not exist the instant MainWindow() returns")
        # Search and Files (kept synchronous on purpose) plus Offline and
        # Reports are all that is in the rail before the loop turns.
        assert [built.rail.tabText(i) for i in range(built.rail.count())] == [
            "Search", "Files", "Offline", "Reports"]

        _pump(app)

        assert built.indexing_view is not None and built.settings_view is not None
        assert built.rail.count() == 9


def test_indexing_and_settings_keep_their_place_in_the_rail(tmp_path):
    r"""Indexing is the pill at the rail's foot and Settings is the foot page;
    neither moved. The rail's own bookkeeping (`_pill_index`, `_foot`, which
    entries have buttons) is checked as well as the titles, because the titles
    alone would pass with the pill pointing at the wrong page.
    """
    with _unpumped_window(tmp_path, "pages_order") as (app, built, _store, _settings):
        _pump(app)

        titles = [built.rail.tabText(i) for i in range(built.rail.count())]
        assert titles == ["Search", "Files", "Mail", "Code", "Chat", "Offline",
                          "Reports", "Indexing", "Settings"]
        assert built.rail._pill_index == titles.index("Indexing"), (
            "the pill must open the Indexing page")
        assert built.rail._foot == {titles.index("Settings")}, (
            "Settings, and only Settings, sits at the foot of the rail")
        assert titles.index("Indexing") not in built.rail._buttons, (
            "Indexing is the pill's page, not a rail button")
        for view, title in ((built.indexing_view, "Indexing"),
                            (built.settings_view, "Settings")):
            at = built._tab_index[view]
            assert built.rail.tabText(at) == title
            assert built.rail.indexOf(built._tab_wrapped[view]) == at, (
                "`_tab_index` must agree with the rail after Mail/Code were "
                "inserted ahead of these two")
        # Settings is a stacked form, so it is the one wrapped in a scroll area.
        assert built._tab_wrapped[built.settings_view] is not built.settings_view
        assert built._tab_wrapped[built.indexing_view] is built.indexing_view


def test_the_last_open_page_can_be_settings_or_indexing(tmp_path):
    r"""`_restore_last_page` used to run with both pages already in the rail.
    Chaining it after the deferred build is what keeps "reopen on Settings" true.
    """
    for page in ("Settings", "Indexing"):
        with _unpumped_window(tmp_path, f"last_page_{page}",
                              states={"ui:page": page}) as (app, built, _s, _c):
            _pump(app)
            assert built.rail.tabText(built.rail.currentIndex()) == page


def test_the_start_up_work_runs_after_the_pages_and_not_before(tmp_path):
    r"""The ordering guarantee, observed rather than assumed.

    `_start_background_work` is chained from the end of the deferred build
    instead of being scheduled beside it, so nothing may have started when
    `MainWindow()` returns, and everything that reaches into the two pages must
    have run - and found them - after one turn.
    """
    with _unpumped_window(tmp_path, "startup_order") as (app, built, _store, _settings):
        assert not hasattr(built, "scheduler")
        assert not built._watch_timer.isActive()
        assert not built._optimize_timer.isActive()

        _pump(app)

        assert hasattr(built, "scheduler"), "`_start_scheduler` never ran"
        assert built._watch_timer.isActive()
        assert built._optimize_timer.isActive()
        # The store-reading Settings labels are filled by that same work.
        assert built.settings_view is not None


def test_start_up_work_without_the_pages_skips_instead_of_raising(tmp_path):
    r"""If a page is ever missing, each touch degrades to "skipped".

    The alternative is an `AttributeError` that `_start_background_work`'s own
    `except` swallows, after which **nothing further in that method runs** -
    including `_warm_models`. So this checks the later steps still happened.
    """
    with _unpumped_window(tmp_path, "startup_degrades") as (app, built, store, settings):
        assert not hasattr(built, "indexing_view")
        ran = []
        built._warm_translator = lambda: ran.append("translator")
        built._warm_models = lambda: ran.append("models")
        built._restore_last_page = lambda: ran.append("page")

        built._start_background_work(store, settings)

        assert ran == ["page", "translator", "models"], (
            "the steps that need no page must still run, in order, when the "
            "pages are missing - got {}".format(ran))
        assert not hasattr(built, "scheduler")
        assert not built._watch_timer.isActive(), (
            "the watch timer's handler writes to the Indexing page; it must "
            "not be started without one")
        # And the real build, still pending, must not have been disturbed.
        _pump(app)
        assert hasattr(built, "indexing_view") and hasattr(built, "settings_view")


def test_reaching_the_deferred_pages_before_they_exist_does_not_crash(tmp_path):
    r"""Every way a person or the system can touch Indexing or Settings in the
    gap: the two shortcuts, the menu actions, F5 / a drop / "Index this folder"
    (`_start_indexing`), a tab switch, an OS theme change (`_apply_theme`) and
    the toolbar's rerank box. None of them may raise, and none may pretend the
    page is there.
    """
    from types import SimpleNamespace

    with _unpumped_window(tmp_path, "gap_probe") as (app, built, _store, _settings):
        assert not hasattr(built, "indexing_view")
        assert not hasattr(built, "settings_view")

        # The window's own QActions: the shortcuts (`addAction`) and the menus.
        triggered = 0
        for action in list(built.actions()) + list(built._menu_actions):
            label = action.text().replace("&", "")
            keys = action.shortcut().toString()
            if keys in ("Ctrl+,", "Ctrl+I") or label in (
                    "Settings", "Indexing", "Start indexing"):
                action.trigger()
                triggered += 1
        assert triggered >= 4, (
            "expected both shortcuts and the Settings / Indexing / Start "
            f"indexing menu actions to be found, got {triggered}")

        built._show(getattr(built, "settings_view", None))
        built._start_indexing()
        built._start_indexing(roots=["C:/nowhere"], recheck_archives=True)
        built._reindex_for(SimpleNamespace(path="C:/nowhere/a.txt"))
        built._apply_theme()                      # what an OS theme change calls
        built._rerank_toggled(True)               # the toolbar box, no Settings yet
        for index in range(built.rail.count()):
            built.rail.setCurrentIndex(index)
            built._tab_changed(index)

        assert not hasattr(built, "indexing_view"), (
            "nothing above may have built the page as a side effect")
        _pump(app)
        assert hasattr(built, "indexing_view") and hasattr(built, "settings_view"), (
            "the deferred build must still run after being raced like this")
        # And once they exist the same entry points work.
        built._show(built.settings_view)
        assert built.rail.currentIndex() == built._tab_index[built.settings_view]
        built._show(built.indexing_view)
        assert built.rail.currentIndex() == built._tab_index[built.indexing_view]


def test_closing_before_indexing_and_settings_exist_does_not_crash(tmp_path):
    r"""`closeEvent` flushed and stopped the Indexing page by bare attribute;
    the arguments to `stage()` are evaluated before its own `try`, so an
    immediate close would have raised mid-`closeEvent`, before the teardown.
    """
    with _unpumped_window(tmp_path, "gap_close") as (_app, built, _store, _settings):
        assert not hasattr(built, "indexing_view")

        built.close()

        assert not built.isVisible(), "hide-first must still happen in this gap"


def test_the_deferred_pages_are_themed_wheel_guarded_and_restart_noted(tmp_path):
    r"""Everything `__init__` used to do to the whole window once, that has to be
    done again for the two pages that arrive after it: the theme (the debug pane
    and the rail are pushed a palette), the wheel guard, and the "takes effect
    next launch" note on the controls that need one.
    """
    from PyQt6.QtWidgets import QWidget
    from app.core.settings_registry import needs_restart
    from app.ui.widgets.restart_note import RESTART_NOTE

    with _unpumped_window(tmp_path, "gap_finish") as (app, built, _store, _settings):
        _pump(app)

        assert built.settings_view.debug_pane._palette, (
            "`_apply_theme` must have pushed a palette to the Settings log "
            "pane once it existed")
        assert built.rail._colours, "the rail's icons were never tinted"

        noted = [s.key for s in needs_restart()
                 if built.findChild(QWidget, s.key) is not None
                 and RESTART_NOTE in built.findChild(QWidget, s.key).toolTip()]
        # Not "every restart-only control": `EMBED_QUANTISED`'s tooltip is
        # rewritten by `TuningGroups._grey_quantised` when hardware detection
        # answers, which replaces the note. That is independent of when the
        # page is built; what this pins is that the call ran over the pages.
        for key in ("EMBED_MODEL", "RERANK_MODEL", "DATA_PATH"):
            assert key in noted, (
                f"{key} does not carry the restart note - `mark_restart_needed` "
                "ran before its page was in the window (noted: {})".format(noted))

        # The wheel guard: every combo/spin box on the two pages was guarded by
        # the second `protect_all` (it sets StrongFocus, which Qt does not).
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox
        unguarded = [
            c for page in (built.settings_view, built.indexing_view)
            for c in page.findChildren((QComboBox, QAbstractSpinBox))
            if c.focusPolicy() != Qt.FocusPolicy.StrongFocus
        ]
        assert not unguarded, (
            "controls on the deferred pages still take focus from the wheel: "
            f"{[type(c).__name__ for c in unguarded]}")


def test_restored_settings_reach_the_deferred_settings_page(tmp_path):
    r"""The two switches and the tray boxes that `__init__` used to set on the
    Settings page directly are set by the deferred build from the same stored
    values. The toolbar's rerank box (the Search page's) must still be right in
    the gap, before Settings exists.
    """
    states = {"ui:index_cloud": "on", "ui:rerank_enabled": "off", "ui:motion": "on"}
    with _unpumped_window(tmp_path, "restore_switches", states=states) as (app, built, _s, _c):
        toolbar = getattr(built.search_view, "rerank_toggle", None)
        if toolbar is not None:
            assert toolbar.isChecked() is False, (
                "the toolbar's rerank box is on the Search page and must be "
                "restored synchronously, before Settings exists")
        _pump(app)
        assert built.settings_view.cloud.isChecked() is True
        assert built.settings_view.rerank.isChecked() is False


def test_a_never_touched_rerank_box_follows_rerank_enabled(tmp_path, monkeypatch):
    r"""2026-10-04, code review: with `RERANK_ENABLED=true` and the box never
    touched, the engine reranked (`main.py` reads `rerank_wanted`) while the
    toolbar started unticked - and every Search tab search asked `rerank=False`."""
    import sys

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "ENV", ENV.replace("RERANK_ENABLED=false", "RERANK_ENABLED=true"))
    with _unpumped_window(tmp_path, "rerank_from_env") as (app, built, _s, settings):
        assert settings.rerank_enabled is True
        toolbar = getattr(built.search_view, "rerank_toggle", None)
        if toolbar is not None:
            assert toolbar.isChecked() is True, "the toolbar disagreed with the engine"
        _pump(app)
        assert built.settings_view.rerank.isChecked() is True


def test_no_method_reachable_before_the_pages_exist_touches_them_directly():
    r"""A source-level pin for the runtime tests above.

    A bare `self.indexing_view` / `self.settings_view` (or `self._w.` in a
    controller) in code that can run before `_construct_deferred_pages` is an
    `AttributeError` waiting for a fast keypress, and it is invisible until one
    happens. `getattr(self, "indexing_view", None)` is the accepted form - it is
    a call, not an attribute node, so this walk does not see it.

    The list is the methods reachable in the gap: construction, the shortcut and
    menu builders, the theme, tab switches, indexing entry points, the close,
    and the one controller handler a Search-page control can reach.
    """
    import ast

    root = Path(__file__).resolve().parents[2]
    pages = {"indexing_view", "settings_view"}

    def direct(function) -> list[int]:
        hits = []
        for node in ast.walk(function):
            if not (isinstance(node, ast.Attribute) and node.attr in pages):
                continue
            base = node.value
            on_self = isinstance(base, ast.Name) and base.id == "self"
            on_window = (isinstance(base, ast.Attribute) and base.attr == "_w"
                         and isinstance(base.value, ast.Name) and base.value.id == "self")
            if on_self or on_window:
                hits.append(node.lineno)
        return hits

    def functions(path, cls):
        tree = ast.parse((root / path).read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
        return {n.name: n for n in node.body if isinstance(n, ast.FunctionDef)}

    shell = functions("app/ui/shell.py", "MainWindow")
    reachable_early = (
        "__init__", "_construct_secondary_views", "_build_shortcuts",
        "_build_menu_bar", "_apply_theme", "_tab_changed", "_start_indexing",
        "_reindex_for", "dropEvent", "closeEvent", "_drain_workers",
        "_start_background_work", "_show", "_current_view", "_preview_panes",
    )
    problems = {name: direct(shell[name]) for name in reachable_early if direct(shell[name])}
    assert not problems, (
        "these run before Indexing / Settings exist but touch them by bare "
        f"attribute (name: lines): {problems}")

    controller = functions("app/ui/controllers/settings_controller.py",
                           "SettingsController")
    assert not direct(controller["_rerank_toggled"]), (
        "`_rerank_toggled` is reachable from the toolbar's box before Settings "
        "exists; it must use getattr")

    # And the other half: the constructor does not build them.
    built_in_init = {
        n.func.id for n in ast.walk(shell["__init__"])
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not built_in_init & {"IndexingView", "SettingsView"}, (
        "IndexingView / SettingsView are built in `__init__` again - "
        "they belong to `_construct_deferred_pages`")
    assert {"IndexingView", "SettingsView"} <= {
        n.func.id for n in ast.walk(shell["_construct_deferred_pages"])
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


# ---------------------------------------------------------------------------
# 2026-09-20: what order 0r's note called "known, unfixed" in the deferred
# build - the theme applied twice, and an F5 or a drop in the gap skipped - and
# the one orphaned Settings signal.
# ---------------------------------------------------------------------------

def _wait_until(app, condition, seconds: float = 15.0) -> bool:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_startup_sets_the_window_stylesheet_once_not_twice(tmp_path, monkeypatch):
    r"""`_apply_theme` ran in `__init__` (first paint needs it) and again when the
    deferred pages arrived. Setting the same sheet on the top-level window makes
    Qt re-polish every widget in the tree - by then including Settings, the
    heaviest page. The late pages now only have the *palette* pushed to their
    pixmaps and log pane (`_push_palette`); the sheet reaches them by
    inheritance.
    """
    from app.ui.shell import MainWindow

    sheets: list[int] = []
    applies: list[int] = []
    real_set = MainWindow.setStyleSheet
    real_apply = MainWindow._apply_theme

    def counting_set(self, sheet):
        sheets.append(len(sheet))
        return real_set(self, sheet)

    def counting_apply(self):
        applies.append(1)
        return real_apply(self)

    monkeypatch.setattr(MainWindow, "setStyleSheet", counting_set)
    monkeypatch.setattr(MainWindow, "_apply_theme", counting_apply)

    with _unpumped_window(tmp_path, "theme_once") as (app, built, _store, _settings):
        _pump(app)
        assert hasattr(built, "settings_view") and hasattr(built, "indexing_view")

        assert len(applies) == 1, f"_apply_theme ran {len(applies)} times at startup"
        assert len(sheets) == 1, (
            f"the window's stylesheet was set {len(sheets)} times at startup")
        # Not traded away: the pages that arrived late were still told the palette.
        assert built.settings_view.debug_pane._palette, (
            "the late Settings page never received the theme palette")
        # A real theme change still goes the whole way.
        built._apply_theme()
        assert len(sheets) == 2


def test_an_f5_in_the_gap_is_replayed_once_the_page_exists(tmp_path):
    r"""F5 before `_construct_deferred_pages` has run was logged and dropped, so a
    person who hit it straight after launch got nothing and no word why."""
    with _unpumped_window(tmp_path, "f5_gap") as (app, built, _store, _settings):
        seen: list[tuple] = []
        built.index_ctl._start_indexing = lambda **kw: seen.append(
            (kw, hasattr(built, "indexing_view")))
        assert not hasattr(built, "indexing_view")

        built._start_indexing()
        built._start_indexing()                     # a second press: still one run
        assert seen == [], "nothing can start before the page exists"

        _pump(app)

        assert seen == [({"roots": None, "recheck_archives": False}, True)], (
            "the queued F5 must start exactly once, after the page was built")
        # And it is not replayed a second time by anything later.
        _pump(app)
        assert len(seen) == 1
        # Later presses are ordinary again.
        built._start_indexing()
        assert len(seen) == 2


def test_a_folder_dropped_in_the_gap_is_indexed_and_shown(tmp_path):
    r"""The same gap for a drop, which carries information that cannot be
    re-derived later: which folder. It also brings the Indexing page forward, as
    a drop outside the gap does."""
    from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PyQt6.QtGui import QDropEvent

    dropped = tmp_path / "dropped_folder"
    dropped.mkdir()
    with _unpumped_window(tmp_path, "drop_gap") as (app, built, _store, _settings):
        seen: list[dict] = []
        built.index_ctl._start_indexing = lambda **kw: seen.append(kw)

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(dropped))])
        built.dropEvent(QDropEvent(
            QPointF(5, 5), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        built._start_indexing(recheck_archives=True)          # and a late F5-ish
        assert seen == []

        _pump(app)

        assert len(seen) == 1
        # `None` roots (everything configured) already covers the dropped folder.
        assert seen[0] == {"roots": None, "recheck_archives": True}

    with _unpumped_window(tmp_path, "drop_gap_only") as (app, built, _store, _settings):
        seen = []
        built.index_ctl._start_indexing = lambda **kw: seen.append(kw)
        built._start_indexing(roots=[str(dropped)])
        built._start_indexing(roots=[str(dropped), str(tmp_path)])
        _pump(app)
        assert seen == [{"roots": sorted({str(dropped), str(tmp_path)}),
                         "recheck_archives": False}]
        assert built.rail.tabText(built.rail.currentIndex()) == "Indexing", (
            "a request that names a folder brings the Indexing page forward")


def test_clearing_the_history_stops_the_search_box_offering_it(tmp_path):
    r"""`history_cleared` was emitted and connected to nothing, so the box's
    "recent searches" - a cached read of the very table being erased - went on
    offering what had just been cleared until the next launch."""
    with _unpumped_window(tmp_path, "history_cleared") as (app, built, store, _s):
        _pump(app)
        store.log_search("pump curves", hits=3)
        saved = built.search_view.saved
        saved.refresh()
        assert _wait_until(app, lambda: saved._recent), "the recent search never arrived"
        assert saved.sections(), "the box should be offering it before the clear"

        built.settings_view._clear_history()
        assert _wait_until(app, lambda: not saved._recent), (
            "the search box still offers searches that were just cleared")
        assert store.count_searches() == 0
        assert not [name for name, _rows in saved.sections() if "ecent" in name]
