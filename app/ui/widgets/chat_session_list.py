"""The sessions sidebar: past conversations, new, rename, delete, pin.

Layer: L5 view

Work order 202626270611 3d. Conversations are kept on this computer only and
listed newest first. Double-click a name (or press Rename, or F2) to change it; a
conversation reopens with its documents on the shelf exactly as you left it.

UI phase B (2026-10-13), "chat should behave like a chat": the list is grouped by
date under headings you cannot pick (`presenter/chat_history.py` decides them),
with a "Search chats" box over it that narrows it as you type, and a right-click
menu - Rename (F2), Pin to top, Delete (Del, which asks first). It stays usable
while an answer is being written: the answer belongs to the conversation that
asked it (`ChatController`), so browsing and switching cannot strand it.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QVBoxLayout, QWidget,
)

from app.ui.presenter.chat_history import (
    PIN_LABEL, SEARCH_CHATS, SEARCH_CHATS_TIP, UNPIN_LABEL, group_sessions,
)

__all__ = ["SessionList", "ID_ROLE"]

ID_ROLE = int(Qt.ItemDataRole.UserRole)


class SessionList(QWidget):
    """The conversations sidebar. Emits only on the person's action; `set_sessions`
    fills quietly (`_loading`).
    """
    selected = Signal(str)
    new_requested = Signal()
    renamed = Signal(str, str)
    deleted = Signal(str)
    #: A conversation pinned to the top (True) or let go back to its date (False).
    pinned = Signal(str, bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatSessions")
        self._loading = False
        self._sessions: list[Any] = []
        self._active = ""
        #: The menu last opened, for the tests (a popped-up menu cannot be awaited).
        self.last_menu: Optional[QMenu] = None
        self.filter = QLineEdit()
        self.filter.setObjectName("chatSessionFilter")
        self.filter.setPlaceholderText(SEARCH_CHATS)
        self.filter.setAccessibleName(SEARCH_CHATS)
        self.filter.setToolTip(SEARCH_CHATS_TIP)
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(lambda _t: self._redraw())
        self.list = QListWidget()
        self.list.setObjectName("chatSessionList")
        self.list.setAccessibleName("Your conversations")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # EditKeyPressed is F2 on Windows and Linux: Rename from the keyboard.
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                  | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.list.currentItemChanged.connect(self._current_changed)
        self.list.itemChanged.connect(self._item_edited)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu_at)
        # The list's own key, not the window's: Delete in the filter box or in a
        # name being typed must stay a text key.
        self._delete_key = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.list)
        self._delete_key.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._delete_key.activated.connect(self._delete)

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
        layout.addWidget(self.filter)
        layout.addWidget(self.list, stretch=1)
        layout.addLayout(buttons)

    def set_sessions(self, sessions: list[Any], active: str = "") -> None:
        """Replace the list without emitting `selected` or `renamed` on the way in."""
        self._sessions, self._active = list(sessions), active
        self._redraw()

    def _redraw(self) -> None:
        """Draw `_sessions` under their headings, narrowed by the filter box."""
        self._loading = True
        try:
            self.list.clear()
            for heading, sessions in group_sessions(self._sessions, time.time(),
                                                    self.filter.text()):
                self.list.addItem(_heading(heading))
                for session in sessions:
                    item = QListWidgetItem(session.title)
                    item.setData(ID_ROLE, session.id)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                    self.list.addItem(item)
                    if session.id == self._active:
                        self.list.setCurrentItem(item)
        finally:
            self._loading = False

    def items(self) -> list[QListWidgetItem]:
        """The conversations' rows as drawn, without the date headings."""
        rows = (self.list.item(i) for i in range(self.list.count()))
        return [item for item in rows if item.data(ID_ROLE)]

    def current_id(self) -> str:
        item = self.list.currentItem()
        return str(item.data(ID_ROLE) or "") if item is not None else ""

    def _current_changed(self, current, _previous) -> None:
        if not self._loading and current is not None and current.data(ID_ROLE):
            self._active = str(current.data(ID_ROLE))
            self.selected.emit(self._active)

    def _item_edited(self, item) -> None:
        """A name typed in place. An empty name is ignored rather than saved."""
        if not self._loading and item.data(ID_ROLE) and item.text().strip():
            self.renamed.emit(str(item.data(ID_ROLE)), item.text().strip())

    def _rename(self) -> None:
        item = self.list.currentItem()
        if item is not None and item.data(ID_ROLE):
            self.list.editItem(item)

    def _session(self, session_id: str) -> Any:
        return next((s for s in self._sessions if s.id == session_id), None)

    # -- the right-click menu ------------------------------------------------------
    def menu_for(self, item: QListWidgetItem) -> QMenu:
        """Rename, Pin to top (or Unpin), Delete - for the conversation `item` names.
        The keys are shown beside the words; the list itself handles them."""
        session_id = str(item.data(ID_ROLE) or "")
        session = self._session(session_id)
        on = bool(getattr(session, "pinned", False))
        from app.ui.widgets import menus  # noqa: PLC0415 - the one menu builder (UI phase B)

        # The keys are labels only (the builder's rule): the list handles F2 and Delete itself.
        menu = menus.build(self, [
            menus.Item("Rename", lambda i=item: self.list.editItem(i), group="act", shortcut="F2"),
            menus.Item(UNPIN_LABEL if on else PIN_LABEL, lambda: self.pinned.emit(session_id, not on),
                       group="remember", icon="pin"),
            menus.Item("Delete", lambda i=item: self._delete(i), group="danger", shortcut="Del"),
        ])
        self.last_menu = menu
        return menu

    def _menu_at(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is not None and item.data(ID_ROLE):
            self.menu_for(item).popup(self.list.viewport().mapToGlobal(pos))

    def confirm_delete(self, title: str) -> bool:
        """Asked before a conversation goes. Overridable so tests need no dialog."""
        # Cancel the default and "Delete" on the button (`widgets/confirm.py`): Qt's
        # Yes/No left Yes the default on Windows, so Enter deleted the conversation.
        from app.ui.widgets.confirm import confirm

        return confirm(
            self, "Delete this conversation?",
            f"Delete \"{title}\"? It is removed from this computer. "
            "Your files are not touched.", "Delete")

    def _delete(self, item: Optional[QListWidgetItem] = None) -> None:
        item = item or self.list.currentItem()
        if item is not None and item.data(ID_ROLE) and self.confirm_delete(item.text()):
            self.deleted.emit(str(item.data(ID_ROLE)))


def _heading(text: str) -> QListWidgetItem:
    """A date heading: drawn, never picked, skipped by the arrow keys."""
    item = QListWidgetItem(text)
    item.setFlags(Qt.ItemFlag.NoItemFlags)
    font = QFont(item.font())
    font.setBold(True)
    item.setFont(font)
    item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{text}, conversations")
    return item
