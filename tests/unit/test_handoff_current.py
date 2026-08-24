"""The handoff document must not silently go stale.

A handoff that has drifted is worse than none: it is confidently wrong, and
whoever picks the project up acts on it. So "update HANDOFF.md" is enforced by
this test rather than trusted to a checklist.

Deliberately stdlib-only, like test_docs_versioned.py: this is repo hygiene, and
it should run even when the app's dependencies are not installed.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HANDOFF = PROJECT_ROOT / "HANDOFF.md"
INSTRUCTIONS = PROJECT_ROOT / "docs" / "PROJECT_INSTRUCTIONS.md"
VERSION_FILE = PROJECT_ROOT / "VERSION"

APPLIES_TO = re.compile(r"\*\*Applies to:\*\* app v(?P<version>\d+\.\d+\.\d+)")


def _app_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8-sig").strip().splitlines()[0]


def test_both_documents_exist() -> None:
    """Every project carries a handoff and a set of instructions."""
    assert HANDOFF.is_file(), "HANDOFF.md is missing - see docs/PROJECT_INSTRUCTIONS.md"
    assert INSTRUCTIONS.is_file(), "docs/PROJECT_INSTRUCTIONS.md is missing"


def test_handoff_applies_to_the_current_version() -> None:
    """The release checklist requires updating this. Here is the enforcement."""
    match = APPLIES_TO.search(HANDOFF.read_text(encoding="utf-8"))
    assert match, "HANDOFF.md has no 'Applies to' version header"

    stated, current = match.group("version"), _app_version()
    assert stated == current, (
        f"HANDOFF.md says it applies to v{stated}, but VERSION is {current}.\n"
        f"Update HANDOFF.md - state, next layer, any new decision or trap - then bump\n"
        f"its header. A stale handoff is worse than none: it is confidently wrong."
    )


def test_instructions_apply_to_the_current_version() -> None:
    match = APPLIES_TO.search(INSTRUCTIONS.read_text(encoding="utf-8"))
    assert match, "PROJECT_INSTRUCTIONS.md has no 'Applies to' version header"
    stated, current = match.group("version"), _app_version()
    assert stated == current, (
        f"docs/PROJECT_INSTRUCTIONS.md says v{stated}, VERSION is {current}."
    )


def test_handoff_answers_the_questions_it_promises() -> None:
    """The sections someone picking this up cold actually needs."""
    text = HANDOFF.read_text(encoding="utf-8").lower()
    for required, why in [
        ("where everything lives", "someone needs to find the code and the index"),
        ("current state", "what works right now"),
        ("resuming from cold", "the new-machine path"),
        ("decisions already made", "so settled questions are not reopened blind"),
        ("traps", "the failures that have already happened"),
        ("open questions", "what is genuinely undecided"),
    ]:
        assert required in text, f"HANDOFF.md is missing a '{required}' section - {why}"


def test_instructions_carry_the_non_negotiables() -> None:
    """The constraints that must survive a change of author or assistant."""
    text = INSTRUCTIONS.read_text(encoding="utf-8").lower()
    for phrase in [
        "no service in the search hot path",
        "how to fix it",
        "never halts",
        "resumable",
        "sqlite is the authority",
        "bom",
    ]:
        assert phrase in text, f"PROJECT_INSTRUCTIONS.md no longer states: {phrase}"


def test_handoff_records_the_layer_state() -> None:
    """It must name the layer that is next, or it cannot be resumed from."""
    text = HANDOFF.read_text(encoding="utf-8")
    assert "**Next**" in text or "Next" in text
    assert re.search(r"\bL[0-9]\b", text), "no layer references found"
