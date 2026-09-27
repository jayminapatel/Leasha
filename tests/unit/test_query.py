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
from datetime import date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
    r"""A typo, not an intent - and the swap covers the same span as the
    correctly-ordered form.

    **The expected values changed with the partial-date fix, and the rule did
    not.** `before:2024` used to resolve to 1 January 2024, so this asked for
    2024-01-01 to 2025-01-01 - a range that excludes all of 2025 while claiming
    to include it. A partial date names a *period*, and which edge is meant
    depends on which side of the range it sits: `after:` takes the first day,
    `before:` the last. Swapping the two resolved days would have produced 31
    December 2024 to 1 January 2025 - two days, from a query plainly meaning two
    whole years - so both are re-resolved against their new side.
    """
    q = parse_query("after:2025 before:2024", today=TODAY)
    assert q.after == date(2024, 1, 1)
    assert q.before == date(2025, 12, 31)
    # The whole point: a reversed range is the same search as the right one.
    correct = parse_query("after:2024 before:2025", today=TODAY)
    assert (q.after, q.before) == (correct.after, correct.before)


# --- `date:` - one period, or a range of them (order "dates" §1a) ----------

@pytest.mark.parametrize(
    "raw,after,before",
    [
        # A whole period: the same edges `after:`/`before:` give a partial date.
        ("date:2017", date(2017, 1, 1), date(2017, 12, 31)),
        ("date:2017-03", date(2017, 3, 1), date(2017, 3, 31)),
        ("date:2016-02", date(2016, 2, 1), date(2016, 2, 29)),
        ("date:2017-03-14", date(2017, 3, 14), date(2017, 3, 14)),
        ("date:14-03-2017", date(2017, 3, 14), date(2017, 3, 14)),
        # Ranges, each end resolved against its own side.
        ("date:2017-01-01..2017-06-30", date(2017, 1, 1), date(2017, 6, 30)),
        ("date:2017-03..2017-06", date(2017, 3, 1), date(2017, 6, 30)),
        ("date:2016..2017", date(2016, 1, 1), date(2017, 12, 31)),
        # Either end open.
        ("date:..2017", None, date(2017, 12, 31)),
        ("date:2017..", date(2017, 1, 1), None),
        # Relatives: "since then", except the two that already name one day.
        ("date:30d", date(2026, 7, 25), None),
        ("date:today", TODAY, TODAY),
        ("date:yesterday", date(2026, 8, 23), date(2026, 8, 23)),
        ("date:30d..7d", date(2026, 7, 25), date(2026, 8, 17)),
        # The slash form reaches the same fields.
        ("/date 2017-03", date(2017, 3, 1), date(2017, 3, 31)),
    ],
)
def test_date_operator_forms(raw: str, after, before) -> None:
    from app.search.commands import expand_slashes

    q = parse_query(expand_slashes(raw), today=TODAY)
    assert (q.after, q.before) == (after, before)
    assert q.unknown_operators == ()
    assert q.terms == ()                    # nothing leaked into the words


def test_date_is_the_same_query_as_the_two_operators_it_stands_for() -> None:
    """The order's own promise: `date:` sets the fields `after:`/`before:`
    set, so filtering, the sent-date rule and the CLI follow for free."""
    ranged = parse_query("date:2017-03..2017-06 pump", today=TODAY)
    spelled = parse_query("after:2017-03 before:2017-06 pump", today=TODAY)
    assert (ranged.after, ranged.before, ranged.terms) == (
        spelled.after, spelled.before, spelled.terms)
    assert ranged.has_filters


def test_a_reversed_date_range_covers_both_periods_whole() -> None:
    q = parse_query("date:2018..2017", today=TODAY)
    assert (q.after, q.before) == (date(2017, 1, 1), date(2018, 12, 31))


# --- times of day (order "dates" §1b) ---------------------------------------

