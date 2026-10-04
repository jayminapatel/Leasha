"""The question router: each class routed correctly, with no model in sight.

Layer: L8b. Work order `202626270611-chat-tab` sections 1a and 5 (*"router: each class
routed correctly on the fixture set (Qt-free)"*).
"""

from __future__ import annotations

import pytest

from app.chat.router import (
    ABSENCE, AGGREGATE, CLASSES, FIND, FOLLOWUP, LOOKUP, SYNTHESIS,
    resolve_followup, route_question,
)
from app.chat.testing import FakeLLM
from app.chat.types import ChatTurn
from tests.fixtures.chat_eval import QUESTIONS


def _history(*questions: str) -> list[ChatTurn]:
    turns: list[ChatTurn] = []
    for question in questions:
        turns += [ChatTurn("user", question), ChatTurn("assistant", "An answer [1].")]
    return turns


@pytest.mark.parametrize("qa", QUESTIONS, ids=lambda q: q.id)
def test_every_fixture_question_goes_to_the_right_machine(qa):
    route = route_question(qa.question, _history(*qa.history))
    assert route.kind == qa.cls, route.explain()
    assert route.by == "rules"                                # no model was needed for any of them


def test_every_class_is_exercised_by_the_fixture():
    assert {qa.cls for qa in QUESTIONS} == set(CLASSES)


@pytest.mark.parametrize("question, expected", [
    ("how many PDFs did Dave send in 2019", AGGREGATE),
    ("How many emails have an attachment?", AGGREGATE),
    ("what is the total size of my photos", AGGREGATE),
    ("list all the spreadsheets", AGGREGATE),
    ("Do I have anything from the solicitor?", ABSENCE),
    ("is there a document about the loft?", ABSENCE),
    ("Show me the photos from Diwali", FIND),
    ("find the safety report", FIND),
    # 1 October 2026: a kind of document and what it is about, no question word.
    ("mail about holiday from maya", FIND),
    ("emails from the bank", FIND),
    ("documents about the boiler", FIND),
    ("what did the mail from maya say?", LOOKUP),
    ("Summarise the audit findings", SYNTHESIS),
    ("compare the two quotes", SYNTHESIS),
    ("What did the landlord say about the boiler?", LOOKUP),
    ("Who signed the contract?", LOOKUP),
])
def test_the_rules_alone_decide_the_obvious(question, expected):
    assert route_question(question).kind == expected


def test_how_many_of_something_inside_a_document_is_read_not_counted():
    """"How many observations were raised in the final report" mentions a report; it
    does not ask how many reports there are."""
    route = route_question("How many observations were raised in the final report?")
    assert route.kind == LOOKUP


def test_show_me_how_is_a_question_wearing_a_command():
    assert route_question("show me how the deposit is calculated").kind == LOOKUP


@pytest.mark.parametrize("question", [
    "can you find jaymins passport number",
    "find me the passport number",
    "show me the invoice number on the Acme invoice",
    "find the landlord's email address",
    "get me the sort code for the deposit account",
    "find the expiry date on my passport",
    "look for the policy reference in the renewal letter",
])
def test_finding_a_value_written_in_a_document_is_read_not_listed(question):
    """2026-10-04, the owner's screen: "can you find jaymins passport number"
    was answered with 23 messages that contain those words, while the passage
    beginning "Passport number :" sat in the sources column unquoted. A
    sentence that names a value - a number, a date, an address - wants the
    value, whatever verb it opens with."""
    route = route_question(question)
    assert route.kind == LOOKUP, route.explain()
    assert "value" in route.reason


@pytest.mark.parametrize("question", [
    "find my passport", "find the photos of the kids", "show me the VAT returns",
    "find the tenancy agreement", "find emails from Dave",
])
def test_finding_a_document_is_still_a_find(question):
    assert route_question(question).kind == FIND, question


def test_where_is_the_thing_is_a_search_only_when_the_thing_is_a_document():
    assert route_question("Where is the valve schedule?").kind == FIND
    assert route_question("Where is the assembly point?").kind == LOOKUP


def test_the_decision_can_be_read_in_plain_words():
    text = route_question("How many PDFs do I have?").explain()
    assert "AGGREGATE" in text and "by rule" in text and "counted" in text


# --------------------------------------------------------------------------- the model, second

def test_a_model_is_asked_only_when_no_rule_fires():
    llm = FakeLLM(router_answer="FIND")
    route = route_question("kids on the beach", llm=llm)          # no wh-word, no verb: no rule
    assert route.kind == FIND and route.by in ("model", "default")
    assert len(llm.calls) <= 1
    llm = FakeLLM(router_answer="LOOKUP")
    route_question("How many PDFs do I have?", llm=llm)
    assert llm.calls == []                                        # the rule spoke; the model was never asked


def test_a_model_that_answers_nonsense_is_ignored():
    llm = FakeLLM(router_answer="I think this is probably about documents, honestly")
    route = route_question("the deposit thing from last spring and all that", llm=llm)
    assert route.by == "default" and route.kind in CLASSES


def test_a_model_cannot_route_to_a_class_that_does_not_exist():
    llm = FakeLLM(router_answer="DELETE")
    assert route_question("landlord boiler", llm=llm).kind in CLASSES


# --------------------------------------------------------------------------- follow-ups

def test_a_follow_up_needs_a_conversation_to_follow():
    assert route_question("And how long do they have to return it?").kind != FOLLOWUP


def test_a_follow_up_is_resolved_against_what_was_asked_before():
    history = _history("What did we agree with the landlord about the deposit?")
    route = route_question("And how long do they have to return it?", history)
    assert route.kind == FOLLOWUP and route.resolved_kind == LOOKUP
    assert "deposit" in route.question and "return" in route.question


def test_a_new_date_replaces_the_old_one_instead_of_fighting_it():
    history = _history("What did we agree about the deposit in 2018?")
    resolved = resolve_followup("And what about 2019?", history)
    assert "2019" in resolved and "2018" not in resolved


def test_a_pronoun_alone_is_a_follow_up_but_a_noun_phrase_is_not():
    history = _history("Who carried out the Leeds safety inspection?")
    assert route_question("When was that?", history).kind == FOLLOWUP
    assert route_question("What does that contract say about notice?", history).kind != FOLLOWUP


def test_a_planner_model_may_rewrite_a_follow_up_but_only_sensibly():
    history = _history("What is the car insurance excess?")
    good = FakeLLM(rewrite="What is the annual premium on the car insurance?")
    assert resolve_followup("And the premium?", history, llm=good).startswith("What is the annual")
    bad = FakeLLM(rewrite="Bananas are yellow")               # shares no word with the conversation
    assert "premium" in resolve_followup("And the premium?", history, llm=bad)
