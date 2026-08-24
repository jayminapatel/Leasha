"""The filter catalogue: discoverable, and the same list everywhere.

Layer: L4

Every filter tested here already worked. `type:pdf from:dave` has parsed
correctly since Layer 4 was built, was covered by tests, and shipped - and in
practice went unused, because nothing in the application ever said it existed.

That is the failure this module exists to fix, so the tests are mostly about
*agreement*: the dropdown, the CLI listing and the prompt Layer 8a hands to the
model must all describe exactly the grammar the parser accepts. A filter offered
by one and rejected by another is worse than an undocumented one - it is a
promise the application breaks in front of the user.
"""

from __future__ import annotations

import pytest

from app.search.commands import (
    COMMANDS,
    command_for,
    expand_slashes,
    grammar_for_model,
    help_lines,
    matching,
)
from app.search.query import parse_query


# -- the catalogue and the parser must agree ---------------------------------

def test_every_documented_spelling_is_one_the_parser_accepts():
    """The test that matters most. Offering a filter the parser rejects means
    the user types what they were told to and gets nothing back."""
    from app.search.query import _FIELD_ALIASES

    documented = {spelling for command in COMMANDS for spelling in command.spellings}
    accepted = set(_FIELD_ALIASES)

    assert documented - accepted == set(), "offered but not parsed"
    assert accepted - documented == set(), "parsed but never mentioned to anybody"


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c.name)
def test_every_example_actually_parses(command):
    """Each example is copy-pasteable. One that does not work is a trap."""
    parsed = parse_query(expand_slashes(command.example))
    assert parsed.has_filters, f"{command.example!r} produced no filter"


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c.name)
def test_every_alias_reaches_the_same_filter(command):
    for alias in command.aliases:
        assert command_for(alias) is command


def test_the_model_is_told_only_about_operators_that_exist():
    """A model given a plausible-but-wrong operator writes queries that match
    nothing and give no hint why - the failure that makes AI search feel
    unpredictable and therefore useless over your own archive."""
    grammar = grammar_for_model()
    for command in COMMANDS:
        assert f"{command.name}:" in grammar
    assert "colour:" not in grammar


# -- slashes are a doorway, not a grammar ------------------------------------

@pytest.mark.parametrize(("typed", "expected"), [
    ("/type pdf", "type:pdf"),
    ("/from dave", "from:dave"),
    ("/after 2024-06-01", "after:2024-06-01"),
    ("/type pdf leeds", "type:pdf leeds"),
    ("/from dave /after 2024-01-01", "from:dave after:2024-01-01"),
    ("type:pdf", "type:pdf"),                      # already in the raw syntax
    ("/ext docx", "type:docx"),                    # aliases normalise
    ("/kind email", "type:email"),
    ("/since 2024", "after:2024"),
    ("/until 2025", "before:2025"),
    ("/folder projects", "path:projects"),
])
def test_a_slash_command_becomes_the_syntax_the_parser_reads(typed, expected):
    assert expand_slashes(typed) == expected


@pytest.mark.parametrize("typed", [
    "report about /var/log",
    "12/03 invoice",
    "D:/Projects/Leeds",
    "and/or",
    "/tpe pdf",                                    # a typo, not a command
    "/",
])
def test_anything_that_is_not_a_command_is_left_exactly_as_typed(typed):
    """The behaviour that keeps the box trustworthy.

    Somebody searching a Unix path, a URL fragment or a date written `12/03`
    must get what they typed. Silently deleting or rewriting part of a query is
    the one thing that would make people stop believing the results.
    """
    assert expand_slashes(typed) == typed


def test_slash_and_colon_forms_produce_identical_queries():
    """`/type pdf` and `type:pdf` are the same query. One grammar, one set of
    tests, one thing to get right."""
    slashed = parse_query(expand_slashes('/type pdf /from dave "site survey" -draft'))
    colons = parse_query('type:pdf from:dave "site survey" -draft')

    assert slashed.ext == colons.ext
    assert slashed.senders == colons.senders
    assert slashed.phrases == colons.phrases
    assert slashed.excluded == colons.excluded


