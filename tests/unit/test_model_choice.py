"""Choosing an Ollama model from what is installed.

Layer: L2

The model is a *speed* decision far more than a quality one, and this session
proved it expensively. Interpret turns one sentence into about twenty tokens of
query. A 1.5B model does that as well as a 7B one and finishes in a fraction of
the time — but the default was `mistral`, the budget was five seconds, and the
feature failed every single time on a machine with five perfectly good models
installed, one of them small enough to have answered instantly.

Nothing in the application said any of that. A bare dropdown of names would ask
somebody to guess at exactly the thing that went wrong, so each row carries what
it will cost.
"""

from __future__ import annotations

import pytest

from app.llm.models import (
    TIMEOUT_RANGE,
    choose,
    describe,
    is_embedding_model,
    parameter_billions,
    rank,
    suggested_timeout_s,
)

#: The owner's actual `ollama list`, which is what found the bug.
INSTALLED = [
    "mistral:latest",
    "nomic-embed-text:latest",
    "llama3:latest",
    "gpt-oss:20b",
    "qwen2.5:1.5b",
]


# ---------------------------------------------------------------------------
# Reading a size out of a tag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "expected"), [
    ("qwen2.5:1.5b", 1.5),
    ("gpt-oss:20b", 20.0),
    ("llama3.2:3b", 3.0),
    ("phi3:14b", 14.0),
    ("mistral:latest", None),      # says nothing, so claim nothing
    ("llama3:latest", None),
    ("nomic-embed-text:latest", None),
])
def test_parameter_count_is_read_only_when_stated(name, expected):
    """Guessing would be worse than admitting ignorance, because the guess would
    drive the speed hint and the budget."""
    assert parameter_billions(name) == expected


def test_the_version_in_a_name_is_not_a_parameter_count():
    """`qwen2.5` is a version. Reading 2.5 out of it as billions would size the
    budget from the wrong number entirely."""
    assert parameter_billions("qwen2.5:1.5b") == 1.5


# ---------------------------------------------------------------------------
# Models that cannot answer a prompt at all
# ---------------------------------------------------------------------------

def test_an_embedding_model_is_recognised():
    """`nomic-embed-text` sits in most people's list precisely because something
    else pulled it. Offering it for Interpret produces a confusing failure
    several seconds later."""
    assert is_embedding_model("nomic-embed-text:latest")
    assert is_embedding_model("bge-large")
    assert not is_embedding_model("mistral:latest")


def test_an_embedding_model_is_listed_but_not_selectable():
    """Listed, because hiding it invites hunting for a model that `ollama list`
    plainly shows. Not selectable, because it cannot do the job."""
    choices = {choice.name: choice for choice in rank(INSTALLED)}
    assert choices["nomic-embed-text:latest"].selectable is False
    assert "cannot answer" in choices["nomic-embed-text:latest"].label


def test_selectable_models_are_still_selectable():
    choices = {choice.name: choice for choice in rank(INSTALLED)}
    assert choices["mistral:latest"].selectable is True


# ---------------------------------------------------------------------------
# The order is a recommendation whether or not it is meant as one
# ---------------------------------------------------------------------------

def test_the_smallest_usable_model_is_offered_first():
    """For this job small is the *right* answer, and on the owner's machine the
    right answer was installed the whole time."""
    assert rank(INSTALLED)[0].name == "qwen2.5:1.5b"


def test_embedding_models_sort_to_the_bottom():
    assert rank(INSTALLED)[-1].name == "nomic-embed-text:latest"


def test_an_unknown_size_sorts_after_every_known_one():
    """It cannot be recommended, so it should not sit at the top."""
    order = [choice.name for choice in rank(INSTALLED) if choice.selectable]
    assert order.index("qwen2.5:1.5b") < order.index("mistral:latest")


def test_ranking_an_empty_list_is_not_an_error():
    assert rank([]) == []


def test_blank_entries_are_dropped():
    assert [c.name for c in rank(["", "  ", "mistral"])] == ["mistral"]


# ---------------------------------------------------------------------------
# The budget must fit the model
# ---------------------------------------------------------------------------

def test_a_bigger_model_gets_a_bigger_budget():
    assert suggested_timeout_s(1.5) < suggested_timeout_s(7) < suggested_timeout_s(20)


def test_every_suggested_budget_clears_a_cold_start():
    """**The bug that started this.** Five seconds could never work: a cold
    model took 8.2s to answer a trivial prompt. Any budget below that is a
    feature that cannot run, not a responsive one."""
    for billions in (None, 1.5, 3, 7, 20, 70):
        assert suggested_timeout_s(billions) > 8.2


