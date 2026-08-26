r"""camelCase search, in both directions.

Layer: L0/L4

**Most of this problem did not exist, and measuring first is what kept the fix
small.** FTS5's `unicode61` tokenizer already splits on every non-alphanumeric
character, so `get_user_name`, `reset-password` and `Order.Service` were always
three tokens each and always findable word by word. Verified against sqlite
before a line was written:

    query "user"      matches "get_user_name"
    query "user"      does NOT match "getUserName"
    query "password"  does NOT match "ResetPasswordHandler"

So the whole gap was capitalisation, and it needed fixing twice: once in the
index, so `password` reaches `ResetPasswordHandler`, and once in the query, so
`getUserName` reaches `get_user_name`. Neither half is sufficient alone.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.core.identifiers import (
    MIN_PART_LENGTH,
    expand_term,
    has_case_boundary,
    split_identifier,
    symbol_tokens,
)


# --- splitting --------------------------------------------------------------

@pytest.mark.parametrize("word,parts", [
    ("getUserName", ["get", "User", "Name"]),
    ("ResetPasswordHandler", ["Reset", "Password", "Handler"]),
    ("userID", ["user", "ID"]),
])
def test_camel_and_pascal_split(word, parts):
    assert split_identifier(word) == parts


def test_an_acronym_keeps_its_run():
    """`XMLHttpRequest` is three words, not `X`, `M`, `L`. The rule is that a
    run of capitals ends one letter early when a lowercase follows."""
    assert split_identifier("XMLHttpRequest") == ["XML", "Http", "Request"]
    assert split_identifier("IOError") == ["IO", "Error"]


def test_digits_are_a_boundary_and_are_kept():
    """`utf8` and `sha256` are searched for as words. Dropping the number loses
    more than it saves."""
    assert split_identifier("utf8") == ["utf", "8"]
    assert split_identifier("parseJSON2Data") == ["parse", "JSON", "2", "Data"]


@pytest.mark.parametrize("word", ["password", "HTTP", "get_user_name", "reset-password"])
def test_a_word_with_no_case_boundary_is_left_alone(word):
    """**This is what stops the index doubling.**

    An ordinary word contributes nothing, and the punctuated spellings are
    already split by the tokenizer - adding them again would be a second copy
    of the text for no gain. At 600GB that is the difference between a feature
    and a regret.
    """
    assert split_identifier(word) == []
    assert has_case_boundary(word) is False


# --- what gets indexed ------------------------------------------------------

def test_prose_costs_nothing():
    """The common case, and it must be free."""
    assert symbol_tokens("The quick brown fox jumped over the lazy dog.") == ""


def test_code_yields_its_parts():
    tokens = symbol_tokens("public class ResetPasswordHandler { void getUserName() {} }")
    assert "Password" in tokens and "Handler" in tokens and "User" in tokens


def test_repeats_are_stored_once():
    """A file naming the same class forty times should not store it forty
    times."""
    tokens = symbol_tokens("getUserName getUserName getUserName")
    assert tokens.split().count("User") == 1


def test_single_letters_are_dropped():
    """`a` matches an enormous number of chunks and helps nobody."""
    assert "a" not in symbol_tokens("aBc").split()
    assert MIN_PART_LENGTH == 2


def test_a_generated_file_cannot_double_the_index():
    """A minified bundle is one long line of camelCase, where the split forms
    approach the size of the text. Truncated rather than allowed to double the
    FTS index across a terabyte."""
    huge = " ".join(f"someLongIdentifier{n}" for n in range(5_000))
    assert len(symbol_tokens(huge)) <= 4_000


# --- what gets searched -----------------------------------------------------

def test_a_compound_query_matches_both_spellings():
    assert expand_term("getUserName") == '("getUserName" OR ("get" AND "User" AND "Name"))'


def test_an_ordinary_word_is_unchanged_and_still_quoted():
    """Nearly every search takes this path and must not get slower or stranger."""
    assert expand_term("password") == '"password"'


def test_fts_syntax_in_a_term_stays_harmless():
    """An unquoted FTS5 term is parsed as an expression, so a word that happens
    to be `OR` or contains a quote must not change the query's meaning."""
    assert expand_term('say"hi') == '"say""hi"'


