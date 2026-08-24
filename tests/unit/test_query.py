"""Layer 4: query parsing and FTS5 sanitisation.

The acceptance criterion from BUILD_SPEC_V2.md is the hard one here:

    Malformed queries ("unclosed, AND AND, emoji, 10k characters) return results
    or a clean AppError - never a crash.

`search_bm25()` meets that today by catching sqlite3.OperationalError and returning
[]. That is a safety net, not a solution: the user sees 'no results' when the truth
is 'your quote was unbalanced'. These tests prove the expression handed to SQLite is
parseable in the first place, by running it against a real FTS5 table.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from app.search.query import (
    MAX_QUERY_CHARS,
    MAX_TERMS,
    ParsedQuery,
    parse_query,
    to_fts_match,
)

TODAY = date(2026, 8, 24)


# --- the sanitiser, proved against real SQLite ------------------------------

@pytest.fixture()
def fts() -> sqlite3.Connection:
    """A real FTS5 table. If the expression parses here, it parses in the app."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(text)")
    conn.executemany(
        "INSERT INTO t(text) VALUES (?)",
        [
            ("annual site survey for the northern plant",),
            ("draft survey notes, superseded",),
            ("commissioning report v1.2 for pump station",),
        ],
    )
    conn.commit()
    return conn


HOSTILE = [
    '"unclosed',
    'AND AND',
    'NOT',
    'OR OR OR',
    '((((',
    ')',
    '*',
    '**',
    '- -- ---',
    '^foo',
    'a NEAR/ b',
    '"" ""',
    'survey"',
    '{}[]()',
    ':::',
    'type:',
    'after:',
    'café naïve',
    'ohmygod',
    '',
    '   ',
    'x' * 10_000,
    'survey OR (draft AND',
    'a" OR "b',
]


@pytest.mark.parametrize("raw", HOSTILE)
def test_hostile_input_produces_a_parseable_expression(raw: str, fts: sqlite3.Connection) -> None:
    """No input may produce an expression SQLite refuses.

    An empty expression is a legitimate outcome - it means 'nothing searchable' -
    but a non-empty one must never raise.
    """
    match = to_fts_match(parse_query(raw))
    if not match:
        return
    fts.execute("SELECT * FROM t WHERE t MATCH ?", (match,)).fetchall()


@pytest.mark.parametrize("raw", HOSTILE)
def test_hostile_input_never_raises_in_the_parser(raw: str) -> None:
    assert isinstance(parse_query(raw), ParsedQuery)


def test_parser_accepts_none() -> None:
    assert parse_query(None).raw == ""  # type: ignore[arg-type]


def test_fts_operators_are_treated_as_text_not_syntax(fts: sqlite3.Connection) -> None:
    """A user searching for the word 'and' means the word, not the operator."""
    match = to_fts_match(parse_query("and or not near"))
    rows = fts.execute("SELECT * FROM t WHERE t MATCH ?", (match,)).fetchall()
    assert rows == []


def test_a_real_query_still_finds_the_right_row(fts: sqlite3.Connection) -> None:
    match = to_fts_match(parse_query('"site survey"'))
    rows = fts.execute("SELECT text FROM t WHERE t MATCH ?", (match,)).fetchall()
    assert len(rows) == 1
    assert "northern plant" in rows[0][0]


def test_exclusion_removes_the_unwanted_row(fts: sqlite3.Connection) -> None:
    match = to_fts_match(parse_query("survey -draft"))
    rows = fts.execute("SELECT text FROM t WHERE t MATCH ?", (match,)).fetchall()
    assert len(rows) == 1
    assert "draft" not in rows[0][0]


def test_prefix_search_survives_quoting(fts: sqlite3.Connection) -> None:
    match = to_fts_match(parse_query("commission*"))
    rows = fts.execute("SELECT text FROM t WHERE t MATCH ?", (match,)).fetchall()
    assert len(rows) == 1


def test_only_exclusions_yields_no_expression() -> None:
    """FTS5 NOT needs a left operand; there is nothing to search here."""
    assert to_fts_match(parse_query("-draft -old")) == ""


def test_unbalanced_quote_still_searches_the_words(fts: sqlite3.Connection) -> None:
    match = to_fts_match(parse_query('"site survey'))
    rows = fts.execute("SELECT * FROM t WHERE t MATCH ?", (match,)).fetchall()
    assert rows, "a dangling quote should degrade to a word search, not to nothing"


# --- operators --------------------------------------------------------------

def test_type_operator_is_stripped_from_the_text() -> None:
    q = parse_query("type:pdf quarterly report")
    assert q.ext == ("pdf",)
    assert q.terms == ("quarterly", "report")
    assert "pdf" not in q.text


