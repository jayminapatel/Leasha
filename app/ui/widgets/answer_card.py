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
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QThreadPool, Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.ui.later import when_done
from app.ui.presenter.answer import answer_wanted, card_kinds_shown
from app.ui.widgets.buttons import action_button

__all__ = ["AnswerCard", "attach", "ANSWER_DEBOUNCE_MS", "HEADING", "WORKING", "CHECKED_NOTE"]

#: How long the box must be still before a question is asked. Not a setting
#: (non-negotiable 11): long enough that a question is not asked half-typed, short
#: enough that the card is there by the time the results have been read. Enter
#: asks at once.
ANSWER_DEBOUNCE_MS = 1200

HEADING = "Answer from your files"
WORKING = "Reading your files to answer that…"
CHECKED_NOTE = "Written by Leasha's AI. Every sentence is checked against the source it cites."


class AnswerCard(QFrame):
    """One answer above the results, or nothing."""

    #: A source number pressed: the `SearchResult` to open, as a results row would.
    open_requested = Signal(object)
    #: "Continue in Chat": the question, for Chat to ask.
    continue_requested = Signal(str)

    def __init__(self, text_of: Callable[[], str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("answerCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAccessibleName("Answer from your files")
        self._text_of = text_of
        self._chat: Any = None
        self._enabled: Callable[[], bool] = lambda: True
        self._generation = 0
        self._stop: Optional[threading.Event] = None
        self._question = ""
        self._receipts: list = []
        self.asked: list[str] = []                        # for tests and the debug log

        self.answer_timer = QTimer(self)
        self.answer_timer.setSingleShot(True)
        self.answer_timer.setInterval(ANSWER_DEBOUNCE_MS)
        self.answer_timer.timeout.connect(self.ask_now)

        self.heading = QLabel(HEADING)
        self.heading.setObjectName("answerCardHeading")
        self.body = QLabel("")
        self.body.setObjectName("answerCardBody")
        self.body.setWordWrap(True)
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

        foot = QHBoxLayout()
        foot.addWidget(self.note, 1)
        foot.addWidget(self.more)
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(6)
        column.addWidget(self.heading)
        column.addWidget(self.body)
        column.addWidget(self.sources)
        column.addLayout(foot)
        self.hide()

    # -- wiring -----------------------------------------------------------------------

    def connect_chat(self, chat: Any, *, enabled: Callable[[], bool]) -> None:
        """Chat's controller (its engine, and whether it is busy), and the switch."""
        self._chat = chat
        self._enabled = enabled

    # -- the box ----------------------------------------------------------------------

    def typed(self, _text: str = "") -> None:
        """The box changed: whatever was being answered is stale."""
        self._cancel()
        self.clear()
        if self._wanted():
            self.answer_timer.start()
        else:
            self.answer_timer.stop()

    def submitted(self) -> None:
        """Enter: ask now rather than after the pause."""
        if self._wanted() and self._question != self._query():
            self.answer_timer.stop()
            self.ask_now()

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
        if not self._wanted() or getattr(self._chat, "_ask", None) is not None:
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
        self.show()
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
            self.clear()
            return
        self._show_turn(turn)

    def _failed(self, generation: int) -> None:
        if generation == self._generation:
            self.clear()

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

    def clear(self) -> None:
        self._question = ""
        self._receipts = []
        self._order = []
        self.body.setText("")
        self.sources.setText("")
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
