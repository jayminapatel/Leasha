"""Chat's sources: the reranker's best of what was already retrieved, each cut to a window.

Layer: L8b. Work order model-sequencing, item 3b (2026-10-10).

Reading the prompt is the cost of a Chat answer. The answer used to be built from the
fused order, with each passage cut only to its share of the budget - nearly whole
chunks. Now the reranker runs **once** over the candidates the search loop already
gathered (never a second retrieval), and each source the model reads is a window of
the reranker's own width around the question's words. The verifier still checks
against the whole stored text, and the file names stay on every source.

Real store and keyword search over the chat fixture corpus; a fake model and a fake
reranker - no model is downloaded or run.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.chat import prompts
from app.chat.config import ChatSettings
from app.chat.engine import ChatEngine
from app.chat.reconcile import audit_answer
from app.chat.testing import FakeLLM
from tests.fixtures import chat_eval as fx
from tests.unit.chat_env import Env, ask

QUESTION = "What did we agree with the landlord about the deposit?"     # fixture L01


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Env(tmp_path_factory.mktemp("sources-rerank"))
    yield e
    e.close()


class _CountingSearch:
    """The real keyword engine, counting its searches, with a reranker of our choosing."""

    def __init__(self, real: Any, reranker: Any) -> None:
        self._real = real
        self.reranker = reranker
        self.searches = 0
        self.rerank_flags: list = []

    def search(self, *args: Any, **kwargs: Any) -> Any:
        self.searches += 1
        self.rerank_flags.append(kwargs.get("rerank"))
        return self._real.search(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _FakeReranker:
    """Scores a passage by how many of the terms it holds; records every call."""

    def __init__(self, window_chars: int = 600, *, available: bool = True,
                 fail: bool = False) -> None:
        self.window_chars = window_chars
        self.available = available
        self.fail = fail
        self.calls: list[tuple[str, list[str], list]] = []

    def rerank(self, query: str, hits: Any, *, text_key: str = "text", terms: Any = None) -> list:
        hits = list(hits)
        self.calls.append((query, [str(h[text_key]) for h in hits], list(terms or ())))
        if self.fail:
            raise RuntimeError("the reranker's host went away")
        words = [w.lower() for w in (terms or query.split())]

        def score(hit: dict) -> int:
            text = str(hit[text_key]).lower()
            return sum(text.count(w) for w in words)

        return sorted(hits, key=lambda h: -score(h))           # stable: ties keep fused order


def _engine(env: Env, reranker: Any, llm: Any = None) -> tuple[ChatEngine, _CountingSearch, FakeLLM]:
    llm = llm or FakeLLM("extractive")
    search = _CountingSearch(env.search, reranker)
    return ChatEngine(search, env.store, llm, ChatSettings(today=fx.TODAY)), search, llm


def _archive_system(llm: FakeLLM) -> str:
    for kind, messages in llm.chat_calls:
        if kind == "archive":
            return str(messages[0]["content"])
    raise AssertionError("the model was never shown the sources")


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def test_the_answer_reranks_once_over_what_was_already_retrieved(env):
    reranker = _FakeReranker()
    engine, search, _llm = _engine(env, reranker)
    turn, _events = ask(engine, QUESTION)

    plain, plain_search, _ = _engine(env, None)
    ask(plain, QUESTION)

    assert turn.kind == "answer"
    assert len(reranker.calls) == 1                         # one rerank call for the answer
    assert search.searches == plain_search.searches         # and no extra retrieval for it
    assert all(flag is False for flag in search.rerank_flags)   # the rounds stay unreranked
    assert turn.debug["reranked"] is True


def test_the_prompt_holds_windows_not_whole_passages(env):
    reranker = _FakeReranker(window_chars=160)
    engine, _search, llm = _engine(env, reranker)
    ask(engine, QUESTION)

    retrieved = reranker.calls[0][1]
    assert any(len(text) > 160 for text in retrieved), \
        "precondition: a retrieved chunk longer than the window, or the cut proves nothing"
    shown = prompts.parse_sources(_archive_system(llm))
    assert shown
    for passage in shown.values():
        assert len(passage.strip()) <= 160
        assert any(_flat(passage) in _flat(text) for text in retrieved)   # a slice, verbatim
    assert not any(_flat(passage) == _flat(text) for passage in shown.values()
                   for text in retrieved if len(text) > 160)


def test_the_cited_file_names_are_kept_and_the_verifier_still_checks(env):
    reranker = _FakeReranker()
    engine, _search, llm = _engine(env, reranker)
    turn, _events = ask(engine, QUESTION)

    assert turn.kind == "answer" and turn.receipts
    assert audit_answer(turn) == []                          # every cited sentence has a receipt
    system = _archive_system(llm)
    names = turn.debug["sources"]
    for n, name in enumerate(names, start=1):
        assert f"[{n}] {name}" in system                    # the file name is on the source
    for receipt in turn.receipts:
        assert receipt.name in names
    assert any("950" in _flat(r.quote) for r in turn.receipts) or "950" in turn.text


def test_the_reranker_s_order_is_the_order_the_sources_are_taken_in(env):
    from types import SimpleNamespace

    def hit(i: int, text: str) -> Any:
        return SimpleNamespace(chunk_id=i, file_id=i, path=f"C:/docs/doc-{i}.txt", text=text,
                               page=None, label="", score=1.0, rank=i)

    results = [hit(1, "nothing to see"), hit(2, "the deposit"), hit(3, "deposit deposit deposit")]
    engine, _search, _llm = _engine(env, _FakeReranker())
    debug: dict = {}
    ordered, width = engine._rerank_sources("deposit", results, ["deposit"], debug)

    assert [r.file_id for r in ordered] == [3, 2, 1]
    assert all(a is b for a, b in zip(sorted(ordered, key=lambda r: r.rank), results))
    assert width == 600 and debug["reranked"] is True


def test_a_reranker_that_fails_keeps_the_fused_order_and_still_answers(env):
    reranker = _FakeReranker(fail=True)
    engine, _search, llm = _engine(env, reranker)
    turn, _events = ask(engine, QUESTION)

    plain, _plain_search, plain_llm = _engine(env, None)
    plain_turn, _ = ask(plain, QUESTION)

    assert turn.kind == "answer" and turn.debug["reranked"] is False
    assert turn.debug["sources"] == plain_turn.debug["sources"]
    assert audit_answer(turn) == []


def test_reranking_switched_off_is_not_called_but_the_window_still_applies(env):
    reranker = _FakeReranker(window_chars=160, available=False)
    engine, _search, llm = _engine(env, reranker)
    ask(engine, QUESTION)

    assert reranker.calls == []
    for passage in prompts.parse_sources(_archive_system(llm)).values():
        assert len(passage.strip()) <= 160


def test_without_any_reranker_the_passage_is_the_default_window(env):
    from app.search.window import RERANK_WINDOW_CHARS

    engine, _search, llm = _engine(env, None)
    turn, _events = ask(engine, QUESTION)

    assert turn.debug["passage_chars"] == RERANK_WINDOW_CHARS
    for passage in prompts.parse_sources(_archive_system(llm)).values():
        assert len(passage.strip()) <= RERANK_WINDOW_CHARS


def test_build_sources_caps_the_window_but_keeps_the_whole_piece():
    from types import SimpleNamespace

    from app.chat.context import build_sources

    text = ("The weather was fine all week. " * 20) + "The deposit is 950 pounds. " + \
           ("Nothing else happened. " * 20)
    hit = SimpleNamespace(chunk_id=1, file_id=7, path="C:/letters/deposit.pdf", text=text,
                          page=None, label="", score=1.0, rank=1)
    [source] = build_sources([hit], ["deposit", "950"], max_sources=6, window_tokens=8192,
                             passage_chars=120)
    assert len(source.passage) <= 120 and "950" in source.passage
    assert source.pieces[0].text == text                    # the verifier's evidence is whole
    assert source.name == "deposit.pdf"
