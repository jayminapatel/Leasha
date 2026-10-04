r"""A preview's answer that arrives after the pane has gone does nothing.

Layer: L5

2026-10-05, the full suite: "wrapped C/C++ object of type QLabel has been
deleted" from `PreviewPane._rendered`, reached by a lambda on the worker's
signal after the pane was destroyed. The answer now goes through `when_done`.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def test_a_late_preview_after_the_pane_is_gone_is_dropped(qapp, monkeypatch):
    from PyQt6.QtCore import QCoreApplication, QEvent

    from app.ui.widgets import preview as module

    held = []
    monkeypatch.setattr(module, "run", lambda _pool, worker: held.append(worker))
    pane = module.PreviewPane()
    pane._row = object()
    pane._start()
    pane._show_image("nowhere.png")
    pane.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for worker in held:                          # the answers arrive late
        worker.signals.finished.emit(None)        # nothing raises
    assert held
