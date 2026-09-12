r"""L8b §1a: which of the six classes a question is, before retrieval runs.

Layer: L8b

**No test here needs a running Ollama** - the same rule `test_translate.py`
holds itself to, for the same reason: the fake client is possible only
because the model's job is narrow (one word out of a fixed six), so it is
testable as a canned string without ever starting a real model.
"""

from __future__ import annotations

import pytest

from app.chat.router import (
    ROUTE_TIMEOUT_S,
    QuestionClass,
    build_prompt,
    classify,
)


class FakeClient:
    """Returns a canned reply. Mirrors `test_translate.py`'s own fake."""

    def __init__(self, reply: str = "", *, healthy: bool = True, raises: BaseException = None):
        self.reply = reply
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

        class Response:
            text = self.reply

        return Response()


# ---------------------------------------------------------------------------
# Rules alone - no client needed at all
# ---------------------------------------------------------------------------

RULE_TABLE = [
    ("how many PDFs did Dave send in 2019", QuestionClass.AGGREGATE),
    ("count the invoices from last year", QuestionClass.AGGREGATE),
    ("list all the photos from the wedding", QuestionClass.AGGREGATE),
    ("do I have any invoices from Amazon", QuestionClass.ABSENCE),
    ("did I ever get a reply from the landlord", QuestionClass.ABSENCE),
    ("is there a contract for the lease anywhere", QuestionClass.ABSENCE),
    ("show me the photos of the kids at the beach", QuestionClass.FIND),
    ("find the safety report for the Leeds site", QuestionClass.FIND),
    ("pictures of Diwali 2019", QuestionClass.FIND),
    ("summarise what we agreed with the landlord", QuestionClass.SYNTHESIS),
    ("compare the two drafts of the contract", QuestionClass.SYNTHESIS),
    ("what's the difference between the March and April invoices",
     QuestionClass.SYNTHESIS),
    ("what did the lease say about the deposit", QuestionClass.LOOKUP),
    ("when is the safety report due", QuestionClass.LOOKUP),
]


@pytest.mark.parametrize("question,expected", RULE_TABLE)
def test_rules_classify_without_a_model(question, expected):
    decision = classify(question)
    assert decision.question_class == expected
    assert not decision.used_model


def test_a_matched_rule_names_itself_for_the_debug_pane():
    decision = classify("how many PDFs did Dave send")
    assert decision.matched_rule == "aggregate_phrase"


def test_how_much_is_not_a_blunt_aggregate_rule():
    r"""**Found live, against the real model this order targets.** "how much
    did the landlord charge for the deposit" is one figure in one document -
    a LOOKUP - while "how much have I spent on the kitchen" sums across
    many. Unlike "how many", which is reliably a count either way, "how
    much" is genuinely ambiguous and must not be decided by a rule."""
    decision = classify("how much did the landlord charge for the deposit")
    assert decision.matched_rule != "aggregate_phrase"


@pytest.mark.parametrize("question", [
    "what changed between the two drafts",
    "what's changed between the two versions of the agreement",
    "what has changed since the last contract",
    "what's been changed in the new draft",
])
def test_what_changed_is_recognised_in_its_common_forms(question):
    """Found live: the bare "what changed" rule missed the contraction
    ("what's changed") a real question actually used, and the model timed
    out on the same call - a genuine synthesis question routed nowhere."""
    decision = classify(question)
    assert decision.question_class == QuestionClass.SYNTHESIS
    assert not decision.used_model


# ---------------------------------------------------------------------------
# FOLLOW_UP: only ever assigned when there is a prior turn
# ---------------------------------------------------------------------------

def test_a_follow_up_phrase_needs_history_to_be_recognised():
    with_history = classify("what about the other one", history=["a prior turn"])
    without_history = classify("what about the other one")

    assert with_history.question_class == QuestionClass.FOLLOW_UP
    assert without_history.question_class != QuestionClass.FOLLOW_UP


