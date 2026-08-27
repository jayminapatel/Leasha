r"""£40,000 and 40000 and 40k are the same amount.

Layer: L4. §5c said measure first and build only if the measurement showed a
gap. It did, completely:

    document text                     indexed as        found by
    "total 40000 for the site"        40000             40000
    "awarded at 40,000 pounds"        40 · 000          40,000
    "we agreed £40k for the package"  40k               40k, £40k
    "Invoice total: 40,000.00 GBP"    40 · 000 · 00     40,000

**No query found more than two of the four, and `40000.00` found nothing at
all** - not even the document containing its exact characters, because
`40,000.00` is three tokens and `40000.00` is one.
`test_every_way_of_writing_it_finds_every_document` is that table, closed.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.search.numbers import (
    MAX_VALUE, MIN_VALUE, expansions_for, quantity, variants,
)


# --------------------------------------------------------------------------
# Reading a quantity
# --------------------------------------------------------------------------

@pytest.mark.parametrize("word,expected", [
    ("40000", 40_000),
    ("40,000", 40_000),
    ("40k", 40_000),
    ("£40k", 40_000),
    ("$40,000", 40_000),
    ("€40000", 40_000),
    ("40,000.00", 40_000),
    ("40000.50", 40_000),
    ("1.5m", 1_500_000),
    ("2m", 2_000_000),
    ("3bn", 3_000_000_000),
])
def test_the_amounts_people_write(word, expected):
    assert quantity(word) == expected


def test_decimals_belong_to_the_multiplier():
    """**`1.5m` is one and a half million.** Dropping the decimals before
    applying the suffix reads it as one million - a wrong answer that looks
    like a right one, and the first version did exactly that."""
    assert quantity("1.5m") == 1_500_000
    assert quantity("2.25m") == 2_250_000


def test_pence_are_dropped_but_pounds_are_not():
    """They are not what makes two documents the same document, and a search
    for the round figure should still find the exact one."""
    assert quantity("40,000.00") == quantity("40000") == 40_000


@pytest.mark.parametrize("word", [
    "2024", "1999", "2026",
])
def test_a_year_is_not_a_quantity(word):
    """**`2024` must not drag in `2 024`.** In a search box a four-digit
    number in this range is overwhelmingly a year, and nobody has ever written
    one with a thousands separator."""
    assert quantity(word) is None


@pytest.mark.parametrize("word", ["300", "42", "0", "999"])
def test_below_a_thousand_there_is_nothing_to_expand(word):
    """No comma form exists, and "0.3k" is not something anybody writes."""
    assert quantity(word) is None


@pytest.mark.parametrize("word", [
    "", "  ", "volcano", "40k9", "abc123", "1,2,3", "--40000", "40..0",
])
def test_things_that_are_not_amounts(word):
    assert quantity(word) is None


def test_an_identifier_is_left_alone():
    """Above the ceiling a long number is an order reference or a phone
    number, not a quantity, and expanding it produces alternatives nobody
    wrote."""
    assert quantity(str(MAX_VALUE + 1)) is None
    assert quantity(str(MIN_VALUE)) == MIN_VALUE


# --------------------------------------------------------------------------
# The forms it could have been written in
# --------------------------------------------------------------------------

def test_the_three_forms_of_forty_thousand():
    assert variants(40_000) == ("40000", "40 000", "40k")


def test_the_middle_form_is_a_phrase_not_a_word():
    """`40 000` is two adjacent tokens, which is exactly what `40,000` became
    when it was indexed - and the form plain digits could never reach."""
    assert "40 000" in variants(40_000)


def test_a_round_number_gets_its_short_form_and_an_odd_one_does_not():
    """Nobody writes `40123` as `40k`, so offering it would match documents
    about a different amount."""
    assert "40k" in variants(40_000)
    assert not any(form.endswith("k") for form in variants(40_123))


def test_nobody_writes_a_thousand_k():
    """`1000k` widens the query for a string no document contains. Above a
    thousand of a unit, the next unit up is the one people reach for."""
    forms = variants(1_000_000)
    assert "1000k" not in forms
    assert "1m" in forms


def test_a_number_out_of_range_has_no_variants():
    assert variants(500) == ()
    assert variants(MAX_VALUE + 1) == ()


# --------------------------------------------------------------------------
# Reassembling what the parser split
# --------------------------------------------------------------------------

def test_a_single_numeric_term_expands():
    assert dict(expansions_for(("40000",)))["40000"] == (
        "40000", "40 000", "40k")


def test_a_comma_split_by_the_parser_is_put_back_together():
    r"""**`40,000` reaches the engine as `40` and `000`**, because the parser
    splits on punctuation and cannot know this comma was inside a number
    rather than between two of them. A trailing group of exactly three digits
    is the giveaway."""
    found = dict(expansions_for(("40", "000")))
    assert found["40"] == ("40000", "40 000", "40k")


def test_the_absorbed_half_is_not_also_searched_for_alone():
    """`000` left in the query would match every document with a `000` in it -
    which is most of them, once a corpus has any numbers at all."""
    assert dict(expansions_for(("40", "000")))["000"] == ()


def test_two_separate_numbers_stay_separate():
    """`40` beside `12` is two numbers, not forty thousand and twelve. Only a
    run of exactly-three-digit groups is a thousands separator."""
    assert expansions_for(("40", "12")) == ()


def test_words_around_a_number_are_untouched():
    found = dict(expansions_for(("budget", "40", "000", "approved")))
    assert "budget" not in found and "approved" not in found
    assert found["40"] == ("40000", "40 000", "40k")


def test_a_query_with_no_numbers_costs_nothing():
    assert expansions_for(("volcano", "homework", "essay")) == ()


# --------------------------------------------------------------------------
# End to end — the measurement, closed
# --------------------------------------------------------------------------

_CORPUS = {
    "budget.xlsx": "Q3 budget total 40000 for the site works",
    "report.docx": "The contract was awarded at 40,000 pounds after review",
    "email.eml": "We agreed \u00a340k for the whole package, all in",
    "invoice.pdf": "Invoice total: 40,000.00 GBP",
    "unrelated.txt": "The pump station drawings and the valve schedule",
}


@pytest.fixture(scope="module")
def engine():
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "amounts.db").connect()
    for name, text in _CORPUS.items():
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext=name.split(".")[-1],
            size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])

    class _NoVectors:
        def search(self, *_args, **_kwargs):
            return []

    class _NoModel:
        def embed(self, _text):
            raise RuntimeError("no embedding model in this test")

        def embed_all(self, _texts):
            raise RuntimeError("no embedding model in this test")

    built = SearchEngine(store, _NoVectors(), _NoModel())
    yield built
    built.close()


def _found(engine, query):
    response = engine.search(query, use_cache=False)
    return {result.path.rsplit("/", 1)[-1] for result in response.results}


@pytest.mark.parametrize("query", [
    "40000", "40,000", "40k", "\u00a340k", "40000.00",
])
def test_every_way_of_writing_it_finds_every_document(engine, query):
    """**The table in this file's docstring, closed.** Before this, no query
    found more than two of these four and `40000.00` found none."""
    assert _found(engine, query) == {
        "budget.xlsx", "report.docx", "email.eml", "invoice.pdf"}


def test_a_year_still_finds_only_what_says_that_year(engine):
    """The guard that keeps this from being a menace: expanding `2024` into
    `2 024` would put a hit in every document with a 2 and a 24 near each
    other."""
    assert _found(engine, "2024") == set()


def test_a_query_with_no_number_is_unchanged(engine):
    assert _found(engine, "pump station drawings") == {"unrelated.txt"}
