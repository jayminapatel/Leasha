"""The prompt a small model can actually follow.

Layer: L3

`qwen2.5:1.5b` answered in 5.09s — fast enough — and handed the sentence back
completely unchanged. `emails from chris about buying a licence` should become
`from:chris licence`; it produced the sentence verbatim.

The prompt was rules with no examples. A 1.5B instruct model pattern-matches far
better than it reasons, and rules-only prompts are exactly where such models
echo. Examples are the fix, and they were paid for by cutting the operator
value-hints — those exist to remind a *person* what a value looks like, and they
were 1,400 of the prompt's 1,901 characters, charged on every translation.

The application then reported the echo as "Nothing to interpret", which is a
claim about the *sentence* and was plainly false about that one. Same family as
every other bug this week: the failure described as something it was not.
"""

from __future__ import annotations

import pytest

from app.search.commands import COMMANDS, EXAMPLES, examples_for_model
from app.search.query import parse_query
from app.search.translate import (
    TEST_SENTENCE,
    _has_nothing_to_interpret,
    build_prompt,
)


def prompt() -> str:
    return build_prompt("find the thing")


# ---------------------------------------------------------------------------
# The examples must be demonstrations, not decoration
# ---------------------------------------------------------------------------

def test_the_prompt_carries_worked_examples():
    assert "Examples:" in prompt()


def test_every_example_parses_to_what_it_claims():
    """**An example that the parser rejects teaches the model to be wrong**, and
    it would do so invisibly - the model would comply perfectly and the query
    would match nothing."""
    for sentence, query in EXAMPLES:
        parsed = parse_query(query)
        assert not parsed.unknown_operators, (
            f"example for {sentence!r} uses an operator the parser does not "
            f"have: {parsed.unknown_operators}"
        )


def test_every_operator_in_an_example_is_a_real_one():
    spellings = {spelling for command in COMMANDS for spelling in command.spellings}
    for _sentence, query in EXAMPLES:
        for token in query.split():
            if ":" in token and not token.startswith('"'):
                assert token.split(":")[0] in spellings, f"{token} is not an operator"


def test_the_examples_cover_the_cases_that_go_wrong():
    """One each: a sender, a date range, a file type, an exclusion - and the one
    most often missed, a sentence with no constraints at all, so the model
    learns that bare words are a correct answer rather than a failure to find
    an operator."""
    queries = " ".join(query for _s, query in EXAMPLES)
    assert "from:" in queries
    assert "after:" in queries
    assert "type:" in queries
    assert "-" in queries
    assert any(":" not in query for _s, query in EXAMPLES), "no plain-words example"


def test_at_least_one_example_leaves_the_sentence_essentially_alone():
    """Without it a model treats 'produce an operator' as the goal and invents
    constraints the sentence never stated."""
    assert any(sentence == query for sentence, query in EXAMPLES)


def test_no_example_is_the_sentence_the_test_button_uses():
    """**A trap worth a test.** If the Test button's sentence appears verbatim
    as an example, the model copies the answer from its own prompt and the test
    reports success for a model that cannot do the job."""
    for sentence, _query in EXAMPLES:
        assert sentence.lower() != TEST_SENTENCE.lower()


def test_the_examples_are_formatted_like_the_request_that_follows_them():
    """The model continues a pattern. If the examples do not look like the real
    request, there is no pattern to continue."""
    rendered = examples_for_model()
    assert rendered.count("Sentence: ") == len(EXAMPLES)
    assert rendered.count("Query: ") == len(EXAMPLES)


# ---------------------------------------------------------------------------
# Paid for, not added on top
# ---------------------------------------------------------------------------

def test_the_prompt_got_shorter_despite_gaining_examples():
    """Every character is re-read on every translation, and prompt evaluation is
    what made mistral take over thirty seconds. The value hints that went are
    for the dropdown, where a person needs them; the model never did.

    **The cap is a ratchet, and it moved once: 1,901 -> 2,100 on 2026-09-20.** The
    prompt measured 2,084 by then. The growth is operators added after this was
    written - `/on` for offline-media sources (order `202626270513`) and the
    `video` / `movie` / `audio` / `recording` kind words (order `202626270515`) -
    not examples creeping back in. Raise it again only with a translation-latency
    measurement to show for it; do not simply let it drift.
    """
    assert len(prompt()) < 2100


def test_every_operator_is_still_named():
    """Shortening must not silently drop an operator - the model can only use
    what it is told about, and one that vanishes is a feature that quietly
    stops working."""
    text = prompt()
    for command in COMMANDS:
        # Order 0x §6a: a spelling row (`between:`) is its filter's other
        # name, and the filter it names is what must be here.
        assert f"{command.alias_of or command.name}:" in text


# ---------------------------------------------------------------------------
# Saying which of the two things happened
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sentence", [
    "emails from chris about buying a licence",
    "the pdf from dave",
    "spreadsheets with attachments",
    "anything before last March",
])
def test_a_sentence_with_a_constraint_is_not_called_uninterpretable(sentence):
    """**The false claim.** Reporting "nothing to interpret" about a sentence
    that says "from chris" sends somebody looking at their own wording, when the
    fix is a different model."""
    assert _has_nothing_to_interpret(sentence) is False


@pytest.mark.parametrize("sentence", [
    "quarterly revenue figures",
    "pump curve",
    "leeds site drawings",
])
def test_a_genuinely_plain_sentence_is_recognised(sentence):
    """The message has to stay true in both directions, or it is just noise."""
    assert _has_nothing_to_interpret(sentence) is True


def test_an_empty_sentence_does_not_raise():
    assert _has_nothing_to_interpret("") is True
