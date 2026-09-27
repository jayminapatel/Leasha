"""Saying "nothing found" honestly: what was searched, scoped to the index, with next steps.

Layer: L8b. Work order `202626270611-chat-tab` sections 1e and 5 (*"absence: planted-
absence fixtures produce the protocol answer"*).
"""

from __future__ import annotations

import pytest

from app.chat.absence import NEXT_STEPS, WORLD_CLAIMS, absence_text, index_summary
from tests.fixtures import chat_eval as fx
from tests.unit.chat_env import Env, ask


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Env(tmp_path_factory.mktemp("absence"))
    yield e
    e.close()


PLANTED = [q for q in fx.QUESTIONS if q.planted]


@pytest.mark.parametrize("qa", PLANTED, ids=lambda q: q.id)
def test_a_planted_absence_gets_the_protocol_answer(env, qa):
    turn, _events = ask(env.engine("extractive"), qa.question)
    assert turn.kind == "absence", turn.text
    text = turn.text.lower()
    # 1. what was searched, visibly
    assert "i searched for" in text and turn.notes[0].startswith("Searched for:")
    # 2. the honest scope sentence, and what the index holds
    assert "nothing in leasha's index matches" in text
    assert "sources currently indexed: 47 files" in text
    # 3. the obvious next steps
    assert NEXT_STEPS.lower() in text
    assert turn.result_set is None and turn.receipts == []


@pytest.mark.parametrize("qa", PLANTED, ids=lambda q: q.id)
def test_it_never_claims_the_thing_does_not_exist_anywhere(env, qa):
    turn, _events = ask(env.engine("extractive"), qa.question)
    text = turn.text.lower()
    assert not any(claim in text for claim in WORLD_CLAIMS), turn.text
    assert "only means the index does not hold it" in text


def test_the_same_question_when_the_thing_is_there_is_not_an_absence(env):
    turn, _events = ask(env.engine("extractive"), "Do I have the MOT certificate?")
    assert turn.kind == "find" and turn.text.startswith("Yes")
    assert [r.name for r in turn.receipts] == ["mot-certificate.pdf"]


def test_what_a_wider_second_search_tried_is_shown(env):
    turn, _events = ask(env.engine("extractive"), "Did I ever get an email from HMRC?")
    assert turn.kind == "absence"
    searched = [n for n in turn.notes if n.startswith("Searched for:")]
    # `type:mail`, not `type:eml`: "email" names the whole mail group (msg,
    # eml, pst) since 2026-09-27 - `type:eml` missed every Outlook message.
    assert len(searched) == 2 and "type:mail" in searched[0]
    assert "names and dates left out" in turn.text


def test_the_index_summary_says_how_much_there_is_to_have_found(env):
    assert index_summary(env.store) == "47 files (12 of them emails)"


def test_an_empty_index_says_so_and_offers_to_start(tmp_path):
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(tmp_path / "empty.db") as store:
        text, notes = absence_text("the deposit letter", ["deposit letter"], store)
    assert "nothing yet" in text and "no folder has been indexed" in text
    assert "Indexing" in text


def test_an_answer_the_model_could_not_give_is_a_refusal_not_a_guess(env):
    """Retrieval found documents; the model found nothing to say that could be checked."""
    # A question about the person's own affairs ("my"): the searched-and-found-nothing account.
    # (A question that is not gets the one plain sentence and a labelled general answer -
    # see tests/unit/test_chat_conversation.py.)
    turn, _events = ask(env.engine("not_found"), "How much is my rent per month on the 2024 agreement?")
    assert turn.kind == "absence"
    assert "none of them states an answer i can point to" in turn.text.lower()
    assert turn.result_set                                   # the documents are offered instead
