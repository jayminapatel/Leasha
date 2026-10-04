r"""The Start Indexing freeze: `resolve_for_run` moved off the UI thread.

Layer: L5

**The bug, found live tonight.** `_start_indexing` called `resolve_for_run`
inline, on the UI thread, which falls through to `compute_profile.detect()` on
a cold or invalidated hardware-profile cache - and `detect()` shells out to
PowerShell twice, with 10s and 15s timeouts, for the disk kind and the display
adapters. Both ran between the click and anything reaching `IndexingView.
start`, which is exactly the window the user is watching for a response.

The fix dispatches the resolve step through a `CallableWorker`, the same
pattern every other background job in this file already uses, and hands the
result to `_index_resolved` by signal - which does exactly what `_start_
indexing` used to do synchronously, just a beat later, on the GUI thread.

These tests exercise the wiring rather than a real index run:
`IndexingView.start` is replaced with a spy, so what is proven is that the
resolved value reaches it, not that a real Pipeline can walk a folder - that
is `test_index_tuning_acceptance.py`'s and the integration suite's job.
"""

from __future__ import annotations

import threading
import time

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
    """Enough of a `SearchEngine` to build a window around. See test_window_opens.py."""

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
    # Order 0r item 2b (second pass): `indexing_view` and `settings_view` are
    # built on the first turn of the event loop, and every test below reaches
    # into `built.indexing_view`. Turning the loop here - rather than at each
    # use - is what "the window is open" means for this file's purposes.
    for _ in range(5):
        app.processEvents()
    assert hasattr(built, "indexing_view"), "the deferred pages were not built"
    return app, built, store, vectors


def _pump(app, ms: int = 5_000) -> None:
    """Drain a chain of workers, not one round of them. *2026-10-04*: Start now
    asks a worker whether this machine needs its device test before the
    resolve worker is even created, so one wait-then-process round stopped
    before the run existed."""
    from PyQt6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(ms)
    for _ in range(5):
        app.processEvents()
    # Then a short round per link, not a full wait: a window keeps workers of
    # its own alive, so `waitForDone` returns at its timeout, not when idle.
    for _round in range(3):
        QThreadPool.globalInstance().waitForDone(300)
        for _ in range(5):
            app.processEvents()


# --- the guard itself: nothing heavy runs on the calling thread -------------


def test_start_indexing_body_dispatches_to_a_worker_rather_than_inline() -> None:
    r"""Structural check, mirroring `test_ui_never_blocks.test_a_long_operation_
    starts_a_worker`'s own technique: read the method body as text and assert it
    hands off rather than calling the resolver in place.

    Asserted on the source rather than by mocking `resolve_for_run` and
    checking the calling thread, because a thread-identity assertion can pass
    by accident (a worker pool under heavy load can still schedule onto the
    calling thread's own queue on some Qt builds) - the source shape is what
    actually decides whether the call can ever reach the UI thread.
    """
    from pathlib import Path

    # `_start_indexing` moved to `IndexController` (work order 202626082352
    # section 7); `MainWindow._start_indexing` only forwards to it.
    text = (Path(__file__).resolve().parents[2] / "app" / "ui" / "controllers"
            / "index_controller.py").read_text(encoding="utf-8")
    body = text.split("def _start_indexing(")[1].split("\n    def ")[0]

    assert "resolve_for_run(" not in body, (
        "_start_indexing must not call resolve_for_run directly any more - "
        "it should hand it to a CallableWorker instead"
    )
    assert "CallableWorker(resolve_for_run" in body
    assert "run(QThreadPool.globalInstance(), worker)" in body