def test_every_suggested_budget_is_inside_what_the_control_allows():
    """A suggestion the spin box would clamp is a suggestion that silently
    becomes a different number."""
    for billions in (None, 1.5, 3, 7, 20, 70):
        assert TIMEOUT_RANGE[0] <= suggested_timeout_s(billions) <= TIMEOUT_RANGE[1]


def test_an_unknown_size_is_given_the_benefit_of_the_doubt():
    """The cost of waiting too long is impatience. The cost of waiting too
    little is a feature that appears broken - which is the one that happened."""
    assert suggested_timeout_s(None) >= suggested_timeout_s(3)


def test_the_floor_is_above_a_cold_start_too():
    assert TIMEOUT_RANGE[0] > 8.2


# ---------------------------------------------------------------------------
# What to say about each one
# ---------------------------------------------------------------------------

def test_a_small_model_is_described_as_ample_rather_than_lesser():
    """It genuinely is ample for this. Describing it as a compromise would push
    people back towards the slow default."""
    assert "ample" in describe("qwen2.5:1.5b", 1.5)


def test_a_very_large_model_says_it_is_probably_wrong_for_this():
    assert "too slow" in describe("gpt-oss:20b", 70)


def test_a_model_that_states_no_size_gets_no_invented_claim():
    assert describe("mistral:latest", None) == "mistral:latest"


# ---------------------------------------------------------------------------
# Which model to actually use
# ---------------------------------------------------------------------------

def test_the_configured_model_is_used_when_it_is_installed():
    model, note = choose("mistral", INSTALLED)
    assert model == "mistral"
    assert note == ""


def test_the_short_form_matches_the_tagged_form():
    """`mistral` and `mistral:latest` are the same model. People write the short
    form, Ollama reports the long one, and treating them as different reports a
    correctly-configured model as missing."""
    _model, note = choose("mistral", ["mistral:latest"])
    assert note == ""


def test_a_failed_probe_never_changes_the_configured_model():
    """**The rule this codebase keeps relearning.** An empty list means Ollama
    was not answering at that moment, not that the choice was wrong. Swapping
    the model because of it would silently change a setting somebody chose."""
    model, note = choose("mistral", [])
    assert model == "mistral"
    assert "Could not reach Ollama" in note


def test_a_model_that_is_not_installed_is_reported_rather_than_replaced():
    """Falling back silently would leave somebody wondering why their chosen
    model never seems to be the one answering."""
    model, note = choose("llama2", INSTALLED)
    assert model == "llama2"
    assert "not installed" in note
    assert "ollama pull llama2" in note


def test_nothing_configured_picks_the_smallest_usable_model():
    model, note = choose("", INSTALLED)
    assert model == "qwen2.5:1.5b"
    assert "qwen2.5:1.5b" in note


def test_nothing_configured_and_nothing_installed_says_what_to_run():
    model, note = choose("", [])
    assert model == ""
    assert "ollama pull" in note


def test_nothing_configured_never_picks_an_embedding_model():
    """It would fail several seconds later with a message about the model not
    following instructions, which is true and completely unhelpful."""
    model, _note = choose("", ["nomic-embed-text:latest"])
    assert model == ""


# --- the Test button --------------------------------------------------------

def test_the_test_button_builds_a_translator_that_is_switched_on():
    r"""**Reported from the window: ticking the box appeared to do nothing.**

    The log filled with *"Interpreting is switched off. Turn it on in Settings
    to have a sentence rewritten as a query"* - said to somebody standing in
    Settings, with it switched on, pressing Test.

    `QueryTranslator` defaults to `enabled=False`, because the feature is
    optional and search must not reach the network when it is off. The probe
    built one with the default, so the button could never once have worked.
    And because each press builds a fresh translator, the "logged once per
    translator" guard did not dedupe them either: every press produced another
    identical line advising the thing already done.

    Driven through `ModelBox._translate` rather than by reading the source, so
    a refactor that reintroduces the default is caught.
    """
    from app.ui.widgets.model_box import ModelBox

    asked: list[str] = []

    class Client:
        model = ""

        def generate(self, prompt, **kwargs):
            asked.append(prompt)
            return '{"query": "invoice barnsley"}'

    box = ModelBox.__new__(ModelBox)              # no Qt: only the worker body
    box._client_factory = Client

    result = box._translate("mistral", 5)

    assert asked, (
        "the probe never called the model - it built a switched-off translator "
        "and returned the fallback"
    )
    assert "switched off" not in (getattr(result, "note", "") or "")
