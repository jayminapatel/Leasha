"""The `/` menu for the chat box: type `/` and the switches every other box has are offered.

Layer: L5

The chat box is a multi-line text edit, and the shared `CommandPopup` is built on a one-line box, so this is a
small menu of its own for the same catalogue (`app.search.commands.COMMANDS`): the same names, the same words,
so `/from dave` means the same thing here as in Search, Mail and Files. What is typed is turned into filters on
the search that answers the question by `app.chat.switches`. Offered when a word that starts with `/` is being
typed; Up and Down choose, Enter or Tab inserts, Esc closes - and when the menu is not open the keys do what
they always did.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QPlainTextEdit

from app.search.commands import COMMANDS, matching
from app.ui.widgets.command_icon import command_icon, picture_for, text_colour
from app.ui.widgets.icons import icon_size

__all__ = ["attach_chat_commands", "ChatCommandMenu"]


class ChatCommandMenu(QObject):
    def __init__(self, edit: QPlainTextEdit) -> None:
        super().__init__(edit)
        self._edit = edit
        self._list = QListWidget(edit)
        self._list.setWindowFlags(Qt.WindowType.ToolTip)
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setAccessibleName("Switches you can type after a slash")
        self._list.setToolTip("Choose a switch: Up and Down, then Enter or Tab")
        self._list.setIconSize(icon_size("menu"))
        self._list.itemClicked.connect(lambda item: self._insert(item.data(Qt.ItemDataRole.UserRole)))
        edit.textChanged.connect(self._refresh)
        edit.installEventFilter(self)
        edit.cursorPositionChanged.connect(self._refresh)

    # -- what is being typed ------------------------------------------------
    def word(self) -> str:
        """The `/word` under the cursor, or ''. Only a word that starts with a slash, at the start of a word."""
        cursor = self._edit.textCursor()
        text = self._edit.toPlainText()[:cursor.position()]
        piece = text.rsplit(None, 1)[-1] if text.strip() and not text[-1].isspace() else ""
        return piece if piece.startswith("/") and "/" not in piece[1:] else ""

    def _refresh(self) -> None:
        word = self.word()
        found = matching(word[1:]) if word else []
        if not found or (len(found) == 1 and word[1:] == found[0].name):
            self._list.hide()
            return
        self._list.clear()
        ink = text_colour(self._list)
        for command in found:
            # The switch's Lucide picture, as in every other `/` menu (UI phase B);
            # the painted character it used to lead with is the fallback.
            item = QListWidgetItem(command_icon(picture_for(command), command.icon, ink),
                                   f"/{command.name}   {command.summary}")
            item.setData(Qt.ItemDataRole.UserRole, command.name)
            item.setToolTip(f"{command.example}  -  {command.value_hint}")
            self._list.addItem(item)
        self._list.setCurrentRow(0)
        rect = self._edit.cursorRect()
        below = self._edit.viewport().mapToGlobal(rect.bottomLeft())
        height = min(len(found), 10) * 24 + 8
        self._list.setFixedSize(520, height)
        self._list.move(below.x(), max(0, below.y() - height - rect.height() - 6))     # above: the box is at the bottom
        self._list.show()

    def _insert(self, name: Any) -> None:
        cursor = self._edit.textCursor()
        word = self.word()
        if not word or not name:
            return
        for _ in word:
            cursor.deletePreviousChar()
        cursor.insertText(f"/{name} ")
        self._edit.setTextCursor(cursor)
        self._list.hide()

    # -- keys ---------------------------------------------------------------
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:                # noqa: N802 - Qt
        if obj is self._edit and event.type() == QEvent.Type.KeyPress and self._list.isVisible():
            key = event.key()
            if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                step = -1 if key == Qt.Key.Key_Up else 1
                self._list.setCurrentRow(max(0, min(self._list.count() - 1, self._list.currentRow() + step)))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                item = self._list.currentItem()
                self._insert(item.data(Qt.ItemDataRole.UserRole) if item else None)
                return True
            if key == Qt.Key.Key_Escape:
                self._list.hide()
                return True
        if event.type() == QEvent.Type.FocusOut:
            self._list.hide()
        return False


def attach_chat_commands(edit: QPlainTextEdit) -> ChatCommandMenu:
    """Give a chat box its `/` menu. Returns it, for tests."""
    return ChatCommandMenu(edit)


#: Every switch the menu can offer: the catalogue, once, so a test can say the chat box offers all of them.
OFFERED = tuple(c.name for c in COMMANDS)
