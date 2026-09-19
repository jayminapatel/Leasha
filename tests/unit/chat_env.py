"""Shared setup for the Chat engine's tests: a real store holding the fixture corpus, a
real keyword-only search engine over it, and a `ChatEngine` around a `FakeLLM`.

Layer: L8b (test support). Not a test module - the leading name has no `test_`.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.chat.config import ChatSettings
from app.chat.engine import ChatEngine
from app.chat.testing import FakeLLM, keyword_engine
from app.chat.types import ChatTurn
from app.storage.sqlite_store import SqliteStore
from tests.fixtures import chat_eval as fx


class Env:
    """`store`, `search`, and a way to make engines. Close with `close()`."""

    def __init__(self, folder) -> None:
        self.store = SqliteStore(folder / "chat-test.db").connect()
        self.ids = fx.load_into(self.store)
        self.search = keyword_engine(self.store)

    def engine(self, llm: Any = "extractive", **settings: Any) -> ChatEngine:
        if isinstance(llm, str):
            llm = FakeLLM(llm)
        return ChatEngine(self.search, self.store, llm,
                          ChatSettings(today=fx.TODAY, **settings))

    def close(self) -> None:
        self.search.close()
        self.store.close()


def ask(engine: ChatEngine, question: str, history: Optional[list] = None,
        should_stop: Callable[[], bool] = lambda: False) -> tuple[ChatTurn, list]:
    """`(turn, events)` for one question."""
    events: list = []
    turn = engine.ask(question, history or [], events.append, should_stop)
    return turn, events


def converse(engine: ChatEngine, *questions: str) -> tuple[ChatTurn, list[ChatTurn]]:
    """Ask several questions in one conversation; return the last turn and the history."""
    history: list[ChatTurn] = []
    turn = None
    for question in questions:
        turn, _events = ask(engine, question, history)
        history += [ChatTurn("user", question), turn]
    return turn, history
