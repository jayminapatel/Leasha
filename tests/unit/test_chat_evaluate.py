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
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.chat.evaluate import FLOORS, ChatReport, run_chat_eval, score_case
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


#: Recorded at the first measurement (FakeLLM, 80 questions) and pinned with margin. A
#: value lower than its floor here is a regression in the router, the loop, the
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
    "synthesis": 0.60,             # measured 80%; no floor at v1 (work order 4b)
}


@pytest.mark.parametrize("measure, floor", sorted(PINNED.items()))
def test_each_measure_holds_its_pinned_floor(report, measure, floor):
    value = report.measures()[measure]
    assert value is not None and value >= floor - 1e-9, (measure, value, floor)


def test_no_rendered_sentence_is_without_a_receipt(report):
    assert report.unreceipted_sentences == 0
    assert sum(r.sentences for r in report.results) >= 60             # and there were sentences to check


def test_every_first_narration_arrives_within_a_second(report):
    lat = report.latency("first_narration_s")
    assert lat["n"] == len(fx.QUESTIONS) and lat["max"] < 1.0        # work order 4c


def test_the_report_states_what_it_measured_and_never_hides_a_stand_in(report):
    text = "\n".join(report.lines())
    assert "NOT a language model" in text and "citation validity" in text
    assert report.as_dict()["real_model"] is False and report.as_dict()["questions"] == 80
    real = ChatReport(model="qwen2.5:1.5b", real_model=True, machine="m")
    assert "a real local model" in "\n".join(real.lines())


# --------------------------------------------------------------------------- the harness can fail

def test_a_lying_model_scores_badly_on_answers_and_still_perfectly_on_receipts(env):
    """The other half of the guarantee: a lying model loses recall, never honesty."""
    lying = run_chat_eval(env.engine("hallucinating"), fx.QUESTIONS, env.store, model="liar")
    assert lying.unreceipted_sentences == 0
    assert lying.absence_honesty == 1.0 and lying.aggregate_exactness == 1.0
    assert lying.extractive < 0.5 and lying.below_floor()             # it does not clear the floor


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


# --------------------------------------------------------------------------- the command line

def test_evaluate_chat_runs_from_the_command_line_without_a_model():
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "evaluate", "--chat", "--chat-fake",
         "--chat-ids", "A01,B01,L06"],
        cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-800:]
    assert "Chat evaluation  (3 questions)" in result.stdout
    assert "NOT a language model" in result.stdout and "citation validity" in result.stdout


def test_the_flag_and_its_helpers_are_on_the_evaluate_subcommand():
    import argparse

    from app.cli.evaluate import add_evaluate_parser

    parser = argparse.ArgumentParser()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true")
    add_evaluate_parser(parser.add_subparsers(), common)
    args = parser.parse_args(["evaluate", "--chat", "--chat-model", "qwen2.5:1.5b",
                              "--chat-ids", "L01,A01", "--chat-fake"])
    assert args.chat and args.chat_model == "qwen2.5:1.5b" and args.chat_fake
    assert args.chat_ids == "L01,A01"


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
