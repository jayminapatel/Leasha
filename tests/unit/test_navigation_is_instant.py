"""The owner's standing rule (2026-10-11): navigation is instant, everywhere, every aspect.

Layer: L5

Every rail page is opened twice in a real window over a small index. The first visit may build the page
(it is lazy, by design) and must still come up in a blink; every later visit must be a switch and nothing
more. The bounds are loose enough for a loaded laptop and tight enough to catch the kind of fault that
froze the Files page for seconds (a list rebuilt in Python on the window's thread at every click).
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

FIRST_VISIT_MS = 400
LATER_VISIT_MS = 120


@pytest.mark.timing
@pytest.mark.slow
def test_every_page_opens_at_once_and_switches_back_faster(gui_mainwindow, qtbot):
    app, window, _store, _engine = gui_mainwindow
    rail = window.rail
    window.resize(1200, 800)
    window.show()
    qtbot.wait(150)
    slow = []
    for visit, limit in (("first", FIRST_VISIT_MS), ("later", LATER_VISIT_MS)):
        for index in range(rail.count()):
            began = time.perf_counter()
            rail.setCurrentIndex(index)
            app.processEvents()
            took = (time.perf_counter() - began) * 1000
            if took > limit:
                slow.append(f"{rail.tabText(index)} ({visit} visit) {took:.0f} ms > {limit} ms")
            qtbot.wait(40)
    rail.setCurrentIndex(0)
    assert not slow, "pages that were not instant: " + "; ".join(slow)
