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


# ---------------------------------------------------------------------------
# File filters: name and size
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("typed", "expected"), [
    ("/name invoice", ("invoice",)),
    ("/filename report", ("report",)),
    ("/file summary.pdf", ("summary.pdf",)),
])
def test_name_filters_the_basename(typed, expected):
    assert parse_query(expand_slashes(typed)).names == expected


def test_name_and_path_are_different_questions():
    """`/path leeds` finds everything in a Leeds folder; `/name leeds` finds
    files called Leeds. Conflating them makes one of the two useless."""
    assert parse_query("name:leeds").names == ("leeds",)
    assert parse_query("name:leeds").paths == ()
    assert parse_query("path:leeds").paths == ("leeds",)
    assert parse_query("path:leeds").names == ()


@pytest.mark.parametrize(("typed", "expected"), [
    ("/size >1mb", (">", 1024**2)),
    ("/size <500kb", ("<", 500 * 1024)),
    ("/size >=10mb", (">=", 10 * 1024**2)),
    ("/size 1mb", (">=", 1024**2)),          # bare means "at least"
    ("/size 2gb", (">=", 2 * 1024**3)),
    ("/size 500b", (">=", 500)),
])
def test_size_parses_with_and_without_a_comparison(typed, expected):
    assert parse_query(expand_slashes(typed)).sizes == (expected,)


def test_a_size_that_is_not_a_size_is_reported_not_ignored():
    """A silently dropped size filter returns more than was asked for, and the
    person has no way to know their filter did nothing."""
    assert parse_query("size:banana").unknown_operators == ("size:banana",)
    assert parse_query("size:banana").sizes == ()


# ---------------------------------------------------------------------------
# Boolean operators
#
# The whole design rests on one rule: **only capitals are operators.**
# "salt and pepper" and "one or two" are things people genuinely search for,
# and a feature that broke them would cost more than it delivers.
# ---------------------------------------------------------------------------

def test_or_splits_terms_into_alternatives():
    parsed = parse_query("pump OR valve")
    assert parsed.or_groups == (("pump",), ("valve",))
    assert parsed.fts_match() == '("pump") OR ("valve")'


def test_and_binds_tighter_than_or():
    """`a b OR c` means `(a AND b) OR c` - the precedence every search engine
    uses and the one people expect without being told."""
    parsed = parse_query("pump station OR valve")
    assert parsed.or_groups == (("pump", "station"), ("valve",))


@pytest.mark.parametrize("query", [
    "salt and pepper",
    "one or two",
    "this and that or the other",
    "not now",
])
def test_lowercase_and_or_not_are_ordinary_words(query):
    """The rule that makes the feature safe to add to a box people already use."""
    parsed = parse_query(query)
    assert len(parsed.or_groups) == 1, f"{query!r} was treated as boolean"
    for word in query.split():
        assert word in parsed.terms


def test_uppercase_not_excludes_like_a_minus_sign():
    parsed = parse_query("leeds NOT draft")
    assert parsed.excluded == ("draft",)
    assert "draft" not in parsed.terms


def test_explicit_and_is_honoured_because_it_is_no_longer_the_default():
    """It *was* the default, and this test asserted AND changed nothing.

    Measuring twenty sentences against a known corpus showed that ANDing every
    word left six of them returning nothing at all, so terms are now joined with
    OR and BM25 ranks by how much matched. That makes an explicit AND a real
    instruction rather than a decoration - and it has to work, because it is the
    answer offered to anybody who finds the default too loose.
    """
    assert parse_query("pump AND valve").or_groups == (("pump", "valve"),)
    assert parse_query("pump AND valve").fts_match() == '"pump" AND "valve"'
    assert parse_query("pump valve").fts_match() == '"pump" OR "valve"'
    assert parse_query("pump AND valve").explicit_and is True
    assert parse_query("pump valve").explicit_and is False


def test_a_phrase_applies_to_every_alternative():
    """`"site survey" pump OR valve` means the phrase in both cases - which is
    how people write it, and the only reading that is not surprising."""
    expression = parse_query('"site survey" pump OR valve').fts_match()
    assert expression.count('"site" + "survey"') == 2


def test_terms_are_joined_with_or_so_a_description_need_not_match_every_word():
    """This asserted AND, as a compatibility guarantee, until the guarantee was
    measured and found to be the bug.

    "drawings of the pump station" required a document to contain "of" and
    "the", and nineteen of twenty plain sentences returned zero results.
    Precision is recovered by BM25 ranking and by the strict forms - quoted
    phrases and an explicit AND - which still mean exactly what they say.
    """
    assert parse_query("pump valve leeds").fts_match() == '"pump" OR "valve" OR "leeds"'


def test_or_still_composes_with_exclusions():
    expression = parse_query("pump OR valve -draft").fts_match()
    assert expression == '(("pump") OR ("valve")) NOT ("draft")'


def test_every_boolean_expression_is_valid_fts5(tmp_path):
    """The guarantee `to_fts_match` exists to make: SQLite must always parse it.
    A malformed expression makes a search report "no results" when what actually
    happened is a syntax error."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
    conn.execute("INSERT INTO t VALUES ('pump valve station leeds survey')")

    for query in ["pump OR valve", "pump station OR valve leeds", "a OR b OR c",
                  'x "site survey" OR y', "pump OR valve -draft", "OR", "OR OR",
                  "pump OR", "OR pump", "NOT", "AND OR NOT"]:
        expression = parse_query(query).fts_match()
        if not expression:
            continue
        conn.execute("SELECT count(*) FROM t WHERE t MATCH ?", (expression,)).fetchone()
