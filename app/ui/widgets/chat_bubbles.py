"""The conversation: bubbles that grow downward and never move under the reader.

Layer: L5 view

Work order 202626270611 section 3a and 4e-2. Two rules make streaming feel
calm rather than twitchy:

* **A bubble only ever grows at its own bottom.** Nothing above it re-lays out
  while tokens arrive, so a line somebody is reading does not move.
* **The list follows the newest text only while the reader is at the bottom.**
  Scroll up to re-read something and it stays exactly where you left it,
  however much arrives below; scroll back down and it follows again.

An answer draws its source numbers as small raised links in the prose. A FIND
answer carries the real results list (`ResultsView`) inline, so a question that
is really a search answers with results, not a paragraph about them.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from app.ui.presenter.chat import (
    EMPTY_HEADING, EMPTY_HINT, Numbering, closing_line, render_answer_html,
)
from app.ui.results_view import ResultsView

__all__ = ["BubbleList", "UserBubble", "AnswerBubble"]

#: A coalescing pause: tokens can arrive faster than a label can lay out.
RENDER_MS = 40
#: Rows an inline result list shows before it scrolls inside itself.
INLINE_ROWS = 6
_ROW_GUESS = 68


class UserBubble(QFrame):
    """What the person asked."""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatBubbleUser")
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setAccessibleName("You asked")
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)


class AnswerBubble(QFrame):
    """The reply: a progress line, the prose, maybe a results list, an ending."""

    receipt_activated = pyqtSignal(int)     # the number the reader sees
    receipt_hovered = pyqtSignal(int)       # 0 when the pointer leaves
    result_opened = pyqtSignal(object)
    result_revealed = pyqtSignal(object)

    def __init__(self, numbering: Optional[Numbering] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatBubbleAnswer")
        self.numbering = numbering or Numbering()
        self.raw = ""
        self._linkable: set[int] = set()
        #: reader-facing number -> the receipt behind it, for this answer.
        self.shown: dict[int, Any] = {}
        self.results: Optional[ResultsView] = None

        self.narration = QLabel("")
        self.narration.setObjectName("chatNarration")
        self.narration.setWordWrap(True)
        self.narration.setAccessibleName("What Leasha is doing")
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.RichText)
        self.body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.body.setAccessibleName("Answer")
        self.body.setVisible(False)
        self.body.linkActivated.connect(self._link_activated)
        self.body.linkHovered.connect(self._link_hovered)
        self.footer = QLabel("")
        self.footer.setObjectName("chatFooter")
        self.footer.setWordWrap(True)
        self.footer.setVisible(False)

        self._layout = QVBoxLayout(self)
        for part in (self.narration, self.body, self.footer):
            self._layout.addWidget(part)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(RENDER_MS)
        self._timer.timeout.connect(self.render)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

    # -- while it is being written ------------------------------------------
    def narrate(self, text: str) -> None:
        self.narration.setText(text)
        self.narration.setVisible(bool(text))

    def append(self, text: str) -> None:
        self.raw += text
        self.request_render()

    def request_render(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def set_linkable(self, engine_numbers: set[int]) -> None:
        self._linkable = set(engine_numbers)

    def render(self) -> None:
        self._timer.stop()
        html_text = render_answer_html(self.raw, self.numbering, self._linkable)
        self.body.setText(html_text)
        self.body.setVisible(bool(self.raw))

    # -- when it is done -----------------------------------------------------
    def finish(self, turn: Any = None, *, stopped: bool = False) -> None:
        if turn is not None and getattr(turn, "text", ""):
            self.raw = turn.text
        self.render()
        self.narrate("")
        if turn is not None and getattr(turn, "result_set", None):
            self.show_results(turn.result_set)
        line = closing_line(turn, stopped=stopped)
        self.footer.setText(line)
        self.footer.setVisible(bool(line))

    def show_results(self, results: list) -> None:
        if self.results is None:
            self.results = ResultsView()
            self.results.opened.connect(self.result_opened.emit)
            self.results.reveal_requested.connect(self.result_revealed.emit)
            self._layout.insertWidget(self._layout.indexOf(self.body) + 1, self.results)
        self.results.show_results(results, [])
        rows = min(len(results), INLINE_ROWS) + 1        # + the "that's all" row
        list_view = self.results._list
        heights = [list_view.sizeHintForRow(i) for i in range(rows)]
        total = sum(h if h > 0 else _ROW_GUESS for h in heights) + 8
        self.results.setFixedHeight(total)

    def _link_activated(self, href: str) -> None:
        if href.startswith("receipt:"):
            self.receipt_activated.emit(int(href.split(":", 1)[1]))

    def _link_hovered(self, href: str) -> None:
        self.receipt_hovered.emit(
            int(href.split(":", 1)[1]) if href.startswith("receipt:") else 0)


class BubbleList(QScrollArea):
    """A vertical run of bubbles that follows new text only from the bottom."""

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
        self._column.setSpacing(10)
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
        if self._stick:
            self.verticalScrollBar().setValue(high)

    def follows_newest(self) -> bool:
        return self._stick

    def add(self, bubble: QWidget) -> None:
        self.empty.setVisible(False)
        self._column.insertWidget(self._column.count() - 1, bubble)

    def scroll_to_end(self) -> None:
        self._stick = True
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def clear(self) -> None:
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
