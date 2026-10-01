"""The chat engine end to end, with a real store, a real search engine and a fake model.

Layer: L8b. Work order `202626270611-chat-tab` sections 1, 2 and 5, as changed on
2026-09-20 (the answer is written as prose, streamed, then checked - see the dated notes
in the order).

The load-bearing test of the whole order is `test_a_hallucinating_model_never_gets_a_lie_
into_the_finished_turn`: a model built to lie - fabricated figures, invented names,
misquotes, markers for sources that do not exist, a sentence with its meaning reversed -
is put behind the real engine and asked a dozen real questions, and nothing it says
reaches a **finished turn**. (What is *streamed* while it writes is raw by design: the
finished turn replaces it - `test_the_raw_stream_is_replaced_by_the_checked_turn`.)
"""

from __future__ import annotations

import threading

import pytest

from app.chat.config import ChatSettings
from app.chat.engine import ChatEngine
from app.chat.reconcile import audit_answer
from app.chat.testing import FakeLLM, hallucinations_for
from app.chat.verify import audit_turn
from app.chat.types import ChatTurn, NarrationEvent, ShelfEvent, SourcesEvent, TokenEvent
from tests.fixtures import chat_eval as fx
from tests.unit.chat_env import Env, ask, converse


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Env(tmp_path_factory.mktemp("engine"))
    yield e
    e.close()


LOOKUPS = [q for q in fx.QUESTIONS if q.cls == "LOOKUP" and q.outcome == "answer"]


# =========================================================================== the guarantee

@pytest.mark.parametrize("qa", LOOKUPS[:14], ids=lambda q: q.id)
def test_a_hallucinating_model_never_gets_a_lie_into_the_finished_turn(env, qa):
    """The load-bearing test. Every claim a lying model makes about the files is dropped:
    what comes back is the honest "nothing found", with no receipts and none of the lies."""
    llm = FakeLLM("hallucinating")
    turn, _events = ask(env.engine(llm), qa.question)

    assert llm.calls, "the model must actually have been asked - otherwise this proves nothing"
    assert audit_answer(turn) == []
    assert turn.kind in ("absence", "general") and turn.receipts == []
    prompt = next(p for kind, p in llm.calls if kind == "answer")
    for lie in hallucinations_for(prompt):
        core = lie.split(" [")[0].rstrip(".")
        assert core not in turn.text


def test_the_raw_stream_is_replaced_by_the_checked_turn(env):
    """The guarantee moved from "no unverified word is ever on screen" to "no unverified
    claim about the files is in the answer that is kept": tokens stream as written, and the
    tab replaces them with the finished turn's text."""
    llm = FakeLLM("hallucinating")
    turn, events = ask(env.engine(llm), "What did we agree with the landlord about the deposit?")
    streamed = "".join(e.text for e in events if isinstance(e, TokenEvent))
    assert "7,341" in streamed                                        # it did stream, raw ...
    assert "7,341" not in turn.text and turn.kind == "absence"        # ... and the kept turn has none of it


def test_a_lie_beside_a_truth_is_dropped_and_the_truth_kept(env):
    llm = FakeLLM("mixed")
    turn, events = ask(env.engine(llm), "How much notice must the tenant give?")
    assert turn.kind == "answer"
    assert audit_answer(turn) == []
    assert "The tenant must give two months notice [1]." in turn.text
    assert "7,341" not in turn.text and "Jonathan" not in turn.text and "non-refundable" not in turn.text
    assert "I could not confirm the rest of that from your files." in turn.text     # said plainly, once
    dropped = " ".join(d["reason"] for d in turn.debug["dropped"])
    assert "quotation" in dropped and "not shown" in dropped
    assert len(turn.debug["dropped"]) >= 5                            # every lie was judged and refused