def test_a_full_combination_parses_into_every_field():
    parsed = parse_query(
        expand_slashes('/type pdf /from dave /after 2024-06-01 "site survey" -draft leeds')
    )
    assert parsed.ext == ("pdf",)
    assert parsed.senders == ("dave",)
    assert parsed.after is not None and parsed.after.year == 2024
    assert parsed.phrases == ("site survey",)
    assert parsed.excluded == ("draft",)
    assert "leeds" in parsed.terms


# -- the dropdown ------------------------------------------------------------

def test_a_bare_slash_offers_everything():
    """The first keystroke reveals the whole set - that is the entire point."""
    assert len(matching("/")) == len(COMMANDS)
    assert len(matching("")) == len(COMMANDS)


def test_a_prefix_narrows_by_alias_as_well_as_name():
    """`/f` must offer `from`. It must also offer `path`, whose alias is
    `folder` - hiding a match because it was spelled differently is how people
    conclude a filter does not exist."""
    names = {command.name for command in matching("/f")}
    assert "from" in names
    assert "path" in names


def test_an_unknown_prefix_offers_nothing_rather_than_everything():
    assert matching("/zzz") == []


def test_the_listing_names_every_command_and_its_aliases():
    text = "\n".join(help_lines())
    for command in COMMANDS:
        assert command.name in text
        assert command.summary in text
        for alias in command.aliases:
            assert alias in text


def test_the_listing_mentions_phrases_and_exclusions():
    """The two most useful and least guessable pieces of syntax. Somebody
    reading this list wants all of it in one place."""
    text = "\n".join(help_lines())
    assert "exact phrase" in text
    assert "-draft" in text


# ---------------------------------------------------------------------------
# Mail search: to, subject, has:attachment
#
# Reported by the owner: "the / command does not have To for mail... and
# subject and has attachments etc". They were missing from the parser, not just
# from the catalogue - and adding them exposed two older bugs, pinned below.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("typed", "field", "expected"), [
    ("/to priya", "recipients", ("priya",)),
    ("/cc chris", "recipients", ("chris",)),
    ("/recipient dave", "recipients", ("dave",)),
    ("/subject licence", "subjects", ("licence",)),
    ("/title invoice", "subjects", ("invoice",)),
    ("/re renewal", "subjects", ("renewal",)),
])
def test_the_mail_fields_parse(typed, field, expected):
    assert getattr(parse_query(expand_slashes(typed)), field) == expected


@pytest.mark.parametrize(("typed", "expected"), [
    ("/has attachment", True),
    ("/has attachments", True),
    ("/has attached", True),
    ("/has no-attachment", False),
    ("/has no-attachments", False),
    ("/has without-attachment", False),
])
def test_has_attachment_is_three_state(typed, expected):
    """`None` means "did not ask", which is a different search from "asked for
    none". A bool cannot tell those apart, and conflating them would silently
    exclude every message with a file attached."""
    assert parse_query(expand_slashes(typed)).has_attachment is expected


def test_not_asking_about_attachments_is_not_the_same_as_asking_for_none():
    assert parse_query("licence").has_attachment is None


def test_an_unrecognised_has_value_is_reported_not_ignored():
    """Silently ignoring `has:banana` would widen the search without saying so,
    and the person would never learn their filter did nothing."""
    assert parse_query("has:banana").unknown_operators == ("has:banana",)


def test_the_operator_pattern_is_built_from_the_alias_table():
    """The bug found while adding these three.

    `_OPERATOR` carried its own hardcoded alternation of field names. Adding
    `to`, `subject` and `has` to `_FIELD_ALIASES` therefore did nothing at all:
    the regex never matched them, so the handler that would have used them was
    unreachable and the words became ordinary search terms. The filter appeared
    to work and silently did nothing.
    """
    from app.search.query import _FIELD_ALIASES, _OPERATOR

    for alias in _FIELD_ALIASES:
        assert _OPERATOR.match(f"{alias}:value"), f"{alias}: is documented but unmatched"


def test_a_mail_query_combines_every_field():
    parsed = parse_query(expand_slashes(
        '/from chris /to priya /subject "licence renewal" /has attachment quote'
    ))
    assert parsed.senders == ("chris",)
    assert parsed.recipients == ("priya",)
    assert parsed.subjects == ("licence renewal",)
    assert parsed.has_attachment is True
    assert "quote" in parsed.terms
