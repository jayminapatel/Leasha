"""A page closed while its worker is out must not have Qt write into its labels.

Layer: L5 (Qt, offscreen).

**Found 1 October 2026 by bisecting 108 test files.** `test_reports_read_only`'s
timeline test failed with "wrapped C/C++ object of type QLabel has been deleted"
and no Python frame - only when `test_read_order_ui` ran before it. That file
closes a Settings page within a moment of building it; the Environment box on
that page had wired its logs worker straight to `self.logs_status.setText`, so
when the folder walk finished, Qt called setText on a deleted label, inside
whatever test was pumping events next.
"""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6 import sip
from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QApplication


def test_a_closed_environment_box_ignores_its_late_logs_answer(qtbot, tmp_path, monkeypatch):
    """`qtbot`, because pytest-qt is what turns an exception raised inside
    Qt's event loop into a failure - exactly how the timeline test failed."""
    import threading

    from app.ui.widgets import environment_box

    release = threading.Event()

    def slow_summary(_folder):
        release.wait(5)                         # still walking when the box goes
        return "12 log files, 3 MB"

    monkeypatch.setattr(environment_box, "logs_summary", slow_summary)
    box = environment_box.EnvironmentBox(SimpleNamespace(log_path=str(tmp_path)))
    box.refresh_logs()
    sip.delete(box)                             # the page closed: box and label gone
    release.set()
    QThreadPool.globalInstance().waitForDone(5_000)
    for _ in range(5):
        QApplication.processEvents()
