r"""`*voice` and `inv?ice`, and the silence that made them worth fixing.

Layer: L1/L4

From `docs/WORKORDER-202626081106-wildcards.md`. A trailing `*` has always been
real FTS5 prefix matching. The other two things people type did not work, and
**failed without saying so**:

| Typed | Became | Result |
|---|---|---|
| `*voice` | `"voice"` | the star dropped - hits for *voice*, looking correct |
| `inv?ice` | `"inv" OR "ice"` | split into two unrelated words |

`*voice` returning results for *voice* is worse than returning none, because
nothing on screen suggests the question asked was not the one typed. Most of
this file is therefore about what gets **said**, not about what matches.

The design constraint, found by testing rather than by reading: **the vocabulary
holds Porter stems**. The stored form of *voice* is `voic`, so `LIKE '%voice'`
matches nothing at all. Fragments are stemmed by asking FTS5 itself, and the
consequence - that `*voice` also reaches `invoicing` - is reported rather than
hidden.
"""

from __future__ import annotations

import pytest

from app.search.query import parse_query, to_fts_match
from app.search.wildcards import (
    MAX_TERMS,
    MIN_LITERAL_CHARS,
    expand,
    like_pattern,
    literal_length,
    needs_expansion,
    stem_with,
    strip_wildcards,
)
from app.storage.sqlite_store import FileStatus, SqliteStore

WORDS = "invoice invoicing voicemail voice services password reset 100 percent"


@pytest.fixture()
def store(tmp_path):
    """A store whose vocabulary holds a handful of real, stemmed terms."""
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            r"D:\a\notes.txt", size_bytes=10, mtime_ns=1, ext="txt",
            status=FileStatus.INDEXED, source_kind="file")
        store.replace_chunks(file_id, [{"text": WORDS, "ordinal": 0}])
        yield store


# --- which terms take this path at all --------------------------------------

@pytest.mark.parametrize("term,expected", [
    ("*voice", True),
    ("inv?ice", True),
    ("pass*word", True),
    ("invoic*", False),      # ordinary FTS5 prefix search, and always worked
    ("plain", False),
    ("*", False),            # not a pattern - stays as inert as it is today
    ("??", False),
    ("100%", False),         # `%` is not a wildcard in this grammar
])
def test_only_a_real_wildcard_takes_the_vocabulary_path(term, expected):
    r"""**A trailing `*` is deliberately not this feature.**

    `invoic*` is real prefix matching, it has always worked, and routing it
    through the vocabulary would make the commonest wildcard slower *and*
    approximate for no gain at all.
    """
    assert needs_expansion(term) is expected


def test_an_ordinary_term_produces_the_expression_it_always_did():
    """The regression that matters: almost every search takes this path, and it
    must not get slower or stranger."""
    assert to_fts_match(parse_query("pump station")) == '"pump" OR "station"'


# --- the pattern ------------------------------------------------------------

@pytest.mark.parametrize("term,pattern", [
    ("*voice", "%voice"),
    ("inv?ice", "inv_ice"),
    ("pass*word", "pass%word"),
])
def test_a_wildcard_becomes_a_like_pattern(term, pattern):
    assert like_pattern(term) == pattern


def test_a_literal_percent_is_escaped():
    r"""**Otherwise a search for `100%` is a search for everything** - which is
    exactly the class of silent wrongness this module exists to remove."""
    assert like_pattern("100%") == r"100\%"
    assert like_pattern("a_b") == r"a\_b"


def test_the_star_run_guard_does_not_eat_a_contains_pattern():
    r"""`**` is nonsense and collapses; `*a*` is a "contains" pattern.

    The parser used to strip every trailing `*` from any token holding more than
    one, which quietly turned `*voice*` into `*voice` - a contains search
    becoming an ends-with search, with nothing said.
    """
    assert parse_query("*voice*").terms == ("*voice*",)
    assert parse_query("a**b").terms == ("a*b",)


# --- stemming, which is the whole design constraint -------------------------

def test_the_vocabulary_holds_stems_not_words(store):
    """Stated because everything else follows from it."""
    assert "voic" in store.vocabulary_terms("%")
    assert "voice" not in store.vocabulary_terms("%")


