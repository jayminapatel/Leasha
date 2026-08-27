r"""The typo that costs somebody their homework.

Layer: L4. Two halves - the arithmetic, which needs nothing but a list, and the
engine's use of it, which needs a real index because the whole feature is
defined by what the index does *not* contain.

**The finding that shaped these tests.** The work order's own example,
"volcanoe", never reaches this code: FTS5's porter stemmer already resolves it
to "volcano", so it is not an unmatched term and there is nothing to correct.
That is the system working - one layer earlier than the order assumed - and it
is why every end-to-end case here uses a typo stemming genuinely cannot fix
("volcanno", "homwork"). Testing with "volcanoe" would have passed for the
wrong reason on the first two surfaces and silently proved nothing.
"""

from __future__ import annotations

import pytest

from app.search.engine import NOTICE_SPELLING, SearchEngine
from app.search.policy import CODE, FILES, SEARCH, for_surface
from app.search.spelling import (
    MIN_LENGTH,
    Suggestion,
    best_match,
    edit_distance,
    suggest,
)

# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------

@pytest.mark.parametrize("left,right,expected", [
    ("volcano", "volcano", 0),
    ("volcanno", "volcano", 1),        # a doubled letter
    ("homwork", "homework", 1),        # a dropped letter
    ("teh", "the", 2),                 # a transposition costs two, not one
    ("cat", "dog", 3),                 # nowhere near, and says so as ceiling+1
])
def test_edit_distance_counts_the_edits(left, right, expected):
    assert edit_distance(left, right) == min(expected, 3)


def test_a_length_gap_wider_than_the_ceiling_is_decided_without_looking():
    """**The cheap refusal that makes this affordable on a keystroke.**

    Two hundred candidates per typed word, and most of them differ in length
    by more than two. Deciding those by subtraction rather than by building a
    matrix is the difference between a suggestion and a stutter.
    """
    assert edit_distance("cat", "catastrophe", ceiling=2) == 3


def test_the_ceiling_caps_what_is_reported():
    """Anything past the ceiling reports `ceiling + 1` and no more. The caller
    only ever asks "is this close enough", so the true distance is wasted
    work."""
    assert edit_distance("volcano", "elephant", ceiling=2) == 3
    assert edit_distance("volcano", "elephant", ceiling=1) == 2


def test_a_short_word_is_never_corrected():
    """**"cat" and "cut" are different words, not a typo.**

    At three letters nearly every string is one edit from several real ones,
    so a correction is a coin toss presented as help.
    """
    assert best_match("cat", ["cut", "cot", "car"]) is None
    assert len("cat") < MIN_LENGTH


def test_a_long_word_may_be_two_edits_out_and_a_short_one_may_not():
    """Two edits is a proportion, not a count. On a seven-letter word it is
    most of the word; on a twelve-letter one it is a slip."""
    assert best_match("mesuremnt", ["measurement"]) is not None   # 9 letters
    assert best_match("blue", ["glue"]) is not None               # one edit
    assert best_match("blue", ["glued"]) is None                  # two, too short


def test_ties_go_to_the_word_the_corpus_uses_more():
    """**Candidates arrive commonest-first and that order decides ties.**

    Two words one edit away are not equally likely: the one written more often
    in these documents is what was meant, and preferring it costs nothing
    because the store already returns them in that order.

    Both of these are exactly one edit from "helo" - an insertion and a
    substitution - so the only thing separating them is the order they came
    back in.
    """
    found = best_match("helo", ["hello", "held"])
    assert found is not None and found.suggestion == "hello"


def test_closeness_beats_order():
    """Frequency only decides a tie. **A word one edit away wins over a
    commoner word two edits away**, or the feature would confidently correct
    towards whatever the corpus talks about most.

    "recieve" is the case in miniature: the familiar answer is "receive", but
    that is two edits (the transposition), while "relieve" is one. Distance is
    what this function knows, so distance is what it uses - and it is why the
    engine above only ever calls it on a word that matched nothing.
    """
    found = best_match("recieve", ["receive", "relieve"])
    assert found is not None and found.suggestion == "relieve"
    assert found.distance == 1


def test_a_word_that_is_itself_in_the_vocabulary_is_refused():
    """Being asked to correct a real word means an upstream mistake - only
    unmatched words should get here. Refusing keeps that visible instead of
    'correcting' it to itself."""
    assert best_match("volcano", ["volcano", "volcanoes"]) is None


def test_suggest_looks_one_letter_shorter_when_the_prefix_itself_is_wrong():
    """**A typo *at* the third letter breaks the three-letter prefix.**

    "voclano" and "volcano" share only "vo", so the three-letter fetch returns
    a set the real word is not in. The second, shorter pass is what makes the
    transposed-third-letter case work at all.
    """
    vocabulary = {"voc%": ["vocabulary"], "vo%": ["volcano"]}
    found = suggest("voclano", lambda pattern: vocabulary.get(pattern, []))
    assert found is not None and found.suggestion == "volcano"


