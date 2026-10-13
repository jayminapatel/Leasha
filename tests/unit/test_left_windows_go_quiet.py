"""A window a finished module leaves open stops every timer it owns.

Layer: L5 (pytest-qt; the helper is `conftest.quieten_a_left_window`)

2026-10-13: `gui_mainwindow` never closes its window (closing crashes), and the
module-end fixture stopped only `_watch_timer`. Measured by a spy at the start of each
module: every left window still had 12 or 13 timers running - the Photos page's 45 s
re-read, the pages' follow-the-index timers, the hourly checks - each sending workers
at a store already closed and into the thread pool every later module shares.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

from app.ui.later import later
from tests.unit.conftest import quieten_a_left_window

pytestmark = pytest.mark.gui


def test_every_timer_under_a_left_window_is_stopped(qtbot) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    page = QWidget(window)                       # a page's own timer, one level down
    window._watch_timer = QTimer(window)
    window._watch_timer.start(4_000)
    follow = QTimer(page)
    follow.start(45_000)
    fired: list = []
    later(window, 50, lambda: fired.append(True))
    idle = QTimer(window)                        # never started: not counted

    assert quieten_a_left_window(window) == 3
    assert not any(t.isActive() for t in window.findChildren(QTimer))
    assert not idle.isActive()
    qtbot.wait(150)
    assert fired == [], "a single-shot fired after its window was put away"


def test_a_window_already_deleted_is_passed_over(qtbot) -> None:
    window = QWidget()
    QTimer(window).start(1_000)
    import shiboken6

    shiboken6.delete(window)
    assert quieten_a_left_window(window) == 0
