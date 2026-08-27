r"""Nothing matched, and the page says what was let go of.

Layer: L4. The arithmetic needs only a parsed query; the engine half needs a
real index, because "found nothing" is a fact about a corpus.

**The order's premise for §2b did not survive measurement, and these tests
encode the replacement.** "Drop the rarest term and re-run" cannot work here:
plain words are joined with `OR`, so zero results from several words means
every one of them missed, and dropping any leaves words that already failed.
`test_plain_words_are_ored_so_there_is_nothing_to_relax` pins that fact, since
it is the entire reason this module drops *instructions* - a phrase, an
explicit `AND`, a filter - rather than words.
"""

from __future__ import annotations

import pytest

from app.search.engine import NOTICE_RELAXED, SearchEngine
from app.search.policy import CODE, SEARCH, for_surface
from app.search.query import parse_query
from app.search.relax import AND, ATTEMPTS, FILTER, PHRASE, candidates


# --------------------------------------------------------------------------
# What gets dropped, and in what order
# --------------------------------------------------------------------------

def test_an_ordinary_query_has_nothing_to_relax():
    """**The common case, and it must cost nothing.**

    No phrase, no explicit AND, no filter: there is no narrowing instruction
    to remove, so the empty page is answered by the spelling suggestion and
    the unmatched-terms notice instead of by a re-run that would return the
    same nothing.
    """
    assert candidates(parse_query("volcano homework essay")) == ()


def test_a_quoted_phrase_becomes_its_words():
    """The commonest way somebody empties their own results page: quotes get
    typed around a whole remembered sentence, and one wrong word matches
    nothing."""
    found = candidates(parse_query('"volcano flavoured bread"'))
    assert found and found[0].kind == PHRASE
    assert found[0].query.phrases == ()
    assert found[0].query.terms == ("volcano", "flavoured", "bread")


def test_relaxing_a_phrase_keeps_the_words_that_were_already_working():
    """`"annual report" budget` relaxes to all three words, not to two. The
    term outside the quotes was never the problem and losing it would make
    the results worse than the relaxation makes them better."""
    found = candidates(parse_query('"annual report" budget'))
    assert found[0].query.terms == ("budget", "annual", "report")


def test_an_explicit_and_is_relaxed_but_only_when_it_was_typed():
    """**The default is already OR**, so there is nothing to widen in an
    ordinary query. This is the one relaxation that contradicts a direct
    instruction, which is exactly why it happens only after that instruction
    has found nothing."""
    typed = candidates(parse_query("volcano AND bread"))
    assert typed and typed[0].kind == AND
    assert typed[0].query.explicit_and is False
    assert candidates(parse_query("volcano bread")) == ()


def test_filters_are_dropped_one_at_a_time():
    """**Dropping all of them answers a question nobody asked.** Somebody who
    typed two filters and sees results from everywhere in every format has
    lost the thread of their own search; one at a time keeps the label a
    sentence."""
    found = candidates(parse_query("volcano type:pdf from:sarah"), limit=5)
    kinds = [(one.kind, one.dropped) for one in found]
    assert ("filter", "the file-type filter") in kinds
    assert ("filter", "the sender filter") in kinds
    first = found[0].query
    assert first.ext == () and first.senders == ("sarah",)


def test_the_file_type_filter_goes_first():
    """It is the one people set by accident - a kind-of-file chip stays on
    from the previous search far more often than a date range does."""
    found = candidates(parse_query("volcano type:pdf after:2023"))
    assert found[0].dropped == "the file-type filter"


def test_a_date_range_is_one_instruction():
    """`after:` and `before:` are two fields and one idea. Dropping half of a
    range leaves a filter nobody typed."""
    found = candidates(parse_query("volcano after:2023 before:2024"))
    assert found[0].kind == FILTER and found[0].dropped == "the date range"
    assert found[0].query.after is None and found[0].query.before is None


def test_asking_for_no_attachments_is_a_filter_that_was_set():
    """**Three states, and `False` is one of them.** `has:no-attachment` is a
    narrowing instruction as much as `has:attachment` is; treating falsiness
    as "unset" would silently refuse to relax it."""
    found = candidates(parse_query("volcano has:no-attachment"))
    assert found and found[0].query.has_attachment is None


def test_what_survives_most_of_the_intent_is_tried_first():
    """Dropping quotes keeps every word typed; dropping a filter discards
    something stated outright. So quotes first."""
    found = candidates(parse_query('"annual report" type:pdf'))
    assert [one.kind for one in found] == [PHRASE, FILTER]


def test_the_number_of_re_runs_is_bounded():
    """An empty page may cost two more passes, not eleven. A query with this
    many narrowing operators belongs to somebody who can read "no results"
    and remove their own filter."""
    crowded = parse_query('"a b" type:pdf from:sarah to:jim path:work after:2023')
    assert len(candidates(crowded)) == ATTEMPTS


