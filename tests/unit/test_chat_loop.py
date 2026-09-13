r"""L8b §1b: plan a search, retrieve, and know when to stop searching.

Layer: L8b

**No test here needs a running Ollama or a real index** - the engine, the
translator and the model client are all fakes, the same discipline
`test_translate.py` and `test_chat_router.py` hold themselves to.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.chat.loop import (
    MAX_ROUNDS,
    RESULT_SUMMARY_LIMIT,
    LoopResult,
    build_sufficiency_prompt,
    run_loop,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class FakeResult:
    path: str
    text: str = "some passage"


@dataclass
class FakeResponse:
    results: list = field(default_factory=list)


class FakeEngine:
    """Returns a canned response per query, or an empty one for anything
    not in the map. Records every query it was asked, in order."""

    def __init__(self, responses: dict[str, FakeResponse]):
        self.responses = responses
        self.calls: list[str] = []

    def search(self, raw: str, **_kwargs):
        self.calls.append(raw)
        return self.responses.get(raw, FakeResponse())


class FakeTranslator:
    def __init__(self, query: str):
        self.query = query
        self.calls: list[str] = []

    def translate(self, sentence: str, store=None):
        self.calls.append(sentence)

        class _T:
            query = self.query

        return _T()


class FakeClient:
    """Returns each reply in `replies` in turn, one per call. Mirrors
    `test_chat_router.py`'s own fake, extended for a scripted sequence
    since the loop may ask more than once."""

    def __init__(self, replies=(), *, healthy: bool = True, raises: BaseException = None):
        self.replies = list(replies)
        self.healthy = healthy
        self.raises = raises
        self.calls = 0
        self.prompts: list[str] = []

    def health(self, *, force: bool = False) -> bool:
        return self.healthy

    def has_model(self) -> bool:
        return self.healthy

    def generate(self, prompt: str, **_kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        if self.raises is not None:
            raise self.raises
        reply = self.replies[self.calls - 1] if self.calls <= len(self.replies) else "ENOUGH"

        class Response:
            text = reply

        return Response()


ONE_RESULT = FakeResponse(results=[FakeResult(path="C:/work/deposit-letter.pdf")])


# ---------------------------------------------------------------------------
# The basics
# ---------------------------------------------------------------------------

def test_an_empty_question_does_nothing():
    result = run_loop("   ", engine=FakeEngine({}))
    assert result.stopped_reason == "empty_question"
    assert result.rounds_used == 0
    assert result.queries == ()


def test_round_one_runs_the_raw_text_with_no_translator():
    engine = FakeEngine({"the lease deposit": ONE_RESULT})
    result = run_loop("the lease deposit", engine=engine)
    assert result.queries == ("the lease deposit",)
    assert engine.calls == ["the lease deposit"]


def test_round_one_uses_the_translated_query_when_a_translator_is_given():
    engine = FakeEngine({"deposit from:landlord": ONE_RESULT})
    translator = FakeTranslator("deposit from:landlord")
    result = run_loop("what did the landlord say about the deposit",
                      engine=engine, translator=translator)
    assert result.queries == ("deposit from:landlord",)
    assert translator.calls == ["what did the landlord say about the deposit"]


def test_no_client_stops_after_round_one():
    """No model to ask means no way to request a further search - one round
    is what there is."""
    engine = FakeEngine({"q": ONE_RESULT})
    result = run_loop("q", engine=engine)
    assert result.rounds_used == 1
    assert result.stopped_reason == "no_further_query"


# ---------------------------------------------------------------------------
# The model deciding sufficiency
# ---------------------------------------------------------------------------

def test_the_model_saying_enough_stops_the_loop():
    engine = FakeEngine({"q": ONE_RESULT})
    client = FakeClient(["ENOUGH"])
    result = run_loop("q", engine=engine, client=client)
    assert result.rounds_used == 1
    assert result.stopped_reason == "sufficient"
    assert client.calls == 1


def test_a_valid_follow_up_query_runs_a_second_round():
    engine = FakeEngine({"q": ONE_RESULT, "from:dave licence": ONE_RESULT})
    client = FakeClient(["from:dave licence", "ENOUGH"])
    result = run_loop("q", engine=engine, client=client)
    assert result.queries == ("q", "from:dave licence")
    assert result.rounds_used == 2
    assert result.stopped_reason == "sufficient"


def test_the_loop_never_exceeds_max_rounds():
    """A model that keeps proposing new queries forever is stopped at the
    ceiling - a small model in a tight loop is the design, not a licence
    for the loop to run indefinitely."""
    engine = FakeEngine({
        "q": ONE_RESULT, "q2": ONE_RESULT, "q3": ONE_RESULT, "q4": ONE_RESULT,
    })
    client = FakeClient(["q2", "q3", "q4"])
    result = run_loop("q", engine=engine, client=client, max_rounds=3)
    assert result.rounds_used == 3
    assert result.stopped_reason == "max_rounds"
    assert len(result.queries) == 3
    # The sufficiency check is never even asked on the final round - there is
    # no further round left to run its answer against.
    assert client.calls == 2


def test_an_invalid_follow_up_is_rejected_not_run():
    """Prose, or anything that will not parse, ends the loop with what
    round one already found - never a partial or nonsense search."""
    engine = FakeEngine({"q": ONE_RESULT})
    client = FakeClient(["I think you should look in the Documents folder"])
    result = run_loop("q", engine=engine, client=client)
    assert result.rounds_used == 1
    assert result.stopped_reason == "invalid_follow_up"
    assert engine.calls == ["q"]


def test_a_repeated_query_is_rejected_as_stalling():
    """The model proposing its own last query again is not new information -
    running it a second time would spend a round to learn nothing."""
    engine = FakeEngine({"q": ONE_RESULT})
    client = FakeClient(["q"])
    result = run_loop("q", engine=engine, client=client)
    assert result.rounds_used == 1
    assert result.stopped_reason == "invalid_follow_up"


def test_an_empty_reply_stops_the_loop():
    engine = FakeEngine({"q": ONE_RESULT})
    client = FakeClient([""])
    result = run_loop("q", engine=engine, client=client)
    assert result.stopped_reason == "no_further_query"


def test_a_raising_client_stops_the_loop_rather_than_crashing():
    engine = FakeEngine({"q": ONE_RESULT})
    client = FakeClient(raises=RuntimeError("boom"))
    result = run_loop("q", engine=engine, client=client)
    assert result.rounds_used == 1
    assert result.stopped_reason == "no_further_query"


def test_an_unhealthy_client_is_never_asked():
    engine = FakeEngine({"q": ONE_RESULT})
    client = FakeClient(["from:dave"], healthy=False)
    result = run_loop("q", engine=engine, client=client)
    assert client.calls == 0
    assert result.stopped_reason == "no_further_query"


# ---------------------------------------------------------------------------
# Narration
# ---------------------------------------------------------------------------

def test_narration_is_captured_even_with_no_callback():
    engine = FakeEngine({"q": ONE_RESULT})
    client = FakeClient(["ENOUGH"])
    result = run_loop("q", engine=engine, client=client)
    assert any("Searching" in line for line in result.narration)
    assert any("Found 1 document" in line for line in result.narration)


def test_a_narrate_callback_receives_every_line_live():
    seen: list[str] = []
    engine = FakeEngine({"q": ONE_RESULT})
    client = FakeClient(["ENOUGH"])
    run_loop("q", engine=engine, client=client, on_narrate=seen.append)
    assert seen == list(run_loop("q", engine=engine, client=FakeClient(["ENOUGH"])).narration)


def test_a_broken_narrate_callback_does_not_break_the_loop():
    engine = FakeEngine({"q": ONE_RESULT})

    def broken(_line):
        raise RuntimeError("the UI blew up")

    result = run_loop("q", engine=engine, on_narrate=broken)
    assert result.rounds_used == 1


def test_zero_results_is_worded_in_the_singular_correctly():
    engine = FakeEngine({"q": FakeResponse(results=[])})
    result = run_loop("q", engine=engine)
    assert any("Found 0 documents" in line for line in result.narration)


# ---------------------------------------------------------------------------
# `all_results` and the sufficiency prompt
# ---------------------------------------------------------------------------

def test_all_results_flattens_every_round():
    engine = FakeEngine({
        "q": FakeResponse(results=[FakeResult(path="a.pdf")]),
        "more": FakeResponse(results=[FakeResult(path="b.pdf")]),
    })
    client = FakeClient(["more", "ENOUGH"])
    result = run_loop("q", engine=engine, client=client)
    assert [r.path for r in result.all_results] == ["a.pdf", "b.pdf"]


def test_the_sufficiency_prompt_never_carries_the_result_text():
    """Only counts and filenames - the retrieved passage text staying out of
    this call is what keeps it cheap and keeps the model from starting to
    answer before verification (§2) exists to check it."""
    response = FakeResponse(results=[
        FakeResult(path="C:/work/deposit-letter.pdf",
                  text="THE SECRET PASSAGE TEXT THAT MUST NOT LEAK"),
    ])
    prompt = build_sufficiency_prompt("q", [("q", response)])
    assert "deposit-letter.pdf" in prompt
    assert "THE SECRET PASSAGE TEXT" not in prompt


def test_the_sufficiency_prompt_caps_how_many_filenames_it_lists():
    results = [FakeResult(path=f"file{n}.pdf") for n in range(RESULT_SUMMARY_LIMIT + 5)]
    prompt = build_sufficiency_prompt("q", [("q", FakeResponse(results=results))])
    for n in range(RESULT_SUMMARY_LIMIT):
        assert f"file{n}.pdf" in prompt
    assert f"file{RESULT_SUMMARY_LIMIT + 4}.pdf" not in prompt


def test_max_rounds_matches_the_ceiling_the_order_names():
    assert MAX_ROUNDS == 3


def test_loop_result_defaults_are_a_valid_empty_result():
    """The dataclass's own defaults must describe a real, consistent state -
    the shape `run_loop` returns for an empty question."""
    result = LoopResult(question="")
    assert result.queries == result.responses == ()
    assert result.all_results == []