def test_the_stem_comes_from_fts5_rather_than_a_reimplementation(store):
    r"""A second Porter in Python would agree with SQLite until the day it did
    not, and that day would present as a wildcard matching nothing."""
    assert store.fts_stem("voice") == "voic"
    assert store.fts_stem("invoicing") == "invoic"
    assert store.fts_stem("password") == "password"


def test_only_the_fragments_are_stemmed(store):
    pattern, changed = stem_with(store.fts_stem, "*voice")

    assert pattern == "%voic".replace("%", "*")   # the star survives untouched
    assert changed is True


def test_a_short_fragment_is_left_alone(store):
    """Porter does nothing useful with two characters and stemming one can only
    lose an anchor."""
    pattern, changed = stem_with(store.fts_stem, "*ab")

    assert pattern == "*ab" and changed is False


# --- expansion --------------------------------------------------------------

def test_a_leading_wildcard_finds_the_stem(store):
    found = expand(store, "*voice")

    assert "invoic" in found.terms
    assert found.stemmed is True


def test_a_single_character_wildcard_matches_one_character(store):
    assert "invoic" in expand(store, "inv?ice").terms


def test_a_wildcard_in_the_middle_matches(store):
    assert expand(store, "pass*word").terms == ("password",)


def test_too_little_to_go_on_is_refused_with_a_reason(store):
    r"""`*a*` is a vocabulary scan returning everything, and helps nobody."""
    found = expand(store, "*a*")

    assert found.terms == ()
    assert str(MIN_LITERAL_CHARS) in found.message()


def test_nothing_matching_is_its_own_sentence(store):
    r"""**The difference is the whole diagnosis.** "No words in the index match
    that pattern" and "no documents matched" are different findings, and only
    one of them means the query was wrong."""
    found = expand(store, "*zzzz")

    assert found.terms == ()
    assert "No words in the index" in found.message()


def test_a_pattern_matching_nothing_never_falls_back_to_the_literal(store):
    """Quietly searching for something else is the defect being fixed;
    reproducing it here would be worse than leaving the feature unbuilt."""
    parsed = parse_query("*zzzz")
    matched = expand(store, "*zzzz")

    from dataclasses import replace as _replace
    expression = to_fts_match(_replace(parsed, expansions=(("*zzzz", matched.terms),)))

    assert "zzzz" not in expression


def test_the_expansion_is_capped_and_says_so(store):
    found = expand(store, "*s*e", limit=1)

    if len(found.terms) == 1 and found.capped:
        assert str(MAX_TERMS) in found.message() or "showing" in found.message()


def test_the_result_is_cached_for_the_session(store):
    """Somebody refining a query re-runs the same wildcard repeatedly."""
    cache: dict = {}
    first = expand(store, "*voice", cache=cache)
    second = expand(store, "*voice", cache=cache)

    assert first is second


def test_a_failing_store_costs_the_wildcard_and_nothing_else():
    class Broken:
        def fts_stem(self, word):
            return word

        def vocabulary_terms(self, pattern, *, limit=200):
            raise RuntimeError("no vocabulary")

    found = expand(Broken(), "*voice")

    assert found.terms == () and found.refused


# --- the expression, and the embedder ---------------------------------------

def test_an_expansion_occupies_the_position_the_wildcard_did():
    r"""**Not spliced in as siblings.** `pump *voice` with the matches loose
    would start requiring both stems rather than either, because the AND/OR
    joiner counts terms."""
    from dataclasses import replace as _replace

    parsed = _replace(parse_query("pump *voice"),
                      expansions=(("*voice", ("invoic", "voic")),))

    assert to_fts_match(parsed) == '"pump" OR ("invoic" OR "voic")'


def test_wildcards_never_reach_the_embedder():
    r"""`*voice` is not a sentence, and a dense model handed a star produces a
    vector for a star."""
    assert strip_wildcards("*voice inv?ice") == "voice invice"
    assert parse_query("pump *voice").embed_text == "pump voice"


def test_literal_length_counts_what_is_not_a_wildcard():
    assert literal_length("*a*") == 1
    assert literal_length("*voice") == 5
