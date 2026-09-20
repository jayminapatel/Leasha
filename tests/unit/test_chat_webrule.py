"""The rule for when the web is worth asking (`app/chat/webrule.py`): a table, not a vibe.

Layer: L8b. Pure text and numbers - no engine, no network, no model. The end-to-end behaviour
(prompt appears only when the files come up thin) is in `test_chat_web_flow.py`.
"""

from __future__ import annotations

import pytest

from app.chat import engine as engine_module
from app.chat import webrule
from app.chat.webrule import PARTIAL_BELOW, decide

# (question, sources, thin, coverage) -> (wanted, reason)
TABLE = [
    # the files answered: never the web, however general the question sounds
    ("How much notice must the tenant give?", 3, False, 1.0, False, "files"),
    ("What is a tenancy agreement?", 4, False, 1.0, False, "files"),
    ("What is the latest notice the tenant must give?", 3, False, 0.85, False, "files"),
    ("who is the landlord named in the lease", 2, False, 0.9, False, "files"),
    ("How much notice must the tenant give?", 3, False, None, False, "files"),  # unknown is not "partial"
    # nothing came back / the assessment said thin
    ("What is a PST file?", 0, True, 0.0, True, "none"),
    ("Who is Ada Lovelace?", 0, True, 0.0, True, "none"),
    ("How does the boiler pressure valve work?", 2, True, 0.4, True, "thin"),
    # the files cover only part of it, and it is about the present / an outside thing
    ("What is the latest notice the tenant must give?", 3, False, 0.75, True, "partial"),
    ("What is the current legal minimum notice?", 2, False, 0.6, True, "partial"),
    ("Who is the new prime minister?", 2, False, 0.6, True, "partial"),
    ("Any news about the tenancy reform?", 2, False, 0.7, True, "partial"),
    # partial but not external: the files simply are what they are
    ("How much deposit was paid on the flat?", 2, False, 0.7, False, "files"),
    # the person's own words ask for the web
    ("How much notice must the tenant give? Search the web too.", 3, False, 1.0, True, "asked"),
    ("google what a PST file is", 3, False, 1.0, True, "asked"),
    ("look it up online: latest outlook version", 3, False, 1.0, True, "asked"),
    ("what is a purchase order online?", 3, False, 1.0, True, "asked"),
    ("check the internet for the tenancy deposit rules", 3, False, 1.0, True, "asked"),
    # about the person's own affairs: never, whatever else is true
    ("What did I agree with my solicitor?", 0, True, 0.0, False, "personal"),
    ("Search the web for what we agreed with the landlord", 0, True, 0.0, False, "personal"),
    ("What is our latest invoice total?", 2, False, 0.5, False, "personal"),
]


@pytest.mark.parametrize("question, sources, thin, coverage, wanted, reason", TABLE,
                         ids=[f"{row[0][:42]}|{row[5]}" for row in TABLE])
def test_the_rule_table(question, sources, thin, coverage, wanted, reason):
    verdict = decide(question, sources=sources, thin=thin, coverage=coverage)
    assert (verdict.wanted, verdict.reason) == (wanted, reason)


def test_the_boundary_is_written_down_and_is_exclusive():
    just_under = PARTIAL_BELOW - 0.01
    assert decide("What is the latest x?", sources=1, thin=False, coverage=just_under).wanted
    assert not decide("What is the latest x?", sources=1, thin=False, coverage=PARTIAL_BELOW).wanted


def test_a_question_the_files_cover_fully_never_wants_the_web_unless_asked():
    for question in ("What is the latest release?", "who is the current tenant", "today's price of rent"):
        assert not decide(question, sources=2, thin=False, coverage=1.0).wanted


def test_the_personal_pattern_means_the_same_as_the_engines():
    assert webrule._PERSONAL.pattern == engine_module._PERSONAL.pattern
    assert webrule._PERSONAL.flags == engine_module._PERSONAL.flags


def test_the_rule_imports_nothing_that_can_reach_the_network():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(webrule.__file__).read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert modules <= {"__future__", "re", "dataclasses", "typing"}