@pytest.mark.parametrize(
    "raw,after,before",
    [
        # `after:` takes the first moment of the minute, `before:` the last -
        # the same rule a partial date follows, one step finer.
        ("after:2017-03-01T10:00", datetime(2017, 3, 1, 10, 0), None),
        ("before:2017-03-01T10:00", None, datetime(2017, 3, 1, 10, 0, 59, 999999)),
        ("after:2017-03-01T10:00:30", datetime(2017, 3, 1, 10, 0, 30), None),
        ("before:2017-03-01T10:00:30", None, datetime(2017, 3, 1, 10, 0, 30, 999999)),
        ("after:2017-03-01T9:05", datetime(2017, 3, 1, 9, 5), None),
        ("after:2017-03-01t10:00", datetime(2017, 3, 1, 10, 0), None),
        # Quoted, so the space survives the tokeniser.
        ('after:"2017-03-01 10:00"', datetime(2017, 3, 1, 10, 0), None),
        ('before:"2017-03-01 10:00"', None, datetime(2017, 3, 1, 10, 0, 59, 999999)),
        ('/before "2017-03-01 10:00"', None, datetime(2017, 3, 1, 10, 0, 59, 999999)),
        ("/after 2017-03-01T10:00", datetime(2017, 3, 1, 10, 0), None),
        # `date:` with a time is that minute, and a range of times is a range.
        ("date:2017-03-01T10:00", datetime(2017, 3, 1, 10, 0),
         datetime(2017, 3, 1, 10, 0, 59, 999999)),
        ("date:2017-03-01T09:00..2017-03-01T17:30", datetime(2017, 3, 1, 9, 0),
         datetime(2017, 3, 1, 17, 30, 59, 999999)),
        ('date:"2017-03-01 09:00..2017-03-01 17:30"', datetime(2017, 3, 1, 9, 0),
         datetime(2017, 3, 1, 17, 30, 59, 999999)),
        # A time on one end, a whole day on the other.
        ("date:2017-03-01T09:00..2017-03-02", datetime(2017, 3, 1, 9, 0),
         date(2017, 3, 2)),
    ],
)
def test_time_of_day_forms(raw: str, after, before) -> None:
    from app.search.commands import expand_slashes

    q = parse_query(expand_slashes(raw), today=TODAY)
    assert (q.after, q.before) == (after, before)
    assert q.unknown_operators == () and q.terms == ()


def test_a_date_without_a_time_is_still_a_whole_day() -> None:
    """§1b's other half: nothing about a date-only value changed."""
    q = parse_query("after:2017-03-01 before:2017-03-01", today=TODAY)
    assert (q.after, q.before) == (date(2017, 3, 1), date(2017, 3, 1))
    assert type(q.after) is date and type(q.before) is date


def test_a_range_mixing_a_time_and_a_day_is_ordered_without_raising() -> None:
    """Python refuses to compare a `datetime` with a `date`, and the swap for
    a reversed range compares them - `parse_query` must never raise."""
    q = parse_query("after:2017-03-01T10:00 before:2017-02", today=TODAY)
    assert (q.after, q.before) == (date(2017, 2, 1), datetime(2017, 3, 1, 10, 0, 59, 999999))
    ordered = parse_query("after:2017-03-01 before:2017-03-01T10:00", today=TODAY)
    assert ordered.after == date(2017, 3, 1)


@pytest.mark.parametrize("raw", [
    "after:2017-03-01T25:00", "after:2017-03-01T10:60", "after:2017-02-30T10:00",
    "after:2017-03T10:00", "before:2017T10:00", "date:2017-03-01T10",
])
def test_a_time_that_is_not_on_the_clock_is_reported(raw: str) -> None:
    q = parse_query(f"{raw} report", today=TODAY)
    assert (q.after, q.before) == (None, None)
    assert q.unknown_operators == (raw,)


