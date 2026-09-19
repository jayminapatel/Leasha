"""The names the Chat engine and the Chat tab share.

Layer: L4 (no Qt, no store)

The engine (`app/chat/engine.py`) produces these; the tab (`app/ui/chat_view.py`
and its controller) draws them. Neither imports the other - this file is the
whole of what they agree on, so a change here is a change to both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

__all__ = ["Receipt", "ChatTurn", "NarrationEvent", "TokenEvent", "ShelfEvent"]


@dataclass(frozen=True)
class Receipt:
    """One verified passage an answer stands on."""

    file_id: Optional[int]
    path: str
    name: str
    quote: str
    locator: str = ""
    chunk_id: Optional[int] = None


@dataclass
class ChatTurn:
    """One side of the conversation."""

    role: str                          # "user" | "assistant"
    text: str
    receipts: list[Receipt] = field(default_factory=list)
    result_set: Optional[list] = None  # FIND-routed answers: list of SearchResult
    kind: str = "answer"               # "answer"|"find"|"aggregate"|"absence"|"error"|"clarify"
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NarrationEvent:
    """One plain-words line about what the search is doing. First one <1s."""

    text: str


@dataclass(frozen=True)
class TokenEvent:
    """A piece of the answer text, streamed."""

    text: str


@dataclass(frozen=True)
class ShelfEvent:
    """A document the conversation has just touched."""

    receipt: Receipt
