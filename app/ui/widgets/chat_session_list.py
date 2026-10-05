"""The sessions sidebar: past conversations, new, rename, delete.

Layer: L5 view

Work order 202626270611 3d. Conversations are kept on this computer only and
listed newest first. Double-click a name (or press Rename) to change it; a
conversation reopens with its documents on the shelf exactly as you left it.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

__all__ = ["SessionList"]

ID_ROLE = int(Qt.ItemDataRole.UserRole)


class SessionList(QWidget):
    selected = Signal(str)
    new_requested = Signal()
    renamed = Signal(str, str)
    deleted = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatSessions")
        self._loading = False
        self.list = QListWidget()
        self.list.setObjectName("chatSessionList")
        self.list.setAccessibleName("Your conversations")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                  | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.list.currentItemChanged.connect(self._current_changed)
        self.list.itemChanged.connect(self._item_edited)

        self.new_button = QPushButton("New chat")
        self.new_button.setToolTip(
            "Start a fresh conversation. The one you are in is kept in the "
            "list, so you can come back to it.")
        self.new_button.clicked.connect(lambda _c=False: self.new_requested.emit())
        self.rename_button = QPushButton("Rename")
        self.rename_button.setToolTip(
            "Give the chosen conversation a name you will recognise. "
            "Double-clicking its name does the same.")
        self.rename_button.clicked.connect(lambda _c=False: self._rename())
        self.delete_button = QPushButton("Delete")
        self.delete_button.setToolTip(
            "Remove the chosen conversation from this computer. Your files "
            "are not touched.")
        self.delete_button.clicked.connect(lambda _c=False: self._delete())

        buttons = QHBoxLayout()
        buttons.addWidget(self.rename_button)
        buttons.addWidget(self.delete_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.new_button)
        layout.addWidget(self.list, stretch=1)
        layout.addLayout(buttons)

    def set_sessions(self, sessions: list[Any], active: str = "") -> None:
        self._loading = True
        try:
            self.list.clear()
            for session in sessions:
                item = QListWidgetItem(session.title)
                item.setData(ID_ROLE, session.id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                self.list.addItem(item)
                if session.id == active:
                    self.list.setCurrentItem(item)
        finally:
            self._loading = False

    def current_id(self) -> str:
        item = self.list.currentItem()
        return str(item.data(ID_ROLE)) if item is not None else ""

    def _current_changed(self, current, _previous) -> None:
        if not self._loading and current is not None:
            self.selected.emit(str(current.data(ID_ROLE)))

    def _item_edited(self, item) -> None:
        if not self._loading and item.text().strip():
            self.renamed.emit(str(item.data(ID_ROLE)), item.text().strip())

    def _rename(self) -> None:
        item = self.list.currentItem()
        if item is not None:
            self.list.editItem(item)

    def confirm_delete(self, title: str) -> bool:
        """Asked before a conversation goes. Overridable so tests need no dialog."""
        answer = QMessageBox.question(
            self, "Delete this conversation?",
            f"Delete \"{title}\"? It is removed from this computer. "
            "Your files are not touched.")
        return answer == QMessageBox.StandardButton.Yes

    def _delete(self) -> None:
        item = self.list.currentItem()
        if item is not None and self.confirm_delete(item.text()):
            self.deleted.emit(str(item.data(ID_ROLE)))

    def set_enabled_for_work(self, idle: bool) -> None:
        """Switching conversations mid-answer would strand the answer."""
        for part in (self.list, self.new_button, self.rename_button, self.delete_button):
            part.setEnabled(idle)
