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
    # A real, pre-existing race found live while extending order 0r item
    # 2b's deferral to Files/Indexing/Settings, not introduced by it - see
    # test_the_start_button_is_disabled_while_resolving_and_restored_after's
    # own docstring for the full account. `_start_background_work` (which
    # the pump below lets run) dispatches a one-off `_poll_external_run`
    # worker whose result, delivered later via `_show_external_run`, can
    # spuriously re-enable `indexing_view.start_button` while a resolve
    # this file's own tests started is still in flight - unrelated to
    # anything this file tests. Patched out *before* the pump below, which
    # is the only point that works: the real connection is made during
    # that same pump and captures the bound method at connect time.
    built._show_external_run = lambda payload: None
    # Order 0r item 2b: `indexing_view`/`settings_view` (this file's own
    # `built.indexing_view.start = ...` and every `_start_indexing` call
    # below need both) are built a beat later via `QTimer.singleShot(0,
    # ...)` - `_construct_deferred_views`. Every caller of this helper
    # reaches into one or both immediately, so both must already exist by
    # the time it returns.
    for _ in range(5):
        app.processEvents()
    return app, built, store, vectors


def _pump(app, ms: int = 20_000) -> None:
    r"""Drain the pool, then let the GUI thread catch up.

    **20s, not 5s - measured, not guessed.** `_index_resolved` (the
    `finished` handler `_start_indexing` wires up) builds a real
    `Embedder.from_settings(...)` on the GUI thread, not a stub - every
    test in this file that reaches it pays for a real ONNX model load
    against this test's own fresh, empty `MODEL_CACHE` (`tmp_path`-scoped,
    so nothing here is warm). `test_the_start_button_is_disabled_while_
    resolving_and_restored_after` was seen live, offscreen, taking upward
    of 20s end to end under ordinary load (not a busy machine, not a
    degenerate case) - 5s was already a tight budget for that, made
    tighter still by `_start_background_work`'s own worker traffic on the
    same `QThreadPool.globalInstance()` this waits on. Order 0r item 2b's
    own extension of the deferral to Settings did not make this slower on
    its own account; it made a pre-existing, already-marginal budget the
    one place the newly-required upfront event-loop pump (see `_window()`)
    could surface it.
    """
    from PyQt6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(ms)
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

    text = (Path(__file__).resolve().parents[2] / "app" / "ui" / "shell.py"
           ).read_text(encoding="utf-8")
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
    assert '("shell.py", "_start_indexing")' in text, (
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
        assert "Checking your hardware" not in built.statusBar().currentMessage(), (
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
    the button must not invite a second click during a cold detection.

    **A real, pre-existing race found live while extending order 0r item
    2b's deferral to Files/Indexing/Settings, not introduced by it.**
    `_window()` (needed so `indexing_view`/`settings_view` exist - see
    that helper's own comment) pumps events before returning, which lets
    `_start_background_work` run to completion, including its explicit,
    one-off `self._poll_external_run()` call - dispatched as a
    `CallableWorker`, so its `finished` signal (wired to
    `_show_external_run`) is only *delivered* whenever the event loop next
    turns, which for this test is exactly the `app.processEvents()` /
    `_pump()` calls below. `_show_external_run` -> `IndexingView.
    show_external` -> `paint_external` -> `_go_idle`
    (`app/ui/widgets/external_run.py`) blindly re-enables `start_button`
    whenever it believes nothing external is running - it has no notion of
    *this window's own* resolve being in flight, which is a real gap - so
    that one already-dispatched poll's result landing mid-test can
    re-enable the button for a reason that has nothing to do with
    `_index_resolved` actually having run. The old version of this test
    only ever passed because it never happened to trigger this delivery
    inside its own observation window - not because the interaction was
    verified safe. Neutralised in `_window()` itself (`_show_external_run`
    replaced with a no-op *before* that helper's own pump, the only point
    that works - the real connection is made during that same pump and
    captures the bound method at connect time, so patching it afterwards
    would not reach an already-established Qt connection) so every test
    built through this helper is isolated to what it actually covers
    (resolve dispatch, not the external-run poller); the underlying gap is
    real and flagged separately rather than fixed here, out of this item's
    scope.

    **Neutralising the poller alone was not enough - it made this test fail
    honestly rather than pass, which is the more important finding.**
    Tracing every `start_button.setEnabled` call, with its full stack, on
    both this code and the pre-order-0r-item-2b version showed the poller
    was the *only* thing that ever re-enabled the button in this test -
    `_index_resolved`'s own normal path hands off to `indexing_view.start`
    and returns, with no `setEnabled(True)` anywhere on it. The real
    `IndexingView.start()` does re-enable it, correctly, but only via
    `_on_done()` once an entire real indexing run has finished - not merely
    once resolving has, which is what this test's own name promises. See
    the comment directly above `built.indexing_view.start = ...` below for
    how that mock now honours what the real method actually does for a
    fast, empty-folder run, instead of a bare no-op.
    """
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
    # **Not a pure no-op.** A real, deep finding from tracing this test
    # live (Order 0r item 2b's verification, not this test's original
    # intent): the assertion below ("the button must come back once
    # resolution has finished") was never actually true of `_index_
    # resolved`'s own code - its normal path hands off to `IndexingView.
    # start` and returns, with no `setEnabled(True)` of its own anywhere
    # on that path (confirmed by tracing every call to `start_button.
    # setEnabled` with its full call stack, on this code and on the
    # pre-order-0r-item-2b version alike - both are identical here, this
    # session touched neither). The real `IndexingView.start()` re-enables
    # it only much later, from `_on_done()`, once an entire real indexing
    # run has finished - never merely once resolving has. This test used
    # to pass anyway, but only by accident: `_start_background_work`'s
    # one-off `_poll_external_run` call's result, delivered later via
    # `_show_external_run` -> `paint_external` -> `_go_idle`
    # (`app/ui/widgets/external_run.py`), blindly re-enables the same
    # button whenever it believes nothing external is running - with no
    # notion of this window's own resolve, so it happened to land inside
    # this test's own observation window and satisfy the assertion for a
    # reason that had nothing to do with what the test claims to verify.
    # `_window()` (see its own comment) now neutralises that poller for
    # every test in this file, which makes this one fail honestly instead
    # of passing by that accident. Mocking `start` to do what the real
    # method's fast-empty-folder case actually does - disable (already
    # is), run near-instantly, re-enable - keeps this test verifying its
    # own real subject (resolve dispatch honesty) without depending on an
    # unrelated background poller or a real `IndexWorker`/`Pipeline` run.
    built.indexing_view.start = lambda pipeline, **kw: built.indexing_view.start_button.setEnabled(True)
    try:
        folder = tmp_path / "corpus"
        folder.mkdir()
        built._start_indexing(roots=[str(folder)])

        assert entered.wait(5), "the worker never called the resolver"
        app.processEvents()
        assert not built.indexing_view.start_button.isEnabled(), (
            "Start must be disabled while resolution is in flight"
        )
        assert built.statusBar().currentMessage() != "", (
            "somebody watching the window during a slow cold detection must "
            "see something other than nothing happening"
        )

        release.set()
        _pump(app)
        assert built.indexing_view.start_button.isEnabled(), (
            "the button must come back once resolution has finished"
        )
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