def test_a_time_reaches_the_sql_as_that_moment_on_the_local_clock() -> None:
    """`epoch_ns` used `datetime.combine`, which takes a `datetime` as a
    plain date and drops the time. The moment has to survive to the SQL, on
    the same local clock a whole day is read on."""
    from app.storage.filters import epoch_ns, file_filter_sql

    moment = datetime(2017, 3, 1, 10, 0)
    assert epoch_ns(moment) == int(moment.timestamp() * 1_000_000_000)
    assert epoch_ns(date(2017, 3, 1)) == int(datetime(2017, 3, 1).timestamp() * 1_000_000_000)
    _where, params = file_filter_sql(parse_query("date:2017-03-01T10:00", today=TODAY))
    start, end = params[0], params[1]
    assert start == int(moment.timestamp() * 1_000_000_000)
    assert end - start == 59_999_999_000                  # the minute, inclusive


# --- a mistyped date says what was wrong and what would work (§1d) ----------

@pytest.mark.parametrize("raw,message", [
    # The order's own example, word for word where it gave the words.
    ("date:2017-13", "date:2017-13 isn't a date — there is no month 13. "
                     "Try date:2017-12 or date:2017-01..2017-06"),
    ("date:2017-00", "date:2017-00 isn't a date — there is no month 0. "
                     "Try date:2017-01 or date:2017-01..2017-06"),
    ("date:2017-02-30", "date:2017-02-30 isn't a date — February 2017 has 28 days. "
                        "Try date:2017-02-28 or date:2017-02"),
    ("before:2016-02-31", "before:2016-02-31 isn't a date — February 2016 has 29 days. "
                          "Try before:2016-02-29 or before:2016-02"),
    ("after:2017-13-05", "after:2017-13-05 isn't a date — there is no month 13. "
                         "Try after:2017-12-05 or after:2017"),
    ("after:2017-03-01T25:00", "after:2017-03-01T25:00 has a time that isn't on the clock "
                               "— try after:2017-03-01T10:00; hours run from 00 to 23, "
                               "minutes from 00 to 59"),
    ("date:..", "date:.. has no dates in it — try date:2017-01..2017-06, "
                "or leave one side open: date:2017.."),
    ("date:2017..2017-13", "date:2017..2017-13 isn't a date — there is no month 13. "
                           "Try date:2017-12 or date:2017-01..2017-06"),
    ("after:2017-01..2017-06", "after:2017-01..2017-06 is two dates, and after: takes one "
                               "— for a range, try date:2017-01..2017-06"),
    ("date:soon", "date:soon isn't a date Leasha can read — try date:2017, "
                  "date:2017-03, date:2017-03-14 or a range, date:2017-01..2017-06"),
    ("after:nextthursday", "after:nextthursday isn't a date Leasha can read — try "
                           "after:2017-03-14, after:2017, after:2017-03-14T10:00 or after:30d"),
    ('before:"2017-03-01 25:00"', 'before:"2017-03-01 25:00" has a time that isn\'t on the '
                                  "clock — try before:2017-03-01T10:00; hours run from "
                                  "00 to 23, minutes from 00 to 59"),
])
def test_a_mistyped_date_says_what_was_wrong_and_what_would_work(raw: str, message: str) -> None:
    q = parse_query(f"report {raw}", today=TODAY)
    assert q.date_problems == (message,)
    # Still reported the old way too, for everything that reads it: the CLI's
    # "(ignored: ...)", `translate`'s rejection, the engine's JSON.
    assert q.unknown_operators == (raw,)
    assert q.terms == ("report",)


def test_every_suggestion_a_problem_makes_is_itself_a_date_that_parses() -> None:
    """A suggestion that does not work is a second wrong answer."""
    import re

    for raw in ("date:2017-13", "date:2017-02-30", "after:2017-13-05",
                "after:2017-03-01T25:00", "date:..", "after:2017-01..2017-06", "date:soon"):
        (problem,) = parse_query(raw, today=TODAY).date_problems
        for suggestion in re.findall(r"\b(?:date|after|before):\S+", problem.split("—", 1)[1]):
            suggestion = suggestion.rstrip(",;.") if not suggestion.endswith("..") else suggestion
            q = parse_query(suggestion, today=TODAY)
            assert q.unknown_operators == (), f"{raw} suggested {suggestion}, which does not parse"