def test_a_long_operation_entry_exists_for_start_indexing() -> None:
    """The house guard (`test_ui_never_blocks.py`) must actually cover this
    method, not just this file's own narrower check above."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent / "test_ui_never_blocks.py"
           ).read_text(encoding="utf-8")
    assert '("controllers/index_controller.py", "_start_indexing")' in text, (
        "test_ui_never_blocks.test_a_long_operation_starts_a_worker must be "
        "extended to cover _start_indexing, or this fix has no standing guard"
    )


# --- the warm/fast path: resolving still reaches indexing -------------------


def test_a_resolved_run_still_reaches_indexing_view_start(tmp_path) -> None:
    r"""No regression on the fast path: whatever `resolve_for_run` returns -
    warm cache or cold - still ends up as a Pipeline handed to `IndexingView.
    start`, just asynchronously now."""
    app, built, store, vectors = _window(tmp_path)
    calls: list = []
    built.indexing_view.start = lambda pipeline, **kw: calls.append((pipeline, kw))

    def fake_resolve(settings, store):
        return Resolved(workers=2, onnx_threads=1, embed_batch=8)

    import app.index.resolve as resolve_module

    real = resolve_module.resolve_for_run
    resolve_module.resolve_for_run = fake_resolve
    try:
        folder = tmp_path / "corpus"
        folder.mkdir()
        built._start_indexing(roots=[str(folder)])
        _pump(app)

        assert calls, "resolving must still hand a Pipeline to IndexingView.start"
        assert not built._resolving_index, "the in-flight flag must clear"
        # Not asserted as empty: other background work (e.g. the totals
        # refresh) legitimately posts its own message once startup settles,
        # and this test cares only that the "Checking..." one specifically
        # does not linger past resolution finishing.
        assert "Checking your hardware" not in built.toast.current_text(), (
            "the 'Checking your hardware...' message must not linger once "
            "resolution has finished"
        )
    finally:
        resolve_module.resolve_for_run = real
        store.close()
        vectors.close()


def test_the_start_button_is_disabled_while_resolving_and_restored_after(
    tmp_path
) -> None:
    """Honest feedback while resolution is in flight, per the work order:
    the button must not invite a second click during a cold detection."""
    app, built, store, vectors = _window(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def slow_resolve(settings, store):
        entered.set()
        release.wait(5)
        return Resolved(workers=1, onnx_threads=1, embed_batch=8)

    import app.index.resolve as resolve_module

    real = resolve_module.resolve_for_run
    resolve_module.resolve_for_run = slow_resolve
    handed: list = []
    built.indexing_view.start = lambda pipeline, **kw: handed.append(pipeline)
    try:
        folder = tmp_path / "corpus"
        folder.mkdir()
        built._start_indexing(roots=[str(folder)])

        assert entered.wait(5), "the worker never called the resolver"
        app.processEvents()
        assert not built.indexing_view.start_button.isEnabled(), (
            "Start must be disabled while resolution is in flight"
        )
        assert built.toast.current_text() != "", (
            "somebody watching the window during a slow cold detection must "
            "see something other than nothing happening"
        )

        release.set()
        _pump(app)
        # **Retargeted by order 0r item 2b (second pass).** This used to end on
        # `start_button.isEnabled()` after the resolve - but `start` is replaced
        # by a no-op above, so nothing in *this* test re-enables the button; it
        # came back only because the window's first event-loop turn (which used
        # to happen inside `_pump`, after the button was disabled) ran
        # `_start_background_work` -> `_poll_external_run` -> `show_external`,
        # which repaints Start as enabled. Building the deferred pages in
        # `_window` moved that turn to before the click, exposing it. What the
        # resolve step itself owns is asserted instead: the in-flight flag and
        # the toast are cleared and the Pipeline is handed to the view exactly
        # once - after that the button belongs to `IndexingView` (`start`
        # disables it, `_on_done` restores it). The failure path, where nothing
        # is handed over and the button must come back, is asserted by
        # `test_a_resolution_failure_shows_the_same_error_dialog_a_synchronous_
        # one_would` and is unchanged.
        assert not built._resolving_index, "the in-flight flag must be cleared"
        # **2026-09-30: "cleared" means that notice is gone, not that the line
        # is empty.** The same line carries the status sentence, and the window's
        # own status refresh ("0 files  ·  0 chunks indexed") can land on it once
        # the notice has gone - which it does whenever this test runs after the
        # others in this file. Asserting an empty line failed on that, in every
        # whole-file run and in the suite, while saying nothing about the notice.
        assert "Checking your hardware" not in built.toast.current_text(), (
            "the 'Checking your hardware' notice must be cleared when resolution ends")
        assert len(handed) == 1, (
            "the resolved Pipeline must be handed to IndexingView.start exactly once")
    finally:
        resolve_module.resolve_for_run = real
        store.close()
        vectors.close()


def test_a_second_click_while_resolving_does_not_dispatch_a_second_worker(
    tmp_path
) -> None:
    """A burst of clicks (or F5, or the scheduler) during a slow cold-cache
    detection must not queue more than one resolve, or `IndexingView.start`
    could be handed more than one Pipeline for the same run."""
    app, built, store, vectors = _window(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    call_count = {"n": 0}

    def slow_resolve(settings, store):
        call_count["n"] += 1
        entered.set()
        release.wait(5)
        return Resolved(workers=1, onnx_threads=1, embed_batch=8)

    import app.index.resolve as resolve_module

    real = resolve_module.resolve_for_run
    resolve_module.resolve_for_run = slow_resolve
    built.indexing_view.start = lambda pipeline, **kw: None
    try:
        folder = tmp_path / "corpus"
        folder.mkdir()
        built._start_indexing(roots=[str(folder)])
        assert entered.wait(5)

        # The second, third and fourth clicks somebody makes while the window
        # looks unresponsive - and the same call `F5`/the scheduler would make.
        built._start_indexing(roots=[str(folder)])
        built._start_indexing(roots=[str(folder)])

        release.set()
        _pump(app)

        assert call_count["n"] == 1, (
            f"resolve_for_run ran {call_count['n']} times for one Start click "
            "- the in-flight guard did not hold"
        )
    finally:
        resolve_module.resolve_for_run = real
        store.close()
        vectors.close()


def test_the_watch_timers_poll_does_not_re_enable_start_while_resolving(
    tmp_path
) -> None:
    r"""The real race: `_poll_external_run` runs on the 4-second `_watch_timer`
    and has no notion of `_resolving_index`. `IndexingView.is_running()` stays
    False for the whole resolve phase (no Pipeline exists yet - it is the very
    thing still being resolved), so nothing in the existing poll guard stops
    it concluding "nothing is indexing" and re-enabling Start mid-resolve -
    inviting the exact second click non-negotiable #5 and this button's own
    disable-on-click logic exist to prevent.

    Exercises the real interaction rather than isolating around it:
    `_watch_timer` is left running (never stopped), and `_poll_external_run`
    is called directly - exactly what the timer's own `timeout` signal does -
    rather than mocked away or skipped.
    """
    app, built, store, vectors = _window(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def slow_resolve(settings, store):
        entered.set()
        # A generous safety net, not a value meant to matter: the real signal
        # is `release.set()` below. `_start_background_work`'s own hardware
        # detection runs synchronously inside the single `processEvents()`
        # call just below and can itself take several real seconds under
        # load - a short wait here would race that unpredictably, exactly
        # the flakiness this test exists to replace.
        release.wait(30)
        return Resolved(workers=1, onnx_threads=1, embed_batch=8)

    import app.index.resolve as resolve_module

    real = resolve_module.resolve_for_run
    resolve_module.resolve_for_run = slow_resolve
    built.indexing_view.start = lambda pipeline, **kw: None
    try:
        folder = tmp_path / "corpus"
        folder.mkdir()
        built._start_indexing(roots=[str(folder)])
        assert entered.wait(5), "the worker never called the resolver"
        app.processEvents()
        assert not built.indexing_view.start_button.isEnabled(), (
            "Start must be disabled once resolution has begun"
        )
        assert built._watch_timer.isActive(), (
            "the watch timer must still be running for this to be the real "
            "interaction, not an isolated one"
        )

        # What the 4-second timer would do on its own - called directly so the
        # test is deterministic rather than sleeping for real seconds, but it
        # is the exact same call the timer's timeout signal makes, against the
        # real, still-running _watch_timer.
        built._poll_external_run()

        # The dispatched _read_external_run worker runs on the global pool
        # alongside the still-blocked resolve worker; poll for it to signal
        # back without a full QThreadPool.waitForDone, which would hang until
        # `release` is set. Kept well under slow_resolve's own 5s release.wait
        # timeout - racing the two would make this test itself flaky, timing
        # out into the *legitimate* re-enable that happens once resolution
        # genuinely completes, rather than proving anything about the poll.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            app.processEvents()
            if built.indexing_view.start_button.isEnabled():
                break
            time.sleep(0.05)

        assert not built.indexing_view.start_button.isEnabled(), (
            "the watch timer's poll spuriously re-enabled Start while a "
            "resolve was still in flight - inviting a second click mid-resolve"
        )

        release.set()
        _pump(app)
        # `indexing_view.start` is stubbed to a no-op above (as the sibling
        # "disabled while resolving" test also does), so no real `_worker`
        # ever gets set - nothing re-enables the button on its own once
        # `_resolving_index` clears. The real window relies on exactly the
        # next watch-timer tick for that (see `_go_idle`'s docstring); calling
        # it here, deterministically, is that tick rather than a guess at how
        # much wall-clock time `_pump` happens to consume.
        built._poll_external_run()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            app.processEvents()
            if built.indexing_view.start_button.isEnabled():
                break
            time.sleep(0.05)
        assert built.indexing_view.start_button.isEnabled(), (
            "the button must still come back once resolution has genuinely "
            "finished"
        )
    finally:
        resolve_module.resolve_for_run = real
        store.close()
        vectors.close()


# --- resolution failure: still surfaced, never swallowed --------------------


def test_a_resolution_failure_shows_the_same_error_dialog_a_synchronous_one_would(
    tmp_path
) -> None:
    r"""H4: `resolve_for_run` is documented never to raise, but the worker
    contract (`CallableWorker`, `app/ui/workers.py`) must still convert
    whatever escapes into an `AppError` and reach `_show_error` - exactly the
    contract every other worker in this file already honours - rather than
    disappearing into a QThreadPool with nothing shown."""
    app, built, store, vectors = _window(tmp_path)
    shown: list = []
    built._show_error = lambda error: shown.append(error)
    built.indexing_view.start = lambda pipeline, **kw: shown.append("started")

    def broken_resolve(settings, store):
        raise RuntimeError("the hardware probe blew up")

    import app.index.resolve as resolve_module

    real = resolve_module.resolve_for_run
    resolve_module.resolve_for_run = broken_resolve
    try:
        folder = tmp_path / "corpus"
        folder.mkdir()
        built._start_indexing(roots=[str(folder)])
        _pump(app)

        assert len(shown) == 1, shown
        error = shown[0]
        assert error != "started", "indexing must not start on a failed resolve"
        assert getattr(error, "message", ""), (
            "the failure must reach _show_error as an AppError-shaped object, "
            "not be swallowed"
        )
        assert not built._resolving_index
        assert built.indexing_view.start_button.isEnabled(), (
            "a failed resolve must give the button back"
        )
    finally:
        resolve_module.resolve_for_run = real
        store.close()
        vectors.close()


# --- the bar during the resolve step ------------------------------------------


def test_the_bar_is_busy_from_the_click_and_settles_if_the_resolve_fails(
    tmp_path
) -> None:
    r"""Bug 2b: between the click and the first progress tick nothing reported
    anything, so a slow hardware check left the bar sitting still at zero - the
    shape of a Start button that did nothing. It goes busy on the click, with a
    sentence, and comes back to rest if no run follows."""
    from app.ui.presenter import PREPARING_WORDS

    app, built, store, vectors = _window(tmp_path)
    built._show_error = lambda error: None
    entered = threading.Event()
    release = threading.Event()

    def slow_then_broken(settings, store):
        entered.set()
        release.wait(5)
        raise RuntimeError("the hardware probe blew up")

    import app.index.resolve as resolve_module

    real = resolve_module.resolve_for_run
    resolve_module.resolve_for_run = slow_then_broken
    try:
        folder = tmp_path / "corpus"
        folder.mkdir()
        built._start_indexing(roots=[str(folder)])

        assert entered.wait(5), "the worker never called the resolver"
        app.processEvents()
        bar = built.indexing_view.bar
        assert bar.maximum() == 0, "the bar must be busy while the run is prepared"
        assert built.indexing_view.detail.text() == PREPARING_WORDS

        release.set()
        _pump(app)
        assert bar.maximum() > 0, "a failed resolve must not leave the bar spinning"
        assert built.indexing_view.detail.text() != PREPARING_WORDS
    finally:
        resolve_module.resolve_for_run = real
        store.close()
        vectors.close()