def test_a_prefix_search_is_left_alone():
    """`getUser*` already matches `getUserName`. Expanding it would turn one
    cheap prefix scan into three."""
    from app.search.query import _fts_quote

    assert _fts_quote("getUser*") == '"getUser"*'


# --- the two halves together, against real sqlite ---------------------------

@pytest.fixture()
def index() -> sqlite3.Connection:
    """The real schema shape: external-content FTS with the `symbols` column."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE chunks(id INTEGER PRIMARY KEY, text TEXT, "
        "symbols TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE chunks_fts USING fts5("
        "text, symbols, content='chunks', content_rowid='id', "
        "tokenize='porter unicode61')"
    )
    conn.execute(
        "CREATE TRIGGER ai AFTER INSERT ON chunks BEGIN "
        "INSERT INTO chunks_fts(rowid, text, symbols) "
        "VALUES(new.id, new.text, new.symbols); END"
    )
    for rowid, text in {
        1: "public class ResetPasswordHandler {}",
        2: "def get_user_name(): pass",
        3: "the password was reset yesterday",
    }.items():
        conn.execute(
            "INSERT INTO chunks(id, text, symbols) VALUES(?, ?, ?)",
            (rowid, text, symbol_tokens(text)),
        )
    return conn


def matches(conn: sqlite3.Connection, term: str) -> list[int]:
    from app.search.query import _fts_quote

    return [
        row[0] for row in conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (_fts_quote(term),)
        )
    ]


def test_a_plain_word_now_reaches_inside_a_pascal_case_name(index):
    """**The headline.** Before this, no search for `password` could ever find
    `ResetPasswordHandler`."""
    assert 1 in matches(index, "password")
    assert 1 in matches(index, "handler")


def test_a_camel_case_query_finds_the_underscored_spelling(index):
    """The other direction: `get_user_name` contains no token `getusername`."""
    assert matches(index, "getUserName") == [2]


def test_prose_still_matches_the_way_it_always_did(index):
    """Row 3 has no identifiers at all. The change must be invisible to it."""
    assert 3 in matches(index, "password")
    assert matches(index, "yesterday") == [3]


def test_the_whole_identifier_still_matches_itself(index):
    """Splitting must not cost the exact search."""
    assert matches(index, "ResetPasswordHandler") == [1]


# --- the migration ----------------------------------------------------------

def test_the_migration_is_registered_and_current():
    from app.storage.migrations import CURRENT_VERSION, MIGRATIONS

    assert 7 in MIGRATIONS
    assert CURRENT_VERSION >= 7


def test_the_migration_runs_twice_without_complaint(tmp_path):
    """**A migration has to survive being re-run.**

    v7 drops and recreates `chunks_fts`, which is not additive - so unlike the
    migrations before it, a half-applied run is imaginable. Running it twice is
    the cheapest proof that it is not.
    """
    import sqlite3

    from app.storage.migrations import apply_migrations, read_version

    conn = sqlite3.connect(tmp_path / "k.db")
    apply_migrations(conn)
    first = read_version(conn)
    apply_migrations(conn)
    assert read_version(conn) == first


def test_upgrading_needs_no_re_extraction(tmp_path):
    """The point of doing this in a migration rather than an index run.

    `rebuild` repopulates the FTS table from `chunks`, so an existing index
    gains identifier search without re-reading one file from disk - which at
    600GB is the difference between a five-minute upgrade and a five-day one.
    """
    import inspect

    from app.storage import migrations

    # **Both halves, because the backfill moved out of the migration.**
    # `_v7_identifier_tokens` still owns the FTS rebuild; the row-by-row fill
    # is now `_backfill_symbols`, which does it in batches with per-batch
    # commits rather than materialising the whole corpus - see
    # `test_review_2026_08_26.test_the_identifier_backfill_reads_in_batches`.
    #
    # The rule this test states is unchanged and is the one that matters:
    # upgrading rewrites the `symbols` column and rebuilds the FTS index from
    # `chunks`, and at no point re-reads a file from disk.
    source = (inspect.getsource(migrations._v7_identifier_tokens)
              + inspect.getsource(migrations._backfill_symbols))
    assert "'rebuild'" in source
    assert "UPDATE chunks SET symbols" in source
