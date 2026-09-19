"""Stand-ins for the Chat engine and the session store, for the Chat tab's tests.

Layer: L0 (test support; no Qt)

The real `ChatEngine` is built by somebody else, so the tab is tested against
the *contract* (`app/chat/types.py`): an object with `.ask(question, history,
emit, should_stop, ...)` that blocks, streams events through `emit`, polls
`should_stop`, and returns a `ChatTurn`; and `.available()`. `FakeChatEngine`
is exactly that, scripted per question, so a scenario reads as the answer it
describes.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from app.chat.types import (
    ChatTurn, NarrationEvent, Receipt, ShelfEvent, TokenEvent,
)

LETTER = Receipt(file_id=101, path="C:/mail/landlord-2019-03.pdf",
                 name="landlord-2019-03.pdf",
                 quote="The deposit of two months' rent will be held in a protected scheme.",
                 locator="page 2", chunk_id=1001)
AGREEMENT = Receipt(file_id=102, path="C:/mail/tenancy-agreement.docx",
                    name="tenancy-agreement.docx",
                    quote="The deposit shall be returned within thirty days of the end of the tenancy.",
                    locator="clause 4", chunk_id=1002)

#: The two sentences and two receipts the order's first acceptance sentence asks for.
DEPOSIT_TEXT = ("The deposit is two months' rent, held in a protected scheme [1]. "
                "It comes back within thirty days of the tenancy ending [2].")


def deposit_turn() -> ChatTurn:
    return ChatTurn("assistant", DEPOSIT_TEXT, receipts=[LETTER, AGREEMENT])


def tokens_of(text: str, size: int = 9) -> list[TokenEvent]:
    return [TokenEvent(text[i:i + size]) for i in range(0, len(text), size)]


def search_results(count: int) -> list:
    from app.search.engine import SearchResult

    return [SearchResult(chunk_id=500 + i, file_id=200 + i,
                         path=f"C:/photos/beach-{i}.jpg", text=f"beach photo {i}",
                         score=1.0 - i / 10, rank=i + 1, ext="jpg")
            for i in range(count)]


class FakeChatEngine:
    """A scripted engine. `script` maps a question to `(events, final turn)`.

    `hold_at` is an event index to pause at until `release()` is called (or the
    UI asks to stop) - which is how a test looks at the screen mid-stream.
    """

    def __init__(self, script: Optional[Callable[[str], tuple]] = None,
                 available: tuple = (True, ""), hold_at: Optional[int] = None) -> None:
        self.script = script or self._default
        self._available = available
        self.hold_at = hold_at
        self.calls: list[dict] = []
        self.threads: list[str] = []
        self.saw_stop = False
        self._release = threading.Event()
        self.held = threading.Event()

    # -- the contract ---------------------------------------------------------
    def available(self) -> tuple:
        self.threads.append(threading.current_thread().name)
        return self._available

    def ask(self, question, history, emit, should_stop, scope=None, style=None):
        self.threads.append(threading.current_thread().name)
        self.calls.append({"question": question, "history": list(history),
                           "scope": scope, "style": style})
        events, turn = self.script(question)
        for index, event in enumerate(events):
            if should_stop():
                self.saw_stop = True
                return ChatTurn("assistant", "", kind="answer")
            if self.hold_at is not None and index == self.hold_at:
                self.held.set()
                deadline = time.monotonic() + 10
                while not self._release.is_set() and time.monotonic() < deadline:
                    if should_stop():
                        self.saw_stop = True
                        return ChatTurn("assistant", "", kind="answer")
                    time.sleep(0.01)
            emit(event)
        return turn

    # -- test controls ----------------------------------------------------------
    def release(self) -> None:
        self._release.set()

    @staticmethod
    def _default(question: str) -> tuple:
        events: list[Any] = [
            NarrationEvent("Searching your files and mail."),
            NarrationEvent("Found 2 documents - reading the 2019 letter."),
            ShelfEvent(LETTER), ShelfEvent(AGREEMENT),
        ]
        events += tokens_of(DEPOSIT_TEXT)
        return events, deposit_turn()


class FakeSessionBackend:
    """The three store methods the engine's migration adds, in memory."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.threads: list[str] = []

    def save_session(self, record: dict) -> None:
        self.threads.append(threading.current_thread().name)
        self.records[record["id"]] = record

    def load_sessions(self) -> list:
        self.threads.append(threading.current_thread().name)
        return list(self.records.values())

    def delete_session(self, session_id: str) -> None:
        self.threads.append(threading.current_thread().name)
        self.records.pop(session_id, None)
