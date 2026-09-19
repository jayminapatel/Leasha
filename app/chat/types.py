"""The shapes the Chat engine and the Chat tab agree on.

Layer: L8b (no Qt, no network). Work order `202626270611-chat-tab`.

This file is the **interface contract** between the engine (`app/chat/engine.py`)
and the tab (`app/ui/...`). The tab is built against exactly these classes, so a
change here is a change to somebody else's code: add a field with a default, never
rename or retype one.

Everything a person can see in a conversation is one of these:

* a `ChatTurn` - one message, the user's or the assistant's;
* a `Receipt` - the document (and the exact words in it) a sentence stands on;
* and, while an answer is being made, three kinds of event the engine pushes
  through its `emit` callback: `NarrationEvent`, `TokenEvent` and `ShelfEvent`.

Nothing here imports the search engine at runtime: `ChatTurn.result_set` holds
`app.search.engine.SearchResult` objects, but only as a type name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "Receipt",
    "ChatTurn",
    "NarrationEvent",
    "TokenEvent",
    "ShelfEvent",
    "KINDS",
    "ROLES",
]

#: What `ChatTurn.kind` may be. The tab branches on it, so it is a closed set.
KINDS = ("answer", "find", "aggregate", "absence", "error", "clarify")

#: `ChatTurn.role`.
ROLES = ("user", "assistant")


@dataclass(frozen=True)
class Receipt:
    """A document an answer stands on, and the exact words that support it.

    **`quote` is always copied from the indexed text by the system**, never
    written by the model - so it can be shown in quotation marks without any
    chance of being a misquote (work order section 2b).

    `file_id` is `None` only for a document that has left the index between the
    answer being made and the receipt being read; `path` and `name` still say
    what it was.
    """

    file_id: Optional[int]
    path: str
    name: str
    quote: str
    #: Where in the document the quote sits, in words: "page 3", "sheet Q3!A14".
    #: Empty when the extractor could not say.
    locator: str = ""
    chunk_id: Optional[int] = None


@dataclass
class ChatTurn:
    """One message in a conversation.

    For an assistant turn of `kind="answer"`, **every sentence of `text` ends in
    a source marker `[n]`, and `receipts[n - 1]` is the document it points at**.
    Markers are numbered in first-mention order and never renumbered while an
    answer streams. Other kinds carry text the *system* wrote from a template
    (a computed count, "that's all 7", the searched-and-found-nothing protocol),
    which makes no claim about the contents of a document.
    """

    role: str                                   # "user" | "assistant"
    text: str
    receipts: list[Receipt] = field(default_factory=list)
    #: For FIND-routed answers (and list-type aggregates): the results, as
    #: `app.search.engine.SearchResult` objects, ready for the result delegate.
    result_set: Optional[list] = None
    kind: str = "answer"                        # see KINDS
    #: Plain-words detail that belongs beside the answer rather than in it, e.g.
    #: what was searched when nothing was found.
    notes: list[str] = field(default_factory=list)
    #: **Added by the engine, not part of the original contract - it has a
    #: default, so nothing that builds a turn without it breaks.** The debug
    #: pane's view of how the answer was made: the route and why, the queries
    #: run, rounds used, timings, model names. Plain JSON-able values only.
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NarrationEvent:
    """A plain-words line about what the engine is doing right now.

    "Looking for invoices from 2024..." The first one is emitted before any
    model is consulted, so it arrives in well under a second.
    """

    text: str


@dataclass(frozen=True)
class TokenEvent:
    """Answer text, streamed. Only text that has already been verified.

    Emitted a verified sentence at a time (its source markers included), never
    a raw model token: an unverified word is not allowed on screen, even for
    the moment before it would have been taken back.
    """

    text: str


@dataclass(frozen=True)
class ShelfEvent:
    """A document the conversation has just touched - it joins the shelf."""

    receipt: Receipt
