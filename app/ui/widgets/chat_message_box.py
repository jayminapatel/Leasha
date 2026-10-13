"""Where the question is typed, and the keys that steer the whole tab.

Layer: L5 view

Work order 202626270611 3a and 4e-3. The keyboard flow is the search tab's
muscle memory, so somebody who lives on the keyboard never reaches for the
mouse:

* Enter sends; Shift+Enter starts a new line. Enter in an empty box does nothing.
* Alt+Up and Alt+Down walk the Sources list **with focus staying in the box**.
* Up in an empty box brings your last message back to be changed, as every chat
  does. It replaces that message and its answer only when it is sent; Esc, or
  emptying the box, lets it go and nothing is lost.
* Esc drops the picked source and returns to the conversation.

UI phase B (2026-10-13): plain Up and Down walked the Sources list and Enter in an
empty box opened the picked source. Up is the chat's own key now, so the walk moved
to Alt (free in the window: `shell._build_shortcuts` binds no Alt key), and a stray
Enter in an empty box no longer opens a document.

While an answer is being written Send gives way to Stop.

**The box grows with what is typed** - by wrapped lines, not just by Enter - up to
`MAX_LINES`, then scrolls inside itself, so a long question never hides its own
first line and a short one takes one line of the page. The **Web** chip beside it is
the per-conversation switch for looking things up on the web (owner, 2026-09-20): it is
absent unless Settings allows the web at all, and off until the person turns it on.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeyEvent, QTextCursor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from app.ui.presenter.chat import PLACEHOLDER, WEB_LABEL, WEB_OFF_TIP, WEB_ON_TIP

__all__ = ["MessageBox"]

MAX_LINES = 5


class _Edit(QPlainTextEdit):
    """The text box itself: Enter sends, Shift+Enter breaks the line, Alt+Up/Down
    walk the Sources list, Up in an empty box recalls, Esc is claimed from the window.
    """
    submitted = Signal()
    walk = Signal(int)
    recall = Signal()
    escaped = Signal()

    def event(self, event: QEvent) -> bool:
        """Claim Esc from the window's own Esc shortcut (which clears Search)."""
        if (event.type() == QEvent.Type.ShortcutOverride
                and getattr(event, "key", lambda: None)() == Qt.Key.Key_Escape):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:           # noqa: N802 - Qt
        key = event.key()
        modifiers = event.modifiers()
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not shift:
            if self.toPlainText().strip():
                self.submitted.emit()
            event.accept()
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and alt and not shift:
            self.walk.emit(-1 if key == Qt.Key.Key_Up else 1)
            event.accept()
            return
        if key == Qt.Key.Key_Up and not (shift or alt) and not self.toPlainText():
            self.recall.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            self.escaped.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class MessageBox(QWidget):
    """The text box with Send and Stop beside it."""

    submitted = Signal(str)
    stop_requested = Signal()
    walk_requested = Signal(int)
    #: Up in the empty box: bring the last message back (`begin_edit`).
    recall_requested = Signal()
    #: A message brought back by Up, sent changed: it replaces the last one.
    edit_submitted = Signal(str)
    escaped = Signal()
    #: The Web chip was switched (`True` = on for this conversation).
    web_toggled = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.edit = _Edit()
        self.edit.setObjectName("chatMessage")
        self.edit.setPlaceholderText(PLACEHOLDER)
        self.edit.setAccessibleName("Your question")
        self.edit.setToolTip(
            "Type your question. Enter sends it; Shift+Enter starts a new line. "
            "Type / for the switches every other search box has (/from, /type, /after, /attachment ...). "
            "Alt+Up and Alt+Down move through the sources on the right without leaving "
            "this box. Up in an empty box brings back your last message to change.")
        self.edit.setTabChangesFocus(True)
        from app.ui.widgets.chat_commands import attach_chat_commands

        self.commands = attach_chat_commands(self.edit)             # `/` offers the same switches as every box
        self.edit.submitted.connect(self._submit)
        self.edit.walk.connect(self.walk_requested)
        self.edit.recall.connect(self.recall_requested)
        self.edit.escaped.connect(self._escaped)
        #: The box holds the last message, brought back by Up (`begin_edit`).
        self.editing = False
        self.edit.textChanged.connect(self._emptied)
        self.edit.document().documentLayout().documentSizeChanged.connect(
            lambda _size: self._fit())
        self.edit.textChanged.connect(self._fit)

        self.send_button = QPushButton("Send")
        self.send_button.setToolTip(
            "Ask this question. Leasha searches what you have kept and answers "
            "from what it finds, with the passages it used.")
        self.send_button.clicked.connect(lambda _c=False: self._submit())

        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip(
            "Stop answering now. What has appeared so far stays on screen.")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(lambda _c=False: self.stop_requested.emit())

        self.web = QPushButton(WEB_LABEL)
        self.web.setObjectName("chatWebChip")
        self.web.setCheckable(True)
        self.web.setAccessibleName("Let this conversation search the web")
        self.web.setToolTip(WEB_OFF_TIP)
        self.web.setVisible(False)
        self.web.toggled.connect(self._web_toggled)

        # Order 1o D4: one rounded box - the words on top, and along its bottom edge a
        # quiet row: the web chip, the model and speed (`add_to_footer`), then Send/Stop.
        self.frame = QFrame()
        self.frame.setObjectName("chatComposer")
        inside = QVBoxLayout(self.frame)
        inside.setContentsMargins(12, 8, 8, 8)
        inside.setSpacing(4)
        inside.addWidget(self.edit)
        self.footer = QHBoxLayout()
        self.footer.setSpacing(6)
        self.footer.addWidget(self.web)
        self.footer.addStretch(1)
        self.footer.addWidget(self.send_button)
        self.footer.addWidget(self.stop_button)
        inside.addLayout(self.footer)
        self.send_button.setObjectName("chatSend")
        self.stop_button.setObjectName("chatStop")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.frame)
        self._busy = False
        self._reason = ""
        self._fit()

    def add_to_footer(self, *widgets: QWidget) -> None:
        """Order 1o D4: the box's own controls, after the web chip, before Send."""
        at = self.footer.indexOf(self.web) + 1
        for offset, widget in enumerate(widgets):
            self.footer.insertWidget(at + offset, widget)

    def _fit(self) -> None:
        """Height for the wrapped text as it stands: one line up to `MAX_LINES`."""
        line = self.edit.fontMetrics().lineSpacing()
        # **Lines, not pixels.** A `QPlainTextEdit`'s document layout reports
        # its height as the number of laid-out (wrapped) lines, so the old
        # `height + 14` compared a line count with pixel bounds: the box only
        # grew once the text wrapped onto more lines than `line + 8` - about
        # 22 with DejaVu Sans, which is why it passed on Linux, and never for
        # a normal question with Segoe UI on Windows, where the Windows CI
        # caught it (2026-09-29). The `+ 22` is the same allowance for frame
        # and padding as the one-line floor below, so one line is exactly it.
        lines = self.edit.document().size().height()
        wanted = int(round(lines * line)) + 22
        low, high = line + 22, line * MAX_LINES + 22
        self.edit.setFixedHeight(max(low, min(high, wanted)))

    def resizeEvent(self, event: object) -> None:                 # noqa: N802 - Qt
        super().resizeEvent(event)
        self._fit()

    def _web_toggled(self, on: bool) -> None:
        self.web.setToolTip(WEB_ON_TIP if on else WEB_OFF_TIP)
        self.web_toggled.emit(bool(on))

    def set_web(self, available: bool, on: bool = False) -> None:
        """Show the Web chip (only when Settings allows the web) and set it, silently."""
        self.web.setVisible(bool(available))
        self.web.blockSignals(True)
        try:
            self.web.setChecked(bool(on) and bool(available))
        finally:
            self.web.blockSignals(False)
        self.web.setToolTip(WEB_ON_TIP if self.web.isChecked() else WEB_OFF_TIP)

    def begin_edit(self, text: str) -> None:
        """The last message, back in the box to be changed. Nothing is taken from the
        conversation until it is sent (`edit_submitted`)."""
        if self._busy or self._reason:
            return
        self.set_text(text)
        self.editing = True

    def _emptied(self) -> None:
        if not self.edit.toPlainText():
            self.editing = False             # emptied: it is a new message from here

    def _escaped(self) -> None:
        if self.editing:
            self.edit.clear()                # Esc lets the brought-back message go
        self.escaped.emit()

    def send_text(self, text: str) -> None:
        """Put `text` in the box and send it (a starter card)."""
        self.edit.setPlainText(text)
        self._submit()

    def set_text(self, text: str) -> None:
        """Put text in the box (an edited message coming back to be changed)."""
        self.edit.setPlainText(text)
        self.focus()

    def _submit(self) -> None:
        """Send what is typed, unless busy, unavailable or empty. Clears the box first."""
        text = self.edit.toPlainText().strip()
        if self._busy or self._reason or not text:
            return
        editing, self.editing = self.editing, False
        self.edit.clear()
        (self.edit_submitted if editing else self.submitted).emit(text)

    def set_busy(self, busy: bool) -> None:
        """An answer is being written: Send gives way to Stop."""
        self._busy = busy
        self.send_button.setVisible(not busy)
        self.stop_button.setVisible(busy)
        if not busy:                            # ready for the next Stop
            self.stop_button.setEnabled(True)
            self.stop_button.setText("Stop")

    def set_unavailable(self, reason: str) -> None:
        """Greyed, with the reason on it - not hidden, not silent."""
        self._reason = reason or ""
        off = bool(self._reason)
        self.send_button.setEnabled(not off)
        self.edit.setEnabled(not off)
        tip = self._reason if off else (
            "Ask this question. Leasha searches what you have kept and answers "
            "from what it finds, with the passages it used.")
        self.send_button.setToolTip(tip)
        self.edit.setPlaceholderText(self._reason if off else PLACEHOLDER)

    def focus(self) -> None:
        if self.edit.isEnabled():
            self.edit.setFocus()
            self.edit.moveCursor(QTextCursor.MoveOperation.End)