def test_every_sentence_of_every_answer_has_a_receipt_that_points_at_its_own_words(env):
    engine = env.engine("extractive")
    for qa in LOOKUPS:
        turn, _events = ask(engine, qa.question)
        assert audit_answer(turn) == [], qa.id
        for receipt in turn.receipts:
            row = env.store.conn.execute(
                "SELECT text FROM chunks WHERE id = ?", (receipt.chunk_id,)).fetchone()
            assert " ".join(receipt.quote.split()) in " ".join(row[0].split()), qa.id


def test_a_model_that_says_nothing_or_declines_is_the_honest_nothing_found_not_a_guess(env):
    for mode in ("empty", "not_found"):
        turn, _events = ask(env.engine(mode), "How much notice must the tenant give?")
        assert turn.kind in ("absence", "general") and turn.receipts == [], mode
        assert "couldn't find" in turn.text or turn.result_set, mode


def test_a_model_that_only_says_unsupported_things_gets_the_plain_nothing_found(env):
    llm = FakeLLM(chat_replies=["Sentence with no marker at all.", "The tenant must give two months notice [1]."])
    turn, _events = ask(env.engine(llm), "How much notice must the tenant give?")
    assert turn.kind in ("absence", "general") and turn.receipts == []      # a marker-less remark is no answer


# =========================================================================== the events

def test_the_first_narration_comes_before_any_model_is_consulted(env):
    llm = FakeLLM()
    seen_calls: list[int] = []

    def emit(event):
        if isinstance(event, NarrationEvent) and not seen_calls:
            seen_calls.append(len(llm.calls))                         # calls made when narration #1 arrived

    env.engine(llm).ask("How much notice must the tenant give?", [], emit, lambda: False)
    assert seen_calls == [0]


def test_events_arrive_in_a_sensible_order(env):
    turn, events = ask(env.engine("extractive"), "What did we agree with the landlord about the deposit?")
    kinds = [type(e).__name__ for e in events]
    assert kinds[0] == "NarrationEvent"
    # the numbered passages are handed over before a word is written, so [n] is a live link
    assert kinds.index("SourcesEvent") < kinds.index("TokenEvent")
    assert "ShelfEvent" not in kinds                                  # the shelf follows the finished answer
    assert turn.debug["timings"]["first_narration_s"] < 1.0
    assert turn.debug["timings"]["first_token_s"] >= turn.debug["timings"]["first_narration_s"]


def test_source_numbers_follow_first_mention_and_the_receipts_match(env):
    turn, events = ask(env.engine("extractive"), "What did we agree with the landlord about the deposit?")
    offered = [e for e in events if isinstance(e, SourcesEvent)][0].receipts
    assert {r.path for r in turn.receipts} <= {r.path for r in offered}
    assert [n for n in range(1, len(turn.receipts) + 1) if f"[{n}]" in turn.text] == list(
        range(1, len(turn.receipts) + 1))                             # [n] is receipts[n - 1]


# =========================================================================== the contract

def test_should_stop_ends_a_streamed_answer_promptly(env):
    calls = {"n": 0}

    def stop_after_a_moment() -> bool:
        calls["n"] += 1
        return calls["n"] > 12

    llm = FakeLLM("extractive", chunk=2)                              # tiny pieces: many stop checks
    turn, _events = ask(env.engine(llm), "What did we agree with the landlord about the deposit?",
                        should_stop=stop_after_a_moment)
    assert turn.kind in ("answer", "error") and (turn.kind == "error" or "Stopped" in " ".join(turn.notes))
    assert calls["n"] < 200                                           # it stopped, it did not run on


def test_stopping_before_anything_is_ready_returns_an_error_turn_not_an_exception(env):
    turn, _events = ask(env.engine("extractive"), "What did we agree with the landlord?",
                        should_stop=lambda: True)
    assert turn.kind == "error" and turn.text == "Stopped."


