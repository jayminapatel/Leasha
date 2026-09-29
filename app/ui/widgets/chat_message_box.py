"""Where the question is typed, and the keys that steer the whole tab.

Layer: L5 view

Work order 202626270611 3a and 4e-3. The keyboard flow is the search tab's
muscle memory, so somebody who lives on the keyboard never reaches for the
mouse:

* Enter sends; Shift+Enter starts a new line.
* Up and Down walk the Sources list **with focus staying in the box** - they
  only move the text cursor when there is a line above or below to move to.
* Enter with the box empty and a source picked opens that source.
* Esc drops the picked source and returns to the conversation.

While an answer is being written Send gives way to Stop.

**The box grows with what is typed** - by wrapped lines, not just by Enter - up to
`MAX_LINES`, then scrolls inside itself, so a long question never hides its own
first line and a short one takes one line of the page. The **Web** chip beside it is
the per-conversation switch for looking things up on the web (owner, 2026-09-20): it is
absent unless Settings allows the web at all, and off until the person turns it on.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QTextCursor
from PyQt6.QtWidgets import QHBoxLayout, QPlainTextEdit, QPushButton, QWidget

from app.ui.presenter.chat import PLACEHOLDER, WEB_LABEL, WEB_OFF_TIP, WEB_ON_TIP

__all__ = ["MessageBox"]

MAX_LINES = 5


class _Edit(QPlainTextEdit):
    submitted = pyqtSignal()
    open_source = pyqtSignal()
    walk = pyqtSignal(int)
    escaped = pyqtSignal()

    def event(self, event: QEvent) -> bool:
        """Claim Esc from the window's own Esc shortcut (which clears Search)."""
        if (event.type() == QEvent.Type.ShortcutOverride
                and getattr(event, "key", lambda: None)() == Qt.Key.Key_Escape):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:           # noqa: N802 - Qt
        key = event.key()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not shift:
            (self.submitted if self.toPlainText().strip() else self.open_source).emit()
            event.accept()
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and not shift:
            cursor = self.textCursor()
            block = cursor.block()
            edge = (not block.previous().isValid() if key == Qt.Key.Key_Up
                    else not block.next().isValid())
            if edge:
                self.walk.emit(-1 if key == Qt.Key.Key_Up else 1)
                event.accept()
                return
        if key == Qt.Key.Key_Escape:
            self.escaped.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class MessageBox(QWidget):
    """The text box with Send and Stop beside it."""

    submitted = pyqtSignal(str)
    stop_requested = pyqtSignal()
    walk_requested = pyqtSignal(int)
    open_source_requested = pyqtSignal()
    escaped = pyqtSignal()
    #: The Web chip was switched (`True` = on for this conversation).
    web_toggled = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.edit = _Edit()
        self.edit.setObjectName("chatMessage")
        self.edit.setPlaceholderText(PLACEHOLDER)
        self.edit.setAccessibleName("Your question")
        self.edit.setToolTip(
            "Type your question. Enter sends it; Shift+Enter starts a new line. "
            "Up and Down move through the sources on the right without leaving "
            "this box.")
        self.edit.setTabChangesFocus(True)
        self.edit.submitted.connect(self._submit)
        self.edit.open_source.connect(self.open_source_requested)
        self.edit.walk.connect(self.walk_requested)
        self.edit.escaped.connect(self.escaped)
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

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setAlignment(Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.web, alignment=Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.edit, stretch=1)
        row.addWidget(self.send_button)
        row.addWidget(self.stop_button)
        self._busy = False
        self._reason = ""
        self._fit()

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

    def set_text(self, text: str) -> None:
        """Put text in the box (an edited message coming back to be changed)."""
        self.edit.setPlainText(text)
        self.focus()

    def _submit(self) -> None:
        text = self.edit.toPlainText().strip()
        if self._busy or self._reason or not text:
            return
        self.edit.clear()
        self.submitted.emit(text)

    def set_busy(self, busy: bool) -> None:
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
