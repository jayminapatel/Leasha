"""The conversation: messages that grow downward and never move under the reader.

Layer: L5 view

Work order 202626270611 section 3a and 4e-2, and the owner's 2026-09-20 requirement
that the tab feel like talking to an AI assistant. Rules that make streaming calm:

* **A message only ever grows at its own bottom.** Nothing above it re-lays out
  while tokens arrive, so a line somebody is reading does not move.
* **The list follows the newest text only while the reader is at the bottom.**
  Scroll up to re-read something and it stays exactly where you left it,
  however much arrives below; scroll back down and it follows again.
* **The answer is rendered markdown** (`chat_markdown.AnswerBody`): bold, lists,
  tables, code blocks with a Copy button each - drawn as it streams, in place.

The person's message is a soft rounded block on the right; the assistant's is plain
text on the page, with a quiet row of actions under it: Copy on every answer,
Regenerate on the last one, Try again beside an answer that stopped part-way. The
last message of the person's own can be edited and sent again.

An answer draws its source numbers as small raised links in the prose. A FIND
answer carries the real results list (`ResultsView`) inline, so a question that
is really a search answers with results, not a paragraph about them.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QTextBrowser,
    QVBoxLayout, QWidget,
)

from app.ui.presenter.chat import (
    COPIED_LABEL, COPY_LABEL, EDIT_LABEL, EMPTY_HEADING, EMPTY_HINT, REGENERATE_LABEL,
    RETRY_LABEL, Numbering, closing_line, plain_answer_text,
)
from app.ui.results_view import ResultsView
from app.ui.widgets.chat_markdown import AnswerBody

__all__ = ["BubbleList", "UserBubble", "AnswerBubble"]

#: A coalescing pause: tokens can arrive faster than a document can lay out.
RENDER_MS = 40
#: Rows an inline result list shows before it scrolls inside itself.
INLINE_ROWS = 6
_ROW_GUESS = 68
#: How wide the person's own message may be, as a share of the conversation.
USER_SHARE = 0.8
#: How long a Copy button says "Copied".
COPIED_MS = 1500


def _action(text: str, tip: str, name: str) -> QPushButton:
    """A flat message action (Copy, Regenerate ...): exempt from the button system."""
    button = QPushButton(text)
    button.setObjectName("chatAction")
    button.setFlat(True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setToolTip(tip)
    button.setAccessibleName(tip)
    button.setProperty("actionName", name)
    return button


class _PlainText(QTextBrowser):
    """The person's words, wrapped, at exactly the height they need.

    **Not a `QLabel`**: a word-wrapped label inside a right-aligned layout gets its
    natural width but not the height that width needs, so a long question showed its
    first line and lost the rest (seen in the real window, 2026-09-20). This asks its own
    document: the ideal width up to a ceiling, and the height that width wraps to."""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setOpenLinks(False)
        self.setStyleSheet("background: transparent; border: none;")
        self.document().setDocumentMargin(0)
        self.setPlainText(text)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.document().documentLayout().documentSizeChanged.connect(lambda _s: self._fit())

    def _ideal(self) -> int:
        """The width the text would take on one line, measured with the document
        briefly unwrapped and then put back - no I/O, one layout pass.
        """
        doc = self.document()
        width = doc.textWidth()
        doc.setTextWidth(-1)
        ideal = int(doc.idealWidth()) + 2
        doc.setTextWidth(width if width > 0 else max(1, self.viewport().width()))
        return ideal

    def sizeHint(self) -> QSize:                                   # noqa: N802 - Qt
        cap = self.maximumWidth()
        return QSize(min(self._ideal(), cap) if cap < 16_000_000 else self._ideal(),
                     max(self.height(), 1))

    def minimumSizeHint(self) -> QSize:                            # noqa: N802 - Qt
        return QSize(40, self.height())

    def _fit(self) -> None:
        height = int(self.document().size().height()) + 2
        if height != self.height():
            self.setFixedHeight(height)
            self.updateGeometry()

    def resizeEvent(self, event: Any) -> None:                     # noqa: N802 - Qt
        super().resizeEvent(event)
        self._fit()


class UserBubble(QFrame):
    """What the person said."""

    edit_requested = Signal()

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatBubbleUser")
        self.text = text
        self.label = _PlainText(text)
        self.label.setAccessibleName("You asked")
        self.edit_button = _action(EDIT_LABEL, "Change this message and send it again", "edit")
        self.edit_button.setVisible(False)
        self.edit_button.clicked.connect(lambda _c=False: self.edit_requested.emit())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 6)
        layout.setSpacing(2)
        layout.addWidget(self.label)
        layout.addWidget(self.edit_button, alignment=Qt.AlignmentFlag.AlignRight)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

    def setMaximumWidth(self, width: int) -> None:                 # noqa: N802 - Qt
        super().setMaximumWidth(width)
        self.label.setMaximumWidth(max(40, width - 24))

    def set_last(self, last: bool) -> None:
        """Only the newest message of the person's can be edited."""
        self.edit_button.setVisible(bool(last))


