r"""The owner's own questions: read from a file, asked, scored.

Layer: L8b

Order `smarter-answers` 6a. The owner writes these against the final index (6b,
held); this pins the format and the scoring on the Chat fixture.
"""

from __future__ import annotations

import pytest

from app.chat.mine import OwnerQuestion, read_owner_questions, run_owner_questions, score
from app.chat.types import ChatTurn, Receipt
from tests.unit.chat_env import Env


def test_the_file_is_read_line_by_line(tmp_path):
    path = tmp_path / "mine.txt"
    path.write_text("# my questions\n\n"
                    "What did we agree about the deposit? | landlord-deposit | 950\n"
                    "Where is the valve schedule? | valve-schedule\n", encoding="utf-8")

    got = read_owner_questions(path)

    assert got == [OwnerQuestion("What did we agree about the deposit?", "landlord-deposit", "950", 3),
                   OwnerQuestion("Where is the valve schedule?", "valve-schedule", "", 4)]


def test_a_line_with_no_file_is_refused_not_guessed(tmp_path):
    path = tmp_path / "mine.txt"
    path.write_text("What did we agree about the deposit?\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        read_owner_questions(path)


def test_right_means_the_file_cited_and_the_fact_said():
    q = OwnerQuestion("deposit?", "landlord-deposit", "950")
    cited = Receipt(1, r"D:\Tenancy\landlord-deposit-letter.docx", "letter", "")
    wrong_file = Receipt(2, "D:/Tenancy/tenancy-agreement-2024.pdf", "agreement", "")

    assert score(ChatTurn("assistant", "It is 950 pounds [1].", [cited]), q).right
    assert not score(ChatTurn("assistant", "It is 950 pounds [1].", [wrong_file]), q).right
    assert not score(ChatTurn("assistant", "It is 800 pounds [1].", [cited]), q).right
    no_fact = OwnerQuestion("deposit?", "LANDLORD-DEPOSIT")
    assert score(ChatTurn("assistant", "Here [1].", [cited]), no_fact).right


def test_the_questions_are_asked_of_the_engine_and_reported(tmp_path):
    env = Env(tmp_path)
    try:
        questions = [OwnerQuestion("What did we agree with the landlord about the deposit?",
                                   "landlord-deposit", "950", 1),
                     OwnerQuestion("How much butter goes in the lemon cake?", "no-such-file", "", 2)]
        report = run_owner_questions(env.engine(), questions, model="stand-in")
    finally:
        env.close()

    summary = report.as_dict()
    assert summary["questions"] == 2 and summary["right"] == 0.5
    assert [w["line"] for w in summary["wrong"]] == [2]
    assert any("Your questions" in line for line in report.lines())
