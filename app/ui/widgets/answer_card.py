"""The answer card: a question typed into Search gets a short answer above its results.

Layer: L7 (UI)

Order `smarter-answers` section 3 (owner, 2026-10-12: "i want it to work like i am
talking to an very smart system"). The finder stays the headline - the results are
drawn first and never wait for this - and a question gets, above them, the answer
Chat would give: written from the person's files, every sentence checked against
the passage it cites, the numbers opening the documents.

**The retrieval path still never calls a model.** This lives in `app/ui`, starts
after the person stops typing (or presses Enter), runs Chat's engine on a worker,
and is dropped the moment the box changes. It never runs while Chat is answering:
there is one chat model, and the question somebody is waiting for in Chat comes
first. Wired like the contact cards (`person_card.attach`), so `search_view.py`
does not grow.

**A fixed slot, reserved from the keystroke** (UI phase B, owner, 2026-10-13).
The card arrived 1.2 s after the rows and pushed every one of them down. The two
ways out were a slot above the list or a collapsible first row inside it; the
slot won because whether a card is wanted is known the moment the box changes
(`answer_wanted` reads only the words), before any row is painted - so the space
is there first and the rows are drawn under it, never moved. A first row would
have arrived with the answer instead, and moved the list just the same. The slot
is one height whatever it holds - "Reading your files…", the answer (scrolling
inside the card when it is long), or the line saying there was none - and it is
given up only when the box changes or the person presses Hide.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QThreadPool, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from app.ui.later import when_done
from app.ui.presenter.answer import answer_wanted, card_kinds_shown
from app.ui.widgets.buttons import action_button, icon_button
from app.ui.widgets.card import CARD_MARGINS, Card

__all__ = ["AnswerCard", "attach", "ANSWER_DEBOUNCE_MS", "HEADING", "WORKING", "CHECKED_NOTE",
           "NO_ANSWER", "BODY_LINES"]

#: How long the box must be still before a question is asked. Not a setting
#: (non-negotiable 11): long enough that a question is not asked half-typed, short
#: enough that the card is there by the time the results have been read. Enter
#: asks at once.
ANSWER_DEBOUNCE_MS = 1200

HEADING = "Answer from your files"
WORKING = "Reading your files to answer that…"
CHECKED_NOTE = "Written by Leasha's AI. Every sentence is checked against the source it cites."
#: New copy (UI phase B): what the reserved slot says when no short answer came.
#: Collapsing instead would pull every row up under the person reading them.
NO_ANSWER = "No short answer from your files for that. The results are below."
#: Lines of answer the slot shows before the answer scrolls inside the card. Not
#: a setting (non-negotiable 11): enough for the two or three sentences the
#: engine writes, few enough that the first results stay on a laptop screen.
BODY_LINES = 4


class AnswerCard(Card):
    """One answer above the results, or nothing."""

    #: A source number pressed: the `SearchResult` to open, as a results row would.
    open_requested = Signal(object)
    #: "Continue in Chat": the question, for Chat to ask.
    continue_requested = Signal(str)

    def __init__(self, text_of: Callable[[], str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("answerCard")
        self.setAccessibleName("Answer from your files")
        self._text_of = text_of
        self._chat: Any = None
        self._enabled: Callable[[], bool] = lambda: True
        self._generation = 0
        self._stop: Optional[threading.Event] = None
        self._question = ""
        #: The query the person pressed Hide on: no card for it until the box changes.
        self._hidden_for = ""
        self._receipts: list = []
        self._order: list = []
        self.asked: list[str] = []                        # for tests and the debug log

        self.answer_timer = QTimer(self)
        self.answer_timer.setSingleShot(True)
        self.answer_timer.setInterval(ANSWER_DEBOUNCE_MS)
        self.answer_timer.timeout.connect(self.ask_now)

        self.heading = QLabel(HEADING)
        self.heading.setObjectName("answerCardHeading")
        self.hide_button = icon_button(
            "Hide answer", name="Hide this answer",
            tooltip="Hide this answer. It stays hidden for this search; "
                    "a new question gets its own.")
        self.hide_button.setObjectName("answerCardHide")
        self.hide_button.clicked.connect(self.hide_for_query)
        self.body = QLabel("")
        self.body.setObjectName("answerCardBody")
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.body.setTextFormat(Qt.TextFormat.RichText)
        self.body.setOpenExternalLinks(False)
        self.body.linkActivated.connect(self._link)
        self.sources = QLabel("")
        self.sources.setObjectName("answerCardSources")
        self.sources.setWordWrap(True)
        self.sources.setTextFormat(Qt.TextFormat.RichText)
        self.sources.linkActivated.connect(self._link)
        self.note = QLabel(CHECKED_NOTE)
        self.note.setObjectName("answerCardNote")
        self.note.setWordWrap(True)
        self.more = action_button("Continue in Chat")
        self.more.setObjectName("answerCardContinue")
        self.more.setToolTip("Ask this in Chat, where you can follow it up")
        self.more.clicked.connect(lambda: self.continue_requested.emit(self._question))

        top = QHBoxLayout()
        top.addWidget(self.heading, 1)
        top.addWidget(self.hide_button)
        # The answer scrolls inside the slot rather than growing it.
        self.scroll = QScrollArea()
        self.scroll.setObjectName("answerCardScroll")
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The sources scroll with the answer: several long names wrap, and outside
        # the scroll they would eat the answer's lines or be cut (review of #74).
        content = QWidget()
        inner = QVBoxLayout(content)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(6)
        inner.addWidget(self.body)
        inner.addWidget(self.sources)
        inner.addStretch(1)
        self.scroll.setWidget(content)
        # The card's own ground shows through: a scroll area paints the window
        # colour by default, which would draw a second box inside the card.
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget "
                                  "{ background: transparent; }")
        self.scroll.viewport().setAutoFillBackground(False)
        foot = QHBoxLayout()
        foot.addWidget(self.note, 1)
        foot.addWidget(self.more)
        column = QVBoxLayout(self)
        column.setContentsMargins(*CARD_MARGINS)
        column.setSpacing(6)
        column.addLayout(top)
        column.addWidget(self.scroll, 1)
        column.addLayout(foot)
        self.hide()

    # -- wiring -----------------------------------------------------------------------

    def connect_chat(self, chat: Any, *, enabled: Callable[[], bool]) -> None:
        """Chat's controller (its engine, and whether it is busy), and the switch."""
        self._chat = chat
        self._enabled = enabled

    # -- the box ----------------------------------------------------------------------

    def typed(self, _text: str = "") -> None:
        """The box changed: whatever was being answered is stale.

        The slot is decided here, at the keystroke, from the words alone - so a
        question has its space before the first row is painted under it.
        """
        self._cancel()
        if self._query() != self._hidden_for:
            self._hidden_for = ""
        if self._reserve_wanted():
            self._reset()
            self._reserve()
            self.answer_timer.start()
        else:
            self.answer_timer.stop()
            self.clear()

    def submitted(self) -> None:
        """Enter: ask now rather than after the pause."""
        if self._reserve_wanted() and self._question != self._query():
            self.answer_timer.stop()
            self.ask_now()

    def hide_for_query(self) -> None:
        """Hide: put the card away for this query only (the owner's Hide control)."""
        self._hidden_for = self._query()
        self.answer_timer.stop()
        self._cancel()
        self.clear()

    def _reserve_wanted(self) -> bool:
        """A slot for this query: a question, not hidden, and Chat free to answer it."""
        return (self._wanted() and self._query() != self._hidden_for
                and getattr(self._chat, "_ask", None) is None)

    def _reserve(self) -> None:
        """Show the slot at its one height, saying the files are being read."""
        self.body.setText(WORKING)
        self.setFixedHeight(self.slot_height())
        self.show()

    def slot_height(self) -> int:
        """Margins, the heading row, `BODY_LINES` of answer, the sources and the foot."""
        self.ensurePolished()
        for part in (self.heading, self.body, self.sources, self.note, self.more, self.hide_button):
            part.ensurePolished()
        margins = self.layout().contentsMargins()
        line = self.body.fontMetrics().lineSpacing()
        top = max(self.heading.sizeHint().height(), self.hide_button.sizeHint().height())
        foot = max(self.note.fontMetrics().lineSpacing(), self.more.sizeHint().height())
        return (margins.top() + margins.bottom() + top + BODY_LINES * line
                + self.sources.fontMetrics().lineSpacing() + foot + 3 * self.layout().spacing())

    def _query(self) -> str:
        return " ".join(str(self._text_of() or "").split())

    def _wanted(self) -> bool:
        try:
            on = bool(self._enabled())
        except Exception:                                 # noqa: BLE001 - off when unsure
            on = False
        return on and self._chat is not None and answer_wanted(self._query())

    # -- asking -------------------------------------------------------------------------

    def ask_now(self) -> None:
        """Ask Chat's engine on a worker. Skipped while Chat itself is answering."""
        question = self._query()
        if not self._wanted() or question == self._hidden_for:
            return
        if getattr(self._chat, "_ask", None) is not None:
            # Chat started answering during the pause: the slot is already
            # there, so it says so rather than collapsing under the rows.
            if not self.isHidden():
                self._nothing()
            return
        from app.ui.workers import CallableWorker, run

        self._cancel()
        self._generation += 1
        generation = self._generation
        stop = threading.Event()
        self._stop = stop
        self._question = question
        self.asked.append(question)
        self.body.setText(WORKING)
        self.sources.setText("")
        self.more.setEnabled(True)
        self._reserve()
        worker = CallableWorker(_answer, self._chat, question, stop, component="ui.answer_card")
        when_done(self, worker,
                  finished=lambda got: self._landed(generation, got),
                  failed=lambda _error: self._failed(generation))
        run(QThreadPool.globalInstance(), worker)

    def _cancel(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._stop = None
        self._generation += 1

    def _landed(self, generation: int, got: Any) -> None:
        if generation != self._generation:
            return
        self._stop = None
        engine, chosen_at, turn = got
        adopt = getattr(self._chat, "_adopt", None)
        if adopt is not None and engine is not None:
            adopt(engine, chosen_at)                      # Chat keeps the engine it built
        if turn is None or not card_kinds_shown(getattr(turn, "kind", "")) \
                or not str(getattr(turn, "text", "") or "").strip():
            self._nothing()
            return
        self._show_turn(turn)

    def _failed(self, generation: int) -> None:
        if generation == self._generation:
            self._nothing()

    def _nothing(self) -> None:
        """No answer to show: the slot stays, and says so - see `NO_ANSWER`."""
        self._receipts, self._order = [], []
        self.body.setText(NO_ANSWER)
        self.sources.setText("")
        self.note.setVisible(False)

    # -- drawing --------------------------------------------------------------------------

    def _show_turn(self, turn: Any) -> None:
        from app.ui.presenter.chat import Numbering, render_answer_html

        self._receipts = list(getattr(turn, "receipts", None) or [])
        numbering = Numbering()
        linkable = range(1, len(self._receipts) + 1)
        self.body.setText(render_answer_html(str(turn.text), numbering, linkable))
        self._order = numbering.in_order() or list(linkable)
        self.sources.setText(" · ".join(
            f'<a href="receipt:{numbering.display(n)}">{numbering.display(n)}</a> '
            f'{_escape(getattr(self._receipts[n - 1], "name", "") or "")}'
            for n in linkable))
        self.note.setVisible(bool(self._receipts))
        self._settle_slot()

    def _settle_slot(self) -> None:
        """Back to the top of a fresh answer, at the slot's one height."""
        self.scroll.verticalScrollBar().setValue(0)
        self.setFixedHeight(self.slot_height())
        self.show()

    def _link(self, href: str) -> None:
        if not str(href).startswith("receipt:"):
            return
        try:
            shown = int(str(href).split(":", 1)[1])
            engine_number = self._order[shown - 1]
            receipt = self._receipts[engine_number - 1]
        except (ValueError, IndexError, AttributeError):
            return
        from app.ui.presenter.chat import receipt_to_result

        self.open_requested.emit(receipt_to_result(receipt, shown))

    def _reset(self) -> None:
        """Empty the card's words without giving up its slot."""
        self._question = ""
        self._receipts = []
        self._order = []
        self.body.setText("")
        self.sources.setText("")
        self.note.setVisible(True)

    def clear(self) -> None:
        """Empty the card and give its slot up."""
        self._reset()
        self.hide()


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _answer(chat: Any, question: str, stop: threading.Event) -> tuple:
    """Worker body: `(engine, generation it was chosen at, the finished turn)`.

    The same engine Chat answers with (`ChatController._engine_now`), asked with
    no history: a question in the Search box starts a conversation of its own.
    Never raises - the engine's own contract - and a stopped answer is dropped by
    the generation check, not drawn."""
    engine, chosen_at = chat._engine_now()
    turn = engine.ask(question, [], lambda _event: None, stop.is_set)
    return engine, chosen_at, turn


def attach(view: Any) -> AnswerCard:
    """Make the card for a `SearchView`, wired to its box. The caller places it.

    `view.answer_timer` is the card's timer, so `workers.stop_timers` finds it by
    name when the window closes. The window connects Chat to it once Chat exists
    (`connect_chat`), and its two signals.
    """
    card = AnswerCard(view.input.text, view)
    view.input.textChanged.connect(card.typed)
    view.input.returnPressed.connect(card.submitted)
    view.answer_card = card
    view.answer_timer = card.answer_timer
    return card
