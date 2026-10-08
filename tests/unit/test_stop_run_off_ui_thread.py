"""Asking another process's index run to stop never writes on the UI thread.

Layer: L5

Found in review 2026-10-08: `IndexController._stop_external_run` called
`run_lock.request_stop(store)`, a synchronous `store.set_state` on the UI
thread - bug 3a's exact shape, on the one button a person presses while
another process holds the write lock per batch. The flag is the same; it now
goes through `state_writes`, the ordered pool every other `ui:*` write uses.
`test_ui_never_blocks` could not see this because the write hid inside an
`app.core` helper.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)


class _Store:
    def __init__(self):
        self.synchronous_writes = []

    def set_state(self, key, value):
        self.synchronous_writes.append((key, value))


def test_the_stop_flag_goes_through_the_state_pool(monkeypatch):
    from app.core.run_lock import STOP_STATE_KEY
    from app.ui.controllers import index_controller as module

    queued = []

    def fake_save_state(store, key, value, **kwargs):
        queued.append((store, key, value, kwargs))

    monkeypatch.setattr(module, "save_state", fake_save_state)
    store = _Store()
    controller = SimpleNamespace(_w=SimpleNamespace(_store=store))

    module.IndexController._stop_external_run(controller)

    assert store.synchronous_writes == [], "nothing may touch the store on the UI thread"
    assert len(queued) == 1
    _, key, value, kwargs = queued[0]
    assert key == STOP_STATE_KEY
    float(value)                                  # the same timestamp the runner reads
    assert kwargs.get("on_failed") is not None, "a refused write is still logged"


def test_the_source_no_longer_reaches_for_request_stop():
    """The helper is a synchronous write by design (the CLI uses it); the
    controller must not."""
    import inspect

    from app.ui.controllers import index_controller as module

    source = inspect.getsource(module.IndexController._stop_external_run)
    assert "request_stop(" not in source
    assert "save_state(" in source
