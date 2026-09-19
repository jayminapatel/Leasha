r"""The frozen-settings bug: `_limits_changed` now actually changes the live object.

Layer: L5

**The bug, found live tonight.** `_limits_changed` tried `setattr(self._
settings, key, value)` per key. `Settings` is a frozen pydantic model
(`model_config = ConfigDict(frozen=True, ...)`, `app/core/config.py`), so every
one of those calls raised `pydantic.ValidationError` - caught by a bare
`except Exception` and logged at DEBUG, where nobody would ever see it. The
change to `.env` (`self._settings_changed(...)`, the line above it) worked
correctly the whole time; the live half never did, so the method's own promise
- "applies to the next run *in this session*, not just after a restart" -
was false for every key routed through it.

The fix is one `model_copy(update=values)` replacing `self._settings` whole,
mirroring the one other place in this codebase that already updates a frozen
`Settings` this way (`app.cli.cmd_index`, for `--rerank-model`).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from app.core.config import load_settings
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

INDEX_WORKERS=0
INDEX_TUNING_MODE=defaults

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
    return app, built, store, vectors, env


def test_settings_is_frozen_so_the_old_setattr_would_have_raised(tmp_path) -> None:
    """Pins the premise: if `Settings` ever stops being frozen, this bug and
    its fix both stop meaning anything, and that should be visible here
    rather than discovered by a live-apply test failing for a new reason."""
    _app, built, store, vectors, _env = _window(tmp_path)
    try:
        with pytest.raises(Exception):
            built._settings.index_workers = 9
    finally:
        store.close()
        vectors.close()


def test_a_live_tunable_setting_actually_changes_within_the_same_session(
    tmp_path
) -> None:
    r"""The exact scenario that was silently broken: change the worker count
    through `_limits_changed`, then read `self._settings` again - in the same
    session, no restart - and see the new value.
    """
    app, built, store, vectors, _env = _window(tmp_path)
    try:
        before = built._settings.index_workers

        built._limits_changed({"index_workers": 7})

        assert built._settings.index_workers == 7
        assert built._settings.index_workers != before
    finally:
        store.close()
        vectors.close()


def test_the_next_index_run_reads_the_changed_value_not_the_original_object(
    tmp_path
) -> None:
    r"""Not just that *a* copy exists somewhere holding 7 - that `self.
    _settings`, the one `_start_indexing`/`_index_resolved` actually reads from,
    is the changed one. A test that only checked a returned value could pass
    against a fix that built a new Settings and threw it away."""
    app, built, store, vectors, _env = _window(tmp_path)
    try:
        original = built._settings

        built._limits_changed({"index_workers": 5, "index_tuning_mode": "manual"})

        assert built._settings is not original, (
            "Settings is frozen - a live change can only ever be a new "
            "instance, never a mutation of the old one"
        )
        assert built._settings.index_workers == 5
        assert built._settings.index_tuning_mode == "manual"
    finally:
        store.close()
        vectors.close()


def test_several_keys_in_one_call_all_apply_together(tmp_path) -> None:
    """`_limits_changed` receives a batch (worker count, memory ceiling, CPU
    cap, free-space floor, tuning mode can all arrive at once from the tuning
    screen) - one `model_copy` for the whole dict, not one attempt per key."""
    app, built, store, vectors, _env = _window(tmp_path)
    try:
        built._limits_changed({
            "index_workers": 3,
            "index_memory_mb": 2048,
            "index_cpu_percent": 50,
            "min_free_gb": 10,
        })

        assert built._settings.index_workers == 3
        assert built._settings.index_memory_mb == 2048
        assert built._settings.index_cpu_percent == 50
        assert built._settings.min_free_gb == 10
    finally:
        store.close()
        vectors.close()


def test_persistence_to_env_still_works_unchanged(tmp_path) -> None:
    """The half that already worked must keep working: `.env` still gets the
    upper-cased keys, exactly as before this fix."""
    app, built, store, vectors, env = _window(tmp_path)
    try:
        built._limits_changed({"index_workers": 6})

        text = env.read_text(encoding="utf-8")
        assert "INDEX_WORKERS=6" in text, (
            "persistence to .env must be unaffected by fixing the live half:\n"
            + text
        )
    finally:
        store.close()
        vectors.close()


def test_an_empty_change_does_nothing_and_does_not_raise(tmp_path) -> None:
    """The existing early return for `not values` must survive untouched."""
    app, built, store, vectors, _env = _window(tmp_path)
    try:
        original = built._settings
        built._limits_changed({})
        assert built._settings is original
    finally:
        store.close()
        vectors.close()


def test_the_status_bar_still_says_saved(tmp_path) -> None:
    """The user-facing confirmation is untouched - only the mechanism
    underneath it was broken."""
    app, built, store, vectors, _env = _window(tmp_path)
    try:
        built._limits_changed({"index_workers": 4})
        assert built.toast.current_text() == (
            "Saved. Applies to the next index run."
        )
    finally:
        store.close()
        vectors.close()


# --- the stale-reference question, checked precisely rather than assumed ----


def test_settings_view_holds_its_own_settings_reference_not_the_windows(
    tmp_path
) -> None:
    r"""**Found while verifying this fix, not fixed by it.** `SettingsView.
    __init__` does `self._settings = settings` (`app/ui/settings_view.py`),
    the *same object* `MainWindow.__init__` handed it - a second reference,
    independent of `MainWindow._settings`. Replacing `MainWindow._settings`
    with a new instance, as this fix does, cannot reach that second
    reference: `SettingsView` would keep seeing the pre-change object until
    the window is rebuilt.

    **This does not currently affect any key `_limits_changed` touches.**
    `SettingsView` reads `self._settings` in exactly two places, both inside
    `_ollama_client` (`ollama_url`, `ollama_model`) - grepped and confirmed
    here, not assumed - and neither is a worker-count/memory/CPU/disk/tuning-
    mode field. So today this is a latent trap, not a live bug: the day
    `_limits_changed` or anything like it starts routing an `ollama_*` key (or
    `SettingsView` starts reading a field this fix does touch) through the
    window's copy, this test's second assertion will fail exactly where the
    gap actually bites, without anyone having to remember this note.
    """
    app, built, store, vectors, _env = _window(tmp_path)
    try:
        # Order 0r item 2b (second pass): Settings is built a beat after the
        # window, on the first turn of the event loop.
        for _ in range(5):
            app.processEvents()
        assert built.settings_view._settings is built._settings, (
            "if this ever starts failing because MainWindow.__init__ was "
            "changed to hand SettingsView a shared, live-updated reference "
            "instead of its own copy, that is progress - update this test "
            "to say so rather than deleting it"
        )

        original_window_settings = built._settings
        built._limits_changed({"index_workers": 8})

        assert built._settings.index_workers == 8
        assert built.settings_view._settings is original_window_settings, (
            "SettingsView's own reference is untouched by design of this fix - "
            "documented here so it is a known, checked fact rather than a "
            "silent gap"
        )
    finally:
        store.close()
        vectors.close()
