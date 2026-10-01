"""`evaluate --chat`: the fixture, the harness, and the floors pinned as regression tests.

Layer: L8b. Work order `202626270611-chat-tab` sections 4a-4c.

**What the pinned floors are, and are not.** The run below uses the deterministic
`FakeLLM`, so these numbers describe the router, the retrieval loop, the verification, the
counting and the fixture - not how any language model writes. They are pinned with margin
so a change that makes any of those worse turns the suite red. What a *real* model scored
is recorded in the work order (section 4b) and is not asserted here, because a test that
needs a model is a test that cannot run on the machines the suite runs on; set
`LEASHA_CHAT_REAL_MODEL=qwen2.5:1.5b` to run the real-model check below.

The ship floors of the order itself (citation validity >= 98%, aggregate exactness 100%,
absence honesty 100%, extractive >= 85%) are `app.chat.evaluate.FLOORS`, and the deterministic
run must clear all four.

**Re-decided 2026-09-20** (the conversational Chat): 96 questions now (13 conversational, 3
general-but-not-in-the-files), citation validity counts the sentences that *claim something
about the files* (a sentence with a marker), and "nothing found" has two honest shapes. The
pinned numbers below are the fake's fresh measurement; the real-model measurements are in the
work order.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.chat.evaluate import (
    FLOORS, ChatReport, ConversationReport, run_chat_eval, run_conversations, score_case,
)
from app.chat.router import CLASSES
from app.chat.testing import FakeLLM
from app.chat.types import ChatTurn
from tests.fixtures import chat_eval as fx
from tests.unit.chat_env import Env

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Env(tmp_path_factory.mktemp("evaluate"))
    yield e
    e.close()


@pytest.fixture(scope="module")
def report(env) -> ChatReport:
    return run_chat_eval(env.engine("extractive"), fx.QUESTIONS, env.store, model="FakeLLM")


# --------------------------------------------------------------------------- the fixture

def test_the_fixture_has_at_least_sixty_pairs_across_every_router_class():
    assert len(fx.QUESTIONS) >= 60
    assert {q.cls for q in fx.QUESTIONS} == set(CLASSES)
    by_class = {c: sum(1 for q in fx.QUESTIONS if q.cls == c) for c in CLASSES}
    assert all(n >= 5 for n in by_class.values()), by_class


def test_it_has_planted_absences_and_trap_questions():
    assert sum(1 for q in fx.QUESTIONS if q.planted) >= 8
    assert sum(1 for q in fx.QUESTIONS if q.trap) >= 5
    assert any(q.forbid for q in fx.QUESTIONS)                       # a tempting wrong answer, named


def test_every_question_points_at_things_that_exist():
    paths = [d.path for d in fx.CORPUS]
    ids = [q.id for q in fx.QUESTIONS]
    assert len(ids) == len(set(ids))
    for q in fx.QUESTIONS:
        for fragment in (*q.find, *([q.cite] if q.cite else ())):
            assert any(fragment in p for p in paths), (q.id, fragment)
        if q.outcome == "aggregate":
            assert q.count is not None and q.count >= 0
        if q.planted:
            words = q.question.lower()
            assert not any(w in " ".join(d.text.lower() for d in fx.CORPUS)
                           for w in ("passport", "hmrc", "whitfield", "mortgage", "loft",
                                     "birth", "alps", "plumbing") if w in words), q.id


def test_the_fixture_is_dated_so_it_never_drifts():
    assert fx.TODAY.year == 2026
    assert all(d.when[:2] == "20" for d in fx.CORPUS)


# --------------------------------------------------------------------------- the floors

def test_the_deterministic_run_clears_the_orders_ship_floors(report):
    assert report.below_floor() == []
    assert set(FLOORS) == {"citation_validity", "aggregate_exactness", "absence_honesty", "extractive"}


#: Recorded at the measurement of 2026-09-20 (FakeLLM, 96 questions) and pinned with margin.
#: A value lower than its floor here is a regression in the router, the loop, the
#: verification, the counting or the fixture - fix that, do not lower the floor.
PINNED = {
    "citation_validity": 0.98,
    "extractive": 0.90,            # measured 96.8%
    "aggregate_exactness": 1.0,    # measured 100% - they are queries
    "absence_honesty": 1.0,
    "absence_recall": 1.0,         # measured 100% of planted absences
    "router": 0.97,                # measured 100%
    "find": 0.95,                  # measured 100%
    "refusal": 0.60,               # measured 66.7%: one of three traps fools a lexical stand-in
    "traps": 0.50,                 # measured 60%
    "synthesis": 0.50,             # measured 60% (was 80% when synthesis was one call per document:
                                   # the fake now answers in one call); no floor at v1 (work order 4b)
    "conversation": 1.0,           # measured 100%: LLM-free structure, the router's rules decide it
    "general_fill": 1.0,           # measured 100%: the sentence and the label are the engine's own
}


@pytest.mark.parametrize("measure, floor", sorted(PINNED.items()))
def test_each_measure_holds_its_pinned_floor(report, measure, floor):
    value = report.measures()[measure]
    assert value is not None and value >= floor - 1e-9, (measure, value, floor)


def test_no_rendered_sentence_is_without_a_receipt(report):
    assert report.unreceipted_sentences == 0
    # ...and there were claims to check: a sentence with a marker is a claim about the files
    # (the count was 60+ when every sentence had to carry one; unmarked prose is judged by
    # `audit_answer` instead, which the zero above is).
    assert sum(r.sentences for r in report.results) >= 45


def test_every_first_narration_arrives_within_a_second(report):
    lat = report.latency("first_narration_s")
    assert lat["n"] == len(fx.QUESTIONS) and lat["max"] < 1.0        # work order 4c


def test_the_report_states_what_it_measured_and_never_hides_a_stand_in(report):
    text = "\n".join(report.lines())
    assert "NOT a language model" in text and "citation validity" in text
    assert report.as_dict()["real_model"] is False and report.as_dict()["questions"] == len(fx.QUESTIONS)
    real = ChatReport(model="qwen2.5:1.5b", real_model=True, machine="m")
    assert "a real local model" in "\n".join(real.lines())


# --------------------------------------------------------------------------- the harness can fail

def test_a_lying_model_scores_badly_on_answers_and_still_perfectly_on_receipts(env):
    """The other half of the guarantee: a lying model loses recall, never honesty."""
    lying = run_chat_eval(env.engine("hallucinating"), fx.QUESTIONS, env.store, model="liar")
    assert lying.unreceipted_sentences == 0
    assert lying.absence_honesty == 1.0 and lying.aggregate_exactness == 1.0
    # It does not clear the floor. **"< 0.5" went on 1 October 2026:** when a
    # model's answer fails the checks the passages are now quoted instead, so
    # even a liar recovers recall (51.6% on this corpus) - honestly, by quoting.
    assert lying.below_floor()


def test_a_turn_without_receipts_is_scored_as_invalid(env):
    qa = next(q for q in fx.QUESTIONS if q.id == "L06")
    bad = ChatTurn("assistant", "The tenant must give two months notice [1].", kind="answer")
    scored = score_case(qa, bad, env.store)
    assert scored.sentences == 1 and scored.valid_sentences == 0 and scored.unreceipted == 1
    assert not scored.correct


def test_a_number_that_differs_from_the_truth_fails_aggregate_exactness(env):
    qa = next(q for q in fx.QUESTIONS if q.id == "A04")
    wrong = ChatTurn("assistant", "9 PDFs - counted across everything Leasha has indexed, offline "
                     "drives included.", kind="aggregate")
    assert not score_case(qa, wrong, env.store).correct
    right = ChatTurn("assistant", f"{qa.count} PDFs - counted across everything Leasha has "
                     "indexed, offline drives included.", kind="aggregate")
    assert score_case(qa, right, env.store).correct


def test_an_absence_that_claims_the_thing_does_not_exist_is_dishonest(env):
    qa = next(q for q in fx.QUESTIONS if q.id == "B01")
    boastful = ChatTurn("assistant", "That does not exist. Nothing in Leasha's index matches; I searched.",
                        kind="absence", notes=["Searched for: passport"])
    assert score_case(qa, boastful, env.store).honest is False
    unscoped = ChatTurn("assistant", "I found nothing.", kind="absence")
    assert score_case(qa, unscoped, env.store).honest is False


# --------------------------------------------------------------------------- conversations

@pytest.fixture(scope="module")
def talk(env) -> ConversationReport:
    return run_conversations(env.engine("extractive"), fx.CONVERSATIONS, model="FakeLLM")


def test_the_scripted_conversations_cover_everything_the_owner_listed():
    steps = [s for _title, ss in fx.CONVERSATIONS for s in ss]
    kinds = {(s.route, s.kind) for s in steps}
    assert ("CHAT", "chat") in kinds                                     # a greeting, "shorter"
    assert ("LOOKUP", "general") in kinds                                # a general question, files first
    assert ("LOOKUP", "answer") in kinds and ("FOLLOWUP", "answer") in kinds   # archive, then a follow-up on it
    assert any(s.regenerate for s in steps) and any(s.say == "shorter" for s in steps)
    assert any(s.memory and s.kind == "chat" for s in steps)             # a follow-up on the previous answer


def test_every_step_of_every_scripted_conversation_has_the_right_shape(talk):
    assert [s.problems for s in talk.steps if not s.ok] == []
    assert talk.structure == 1.0 and len(talk.steps) == 10
    assert all(s.memory_kept >= 2 for s in talk.steps[1:3])              # the talk reached the model


def test_a_regenerated_step_is_asked_again_with_a_fresh_variant(talk):
    last = [s for s in talk.steps if s.regenerated]
    assert len(last) == 1 and last[0].say == "shorter" and last[0].ok


def test_the_conversation_harness_can_fail(env):
    class Refuses:
        """An engine that answers every greeting with a search refusal."""

        def ask(self, question, history, emit, should_stop, **kwargs):
            return ChatTurn("assistant", "I couldn't find that in your files.", kind="absence",
                            debug={"route": {"kind": "LOOKUP"}, "memory": {"kept": 0}})

    bad = run_conversations(Refuses(), fx.CONVERSATIONS[:1], model="broken")
    assert bad.structure < 0.5
    first = bad.steps[0]
    assert not first.ok and any("routed LOOKUP" in p for p in first.problems)


def test_the_transcript_is_kept_whole_for_a_person_to_read(talk):
    text = "\n".join(talk.lines())
    assert "You:  hi" in text and "Leasha [CHAT/chat" in text and "(regenerated)" in text
    assert talk.as_dict()["conversations"][0]["steps"][0]["reply"]


def test_a_chat_turn_that_searched_or_refused_scores_as_wrong(env):
    qa = next(q for q in fx.QUESTIONS if q.id == "C01")
    refusal = ChatTurn("assistant", "I couldn't find that in your files.", kind="chat",
                       debug={"route": {"kind": "CHAT"}})
    assert not score_case(qa, refusal, env.store).correct
    searched = ChatTurn("assistant", "Hello!", kind="chat",
                        debug={"route": {"kind": "CHAT"}, "queries": ["hello"]})
    assert not score_case(qa, searched, env.store).correct
    fine = ChatTurn("assistant", "Hello! What can I do for you?", kind="chat", debug={"route": {"kind": "CHAT"}})
    assert score_case(qa, fine, env.store).correct


def test_the_two_honest_shapes_of_nothing_found_both_score(env):
    qa = next(q for q in fx.QUESTIONS if q.id == "B01")
    general = ChatTurn("assistant", "I couldn't find that in your files.\n\n**Not from your files:** It is a document.",
                       kind="general", debug={"route": {"kind": "ABSENCE"}})
    assert score_case(qa, general, env.store).honest is True
    unlabelled = ChatTurn("assistant", "I couldn't find that in your files. It is definitely in the drawer.",
                          kind="general")
    assert score_case(qa, unlabelled, env.store).honest is False


# --------------------------------------------------------------------------- nothing waits forever

def test_a_turn_that_never_comes_back_is_a_failed_turn_not_a_hung_run(env):
    """Every turn of an evaluation has a wall-clock limit: past it the turn is asked to stop, and
    if it still does not come back it is reported as failed and the run goes on."""
    import threading

    from app.chat.evaluate import ask_with_limit

    release = threading.Event()

    class Stuck:
        def ask(self, question, history, emit, should_stop, **kw):
            release.wait(timeout=30)                   # ignores should_stop, like a model still loading
            return ChatTurn("assistant", "too late")

    started = __import__("time").monotonic()
    turn = ask_with_limit(Stuck(), "hello?", [], lambda _e: None, limit_s=0.2, grace_s=0.2)
    assert __import__("time").monotonic() - started < 5
    assert turn.kind == "error" and "did not finish within 0.2 seconds" in turn.text and turn.debug["stalled"]
    release.set()

    class Breaks:
        def ask(self, *a, **k):
            raise RuntimeError("the model exploded")

    broken = ask_with_limit(Breaks(), "hello?", [], lambda _e: None)
    assert broken.kind == "error" and "RuntimeError: the model exploded" in broken.text


def test_a_stalled_step_is_counted_and_the_conversations_carry_on(env):
    class Stalls:
        def __init__(self):
            self.n = 0

        def ask(self, question, history, emit, should_stop, **kw):
            self.n += 1
            if self.n == 1:
                while not should_stop():
                    __import__("time").sleep(0.01)
                return ChatTurn("assistant", "stopped", kind="error")
            return env.engine("extractive").ask(question, history, emit, should_stop, **kw)

    report = run_conversations(Stalls(), fx.CONVERSATIONS[:1], model="stalls", limit_s=0.2)
    assert len(report.steps) == 7 and not report.steps[0].ok and report.structure < 1.0
    assert report.steps[1].reply                                            # it went on to the next step


def test_a_reply_that_trickles_forever_is_ended_by_the_wall_clock_with_what_had_arrived(env, monkeypatch):
    from app.chat import engine as engine_module
    from app.chat.testing import FakeLLM

    monkeypatch.setattr(engine_module, "REPLY_DEADLINE_S", 0.05)
    llm = FakeLLM(chat_replies=lambda messages: "word " * 200_000, chunk=1)
    turn, _events = __import__("tests.unit.chat_env", fromlist=["ask"]).ask(env.engine(llm), "hi")
    assert turn.kind == "chat" and turn.partial and turn.text.startswith("word")
    assert "did not finish" in turn.notes[-1]


# --------------------------------------------------------------------------- the command line

def test_evaluate_chat_runs_from_the_command_line_without_a_model():
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "evaluate", "--chat", "--chat-fake",
         "--chat-ids", "A01,B01,L06"],
        cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-800:]
    assert "Chat evaluation  (3 questions)" in result.stdout
    assert "NOT a language model" in result.stdout and "citation validity" in result.stdout


def test_conversations_run_from_the_command_line_with_a_transcript():
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "evaluate", "--chat", "--chat-fake", "--chat-conversation-only"],
        cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-800:]
    assert "Conversation evaluation  (10 steps)" in result.stdout and "You:  hi" in result.stdout
    assert "Chat evaluation" not in result.stdout


def test_the_flag_and_its_helpers_are_on_the_evaluate_subcommand():
    import argparse

    from app.cli.evaluate import add_evaluate_parser

    parser = argparse.ArgumentParser()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true")
    add_evaluate_parser(parser.add_subparsers(), common)
    args = parser.parse_args(["evaluate", "--chat", "--chat-model", "qwen2.5:1.5b",
                              "--chat-ids", "L01,A01", "--chat-fake", "--chat-conversation",
                              "--chat-runs", "3"])
    assert args.chat and args.chat_model == "qwen2.5:1.5b" and args.chat_fake
    assert args.chat_ids == "L01,A01" and args.chat_conversation and args.chat_runs == 3


class _Inside:
    """A stand-in for the chat model inside Leasha: downloaded or not, never loaded."""

    engine = "onnx"
    model = "qwen2.5-1.5b-instruct"

    def __init__(self, downloaded: bool) -> None:
        self.downloaded = downloaded
        self.warmed = 0

    def has_model(self) -> bool:
        return self.downloaded

    def serving(self) -> str:
        return "Qwen 2.5 1.5B Instruct, 4-bit (chat, Interpret)" if self.downloaded else ""

    def warm(self, **_kwargs) -> bool:
        self.warmed += 1
        return True


def test_with_no_model_named_the_engine_in_the_settings_is_the_one_measured(monkeypatch):
    """2026-09-30: `CHAT_ENGINE` is `onnx` by default and this command measured Ollama
    whatever it said. With no `--chat-model`, the model inside Leasha is what answers,
    and the heading names the copy on disk."""
    from types import SimpleNamespace

    from app.chat.evaluate import _pick_models
    from app.llm import engines

    inside = _Inside(downloaded=True)
    monkeypatch.setattr(engines, "text_model", lambda settings, **_k: inside)
    llm, label, real, note = _pick_models(SimpleNamespace(chat_engine="onnx"), "", False)
    assert llm is inside and real and inside.warmed == 1
    assert "4-bit" in label and "inside Leasha" in label and note == ""


def test_a_named_model_is_still_an_ollama_model_and_the_inside_one_is_not_asked(monkeypatch):
    from types import SimpleNamespace

    from app.chat import llm as chat_llm
    from app.chat.evaluate import _pick_models
    from app.llm import engines

    def never(*_a, **_k):
        raise AssertionError("--chat-model names an Ollama model; the inside one is not used")

    monkeypatch.setattr(engines, "text_model", never)
    monkeypatch.setattr(chat_llm.OllamaLLM, "health", lambda self, force=False: False)
    llm, label, real, note = _pick_models(SimpleNamespace(chat_engine="onnx"), "mistral", False)
    assert not real and "FakeLLM" in label and "Nothing answered" in note


def test_an_inside_model_that_is_not_downloaded_is_said_and_ollama_is_tried(monkeypatch):
    from types import SimpleNamespace

    from app.chat import llm as chat_llm
    from app.chat.evaluate import _pick_models
    from app.llm import engines

    monkeypatch.setattr(engines, "text_model", lambda settings, **_k: _Inside(downloaded=False))
    monkeypatch.setattr(chat_llm.OllamaLLM, "health", lambda self, force=False: False)
    llm, label, real, note = _pick_models(SimpleNamespace(chat_engine="onnx"), "", False)
    assert not real and "not downloaded" in note and "Nothing answered" in note
    # ...and with Ollama chosen in the settings the inside model is not looked at.
    monkeypatch.setattr(engines, "text_model",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("not asked")))
    _llm, _label, real, note = _pick_models(SimpleNamespace(chat_engine="ollama"), "", False)
    assert not real and "not downloaded" not in note


@pytest.mark.skipif(not os.environ.get("LEASHA_CHAT_REAL_MODEL"),
                    reason="set LEASHA_CHAT_REAL_MODEL=<ollama model> to measure a real model")
def test_a_real_model_keeps_the_guarantee_even_when_it_answers_badly(env):
    """Not a quality gate - a model's recall is what the work order records. This asserts
    the part that must hold for *any* model: no sentence without a receipt, and
    honest absences."""
    from app.chat.evaluate import _pick_models

    llm, label, real, note = _pick_models(None, os.environ["LEASHA_CHAT_REAL_MODEL"], False)
    if not real:
        pytest.skip(note)
    subset = [q for q in fx.QUESTIONS if q.id in {"L01", "L06", "L18", "B01", "A01", "S03"}]
    got = run_chat_eval(env.engine(llm), subset, env.store, model=label, real_model=True)
    assert got.unreceipted_sentences == 0
    assert got.absence_honesty in (None, 1.0) and got.aggregate_exactness == 1.0
