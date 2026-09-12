r"""Search experience §1: what each surface may do on the person's behalf.

**The seam exists so that no view branches on which tab it is.** The
alternative was `if self.is_search_tab:` in four view files, and every one of
those would have written a rule twice - once where it was decided and once
where it was almost decided. Here the difference between tabs is a table
anybody can read, and these tests are over the table.

The rule that does the most work: **a global switch can only turn a behaviour
off, never force it on.** Seven settings then do what twenty-eight would - off
means off everywhere, on means "follow this surface's contract" - and spelling
correction stays away from identifiers on the Code tab without anybody having
to remember to keep it there.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.search import policy

ROOT = Path(__file__).resolve().parents[2]

#: Words a search surface may not say. **Hoisted out of the test below, not
#: rewritten**: the same tuple, in the same order, now importable - the
#: adoptions order's own string sweep
#: (`test_adoption_scenarios.py`) runs this list over the sentences it added,
#: and a second copy of these words is a second guard that can disagree with
#: this one.
SURFACE_JARGON = ("fts", "bm25", "vector", "embedding", "policy",
                  "register", "edit distance", "rrf")


class Settings:
    """Only the six fields the policy reads."""

    search_fix_spelling = "auto"
    search_relax_on_empty = True
    search_auto_chips = True
    search_recency_blend = True
    search_version_folding = True
    search_plain_words = True


# --- the two contracts ------------------------------------------------------


def test_the_universal_tab_has_everything_on() -> None:
    """Its acceptance test is literal: an 8-year-old finds her homework."""
    found = policy.for_surface(policy.SEARCH)

    assert found.typo_correction == "auto"
    assert found.relax_on_empty
    assert found.auto_chips
    assert found.notice_register == "plain"


def test_the_power_surfaces_propose_rather_than_act() -> None:
    """Somebody who typed `/type:pdf` did it on purpose, and a tool that
    second-guesses an expert is a tool the expert switches off."""
    for surface in (policy.FILES, policy.MAIL):
        found = policy.for_surface(surface)
        assert found.typo_correction == "suggest", surface
        assert found.notice_register == "technical", surface
        assert not found.auto_chips, surface


def test_the_code_tab_never_corrects_a_spelling() -> None:
    r"""**A misspelt identifier is not a misspelt word.** `recieve_handler`
    may be exactly what is in the codebase, and helpfully searching for
    `receive_handler` instead hides the thing somebody is looking for."""
    found = policy.for_surface(policy.CODE)

    assert found.typo_correction == "off"
    assert not found.relax_on_empty, "three pasted terms means all three"


def test_a_surface_nobody_has_thought_about_gets_the_strict_contract() -> None:
    """Unknown means a new tab, and the safe answer there is propose, never
    act - a helpful default on a surface nobody has considered is a guess."""
    assert policy.for_surface("brand-new") == policy.for_surface(policy.CODE)
    assert policy.for_surface("") == policy.for_surface(policy.CODE)


# --- the global switches, and the one rule that makes seven enough ---------


def test_a_global_switch_turns_a_behaviour_off_everywhere() -> None:
    Settings.search_fix_spelling = "off"
    try:
        found = policy.preferences(Settings())
        for surface in policy.SURFACES:
            assert policy.from_settings(surface, found).typo_correction == "off"
    finally:
        Settings.search_fix_spelling = "auto"


def test_a_global_switch_cannot_force_a_behaviour_on() -> None:
    r"""**The rule that makes seven settings do the work of twenty-eight.**

    A global `on` overriding each surface would put plain-words notices on the
    Code tab and spelling correction on identifiers - precisely what the
    per-surface defaults exist to prevent.
    """
    found = policy.preferences(Settings())          # everything on

    assert policy.from_settings(policy.CODE, found).typo_correction == "off"
    assert policy.from_settings(policy.CODE, found).notice_register == "technical"
    assert policy.from_settings(policy.SEARCH, found).typo_correction == "auto"


def test_a_per_cell_value_beats_the_global_one() -> None:
    """It is the more specific statement of intent, and it is how somebody who
    genuinely wants plain notices on the Code tab gets them."""
    found = dict(policy.preferences(Settings()))
    found["code:notice_register"] = "plain"

    assert policy.from_settings(policy.CODE, found).notice_register == "plain"


def test_switching_plain_words_off_gives_the_technical_register() -> None:
    """A switch on one side and a register on the other. Off must land on a
    real register rather than on an empty string nothing knows how to read."""
    Settings.search_plain_words = False
    try:
        found = policy.preferences(Settings())
        assert policy.from_settings(
            policy.SEARCH, found).notice_register == "technical"
    finally:
        Settings.search_plain_words = True


def test_env_strings_and_panel_booleans_both_arrive_correctly() -> None:
    """`.env` holds `"false"`; a checkbox holds `False`. Converting at the call
    site would be a second place that has to know that, and the second place is
    the one that gets it wrong."""
    for raw in ("false", "0", "no", "off", False):
        found = policy.preferences(Settings(), {"SEARCH_RELAX_ON_EMPTY": raw})
        assert found["relax_on_empty"] is False, raw

    for raw in ("true", "1", "yes", "on", True):
        found = policy.preferences(Settings(), {"SEARCH_RELAX_ON_EMPTY": raw})
        assert found["relax_on_empty"] is True, raw


def test_an_unfamiliar_preference_is_ignored_rather_than_fatal() -> None:
    """The dictionary may hold keys from a newer build, and a search that
    refuses to run because it met an unfamiliar preference is a worse outcome
    than one that ignores it."""
    found = policy.from_settings(policy.SEARCH, {"telepathy": True,
                                                 "search:relax_on_empty": False})

    assert found.relax_on_empty is False, "the known one still applied"


def test_no_settings_at_all_gives_the_surface_default() -> None:
    assert policy.from_settings(policy.SEARCH) == policy.for_surface(policy.SEARCH)
    assert policy.from_settings(policy.SEARCH, {}) == policy.for_surface(policy.SEARCH)


# --- every behaviour is a setting, and every setting is a behaviour ---------


def test_each_behaviour_has_a_control() -> None:
    """§0's principle 2: helpful behaviour ships on and each one is
    individually switch-off-able. A behaviour with no switch is a direction
    somebody is being forced in."""
    from app.core.settings_registry import keys

    declared = set(keys())
    for field in policy.SETTING_FIELDS:
        assert field.upper() in declared, field


def test_each_behaviour_has_a_sentence_that_states_its_effect() -> None:
    """§0's principle 5, and already the house style: what pressing it does and
    what happens at the limit, in plain words."""
    named = {name for name, _label, _help in policy.BEHAVIOURS}

    assert named == set(policy.SearchPolicy().__dataclass_fields__)
    for _name, label, help_text in policy.BEHAVIOURS:
        assert label and len(help_text) > 60
        assert help_text[0].isupper() and help_text.rstrip().endswith(".")


def test_the_behaviour_help_speaks_english() -> None:
    """The same deny-list discipline as the tuning screen's plain-words guard.
    This surface is the one an eight-year-old uses."""
    for name, label, help_text in policy.BEHAVIOURS:
        text = f"{label} {help_text}".lower()
        for jargon in SURFACE_JARGON:
            assert jargon not in text, (name, jargon)


def test_a_policy_can_say_what_it_does(  ) -> None:
    """A policy that cannot explain itself is a policy people work around."""
    said = policy.describe(policy.for_surface(policy.SEARCH))

    assert len(said) >= 4
    assert policy.describe(policy.for_surface(policy.CODE)) == []
    assert "Suggest a spelling, but do not change what you typed" in \
        policy.describe(policy.for_surface(policy.FILES))


def test_the_safety_invariants_are_written_down() -> None:
    """§0's principle 3, drawn on purpose so nobody debates it later. A list
    nobody wrote is a list somebody eventually argues with."""
    assert len(policy.SAFETY) >= 3
    joined = " ".join(policy.SAFETY).lower()
    # Asserted by subject, not by phrasing: pinning the exact words would make
    # this a test of the sentence rather than of the promise, and the sentence
    # is allowed to get better.
    assert "delete" in joined and "your files" in joined
    assert "leaves this computer" in joined
    assert "says so" in joined, "the degraded-search promise"


# --- the seam is a seam -----------------------------------------------------


def test_the_policy_module_is_pure() -> None:
    """No Qt, no store, no I/O - it is a dataclass and a table, and its whole
    value is that every surface's contract can be read in one place."""
    tree = ast.parse((ROOT / "app" / "search" / "policy.py")
                     .read_text(encoding="utf-8"))
    imported = {node.module.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module}

    assert imported <= {"__future__", "dataclasses", "typing"}, imported


def test_no_view_branches_on_which_tab_it_is() -> None:
    r"""**The reason the seam exists.** A view asking "am I the search tab?"
    is a rule written in the wrong place, and four of them are four rules that
    drift apart.
    """
    import re

    offenders: list[str] = []
    for path in (ROOT / "app" / "ui").glob("*_view.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"if .*\bis_search_tab\b|if .*surface\s*==",
                                 source):
            offenders.append(f"{path.name}: {match.group(0)}")

    assert not offenders, offenders


@pytest.mark.parametrize("surface", policy.SURFACES)
def test_every_surface_resolves_without_raising(surface: str) -> None:
    """Called on every keystroke, so it may not have an unhappy path."""
    assert isinstance(policy.from_settings(surface, policy.preferences(Settings())),
                      policy.SearchPolicy)
