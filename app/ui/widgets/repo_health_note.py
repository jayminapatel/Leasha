r"""The Code tab's "this does not look like a checkout" line.

Layer: L5

`app.index.repo_health` was written, with its own warning text, after a copy
of this project's `.git` dragged into a document archive was adopted as a
repository and 44% of the corpus became "code". Its docstring says the warning
"has to arrive where somebody meets it" - and nothing ever called it, so it
lived only in `app.cli repos`. This is where a person actually looks.

Hidden when there is nothing to say, which is nearly always: a warning that is
on screen every day is a warning nobody reads.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QLabel, QWidget

from app.ui.tasks import repo_health_notes
from app.ui.workers import CallableWorker, run

__all__ = ["RepoHealthNote"]


class RepoHealthNote(QLabel):
    """A wrapped, initially hidden label that fills itself from the store."""

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__("", parent)
        self._store = store
        self._generation = 0
        self.setObjectName("repoHealthNote")
        self.setWordWrap(True)
        self.setToolTip(
            "Leasha found a folder with a .git in it and treated it as a "
            "repository, but almost none of what is indexed there is code. "
            "'Code only' searches include it anyway; the command in the "
            "message removes the repository without touching any file.")
        self.setVisible(False)

    def refresh(self) -> None:
        """Ask the store again, off the interface thread."""
        self._generation += 1
        generation = self._generation
        worker = CallableWorker(repo_health_notes, self._store, component="ui.code.health")
        # A plain connect: the note is a child of the Code view, which lives as
        # long as the window; a late answer finds it in place.
        worker.signals.finished.connect(lambda notes, g=generation: self._show(notes, g))
        worker.signals.failed.connect(lambda _e: self._show([], generation))
        run(QThreadPool.globalInstance(), worker)

    def _show(self, notes: Any, generation: int) -> None:
        """UI thread: the notes landed. Dropped if `refresh` was called again since."""
        if generation != self._generation:
            return
        text = "\n\n".join(str(one) for one in (notes or ()))
        self.setText(text)
        self.setVisible(bool(text))
