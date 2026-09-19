"""One answer being written: which events go where.

Layer: L5 (a plain object - it holds widgets but is not one)

Work order 202626270611 3a, 3c and 4e-2. The engine sends three kinds of event
while it works; this decides what each one changes on screen:

* a **narration** line replaces the small progress line at the top of the
  bubble - the search explaining itself;
* a **token** adds text at the bubble's end, and any source the text points at
  for the first time is *appended* to the Sources pane, numbered in that order;
* a **shelf** event puts the document on the shelf.

The numbers never change once shown (see `Numbering`). When the answer is
finished the engine's final turn is the truth: its text replaces the streamed
text, and any source the prose did not point at is appended after the rest.
"""

from __future__ import annotations

from typing import Any

from app.chat.types import NarrationEvent, ShelfEvent, TokenEvent
from app.ui.presenter.chat import Numbering

__all__ = ["AnswerRun"]


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

    def event(self, event: Any) -> None:
        if self.finished:
            return
        if isinstance(event, NarrationEvent):
            self.bubble.narrate(event.text)
        elif isinstance(event, TokenEvent):
            self.bubble.append(event.text)
            self._place_new_sources()
        elif isinstance(event, ShelfEvent):
            self.known.append(event.receipt)
            self.shelf.add_receipt(event.receipt)
            self._place_new_sources()

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

    def finish(self, turn: Any = None, *, stopped: bool = False) -> None:
        if self.finished:
            return
        if turn is not None and getattr(turn, "text", ""):
            self.bubble.raw = turn.text
        receipts = list(getattr(turn, "receipts", None) or [])
        if receipts:
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