def test_type_group_expands() -> None:
    assert set(parse_query("type:word memo").ext) == {"doc", "docx"}


def test_type_accepts_a_list_and_a_leading_dot() -> None:
    assert set(parse_query("type:.pdf,docx x").ext) == {"pdf", "docx"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("after:2024-01-15", date(2024, 1, 15)),
        ("after:2024-01", date(2024, 1, 1)),
        ("after:2024", date(2024, 1, 1)),
        ("after:today", TODAY),
        ("after:yesterday", date(2026, 8, 23)),
        ("after:7d", date(2026, 8, 17)),
        ("after:last-month", date(2026, 7, 24)),
        ("since:2y", date(2024, 8, 24)),
    ],
)
def test_date_forms(raw: str, expected: date) -> None:
    assert parse_query(raw, today=TODAY).after == expected


def test_unparseable_date_is_reported_not_silently_dropped() -> None:
    q = parse_query("after:nextthursday report", today=TODAY)
    assert q.after is None
    assert q.unknown_operators == ("after:nextthursday",)
    assert q.terms == ("report",)


def test_reversed_date_range_is_swapped() -> None:
    q = parse_query("after:2025 before:2024", today=TODAY)
    assert q.after == date(2024, 1, 1)
    assert q.before == date(2025, 1, 1)


def test_path_and_sender_operators() -> None:
    q = parse_query('path:"D:\\Projects\\Alpha" from:Jane@Example.com notes')
    assert q.paths == ("D:\\Projects\\Alpha",)
    assert q.senders == ("jane@example.com",)
    assert q.terms == ("notes",)


def test_aliases_agree() -> None:
    assert parse_query("folder:x").paths == parse_query("dir:x").paths


def test_filters_only_query_is_valid_but_has_no_text() -> None:
    q = parse_query("type:pdf after:2024", today=TODAY)
    assert q.has_filters and not q.has_text
    assert to_fts_match(q) == ""


# --- shape and budget -------------------------------------------------------

def test_embed_text_excludes_operators() -> None:
    """Operators are noise to a dense model - never embed them."""
    q = parse_query('type:pdf after:2024 "site survey" northern')
    assert q.embed_text == "site survey northern"


def test_terms_are_deduped_preserving_order() -> None:
    assert parse_query("survey Survey SURVEY report").terms == ("survey", "report")


def test_term_count_is_capped() -> None:
    assert len(parse_query(" ".join(f"w{i}" for i in range(500))).terms) <= MAX_TERMS


def test_long_input_is_truncated_not_rejected() -> None:
    q = parse_query("survey " + "x" * 50_000)
    assert len(q.raw) == 50_007, "raw is preserved whole for the cache key"
    assert len(q.terms) <= MAX_TERMS
    assert MAX_QUERY_CHARS == 4096


def test_intra_word_punctuation_survives() -> None:
    assert "v1.2" in parse_query("commissioning v1.2").terms


def test_parse_is_deterministic() -> None:
    raw = 'type:pdf after:2024 "site survey" -draft northern'
    assert parse_query(raw, today=TODAY) == parse_query(raw, today=TODAY)


# ---------------------------------------------------------------------------
# Scope: the chips beside the search box.
#
# A filter, not a mode. You should never have to decide whether a thing was an
# email or a document *before* typing, because the usual answer is "I do not
# remember, that is why I am searching".
# ---------------------------------------------------------------------------

def test_the_default_scope_is_everything():
    assert parse_query("barnsley").scope == "all"


def test_scoping_returns_a_copy_rather_than_mutating():
    """`ParsedQuery` is frozen so it can be a cache key. A scope changed in
    place would leave the cache serving one scope's results under another's
    name - the same shape of bug as a stale generation."""
    original = parse_query("barnsley")
    scoped = original.scoped("mail")

    assert original.scope == "all"
    assert scoped.scope == "mail"
    assert scoped is not original
    assert scoped.raw == original.raw


def test_an_unknown_scope_falls_back_to_everything():
    """A stored setting from a future version must not silently return nothing."""
    assert parse_query("x").scoped("nonsense").scope == "all"
    assert parse_query("x").scoped("").scope == "all"


def test_a_scope_counts_as_a_filter():
    """"mail" with no search terms is a legitimate browse, and `has_filters` is
    what stops the engine treating it as an empty query."""
    assert not parse_query("").scoped("all").has_filters
    assert parse_query("").scoped("mail").has_filters


def test_scoping_preserves_every_other_operator():
    parsed = parse_query('type:pdf after:2024 from:jen@acme.co.uk "site survey"').scoped("mail")
    assert parsed.ext == ("pdf",)
    assert parsed.senders == ("jen@acme.co.uk",)
    assert parsed.phrases == ("site survey",)
    assert parsed.scope == "mail"