class AnswerBubble(QFrame):
    """The reply: a progress line, the prose, maybe a results list, an ending."""

    receipt_activated = Signal(int)     # the number the reader sees
    receipt_hovered = Signal(int)       # 0 when the pointer leaves
    result_opened = Signal(object)
    result_revealed = Signal(object)
    #: 2026-10-04: a result row picked with one click, for the preview.
    result_selected = Signal(object)
    regenerate_requested = Signal()
    retry_requested = Signal()
    #: A link in the prose that is not a source number (a web page).
    link_activated = Signal(str)
    #: The person's answer to "Search the web for ...?" (`True` = Allow).
    web_decided = Signal(bool)

    def __init__(self, numbering: Optional[Numbering] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatBubbleAnswer")
        self.numbering = numbering or Numbering()
        self.raw = ""
        self._linkable: set[int] = set()
        #: reader-facing number -> the receipt behind it, for this answer.
        self.shown: dict[int, Any] = {}
        #: 2026-10-08: file_id -> mail metadata for this answer's messages, so a
        #: message is drawn by its subject and sender, never its entry id.
        self.details: dict[int, Any] = {}
        self.results: Optional[ResultsView] = None
        self.done = False
        self._is_last = False
        self._retryable = False

        self.narration = QLabel("")
        self.narration.setObjectName("chatNarration")
        self.narration.setWordWrap(True)
        self.narration.setAccessibleName("What Leasha is doing")
        self.body = AnswerBody()
        self.body.setVisible(False)
        self.body.receipt_activated.connect(self.receipt_activated)
        self.body.receipt_hovered.connect(self.receipt_hovered)
        self.body.link_activated.connect(self.link_activated)
        self.footer = QLabel("")
        self.footer.setObjectName("chatFooter")
        self.footer.setWordWrap(True)
        self.footer.setVisible(False)

        self.copy_button = _action(COPY_LABEL, "Copy this answer", "copy")
        self.copy_button.clicked.connect(lambda _c=False: self.copy())
        self.regenerate_button = _action(
            REGENERATE_LABEL, "Ask again and get a fresh answer", "regenerate")
        self.regenerate_button.clicked.connect(lambda _c=False: self.regenerate_requested.emit())
        self.retry_button = _action(RETRY_LABEL, "Ask this again", "retry")
        self.retry_button.clicked.connect(lambda _c=False: self.retry_requested.emit())
        self.web_prompt = QFrame()
        self.web_prompt.setObjectName("chatWebPrompt")
        self.web_question = QLabel("")
        self.web_question.setWordWrap(True)
        self.web_question.setAccessibleName("Search the web?")
        self.web_allow = QPushButton("Allow")
        self.web_allow.setToolTip("Send this search phrase to the web search service.")
        self.web_skip = QPushButton("Skip")
        self.web_skip.setToolTip("Do not search the web. Chat answers from your files.")
        self.web_allow.clicked.connect(lambda _c=False: self._web_answered(True))
        self.web_skip.clicked.connect(lambda _c=False: self._web_answered(False))
        prompt = QHBoxLayout(self.web_prompt)
        prompt.addWidget(self.web_question, stretch=1)
        prompt.addWidget(self.web_allow)
        prompt.addWidget(self.web_skip)
        self.web_prompt.setVisible(False)

        self.actions = QWidget()
        self.actions.setObjectName("chatActions")
        row = QHBoxLayout(self.actions)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        for button in (self.copy_button, self.regenerate_button, self.retry_button):
            row.addWidget(button)
        row.addStretch(1)
        self.actions.setVisible(False)

        self._layout = QVBoxLayout(self)
        for part in (self.narration, self.web_prompt, self.body, self.footer, self.actions):
            self._layout.addWidget(part)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(RENDER_MS)
        self._timer.timeout.connect(self.render)
        self._copied = QTimer(self)
        self._copied.setSingleShot(True)
        self._copied.setInterval(COPIED_MS)
        self._copied.timeout.connect(lambda: self.copy_button.setText(COPY_LABEL))
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._refresh_actions()

    # -- while it is being written ------------------------------------------
    def narrate(self, text: str) -> None:
        self.narration.setText(text)
        self.narration.setVisible(bool(text))

    def append(self, text: str) -> None:
        """A streamed token. The redraw is coalesced by `request_render` (`RENDER_MS`)."""
        self.raw += text
        self.request_render()

    def ask_web(self, query: str) -> None:
        """Show the exact search phrase and wait for Allow or Skip."""
        self.web_question.setText(f'Search the web for: "{query}"?')
        self.web_prompt.setVisible(True)
        self.web_allow.setFocus()

    def _web_answered(self, allowed: bool) -> None:
        self.web_prompt.setVisible(False)
        self.web_decided.emit(bool(allowed))

    def request_render(self) -> None:
        """Redraw shortly, once, however many tokens arrive before the timer
        fires - a document laid out per token stutters.
        """
        if not self._timer.isActive():
            self._timer.start()

    def set_linkable(self, engine_numbers: set[int]) -> None:
        self._linkable = set(engine_numbers)

    def render(self, *, final: bool = False) -> None:
        """Draw `raw` as markdown. UI thread. `final` lets the unfinished tail be
        shown as it is rather than closed for display.
        """
        self._timer.stop()
        self.body.set_text(self.raw, show_number=self.numbering.display,
                           linkable=frozenset(self._linkable), final=final)
        self.body.setVisible(bool(self.raw.strip()))

    # -- text the person can take away -------------------------------------------
    def body_text(self) -> str:
        """The answer as it reads on screen."""
        return self.body.plain_text()

    def copy_text(self) -> str:
        """What Copy puts on the clipboard: the markdown, without source numbers."""
        return self.body.copy_all_text() if self.raw.strip() else ""

    def copy(self) -> None:
        text = self.copy_text() or plain_answer_text(self.raw)
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self.copy_button.setText(COPIED_LABEL)
        self._copied.start()

    # -- when it is done -----------------------------------------------------
    def finish(self, turn: Any = None, *, stopped: bool = False) -> None:
        if turn is not None and getattr(turn, "text", ""):
            self.raw = turn.text
        self.render(final=True)
        self.narrate("")
        if turn is not None and getattr(turn, "details", None):
            self.details.update({int(k): v for k, v in dict(turn.details).items()})
        if turn is not None and getattr(turn, "result_set", None):
            self.show_results(turn.result_set)
        line = closing_line(turn, stopped=stopped)
        self.footer.setText(line)
        self.footer.setVisible(bool(line))
        failed = turn is not None and getattr(turn, "kind", "") == "error"
        self._retryable = bool(stopped or failed or getattr(turn, "partial", False)) \
            and not (turn is not None and getattr(turn, "kind", "") == "clarify")
        self.done = True
        self._refresh_actions()

    def set_last(self, last: bool) -> None:
        """Regenerate is offered on the newest answer only."""
        self._is_last = bool(last)
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        """Which action buttons show: Copy with text, Retry on a stopped or failed
        last answer, Regenerate on a whole last answer.
        """
        has_text = bool(self.raw.strip())
        self.copy_button.setVisible(has_text)
        self.retry_button.setVisible(self.done and self._is_last and self._retryable)
        self.regenerate_button.setVisible(
            self.done and self._is_last and has_text and not self._retryable)
        self.actions.setVisible(self.done and (has_text or self._retryable))

    def show_results(self, results: list) -> None:
        """A FIND answer's rows as a real `ResultsView` inline, sized to a few rows
        so it scrolls inside itself rather than stretching the conversation.
        """
        if self.results is None:
            self.results = ResultsView()
            self.results.opened.connect(self.result_opened.emit)
            self.results.reveal_requested.connect(self.result_revealed.emit)
            # 2026-10-04: one click previews the row in the Sources column.
            self.results.selected.connect(self.result_selected.emit)
            self._layout.insertWidget(self._layout.indexOf(self.body) + 1, self.results)
        self.results.show_results(results, [], details=self.details)
        rows = min(len(results), INLINE_ROWS) + 1        # + the "that's all" row
        list_view = self.results._list
        heights = [list_view.sizeHintForRow(i) for i in range(rows)]
        total = sum(h if h > 0 else _ROW_GUESS for h in heights) + 8
        self.results.setFixedHeight(total)


class BubbleList(QScrollArea):
    """A vertical run of messages that follows new text only from the bottom."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatConversation")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAccessibleName("Conversation")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._stick = True
        self._holder = QWidget()
        self._column = QVBoxLayout(self._holder)
        self._column.setSpacing(14)
        self.empty = QLabel(EMPTY_HEADING + "\n\n" + EMPTY_HINT)
        self.empty.setObjectName("chatEmpty")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._column.addWidget(self.empty)
        self._column.addStretch(1)
        self.setWidget(self._holder)
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._noticed_scroll)
        bar.rangeChanged.connect(self._range_changed)

    def _at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _noticed_scroll(self, _value: int) -> None:
        self._stick = self._at_bottom()

    def _range_changed(self, _low: int, high: int) -> None:
        """New content grew the scroll range: follow it only while the reader was
        already at the bottom (`_stick`).
        """
        if self._stick:
            self.verticalScrollBar().setValue(high)

    def follows_newest(self) -> bool:
        return self._stick

    def resizeEvent(self, event: Any) -> None:                    # noqa: N802 - Qt
        super().resizeEvent(event)
        limit = int(self.viewport().width() * USER_SHARE)
        for bubble in self.bubbles():
            if isinstance(bubble, UserBubble):
                bubble.setMaximumWidth(limit)

    def add(self, bubble: QWidget) -> None:
        self.empty.setVisible(False)
        if isinstance(bubble, UserBubble):
            bubble.setMaximumWidth(int(self.viewport().width() * USER_SHARE))
            self._column.insertWidget(self._column.count() - 1, bubble,
                                      alignment=Qt.AlignmentFlag.AlignRight)
        else:
            self._column.insertWidget(self._column.count() - 1, bubble)
        self.refresh_last()

    def refresh_last(self) -> None:
        """Regenerate belongs to the newest answer and Edit to the newest message of the
        person's - never to older ones, which would rewrite history."""
        last_user = last_answer = None
        for bubble in self.bubbles():
            if isinstance(bubble, UserBubble):
                last_user = bubble
            elif isinstance(bubble, AnswerBubble):
                last_answer = bubble
        for bubble in self.bubbles():
            if isinstance(bubble, UserBubble):
                bubble.set_last(bubble is last_user and (last_answer is None or last_answer.done))
            elif isinstance(bubble, AnswerBubble):
                bubble.set_last(bubble is last_answer)

    def scroll_to_end(self) -> None:
        self._stick = True
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def clear(self) -> None:
        """Remove every bubble (deferred deletion) and show the empty-state text again."""
        for index in reversed(range(self._column.count() - 1)):
            item = self._column.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is not None and widget is not self.empty:
                self._column.removeWidget(widget)
                widget.deleteLater()
        self.empty.setVisible(True)
        self._stick = True

    def bubbles(self) -> list[QWidget]:
        return [w for i in range(self._column.count())
                if (w := self._column.itemAt(i).widget()) is not None
                and w is not self.empty]