def test_ask_never_raises_when_the_model_dies_mid_answer(env):
    llm = FakeLLM("extractive", chunk=3, fail_after=4)
    turn, _events = ask(env.engine(llm), "What did we agree with the landlord about the deposit?")
    assert isinstance(turn, ChatTurn) and turn.kind in ("error", "answer", "absence")
    if turn.kind == "error":
        assert "Ollama" in turn.text and "ollama serve" in turn.text or "Search still works" in turn.text


def test_ask_never_raises_when_search_itself_fails(env):
    class BrokenSearch:
        def search(self, *args, **kwargs):
            raise RuntimeError("the search index is on fire")

    engine = ChatEngine(BrokenSearch(), env.store, FakeLLM(), None)
    turn, _events = ask(engine, "What did we agree with the landlord about the deposit?")
    assert turn.kind == "error"
    assert "went wrong" in turn.text and "Search is unaffected" in turn.text
    assert "on fire" in turn.debug["error_detail"]                    # the detail is for the debug pane


def test_a_broken_store_degrades_the_helpers_but_never_the_engine(env):
    class BrokenStore:
        def __getattr__(self, name):
            raise RuntimeError("the database is on fire")

    engine = ChatEngine(env.search, BrokenStore(), FakeLLM(), None)
    turn, _events = ask(engine, "How many PDFs do I have?")
    assert isinstance(turn, ChatTurn) and turn.kind == "error"


def test_a_broken_listener_cannot_kill_an_answer(env):
    def emit(_event):
        raise RuntimeError("the window closed")

    turn = env.engine("extractive").ask("How much notice must the tenant give?", [], emit, lambda: False)
    assert turn.kind == "answer"


def test_an_empty_question_is_asked_about_not_searched_for(env):
    turn, events = ask(env.engine("extractive"), "   ")
    assert turn.kind == "clarify" and events == []


def test_the_route_and_the_working_are_in_the_debug_pane(env):
    turn, _events = ask(env.engine("extractive"), "How many observations were raised against guarding in the final Leeds safety report?")
    debug = turn.debug
    assert debug["route"]["kind"] == "LOOKUP" and "by rule" in debug["route_explained"]
    assert debug["queries"] and debug["rounds"] >= 1
    assert debug["sources"] and debug["models"]["answerer"] == "fake-1b"
    assert "total_s" in debug["timings"]


# =========================================================================== Ollama absent

def test_counting_finding_and_absence_need_no_model_at_all(env):
    engine = env.engine(FakeLLM(up=False))
    for question, kind in (("How many PDFs do I have?", "aggregate"),
                           ("Find the tenancy agreement", "find"),
                           ("Do I have my passport scan?", "absence")):
        turn, _events = ask(engine, question)
        assert turn.kind == kind, question


def test_without_a_model_a_content_question_quotes_the_best_passages_and_says_so(env):
    engine = env.engine(FakeLLM(up=False))
    turn, _events = ask(engine, "How much notice must the tenant give?")
    assert turn.kind == "answer" and audit_turn(turn) == []
    assert "No AI model is running" in turn.notes[0]
    assert "two months notice" in turn.text


def test_available_says_what_is_wrong_and_how_to_fix_it(env):
    assert env.engine(FakeLLM()).available() == (True, "")
    ok, why = env.engine(FakeLLM(up=False)).available()
    assert not ok and "Ollama is not running" in why and "ollama serve" in why
    ok, why = env.engine(FakeLLM(installed=False, model="qwen2.5:1.5b")).available()
    assert not ok and "not installed" in why and "ollama pull qwen2.5:1.5b" in why
    # Ollama's own sentences, when Ollama is the engine (CHAT_ENGINE, 2026-09-29).
    nothing_listening = ChatEngine(env.search, env.store, None,
                                   ChatSettings(engine="ollama", ollama_url="http://127.0.0.1:9"))
    ok, why = nothing_listening.available()
    assert not ok and "Ollama is not running" in why