def test_a_good_date_or_a_negation_makes_no_problem() -> None:
    """`-date:2017` is reported as not understood, as `-after:` always was -
    it is not a mistyped date, and a date sentence about it would mislead."""
    assert parse_query("date:2017 after:2016 before:2018", today=TODAY).date_problems == ()
    assert parse_query("-date:2017", today=TODAY).date_problems == ()


def test_an_unreadable_date_value_is_reported_not_searched_for() -> None:
    """It used to be no operator at all, so `date:2017-13` became the search
    terms `date` and `2017-13` and the filter silently did nothing."""
    for raw in ("date:2017-13", "date:..", "date:soon", "-date:2017"):
        q = parse_query(f"{raw} report", today=TODAY)
        assert (q.after, q.before) == (None, None), raw
        assert q.unknown_operators == (raw,), raw
        assert q.terms == ("report",), raw


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
# hypothesis (order 0m §2a) - properties over generated queries rather than
# hand-picked examples. `parse_query` has no renderer to round-trip a
# `ParsedQuery` back through, so the round-trip property here is driven the
# other way: build a query string from a known filter and confirm the exact
# same filter comes back out, for every shape hypothesis can generate.
# ---------------------------------------------------------------------------

#: Lower-case ASCII words only - `parse_query`'s tokeniser has its own rules
#: for punctuation and whitespace (`test_intra_word_punctuation_survives`
#: above), which is a separate, already-covered concern from what these
#: properties are checking.
_SAFE_WORD = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122),
    min_size=1, max_size=12)


@given(st.text(max_size=500))
def test_any_generated_string_parses_without_raising(raw: str) -> None:
    """The acceptance line at the top of this file: malformed input returns
    a `ParsedQuery` or a clean `AppError` upstream - never a crash here."""
    parse_query(raw, today=TODAY)


@given(st.text(min_size=0, max_size=MAX_QUERY_CHARS + 200))
def test_arbitrarily_long_input_still_does_not_raise(raw: str) -> None:
    """Past `MAX_QUERY_CHARS` the parser truncates or caps rather than
    raising - covered at one example above; this is the same property
    fuzzed across the boundary itself."""
    parse_query(raw, today=TODAY)


@given(_SAFE_WORD)
def test_a_bare_negated_word_is_excluded_not_a_term(word: str) -> None:
    """`-word` -> `excluded`, never `terms` - for any word, not just the one
    example already in this file."""
    parsed = parse_query(f"-{word}", today=TODAY)
    assert word not in parsed.terms
    assert word in parsed.excluded


@given(_SAFE_WORD, _SAFE_WORD)
def test_a_quoted_phrase_survives_as_one_phrase_not_two_terms(a: str, b: str) -> None:
    parsed = parse_query(f'"{a} {b}"', today=TODAY)
    assert f"{a} {b}" in parsed.phrases
    assert parsed.terms == ()


@given(st.sampled_from(["pdf", "docx", "jpg", "png", "txt", "xlsx"]))
def test_a_type_filter_round_trips_into_ext(ext: str) -> None:
    assert parse_query(f"type:{ext}", today=TODAY).ext == (ext,)


@given(st.sampled_from(["pdf", "docx", "jpg", "png", "txt", "xlsx"]))
def test_a_negated_type_filter_round_trips_into_not_ext_only(ext: str) -> None:
    """The M4 family: a negated operator used to be read as its opposite -
    the minus dropped as punctuation before the operator was matched, so
    `-type:pdf` returned only PDFs. Fuzzed here across every extension this
    test file already trusts, not just the one regression example."""
    parsed = parse_query(f"-type:{ext}", today=TODAY)
    assert parsed.not_ext == (ext,)
    assert parsed.ext == ()


@given(_SAFE_WORD)
def test_a_bare_word_and_its_quoted_form_land_in_the_matching_field(word: str) -> None:
    bare = parse_query(word, today=TODAY)
    quoted = parse_query(f'"{word}"', today=TODAY)
    assert word in bare.terms
    assert word in quoted.phrases


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
