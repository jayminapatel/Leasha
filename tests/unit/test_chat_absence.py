r"""L8b §1e: when nothing matches, say so honestly.

Layer: L8b

Pure functions over `LoopResult` - no client, no store, no fake needed
beyond a bare response shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.chat.absence import answer_absence, is_empty
from app.chat.loop import LoopResult


@dataclass
class FakeResult:
    path: str = "a.pdf"


@dataclass
class FakeResponse:
    results: list = field(default_factory=list)


def _result(*, queries=(), responses=(), question="q") -> LoopResult:
    return LoopResult(question=question, queries=queries, responses=responses)


# ---------------------------------------------------------------------------
# is_empty
# ---------------------------------------------------------------------------

def test_a_loop_with_no_responses_is_empty():
    assert is_empty(_result())


def test_a_loop_where_every_round_found_nothing_is_empty():
    result = _result(queries=("q1", "q2"),
                     responses=(FakeResponse(results=[]), FakeResponse(results=[])))
    assert is_empty(result)


def test_a_loop_with_at_least_one_result_is_not_empty():
    result = _result(queries=("q1", "q2"),
                     responses=(FakeResponse(results=[]),
                                FakeResponse(results=[FakeResult()])))
    assert not is_empty(result)


# ---------------------------------------------------------------------------
# answer_absence
# ---------------------------------------------------------------------------

def test_the_sentence_names_the_original_question():
    result = _result(question="do I have the deposit letter", queries=("deposit letter",),
                     responses=(FakeResponse(results=[]),))
    answer = answer_absence(result)
    assert "do I have the deposit letter" in answer.sentence


def test_the_sentence_never_claims_the_thing_does_not_exist():
    """Principle 4 of the order's own opening paragraph: absence is scoped
    to the index, never a claim about the world."""
    result = _result(queries=("q",), responses=(FakeResponse(results=[]),))
    answer = answer_absence(result)
    lowered = answer.sentence.lower()
    assert "does not exist" not in lowered
    assert "doesn't exist" not in lowered
    assert "in leasha's index" in lowered


def test_every_query_actually_run_is_shown():
    result = _result(queries=("deposit letter", "from:landlord deposit"),
                     responses=(FakeResponse(results=[]), FakeResponse(results=[])))
    answer = answer_absence(result)
    assert '"deposit letter"' in answer.sentence
    assert '"from:landlord deposit"' in answer.sentence


def test_no_queries_at_all_falls_back_to_the_bare_question():
    """An empty-question loop, say - nothing was ever searched, so the
    sentence still has to name *something* as "what was searched"."""
    result = _result(question="", queries=(), responses=())
    answer = answer_absence(result)
    assert "Searched:" in answer.sentence


def test_the_configured_roots_are_named_as_the_scope():
    result = _result(queries=("q",), responses=(FakeResponse(results=[]),))
    answer = answer_absence(result, roots=["D:\\Documents", "D:\\Mail"])
    assert "D:\\Documents" in answer.sentence
    assert "D:\\Mail" in answer.sentence


def test_no_configured_roots_says_so_plainly_not_as_a_fault():
    result = _result(queries=("q",), responses=(FakeResponse(results=[]),))
    answer = answer_absence(result, roots=[])
    assert "no folders are set up to be searched yet" in answer.sentence


def test_the_next_steps_are_offered():
    result = _result(queries=("q",), responses=(FakeResponse(results=[]),))
    answer = answer_absence(result)
    assert "different words" in answer.sentence.lower()
    assert "not been indexed" in answer.sentence.lower()


def test_the_answer_carries_the_queries_for_a_caller_that_wants_them_structured():
    result = _result(queries=("a", "b"), responses=(FakeResponse(results=[]), FakeResponse(results=[])))
    answer = answer_absence(result)
    assert answer.queries == ("a", "b")