def test_a_vocabulary_that_cannot_be_read_is_a_search_without_a_suggestion():
    """**Never raises.** This runs while somebody is typing; a failure here
    must cost them a suggestion, never their results."""
    def broken(_pattern):
        raise RuntimeError("the vocabulary table is not there")

    assert suggest("volcanno", broken) is None


def test_the_two_sentences_say_different_things():
    """Past tense when it was done, a question when it was not. "Did you
    mean…" over results already on screen reads as doubt about them."""
    found = Suggestion("volcanno", "volcano", 1)
    assert found.sentence() == "also looked for 'volcano'"
    assert found.question() == "Did you mean 'volcano'?"


# --------------------------------------------------------------------------
# End to end, against a real index
# --------------------------------------------------------------------------

class _NoVectors:
    def search(self, *_args, **_kwargs):
        return []


class _NoModel:
    def embed(self, _text):
        raise RuntimeError("no embedding model in this test")

    def embed_all(self, _texts):
        raise RuntimeError("no embedding model in this test")


@pytest.fixture()
def engine(tmp_path):
    """A one-document index. Keyword-only, because spelling has nothing to do
    with the vector half and a model here would only be slow."""
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(tmp_path / "index.db").connect()
    file_id = store.upsert_file(
        "C:/work/essay.txt", parent_dir="C:/work", ext="txt",
        size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file",
    )
    store.replace_chunks(file_id, [{
        "ordinal": 0,
        "text": "My homework about volcanoes. A volcano erupts when magma rises.",
    }])
    built = SearchEngine(store, _NoVectors(), _NoModel())
    yield built
    built.close()


def _notice(response):
    return next((n.message for n in response.notices
                 if n.code == NOTICE_SPELLING), None)


def test_stemming_already_handles_the_orders_own_example(engine):
    """**Recorded because it reverses the order's premise, not to be clever.**

    §2a is written around a child typing "volcanoe" and getting nothing. She
    does not: FTS5 stems it to "volcano" and finds the essay. The correction
    below this line is therefore narrower than the order assumed - and more
    clearly worth having, because everything that reaches it is a genuine miss
    that stemming has already failed on.
    """
    from app.search import keyword

    assert keyword.unmatched_terms(engine.store, ("volcanoe",)) == ()
    assert keyword.unmatched_terms(engine.store, ("volcanoes",)) == ()
    assert keyword.unmatched_terms(engine.store, ("volcanno",)) == ("volcanno",)


@pytest.mark.parametrize("typed,corrected", [
    ("volcanno", "volcano"),
    ("homwork", "homework"),
])
def test_auto_searches_for_the_real_word_and_says_so(engine, typed, corrected):
    """The universal surface. **Changing what somebody typed is only legal
    because the change is on screen.**"""
    response = engine.search(typed, policy=for_surface(SEARCH), use_cache=False)
    assert response.parsed.terms == (corrected,)
    assert _notice(response) == f"also looked for '{corrected}'"


def test_suggest_leaves_the_query_exactly_as_typed(engine):
    """Files offers the correction and does nothing. Somebody looking for a
    filename may well have typed it right."""
    response = engine.search("volcanno", policy=for_surface(FILES),
                             use_cache=False)
    assert response.parsed.terms == ("volcanno",)
    assert _notice(response) == "Did you mean 'volcano'?"


def test_the_code_tab_neither_corrects_nor_asks(engine):
    """**`recieve_handler` may be precisely what is in the codebase.**

    Silently searching for something else hides the very line somebody is
    looking for, and a chip suggesting the "correct" spelling of an identifier
    is noise on every query.
    """
    response = engine.search("volcanno", policy=for_surface(CODE),
                             use_cache=False)
    assert response.parsed.terms == ("volcanno",)
    assert _notice(response) is None
    assert response.spelling is None


def test_a_word_the_index_contains_is_never_second_guessed(engine):
    """**The safety rule the whole feature rests on.** Only a word matching
    nothing is corrected, so the worst case is a query that was going to
    return nothing returning something."""
    response = engine.search("volcano", policy=for_surface(SEARCH),
                             use_cache=False)
    assert response.parsed.terms == ("volcano",)
    assert _notice(response) is None


def test_a_query_where_every_word_is_unknown_is_left_alone(engine):
    """Two unknown words is somebody searching a corpus that has nothing to do
    with their question. Correcting each in turn manufactures a query nobody
    typed."""
    response = engine.search("zzzqqq wwwxxx", policy=for_surface(SEARCH),
                             use_cache=False)
    assert response.parsed.terms == ("zzzqqq", "wwwxxx")
    assert _notice(response) is None


def test_nothing_close_enough_means_no_suggestion(engine):
    """A word nowhere near anything indexed gets the unmatched-terms notice it
    always got, and no invented correction."""
    response = engine.search("zzzqqq", policy=for_surface(SEARCH),
                             use_cache=False)
    assert response.parsed.terms == ("zzzqqq",)
    assert _notice(response) is None
