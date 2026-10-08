"""One answer being written: which events go where.

Layer: L5 (a plain object - it holds widgets but is not one)

Work order 202626270611 3a, 3c and 4e-2. The engine sends four kinds of event
while it works; this decides what each one changes on screen:

* a **narration** line replaces the small progress line at the top of the
  bubble - the search explaining itself (and "Thinking..." before anything else);
* a **sources** event hands over the numbered passages the model has been shown, so a
  source number is a live link the moment it appears - *without* touching the shelf;
* a **token** adds text at the bubble's end (the first one takes the progress line
  away, as any chat does), and any source the text points at for the first time is
  *appended* to the Sources pane, numbered in that order;
* a **shelf** event puts the document on the shelf.

While it streams, the numbers are the ones the model used. When the answer is
finished the engine's final turn is the truth: its text replaces the streamed text,
its receipts replace the streamed sources (it renumbers by first mention once the
sentences no passage supports are out), and the Sources pane is rebuilt **only if that
changed anything** - an answer that survived whole does not blink.
"""

from __future__ import annotations

from typing import Any

from app.chat.types import NarrationEvent, ShelfEvent, SourcesEvent, TokenEvent, WebAskEvent
from app.ui.presenter.chat import Numbering

__all__ = ["AnswerRun", "NoShelf"]


class NoShelf:
    """A shelf that ignores adds: redrawing history must not change the shelf.
    (Moved here from `chat_view.py` on 2026-10-08, beside the run that uses it.)"""

    def add_receipt(self, _receipt: Any) -> None:
        return None


class AnswerRun:
    def __init__(self, bubble: Any, sources: Any, shelf: Any) -> None:
        self.bubble = bubble
        self.sources = sources
        self.shelf = shelf
        self.numbering: Numbering = bubble.numbering
        #: Receipts in the order the engine reported them; the prose's own
        #: `[n]` refers to position n in this list (and in the final turn).
        self.known: list[Any] = []
        #: Reader-facing number -> receipt, in the order they were shown.
        self.shown: dict[int, Any] = bubble.shown
        self._placed: set[int] = set()          # engine numbers already shown
        self.finished = False
        self.streamed = False

    def event(self, event: Any) -> None:
        if self.finished:
            return
        if isinstance(event, NarrationEvent):
            if not self.streamed:
                self.bubble.narrate(event.text)
        elif isinstance(event, WebAskEvent):
            self.bubble.ask_web(event.query)
        elif isinstance(event, SourcesEvent):
            self.known = list(event.receipts)
            self._details(getattr(event, "details", None))
            self._place_new_sources()
        elif isinstance(event, TokenEvent):
            if not self.streamed:
                self.streamed = True
                self.bubble.narrate("")
            self.bubble.append(event.text)
            self._place_new_sources()
        elif isinstance(event, ShelfEvent):
            self.known.append(event.receipt)
            self.shelf.add_receipt(event.receipt)
            self._place_new_sources()

    def _details(self, details: Any) -> None:
        """Mail metadata for the sources (2026-10-08): the bubble keeps it for a
        redraw, the Sources list draws a message by its subject with it."""
        if not details:
            return
        self.bubble.details.update({int(k): v for k, v in dict(details).items()})
        if self.sources is not None:
            self.sources.set_details(details)

    def _add(self, number: int, receipt: Any) -> None:
        self.shown[number] = receipt
        if self.sources is not None:
            self.sources.add(number, receipt)

    def _receipt_for(self, engine_number: int, final: Any = None) -> Any:
        pool = final if final is not None else self.known
        index = engine_number - 1
        return pool[index] if 0 <= index < len(pool) else None

    def _place_new_sources(self, final: Any = None) -> None:
        self.numbering.feed(self.bubble.raw)
        for engine_number in self.numbering.in_order():
            if engine_number in self._placed:
                continue
            receipt = self._receipt_for(engine_number, final)
            if receipt is None:
                continue                         # its receipt has not arrived yet
            self._placed.add(engine_number)
            self._add(self.numbering.display(engine_number), receipt)
        self.bubble.set_linkable(self._placed)
        self.bubble.request_render()

    def _restart_numbering(self) -> None:
        """Forget what streaming numbered: the final answer numbers its own."""
        self.numbering = Numbering()
        self.bubble.numbering = self.numbering
        self.shown.clear()
        self._placed.clear()
        if self.sources is not None:
            self.sources.clear()

    def _streamed_agrees_with(self, receipts: list[Any]) -> bool:
        """Do the sources already on screen match what the finished turn cites?

        The final text numbers by first mention, so display number `d` is
        `receipts[d - 1]`; streaming numbered the model's own `[n]` in its order of
        appearance. They agree when every number shown is the same document."""
        if not self.shown:
            return not receipts
        for number, receipt in self.shown.items():
            if not 1 <= number <= len(receipts):
                return False
            if getattr(receipts[number - 1], "path", None) != getattr(receipt, "path", ""):
                return False
        return True

    def finish(self, turn: Any = None, *, stopped: bool = False) -> None:
        if self.finished:
            return
        if turn is not None and getattr(turn, "text", ""):
            self.bubble.raw = turn.text
        self._details(getattr(turn, "details", None))
        receipts = list(getattr(turn, "receipts", None) or [])
        if turn is not None and getattr(turn, "text", "") and self._placed \
                and not self._streamed_agrees_with(receipts):
            self._restart_numbering()
        if receipts:
            self.known = list(receipts)
            self._place_new_sources(receipts)
            # Sources the prose never pointed at still belong to the answer.
            for engine_number, receipt in enumerate(receipts, start=1):
                if engine_number not in self._placed:
                    self._placed.add(engine_number)
                    self._add(self.numbering.display(engine_number), receipt)
                self.shelf.add_receipt(receipt)
        else:
            self._place_new_sources()
        self.bubble.set_linkable(self._placed)
        self.bubble.finish(turn, stopped=stopped)
        self.finished = True