def test_relaxing_never_alters_what_was_typed():
    """`ParsedQuery` is frozen and `raw` is preserved, so the query the person
    typed survives every relaxation - which is what lets the page offer them
    their own search back."""
    original = parse_query('"volcano flavoured bread"')
    found = candidates(original)
    assert original.phrases == ("volcano flavoured bread",)
    assert found[0].query.raw == original.raw


def test_each_sentence_says_what_was_not_found_before_what_was():
    """**The question the person is holding is "why isn't my search
    working".** The label answers that first, then explains the list."""
    phrase = candidates(parse_query('"volcano flavoured bread"'))[0]
    assert phrase.sentence().startswith("Nothing contains the exact phrase")
    conjunction = candidates(parse_query("volcano AND bread"))[0]
    assert conjunction.sentence().startswith("Nothing matched all your words")
    filtered = candidates(parse_query("volcano type:pdf"))[0]
    assert filtered.sentence() == (
        "Nothing matched with the file-type filter applied - these ignore it.")


# --------------------------------------------------------------------------
# End to end
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
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(tmp_path / "index.db").connect()
    for name, text in (
        ("essay.txt", "My homework about volcanoes. A volcano erupts when "
                      "magma rises."),
        ("notes.txt", "Shopping list: bread, milk, cheese."),
    ):
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext="txt",
            size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file",
        )
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])
    built = SearchEngine(store, _NoVectors(), _NoModel())
    yield built
    built.close()


def _notice(response):
    return next((n.message for n in response.notices
                 if n.code == NOTICE_RELAXED), None)


def test_plain_words_are_ored_so_there_is_nothing_to_relax(engine):
    r"""**The measurement that replaced the order's premise.**

    §2b was written as "drop the rarest term". Terms are ORed
    (`AND_TERM_LIMIT = 1`, chosen against twenty real sentences), so a
    multi-word query returns something as long as *any* word matches - and
    when it returns nothing, every word missed and there is nothing to drop
    to. Dropping instructions is the version of this item that can work.
    """
    assert parse_query("volcano zzzqqq").fts_match() == '"volcano" OR "zzzqqq"'
    assert len(engine.search("volcano zzzqqq", use_cache=False).results) == 1
    empty = engine.search("zzzqqq wwwxxx", use_cache=False)
    assert empty.results == [] and _notice(empty) is None


def test_a_phrase_that_matches_nothing_finds_its_words(engine):
    response = engine.search('"volcano flavoured bread"',
                             policy=for_surface(SEARCH), use_cache=False)
    assert len(response.results) == 2
    assert _notice(response) == (
        'Nothing contains the exact phrase "volcano flavoured bread" - '
        "these match its words instead.")


def test_a_filter_that_excluded_everything_is_named_and_dropped(engine):
    """The word matched; the filter cut it. **Without the label this looks
    like a filter that does not work** - the person sees .txt files after
    asking for PDFs and concludes the search is broken."""
    response = engine.search("volcano type:pdf", policy=for_surface(SEARCH),
                             use_cache=False)
    assert len(response.results) == 1
    assert _notice(response) == (
        "Nothing matched with the file-type filter applied - these ignore it.")


def test_an_explicit_and_widens_to_or_and_says_so(engine):
    response = engine.search("volcano AND bread", policy=for_surface(SEARCH),
                             use_cache=False)
    assert len(response.results) == 2
    assert _notice(response) == (
        "Nothing matched all your words at once - these match some of them.")


def test_a_query_that_found_something_is_never_relaxed(engine):
    """Relaxation is for an empty page only. A thin result set is still an
    answer to the question that was asked."""
    response = engine.search("volcano magma", policy=for_surface(SEARCH),
                             use_cache=False)
    assert response.results and _notice(response) is None
    assert response.relaxed is None


@pytest.mark.parametrize("query", [
    '"volcano flavoured bread"', "volcano type:pdf", "volcano AND bread",
])
def test_the_code_tab_returns_nothing_rather_than_something_else(engine, query):
    """**`relax_on_empty` is off for Code, and off means off.**

    A developer who asked for an exact phrase and got its words scattered
    across three files has been given noise; the empty result is the true
    answer and they know what to do with it.
    """
    response = engine.search(query, policy=for_surface(CODE), use_cache=False)
    assert response.results == []
    assert _notice(response) is None and response.relaxed is None


def test_the_response_carries_the_query_that_actually_ran(engine):
    """**Everything downstream describes the search that happened** -
    highlighting, the unmatched-terms check, the usage log. `parsed.raw`
    still holds what the person typed, so the page can offer it back."""
    response = engine.search('"volcano flavoured bread"',
                             policy=for_surface(SEARCH), use_cache=False)
    assert response.relaxed is not None
    assert response.parsed.phrases == ()
    assert response.parsed.raw == '"volcano flavoured bread"'
