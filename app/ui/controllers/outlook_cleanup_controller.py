"""Settings > Mail archives > "Move duplicates to Deleted Items", one archive at a time.

Layer: L5

Order 1l (2026-10-11). The one action in Leasha that changes a mail archive, so
it is three steps and the middle one is the owner's: the copies are counted on
a worker, through Outlook (`outlook_cleanup.find_copies`, reads only); the owner
is asked with the number; only a yes moves them, on a worker
(`outlook_cleanup.move_copies`), to that archive's own Deleted Items. The
window still owns the views - everything is reached through `self._w`.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThreadPool

from app.ui.workers import CallableWorker, run

__all__ = ["OutlookCleanupController"]


class OutlookCleanupController(QObject):
    """Count, ask, move. Holds the plan between the count and the yes, nothing else."""

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self._w = window

    def request(self, path: str) -> None:
        """The button: count the copies in this archive, off the window's thread."""
        from app.extract.outlook_cleanup import find_copies
        from app.ui.presenter import copies_counting_message

        worker = CallableWorker(find_copies, path, component="ui.outlook_cleanup")
        worker.signals.finished.connect(lambda plan: self._confirm(path, plan))
        worker.signals.failed.connect(self._w._show_error)
        self._w.notify(copies_counting_message(path))
        run(QThreadPool.globalInstance(), worker)

    def _confirm(self, path: str, plan: Any) -> None:
        """UI thread: nothing to move says so; otherwise ask, and move only on a yes."""
        from app.extract.outlook_cleanup import move_copies
        from app.ui.presenter import no_copies_message

        if not plan.copies:
            self._w.notify(no_copies_message(path, plan.examined), 20_000)
            return
        if not self.ask(path, plan):
            return
        worker = CallableWorker(move_copies, plan, component="ui.outlook_cleanup")
        worker.signals.finished.connect(lambda result: self._moved(path, result))
        worker.signals.failed.connect(self._w._show_error)
        run(QThreadPool.globalInstance(), worker)

    def ask(self, path: str, plan: Any) -> bool:
        """The question, with the count. Overridable so tests need no dialog."""
        from PySide6.QtWidgets import QMessageBox

        from app.ui.presenter import copies_confirmation

        examples = [f"\"{(c.subject or '(no subject)')[:50]}\" in {c.folder or 'the top'}"
                    for c in plan.copies[:5]]
        title, body = copies_confirmation(path, plan.count, examples)
        answer = QMessageBox.question(
            self._w, title, body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes

    def _moved(self, path: str, result: Any) -> None:
        from app.ui.presenter import copies_moved_message

        moved, left = result
        self._w.notify(copies_moved_message(path, int(moved), int(left)), 20_000)