def test_available_with_the_model_inside_leasha_says_download_not_ollama(env, tmp_path):
    """The default engine: a missing model is a Download, and Ollama is never named."""
    engine = ChatEngine(env.search, env.store, None,
                        ChatSettings(engine="onnx", model_cache=str(tmp_path)))
    ok, why = engine.available()
    assert not ok and "not downloaded yet" in why and "Download" in why
    assert "Ollama" not in why and "ollama" not in why


def test_the_search_engine_never_touches_the_model(env):
    """Chat is opt-in and separate: search works with the model stopped, and nothing in
    `app/search` imports `app.chat` or `app.llm` (besides the translator)."""
    llm = FakeLLM()
    env.engine(llm)                                                    # built, never asked
    assert env.search.search("deposit", limit=5).results
    assert llm.calls == []


# =========================================================================== the loop

def test_a_thin_first_search_is_widened_visibly_and_bounded(env):
    turn, events = ask(env.engine("extractive", max_rounds=3), "Did I ever get an email from HMRC?")
    assert turn.debug["rounds"] <= 3
    assert any(isinstance(e, NarrationEvent) and e.text.startswith("That was thin") for e in events)


def test_the_number_of_rounds_can_never_exceed_three(env):
    engine = env.engine("extractive", max_rounds=99)
    turn, _events = ask(engine, "Did I ever get an email from HMRC?")
    assert turn.debug["rounds"] <= 3


def test_one_round_means_no_retry(env):
    turn, _events = ask(env.engine("extractive", max_rounds=1), "Did I ever get an email from HMRC?")
    assert turn.debug["rounds"] == 1 and len(turn.debug["queries"]) == 1


def test_a_planner_model_can_find_what_the_first_words_missed(env):
    """The deposit is in the corpus as "tenancy deposit"; asking about a "bond" finds
    nothing until the planner proposes the synonym - and the answer is still verified."""
    llm = FakeLLM("extractive", planner_queries=["tenancy deposit returned"])
    turn, _events = ask(env.engine(llm), "How long until the bond comes back?")
    assert "tenancy deposit returned" in turn.debug["queries"]
    assert audit_turn(turn) == []


def test_a_planner_that_returns_rubbish_is_ignored(env):
    llm = FakeLLM("extractive", script=lambda prompt: "not json at all" if "search a personal archive" in prompt else "")
    turn, _events = ask(env.engine(llm), "Do I have my passport scan?")
    assert turn.kind == "absence"


# =========================================================================== follow-ups and the shelf

def test_a_follow_up_is_answered_from_the_documents_already_touched(env):
    turn, history = converse(env.engine("extractive"),
                             "What is the car insurance excess?", "And the annual premium?")
    assert turn.debug["route"]["kind"] == "FOLLOWUP"
    assert turn.debug.get("scope") == "shelf" and turn.debug["queries"] == []
    assert "612 pounds" in turn.text and turn.receipts[0].name == "insurance-renewal.pdf"


def test_a_follow_up_the_shelf_cannot_answer_searches_the_corpus(env):
    turn, _history = converse(env.engine("extractive"),
                              "How much was Chris's quote for the licence renewal?", "Who approved it?")
    assert turn.debug.get("scope") != "shelf" and turn.debug["queries"]


def test_an_excluded_document_is_never_used_and_a_pinned_one_is_always_in_scope(env):
    engine = env.engine("extractive")
    ids = env.ids
    engine.set_shelf(excluded=[ids["C:/Archive/Tenancy/tenancy-agreement-2024.pdf"]])
    turn, _events = ask(engine, "How much notice must the tenant give?")
    assert all(r.name != "tenancy-agreement-2024.pdf" for r in turn.receipts)
    engine.set_shelf()