def test_the_same_wording_with_no_history_falls_through_to_lookup():
    """A vague question opening a fresh conversation has nothing to follow up
    on - it is answered as an ordinary, if vague, LOOKUP rather than routed
    into a class with no history to draw from."""
    decision = classify("tell me more")
    assert decision.question_class == QuestionClass.LOOKUP


# ---------------------------------------------------------------------------
# Model assist - only reached when the rules cannot tell
# ---------------------------------------------------------------------------

def test_the_model_is_not_asked_when_a_rule_already_matched():
    client = FakeClient("LOOKUP")
    classify("how many photos are there", client=client)
    assert client.calls == 0, "a rule already answered this - the model must not be asked"


def test_a_valid_model_label_is_used():
    client = FakeClient("SYNTHESIS")
    decision = classify("ambiguous question with no rule shape", client=client)
    assert decision.question_class == QuestionClass.SYNTHESIS
    assert decision.used_model


def test_the_model_reply_is_read_from_a_full_sentence():
    """A model asked for one word reliably wraps it in a sentence anyway."""
    client = FakeClient("The class is: AGGREGATE.")
    decision = classify("ambiguous question with no rule shape", client=client)
    assert decision.question_class == QuestionClass.AGGREGATE


def test_an_invented_label_is_rejected_not_passed_through():
    """The same discipline `translate._rejects` applies to an invented
    operator, applied here to an invented class."""
    client = FakeClient("SENTIMENT")
    decision = classify("ambiguous question with no rule shape", client=client)
    assert decision.question_class == QuestionClass.LOOKUP
    assert decision.used_model
    assert decision.raw_model_output == "SENTIMENT"


def test_the_model_cannot_claim_follow_up():
    """The prompt never offers FOLLOW_UP - see `build_prompt` - so a model
    that answers it anyway is an invention like any other, not a valid class
    this call path could have produced."""
    client = FakeClient("FOLLOW_UP")
    decision = classify("ambiguous question with no rule shape", client=client)
    assert decision.question_class == QuestionClass.LOOKUP


def test_an_empty_reply_falls_back_to_lookup():
    client = FakeClient("")
    decision = classify("ambiguous question with no rule shape", client=client)
    assert decision.question_class == QuestionClass.LOOKUP


def test_a_raising_client_falls_back_to_lookup_rather_than_raising():
    decision = classify("ambiguous question with no rule shape",
                        client=FakeClient(raises=RuntimeError("boom")))
    assert decision.question_class == QuestionClass.LOOKUP


def test_an_unhealthy_client_is_never_asked():
    client = FakeClient("SYNTHESIS", healthy=False)
    decision = classify("ambiguous question with no rule shape", client=client)
    assert decision.question_class == QuestionClass.LOOKUP
    assert client.calls == 0


def test_no_client_at_all_falls_back_to_lookup_without_claiming_the_model_ran():
    decision = classify("ambiguous question with no rule shape")
    assert decision.question_class == QuestionClass.LOOKUP
    assert not decision.used_model


def test_an_empty_question_is_lookup_and_never_reaches_the_model():
    client = FakeClient("SYNTHESIS")
    decision = classify("   ", client=client)
    assert decision.question_class == QuestionClass.LOOKUP
    assert client.calls == 0


# ---------------------------------------------------------------------------
# The prompt itself
# ---------------------------------------------------------------------------

def test_the_prompt_never_offers_follow_up():
    prompt = build_prompt("what about the other one")
    assert "FOLLOW_UP" not in prompt


def test_the_prompt_carries_the_question_verbatim():
    prompt = build_prompt("what did the lease say about the deposit")
    assert "what did the lease say about the deposit" in prompt


def test_the_route_timeout_is_short_because_nobody_asked_to_wait():
    """Unlike translation - a five-to-thirty-second wait behind an explicit
    button - routing gates every step of a loop nobody chose to wait on
    yet."""
    assert ROUTE_TIMEOUT_S <= 5.0
