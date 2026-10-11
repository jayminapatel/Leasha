r"""Questions across documents: a comparison searches each side, a timeline is in date order.

Layer: L8b

Order `smarter-answers` section 5 (owner, 2026-10-12). Before this, "compare the
2023 agreement with the 2024 agreement" was one search and one prompt: whichever
agreement said more of the question's words came first, and the other could miss
the sources, so the model compared one document with nothing. No floor at v1;
these pin the behaviour, and the evaluation measures it.
"""

from __future__ import annotations

import pytest

from app.chat import prompts
from app.chat.router import COMPARE, TIMELINE, cross_shape
from app.chat.testing import FakeLLM
from tests.unit.chat_env import Env, ask


@pytest.mark.parametrize("question, shape, sides", [
    ("Compare the 2023 tenancy agreement with the 2024 tenancy agreement", COMPARE,
     ("2023 tenancy agreement", "2024 tenancy agreement")),
    ("What are the differences between the draft safety report and the final safety report?",
     COMPARE, ("draft safety report", "final safety report")),
    ("Compare the two tenancy agreements", COMPARE, ()),
    ("Give me a timeline of the Leeds safety report", TIMELINE, ()),
    ("How has the rent changed?", TIMELINE, ()),
    ("What did we agree with the landlord about the deposit?", "", ()),
    ("", "", ()),
])
def test_the_shape_of_a_question_is_read_by_rules(question, shape, sides):
    assert cross_shape(question) == (shape, sides)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Env(tmp_path_factory.mktemp("cross"))
    yield e
    e.close()


def _names_in_prompt(llm) -> list[str]:
    for _kind, prompt in reversed(llm.calls + list(getattr(llm, "chat_calls", []))):
        text = str(prompt)
        if "Sources:" in text:
            return [line for line in text.splitlines() if line.startswith("[")]
    return []


def test_a_comparison_brings_both_sides_to_the_model(env):
    llm = FakeLLM("extractive")
    turn, _events = ask(env.engine(llm),
                        "Compare the 2023 tenancy agreement with the 2024 tenancy agreement")

    assert turn.debug["shape"] == COMPARE
    assert turn.debug["sides"] == ["2023 tenancy agreement", "2024 tenancy agreement"]
    # Each side found something of its own: the question's "2023" date filter once
    # rode along into the 2024 side, which then found nothing (2026-10-12, real model).
    assert all(turn.debug["side_files"]), turn.debug["side_files"]
    shown = " ".join(_names_in_prompt(llm))
    assert "tenancy-agreement-2023-expired.pdf" in shown
    assert "tenancy-agreement-2024.pdf" in shown
    system = str(llm.chat_calls[-1]) if getattr(llm, "chat_calls", None) else ""
    assert prompts.SHAPE_NOTES[COMPARE] in system


def test_a_timeline_lists_its_sources_oldest_first(env):
    llm = FakeLLM("extractive")
    turn, _events = ask(env.engine(llm), "Give me a timeline of the Leeds safety report")

    assert turn.debug["shape"] == TIMELINE
    names = turn.debug["sources"]
    draft, final = names.index("safety-report-draft.docx"), names.index("safety-report-final.pdf")
    assert draft < final                                # 25 April before 20 May
    system = str(llm.chat_calls[-1])
    assert "file dated 2025-04-25" in system
    assert prompts.SHAPE_NOTES[TIMELINE] in system


def test_an_ordinary_question_has_no_shape(env):
    llm = FakeLLM("extractive")
    turn, _events = ask(env.engine(llm), "What did we agree with the landlord about the deposit?")
    assert "shape" not in turn.debug
