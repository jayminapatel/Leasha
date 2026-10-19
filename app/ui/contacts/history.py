"""History: the journal of one contact, with Undo beside each change.

Layer: L5

Order 1k 6e, 3e. Every change - an edit, a merge, a clean-up, a sync - is a journal entry in the
hub, newest first here, each with the words for what happened, who did it and when. Undo puts
that entry's "before" back (`hub.undo_entry`); the page runs it on a worker and, when the host
has `service.push_accounts`, sends the accounts that follow the change.
"""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHeaderView, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from app.ui.contacts.logic import describe_entry
from app.ui.theme import SPACE_S, SPACE_M, theme_colours
from app.ui.widgets.buttons import action_button, put_on_row
from app.ui.widgets.icons import icon
from app.ui.widgets.row_ring import install_row_ring

__all__ = ["HistoryPanel"]

#: Entries that make no sense to undo one by one.
NOT_UNDOABLE = ("undo", "conflict")


def when(ms: int) -> str:
    return time.strftime("%d %b %Y %H:%M", time.localtime(ms / 1000))


class HistoryPanel(QWidget):
    undo_requested = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.title = QLabel("History")
        self.title.setObjectName("contactSectionTitle")
        self.empty = QLabel("Nothing has changed on this contact yet.")
        self.empty.setObjectName("resultsSummary")
        self.tree = QTreeWidget()
        install_row_ring(self.tree)
        self.tree.setHeaderLabels(["When", "What happened", ""])
        self.tree.setRootIsDecorated(False)
        self.tree.setAccessibleName("Changes to this contact")
        self.tree.setToolTip("Every change to this contact, newest first")
        self.tree.setUniformRowHeights(False)
        head = self.tree.header()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_M, SPACE_S, SPACE_M, SPACE_S)
        layout.addWidget(self.title)
        layout.addWidget(self.empty)
        layout.addWidget(self.tree, 1)
        self.setAccessibleName("Contact history")
        self._cid: Optional[int] = None

    def clear(self, words: str = "Pick a contact to see its history.") -> None:
        self._cid = None
        self.tree.clear()
        self.empty.setText(words)
        self.empty.show()
        self.title.setText("History")

    def show_history(self, cid: int, name: str, rows: list, account_names: dict[int, str]) -> None:
        self._cid = cid
        self.title.setText(f"History of {name}" if name else "History")
        self.tree.clear()
        dim = theme_colours().get("text_dim", "#888888")
        self.empty.setVisible(not rows)
        self.empty.setText("Nothing has changed on this contact yet.")
        for j in rows:
            glyph, words = describe_entry(j, account_names)
            item = QTreeWidgetItem([when(j.at), words + ("  (undone)" if j.undone_at else ""), ""])
            item.setIcon(1, icon(glyph, dim))
            item.setData(0, Qt.ItemDataRole.UserRole, j.id)
            self.tree.addTopLevelItem(item)
            if j.undone_at is None and j.action not in NOT_UNDOABLE:
                button = action_button("Undo", "undo-2", "secondary")
                button.setToolTip("Put this contact back as it was before this change")
                button.setAccessibleName(f"Undo: {words}")
                button.clicked.connect(lambda _c=False, i=j.id: self.undo_requested.emit(i))
                put_on_row(self.tree, item, 2, button)
