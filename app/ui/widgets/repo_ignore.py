r"""The Code tab's "Ignore this repository", with an Undo that is exact.

Layer: L5

Order 0y §1c, finishing `WORKORDER-202626081149-code-tab.md` §2 item 1. A
copy of a project's `.git` dragged into a document archive once made 44% of a
corpus "code"; the way back was built as `app.cli repos --forget` and never
reached the window. This puts it on the row menu of the list where the wrong
repository is seen.

**Nothing is deleted and nothing is re-indexed.** The files keep their rows,
passages and vectors; they only stop counting as code (`forget_repo`), and the
folder is remembered as "not a repository" so the next walk does not adopt it
again (`ignore_repo_root`). That is why one confirmation is enough.

**Undo is immediate and exact.** The record `repo_undo_record` reads *before*
the forget holds the name, kind and file ids, and `restore_repo` puts back
precisely those - no index run in between. The note offering it stays until it
is used or dismissed.

Everything that touches the store runs on a worker (non-negotiable #5).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QLabel, QMessageBox, QWidget

from app.ui.workers import CallableWorker, run

__all__ = ["confirm_ignore", "ignore_repository", "undo_note"]


def confirm_ignore(parent: QWidget, name: str) -> bool:
    """Ask once, in words that say what happens and what does not."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Ignore this repository")
    box.setText(f"Stop treating “{name}” as a repository?")
    box.setInformativeText(
        "Its files stay indexed and searchable - they stop counting as code, "
        "and Leasha will not treat this folder as a repository again. "
        "Nothing is deleted, and you can undo it.")
    ignore = box.addButton("Ignore repository", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    return box.clickedButton() is ignore


def _ignore(store: Any, root: str) -> Optional[dict]:
    """Worker side: remember how to undo, then forget and ignore."""
    record = store.repo_undo_record(root)
    store.forget_repo(root)
    store.ignore_repo_root(root)
    return record


def undo_note(view: Any) -> QLabel:
    """The one line under the summary that offers Undo. Made on first use."""
    note = getattr(view, "_ignore_note", None)
    if note is None:
        note = QLabel("", wordWrap=True, visible=False)
        note.setObjectName("repoIgnoredNote")
        view.layout().insertWidget(2, note)
        view._ignore_note = note
    return note


def ignore_repository(view: Any, name: str, *,
                      confirm: Optional[Callable[[QWidget, str], bool]] = None) -> None:
    """Row menu → confirmation → forget on a worker → a note with Undo.

    `confirm` defaults to `confirm_ignore`, looked up when called so a test can
    stand in for the dialog."""
    confirm = confirm or confirm_ignore
    from app.ui.presenter import repo_root_for

    root = repo_root_for(view._repos, name)
    if not root or not confirm(view, name):
        return

    def done(record: Any) -> None:
        """UI thread: the forget is written. Offer Undo on the note and refresh the list."""
        note = undo_note(view)
        count = len((record or {}).get("file_ids") or ())
        note.setText(
            f"“{name}” is no longer treated as a repository - its {count:,} "
            f"file(s) are still indexed and searchable. "
            f"<a href='undo'>Undo</a> · <a href='dismiss'>Dismiss</a>")
        try:
            note.linkActivated.disconnect()
        # `disconnect()` with nothing connected raises TypeError under PySide6;
        # the first note has no handler yet, and that is fine.
        except TypeError:
            pass
        note.linkActivated.connect(
            lambda link: _undo(view, record) if link == "undo" else note.setVisible(False))
        note.setVisible(True)
        view.refresh()

    worker = CallableWorker(_ignore, view._store, root, component="ui.code.ignore")
    worker.signals.finished.connect(done)
    worker.signals.failed.connect(view.error.emit)
    run(QThreadPool.globalInstance(), worker)


def _undo(view: Any, record: Any) -> None:
    """Put the repository back from the record, on a worker, and refresh."""
    note = undo_note(view)
    note.setVisible(False)
    if not record:
        return
    worker = CallableWorker(view._store.restore_repo, record, component="ui.code.ignore")
    worker.signals.finished.connect(lambda _n: view.refresh())
    worker.signals.failed.connect(view.error.emit)
    run(QThreadPool.globalInstance(), worker)