def test_a_synthesis_answer_is_one_flowing_account_that_is_still_checked(env):
    turn, _events = ask(env.engine("extractive"), "Summarise what the emails say about the licence")
    assert turn.kind == "answer" and audit_answer(turn) == []
    assert turn.debug["mode"].startswith("answer (conversational")
    assert env.engine("extractive").cfg.synthesis_combine is True     # on by default (owner, 2026-09-20)


def test_synthesis_one_paragraph_per_document_is_a_setting_not_a_second_model_call(env):
    llm = FakeLLM("extractive")
    turn, _events = ask(env.engine(llm, synthesis_combine=False), "Summarise what the emails say about the licence")
    assert turn.kind == "answer"
    system = llm.chat_calls[0][1][0]["content"]
    assert "one at a time" in system                                  # asked for in the prompt
    assert [k for k, _p in llm.calls].count("answer") == 1            # one call, not one per document


# =========================================================================== roles

def test_roles_default_to_one_model_and_split_only_on_request(env):
    engine = env.engine(FakeLLM(model="fake-1b"))
    roles = engine.roles()
    assert roles.router == roles.planner == roles.answerer == "fake-1b"
    split = env.engine({"router": FakeLLM(model="tiny"), "planner": FakeLLM(model="tiny"),
                        "answerer": FakeLLM(model="big")})
    roles = split.roles()
    assert (roles.router, roles.planner, roles.answerer) == ("tiny", "tiny", "big")


def test_the_router_model_is_asked_only_when_the_rules_cannot_tell(env):
    router = FakeLLM(model="tiny", router_answer="FIND")
    engine = env.engine({"router": router, "planner": FakeLLM(model="tiny"),
                         "answerer": FakeLLM(model="big")})
    ask(engine, "How many PDFs do I have?")                            # rules decide: model untouched
    assert router.calls == []
    ask(engine, "kids at the beach in 2015")                           # no rule fires: model asked once
    assert [k for k, _p in router.calls] == ["router"]


def test_set_model_writes_through_to_the_answering_role(env):
    llm = FakeLLM(model="fast-1b")
    engine = env.engine(llm)
    engine.set_model("answerer", "thoughtful-7b")
    assert llm.model == "thoughtful-7b"
    with pytest.raises(ValueError):
        engine.set_model("chef", "x")


# =========================================================================== concurrency

def test_two_questions_at_once_do_not_share_state(env):
    """The tab may run a question while the previous stream is finishing."""
    engine = env.engine("extractive")
    results: dict[str, ChatTurn] = {}

    def run(name: str, question: str) -> None:
        results[name] = engine.ask(question, [], lambda _e: None, lambda: False)

    threads = [threading.Thread(target=run, args=("a", "How much notice must the tenant give?")),
               threading.Thread(target=run, args=("b", "How many PDFs do I have?"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert results["a"].kind == "answer" and results["b"].kind == "aggregate"
    assert "two months" in results["a"].text


# =========================================================================== their own mail
#
# 1 October 2026. "mail about holiday from maya" found the right email, the model
# said NOT FOUND, and the general fill answered "Maya has sent you a holiday email
# in May" under *Not from your files* - a claim about their own mail that nothing
# supports. The general fill had only refused questions containing "my" or "me".


def test_asking_for_mail_about_something_lists_the_mail(env):
    turn, _events = ask(env.engine(FakeLLM("not_found")), "mail about the licence renewal from chris")
    assert turn.kind == "find"
    assert turn.result_set
    assert "Not from your files" not in turn.text


def test_a_question_about_their_mail_never_gets_a_general_answer(env):
    turn, _events = ask(env.engine(FakeLLM("not_found")),
                        "What did the email from Chris say about the moon?")
    assert turn.kind != "general"
    assert "Not from your files" not in turn.text


def test_when_documents_mention_it_they_are_shown_rather_than_a_general_answer(env):
    turn, _events = ask(env.engine(FakeLLM("not_found")), "What colour is the licence renewal?")
    assert turn.kind == "absence"
    assert turn.result_set
    assert "Not from your files" not in turn.text
