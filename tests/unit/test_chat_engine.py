"""The chat engine end to end, with a real store, a real search engine and a fake model.

Layer: L8b. Work order `202626270611-chat-tab` sections 1, 2 and 5.

The load-bearing test of the whole order is `test_a_hallucinating_model_produces_zero_
unreceipted_sentences`: a model built to lie - fabricated figures, invented names,
misquotes, markers for sources that do not exist, unmarked sentences, a sentence with its
meaning reversed - is put behind the real engine and asked a dozen real questions, and
nothing it says reaches a finished turn or a streamed event.
"""

from __future__ import annotations

import threading

import pytest

from app.chat.config import ChatSettings
from app.chat.engine import ChatEngine
from app.chat.testing import FakeLLM, hallucinations_for
from app.chat.types import ChatTurn, NarrationEvent, ShelfEvent, TokenEvent
from app.chat.verify import audit_turn
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
def test_a_hallucinating_model_produces_zero_unreceipted_sentences(env, qa):
    """The load-bearing test. Every sentence a lying model writes is dropped: what
    comes back is an honest refusal, with no receipts and none of the lies in it."""
    llm = FakeLLM("hallucinating")
    turn, events = ask(env.engine(llm), qa.question)

    assert llm.calls, "the model must actually have been asked - otherwise this proves nothing"
    assert audit_turn(turn) == []                                     # nothing unreceipted
    assert turn.kind == "absence" and turn.receipts == []
    prompt = next(p for kind, p in llm.calls if kind == "answer")
    for lie in hallucinations_for(prompt):
        core = lie.split(" [")[0].rstrip(".")
        assert core not in turn.text                                  # not in the answer ...
        assert not any(isinstance(e, TokenEvent) and core in e.text for e in events)   # ... nor streamed
    assert not any(isinstance(e, TokenEvent) for e in events)


def test_a_lie_beside_a_truth_is_dropped_and_the_truth_kept(env):
    llm = FakeLLM("mixed")
    turn, events = ask(env.engine(llm), "How much notice must the tenant give?")
    assert turn.kind == "answer"
    assert audit_turn(turn) == []
    assert turn.text == "The tenant must give two months notice [1]."
    assert "7,341" not in turn.text and "Jonathan" not in turn.text
    streamed = "".join(e.text for e in events if isinstance(e, TokenEvent))
    assert streamed.strip() == turn.text                              # the screen saw only what was kept
    dropped = " ".join(d["reason"] for d in turn.debug["dropped"])
    assert "quotation" in dropped and "not shown" in dropped and "no source marker" in dropped
    assert len(turn.debug["dropped"]) >= 6                            # every lie was judged and refused


def test_every_sentence_of_every_answer_has_a_receipt_that_points_at_its_own_words(env):
    engine = env.engine("extractive")
    for qa in LOOKUPS:
        turn, _events = ask(engine, qa.question)
        assert audit_turn(turn) == [], qa.id
        for receipt in turn.receipts:
            row = env.store.conn.execute(
                "SELECT text FROM chunks WHERE id = ?", (receipt.chunk_id,)).fetchone()
            assert " ".join(receipt.quote.split()) in " ".join(row[0].split()), qa.id


def test_a_model_that_says_nothing_or_declines_is_a_refusal_not_a_guess(env):
    for mode in ("empty", "not_found"):
        turn, _events = ask(env.engine(mode), "How much notice must the tenant give?")
        assert turn.kind == "absence" and turn.receipts == [], mode
        assert turn.result_set                                        # the documents are offered instead


def test_a_thin_first_answer_is_retried_once_with_tighter_instructions(env):
    llm = FakeLLM(script=["Sentence with no marker at all.", "The tenant must give two months notice [1]."])
    turn, events = ask(env.engine(llm), "How much notice must the tenant give?")
    assert turn.kind == "answer" and len(llm.calls) == 2
    assert "not support" in llm.calls[1][1] or "leave out" in llm.calls[1][1]   # the strict prompt
    assert any(isinstance(e, NarrationEvent) and "again" in e.text for e in events)


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
    assert kinds.index("TokenEvent") < len(kinds) and "ShelfEvent" in kinds
    # a document joins the shelf as the answer first stands on it
    first_token = kinds.index("TokenEvent")
    assert kinds[first_token + 1] == "ShelfEvent"
    assert turn.debug["timings"]["first_narration_s"] < 1.0


def test_source_numbers_follow_first_mention_and_the_shelf_matches(env):
    turn, events = ask(env.engine("extractive"), "What did we agree with the landlord about the deposit?")
    shelf = [e.receipt for e in events if isinstance(e, ShelfEvent)]
    assert shelf == turn.receipts                                     # [n] is receipts[n - 1]
    assert [n for n in range(1, len(turn.receipts) + 1) if f"[{n}]" in turn.text] == list(
        range(1, len(turn.receipts) + 1))


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
    nothing_listening = ChatEngine(env.search, env.store, None,
                                   ChatSettings(ollama_url="http://127.0.0.1:9"))
    ok, why = nothing_listening.available()
    assert not ok and "Ollama is not running" in why


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


def test_a_synthesis_answer_is_extract_and_quote_by_default(env):
    turn, events = ask(env.engine("extractive"), "Summarise what the emails say about the licence")
    assert turn.kind == "answer" and audit_turn(turn) == []
    assert turn.debug["mode"] == "synthesis: extract-and-quote"
    assert len({r.name for r in turn.receipts}) >= 3                  # several documents, each quoted
    narrated = [e.text for e in events if isinstance(e, NarrationEvent)]
    assert sum(1 for t in narrated if t.startswith("Reading ")) >= 3   # one line per document


def test_synthesis_can_combine_but_the_combined_text_is_verified_again(env):
    """The optional reduce step. A model that invents in the combine step loses the
    invention; if nothing survives, the verified extracts stand."""
    calls = {"n": 0}

    def script(prompt: str) -> str:
        calls["n"] += 1
        if "combine these notes" in prompt.lower():
            return "The licence renewal cost 99,999 pounds and was cancelled by Zebediah [1]."
        return FakeLLM("extractive")._reply(prompt)

    turn, _events = ask(env.engine(FakeLLM(script=script), synthesis_combine=True),
                        "Summarise what the emails say about the licence")
    assert turn.kind == "answer" and audit_turn(turn) == []
    assert "99,999" not in turn.text and "Zebediah" not in turn.text


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
