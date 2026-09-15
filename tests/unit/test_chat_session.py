r"""One question, start to finish - the composition of §1a/§1b/§1d/§1e and
generation+verification into the single call a UI makes.

Layer: L8b

Every module `ask` composes is already tested on its own terms; these tests
are about the *branching* - which path a question takes and when the
result is downgraded to the honest-absence answer - not about re-proving
routing, retrieval or verification each work correctly in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.chat.session import ask


@dataclass
class FakeResult:
    path: str
    text: str = "some passage"


@dataclass
class FakeResponse:
    results: list = field(default_factory=list)


class FakeEngine:
    def __init__(self, responses: dict[str, FakeResponse]):
        self.responses = responses
        self.calls: list[str] = []

    def search(self, raw: str, **_kwargs):
        self.calls.append(raw)
        return self.responses.get(raw, FakeResponse())


class FakeStore:
    """Just enough for `count_matching` to run against an empty database -
    a real `SqliteStore` is simpler to fake honestly than to stub the SQL
    surface `count_matching` actually calls."""

    def __init__(self, tmp_path):
        from app.storage.sqlite_store import SqliteStore

        self._store = SqliteStore(tmp_path / "index.db").connect()
        self.conn = self._store.conn

    def close(self):
        self._store.close()


class SniffingClient:
    """Replies keyed by a substring of the prompt - the different calls
    `ask` makes (classify, sufficiency, generation) are told apart by what
    they actually ask for, not by call order, which varies by route."""

    def __init__(self, replies: dict[str, str], *, healthy: bool = True):
        self.replies = replies
        self.healthy = healthy
        self.prompts: list[str] = []

    def health(self, *, force: bool = False) -> bool:
        return self.healthy

    def has_model(self) -> bool:
        return self.healthy

    def generate(self, prompt: str, **_kwargs):
        self.prompts.append(prompt)
        text = ""
        for key, reply in self.replies.items():
            if key in prompt:
                text = reply
                break

        class Response:
            pass

        Response.text = text
        return Response()


class FakeEmbedder:
    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors

    def embed(self, texts):
        return [self.vectors.get(text, [0.0, 0.0, 1.0]) for text in texts]


SUPPORTED = [1.0, 0.0, 0.0]
UNRELATED = [0.0, 1.0, 0.0]
ONE_RESULT = FakeResponse(results=[FakeResult(path="C:/work/a.pdf", text="the tenancy deposit was 500 pounds")])


# ---------------------------------------------------------------------------
# AGGREGATE never retrieves
# ---------------------------------------------------------------------------

def test_aggregate_questions_never_call_the_search_engine(tmp_path):
    engine = FakeEngine({})
    store = FakeStore(tmp_path)
    try:
        result = ask("how many invoices are there", engine=engine, store=store)
        assert result.kind == "aggregate"
        assert result.aggregate is not None
        assert engine.calls == [], "AGGREGATE must be answered by a query, never a search"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# FIND returns results, not prose
# ---------------------------------------------------------------------------

def test_find_questions_return_result_rows_not_a_generated_answer(tmp_path):
    engine = FakeEngine({"show me the deposit records": ONE_RESULT})
    store = FakeStore(tmp_path)
    client = SniffingClient({})
    try:
        result = ask("show me the deposit records", engine=engine, store=store, client=client)
        assert result.kind == "find"
        assert len(result.find_results) == 1
        assert not any("Answer the question" in p for p in client.prompts), (
            "a FIND question must never reach the generation prompt")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Empty retrieval -> honest absence, whatever the route
# ---------------------------------------------------------------------------

def test_a_lookup_with_nothing_found_is_answered_honestly(tmp_path):
    engine = FakeEngine({})                       # every search returns empty
    store = FakeStore(tmp_path)
    try:
        result = ask("what did the lease say about the boiler", engine=engine, store=store,
                     roots=["D:\\Documents"])
        assert result.kind == "absence"
        assert "D:\\Documents" in result.absence.sentence
    finally:
        store.close()


def test_an_absence_shaped_question_that_finds_something_is_still_answered(tmp_path):
    r"""**The router's class is a hint, not a verdict.** "Do I have..." is
    shaped like an existence check, but if retrieval actually finds
    something, the honest thing is to answer it - `is_empty` is the real
    gate, checked after retrieval, not the route."""
    engine = FakeEngine({"do i have the deposit records": ONE_RESULT})
    store = FakeStore(tmp_path)
    embedder = FakeEmbedder({
        "The deposit was 500 pounds .": SUPPORTED,
        "the tenancy deposit was 500 pounds": SUPPORTED,
    })
    client = SniffingClient({"Reply with ENOUGH": "ENOUGH",
                            "Answer the question": "The deposit was 500 pounds [1]."})
    try:
        result = ask("do i have the deposit records", engine=engine, store=store,
                     client=client, embedder=embedder)
        assert result.kind == "answer"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# A generated answer that fails verification falls back to absence
# ---------------------------------------------------------------------------

def test_a_thin_answer_falls_back_to_the_absence_protocol(tmp_path):
    r"""**Retrieval succeeding is not the same as having an answer.** A
    question can find documents and still not produce anything verified -
    a fabrication, or a question the passages happen not to answer - and
    that must read as the same honest "nothing confirmed" the empty-
    retrieval case gives, not a thin or empty-looking "answer"."""
    engine = FakeEngine({"what colour is the front door": ONE_RESULT})
    store = FakeStore(tmp_path)
    # The fabricated sentence and the real passage map to orthogonal
    # vectors - genuinely unrelated, not merely "unmapped and therefore
    # accidentally identical" (both default to the same fallback vector).
    embedder = FakeEmbedder({
        "The door is red .": UNRELATED,
        "the tenancy deposit was 500 pounds": SUPPORTED,
    })
    client = SniffingClient({"Reply with ENOUGH": "ENOUGH",
                            "Answer the question": "The door is red [1]."})
    try:
        result = ask("what colour is the front door", engine=engine, store=store,
                     client=client, embedder=embedder, roots=["D:\\Documents"])
        assert result.kind == "absence"
        assert result.absence is not None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# LOOKUP with a real, verifiable answer
# ---------------------------------------------------------------------------

def test_a_well_supported_lookup_produces_a_real_answer(tmp_path):
    engine = FakeEngine({"what was the deposit": ONE_RESULT})
    store = FakeStore(tmp_path)
    embedder = FakeEmbedder({
        "The deposit was 500 pounds .": SUPPORTED,
        "the tenancy deposit was 500 pounds": SUPPORTED,
    })
    client = SniffingClient({"Reply with ENOUGH": "ENOUGH",
                            "Answer the question": "The deposit was 500 pounds [1]."})
    try:
        result = ask("what was the deposit", engine=engine, store=store,
                     client=client, embedder=embedder)
        assert result.kind == "answer"
        assert result.answer.sentences == ("The deposit was 500 pounds .",)
    finally:
        store.close()


def test_the_route_is_always_recorded_on_the_result(tmp_path):
    engine = FakeEngine({})
    store = FakeStore(tmp_path)
    try:
        result = ask("how many invoices", engine=engine, store=store)
        assert result.route.question_class.value == "AGGREGATE"
    finally:
        store.close()
