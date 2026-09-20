"""Chat: talk to Leasha - it reads your files first, and answers in plain words.

Layer: L5 view - thin. Decisions are in `app/ui/presenter/chat.py`; the pieces
are in `app/ui/widgets/chat_*.py`; the engine, the worker and the saving are in
`app/ui/controllers/chat_controller.py`. This file only lays them out and
turns the person's keys and clicks into signals.

Work order 202626270611 section 3. The layout is three columns: your
conversations, the conversation itself (bubbles, the documents on the shelf,
the box you type in), and the local sources the answer stands on. **No banners:**
the receipts are the honesty - every source number in the prose opens the passage
it came from. The message actions (Copy, Regenerate, Try again, Edit) live on the
messages themselves (`chat_bubbles.py`); this file only forwards them.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from app.ui.presenter.chat import NOT_BUILT_LINE, THINKING_LINE, unavailable_text
from app.ui.widgets.chat_answer_run import AnswerRun
from app.ui.widgets.chat_bubbles import AnswerBubble, BubbleList, UserBubble
from app.ui.widgets.chat_message_box import MessageBox
from app.ui.widgets.chat_session_list import SessionList
from app.ui.widgets.chat_shelf import ShelfBar
from app.ui.widgets.chat_sources import SourcesPane

__all__ = ["ChatView"]

SPEEDS = (("Fast", "fast"), ("Thoughtful", "thoughtful"))


class ChatView(QWidget):
    question_submitted = pyqtSignal(str)
    stop_requested = pyqtSignal()
    recheck_requested = pyqtSignal()
    speed_changed = pyqtSignal(str)
    shelf_changed = pyqtSignal()
    open_requested = pyqtSignal(str)            # a path (a shelf chip)
    result_opened = pyqtSignal(object)          # a result row (a source, a hit)
    result_revealed = pyqtSignal(object)
    #: The Sources pane's right-click menu: the row that was clicked.
    pin_requested = pyqtSignal(object)
    reindex_requested = pyqtSignal(object)
    similar_requested = pyqtSignal(object)
    error = pyqtSignal(object)
    #: The window is closing: the controller stops any answer still running.
    closing = pyqtSignal()
    #: The last answer's Regenerate / Try again, and the last message's Edit.
    regenerate_requested = pyqtSignal()
    retry_requested = pyqtSignal()
    edit_requested = pyqtSignal()
    #: The Web chip was switched for this conversation.
    web_toggled = pyqtSignal(bool)
    #: The person answered "Search the web for ...?" - `True` is Allow.
    web_decided = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.sessions = SessionList()
        self.bubbles = BubbleList()
        self.shelf = ShelfBar()
        self.box = MessageBox()
        self.sources = SourcesPane()
        self._run: Optional[AnswerRun] = None
        self._owner: Any = None                  # whose sources the pane shows

        self.notice = QLabel("")
        self.notice.setObjectName("chatNotice")
        self.notice.setWordWrap(True)
        self.notice.setAccessibleName("Why chat is not ready")
        self.notice.setVisible(False)
        self.recheck_button = QPushButton("Check again")
        self.recheck_button.setToolTip(
            "Look again for the helper program Chat needs. Use this after "
            "installing or starting it. Searching is not affected either way.")
        self.recheck_button.setVisible(False)
        self.recheck_button.clicked.connect(lambda _c=False: self.recheck_requested.emit())
        self.speed = QComboBox()
        self.speed.setAccessibleName("How Chat answers")
        self.speed.setToolTip(
            "Fast answers sooner; Thoughtful takes longer over harder "
            "questions. Applies to the next question you ask.")
        for label, value in SPEEDS:
            self.speed.addItem(label, value)
        self.speed.currentIndexChanged.connect(
            lambda _i: self.speed_changed.emit(str(self.speed.currentData())))

        self.speed_note = QLabel("")
        self.speed_note.setObjectName("chatSpeedNote")
        self.speed_note.setWordWrap(True)
        self.speed_note.setAccessibleName("What Fast and Thoughtful do")
        self.speed_note.setVisible(False)

        head = QHBoxLayout()
        head.addWidget(self.notice, stretch=1)
        head.addWidget(self.recheck_button)
        head.addWidget(self.speed)
        centre = QWidget()
        column = QVBoxLayout(centre)
        # 10 a side: the three panes of the splitter met with a hairline between
        # them, so the footer line, the question box and "Send" all touched it.
        column.setContentsMargins(10, 0, 10, 0)
        column.addLayout(head)
        column.addWidget(self.speed_note)
        column.addWidget(self.bubbles, stretch=1)
        column.addWidget(self.shelf)
        column.addWidget(self.box)

        split = QSplitter(Qt.Orientation.Horizontal)
        for part, weight in ((self.sessions, 1), (centre, 4), (self.sources, 2)):
            split.addWidget(part)
            split.setStretchFactor(split.indexOf(part), weight)
        split.setChildrenCollapsible(False)
        outer = QHBoxLayout(self)
        outer.addWidget(split)

        self.box.submitted.connect(self.question_submitted)
        self.box.stop_requested.connect(self.stop_requested)
        self.box.web_toggled.connect(self.web_toggled)
        self.box.walk_requested.connect(self.sources.walk)
        self.box.open_source_requested.connect(self.sources.open_selected)
        self.box.escaped.connect(self._back_to_conversation)
        self.shelf.changed.connect(self.shelf_changed)
        self.shelf.open_requested.connect(self.open_requested)
        self.sources.opened.connect(self._opened)
        self.sources.revealed.connect(self.result_revealed)
        self.sources.results.pin_requested.connect(self.pin_requested)
        self.sources.results.reindex_requested.connect(self.reindex_requested)
        self.sources.results.similar_requested.connect(self.similar_requested)

    # -- state the controller sets --------------------------------------------
    def focus(self) -> None:
        self.box.focus()

    def shutdown(self) -> None:
        self.closing.emit()

    def show_available(self, ok: bool, reason: str = "", *, built: bool = True) -> None:
        text = "" if ok else (unavailable_text(reason) if built else NOT_BUILT_LINE)
        self.notice.setText(text)
        self.notice.setVisible(not ok)
        self.recheck_button.setVisible(not ok and built)
        self.box.set_unavailable("" if ok else text)
        self.speed.setEnabled(ok)

    def show_speed_note(self, text: str) -> None:
        self.speed_note.setText(text)
        self.speed_note.setVisible(bool(text))

    def set_busy(self, busy: bool) -> None:
        self.box.set_busy(busy)
        self.sessions.set_enabled_for_work(not busy)

    def set_web(self, available: bool, on: bool = False) -> None:
        """The Web chip: shown only when Settings allows the web, set for this conversation."""
        self.box.set_web(available, on)

    def _opened(self, row: Any) -> None:
        """A source was opened. A web page opens in the person's browser; a file goes
        to the window, which opens it the way every other page does."""
        path = str(getattr(row, "path", "") or "")
        if path.lower().startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(path))
            return
        self.result_opened.emit(row)

    def open_link(self, href: str) -> None:
        if str(href).lower().startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(href))

    def set_speed(self, value: str) -> None:
        index = self.speed.findData(value)
        if index >= 0:
            self.speed.blockSignals(True)
            self.speed.setCurrentIndex(index)
            self.speed.blockSignals(False)

    # -- the conversation --------------------------------------------------------
    def add_user(self, text: str) -> None:
        self.bubbles.add(self._user_bubble(text))
        self.bubbles.scroll_to_end()

    def _user_bubble(self, text: str) -> UserBubble:
        bubble = UserBubble(text)
        bubble.edit_requested.connect(self.edit_requested)
        return bubble

    def begin_answer(self) -> AnswerRun:
        """A fresh answer bubble; the Sources pane starts again for it."""
        bubble = self._answer_bubble()
        bubble.narrate(THINKING_LINE)                # a slim state before the first word
        self._owner = bubble
        self.sources.clear()
        self.bubbles.add(bubble)
        self._run = AnswerRun(bubble, self.sources, self.shelf)
        return self._run

    def end_answer(self) -> None:
        """The answer is finished: the newest answer takes Regenerate, the newest
        message Edit."""
        self.bubbles.refresh_last()

    def _answer_bubble(self) -> AnswerBubble:
        bubble = AnswerBubble()
        bubble.receipt_activated.connect(lambda n, b=bubble: self._activate(b, n))
        bubble.receipt_hovered.connect(self._hovered)
        bubble.result_opened.connect(self._opened)
        bubble.result_revealed.connect(self.result_revealed)
        bubble.regenerate_requested.connect(self.regenerate_requested)
        bubble.retry_requested.connect(self.retry_requested)
        bubble.link_activated.connect(self.open_link)
        bubble.web_decided.connect(self.web_decided)
        return bubble

    def show_turns(self, turns: list) -> None:
        """Redraw a stored conversation, without streaming."""
        self.bubbles.clear()
        self.sources.clear()
        self._run, self._owner = None, None
        last = None
        for turn in turns:
            if turn.role == "user":
                self.bubbles.add(self._user_bubble(turn.text))
                continue
            bubble = self._answer_bubble()
            run = AnswerRun(bubble, None, _NoShelf())
            run.finish(turn)
            self.bubbles.add(bubble)
            last = bubble
        if last is not None:
            self._show_sources_of(last)
        self.bubbles.refresh_last()
        self.bubbles.scroll_to_end()

    # -- sources -----------------------------------------------------------------
    def _show_sources_of(self, bubble: AnswerBubble) -> None:
        if self._owner is bubble:
            return
        self._owner = bubble
        self.sources.clear()
        for number in sorted(bubble.shown):
            self.sources.add(number, bubble.shown[number])

    def _activate(self, bubble: AnswerBubble, number: int) -> None:
        self._show_sources_of(bubble)
        self.sources.select_number(number)

    def _hovered(self, number: int) -> None:
        self.sources.hover_number(number)

    def _back_to_conversation(self) -> None:
        self.sources.deselect()
        self.bubbles.scroll_to_end()
        self.box.focus()


class _NoShelf:
    """A shelf that ignores adds: redrawing history must not change the shelf."""

    def add_receipt(self, _receipt: Any) -> None:
        return None
