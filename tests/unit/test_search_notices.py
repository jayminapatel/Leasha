"""A search that quietly returns worse results must say so.

Layer: L4

The standing rule, from the owner: **nothing fails silently.** If it cannot do
what it was asked, it says so somewhere a person will see it.

The case that produced the rule:

    16:26:25 WARNING search.engine no vector hits for a query with 60 keyword
    hits - meaning-based search may not be working. Check: app.cli stats

That line had existed all along. It is visible to somebody running from a
console and to nobody else - in the window there was nothing at all, and the
search looked like it had worked. A degraded search that is indistinguishable
from a working one is the failure nobody ever reports.

`keyword_count` and `vector_count` were already on the response for exactly
this, and they were not enough: they are raw numbers, so every caller had to
know the rule that turns them into a conclusion. The judgement now lives in one
place and comes out as a `Notice`, which carries a **code** - the contract is
that the UI never parses a message string to decide anything.
"""

from __future__ import annotations

import math

import pytest

from app.index.embedder import Embedder, l2_normalise
from app.search.engine import (
    NOTICE_NO_VECTORS,
    NOTICE_RERANK_UNAVAILABLE,
    NOTICE_UNMATCHED_TERMS,
    Notice,
    SearchEngine,
    SearchResponse,
    SearchResult,
)
from app.storage.sqlite_store import SqliteStore
from app.storage.vector_store import VectorStore


def _result(rank=1):
    return SearchResult(chunk_id=rank, file_id=rank, path=f"/f{rank}.txt",
                        text="body", score=1.0, rank=rank, sources=(0,))


def codes(response):
    return [notice.code for notice in response.notices]


# --- the contract -----------------------------------------------------------

def test_a_notice_carries_a_code_and_a_message():
    """The UI branches on the code; the wording is free to change."""
    notice = Notice(NOTICE_NO_VECTORS, "anything at all")

    assert notice.code == NOTICE_NO_VECTORS
    assert notice.as_dict() == {"code": NOTICE_NO_VECTORS,
                                "message": "anything at all"}


def test_notices_reach_the_json_output():
    """`--json` is how a script or another tool finds out."""
    response = SearchResponse(
        results=[_result()],
        notices=(Notice(NOTICE_NO_VECTORS, "keyword matches only"),),
    )

    payload = response.as_dict()

    assert payload["notices"] == [
        {"code": NOTICE_NO_VECTORS, "message": "keyword matches only"}]


def test_the_json_lists_notices_before_results():
    """A degradation buried under twenty result objects has been read by nobody."""
    keys = list(SearchResponse(results=[_result()]).as_dict())

    assert keys.index("notices") < keys.index("results")


def test_a_healthy_search_carries_no_notices():
    """Otherwise the signal is noise within a week."""
    assert SearchResponse(results=[_result()]).notices == ()


# --- the judgements, through the real engine --------------------------------
#
# Built against a real store and a real (empty) vector store, because the first
# version of these asserted the *condition* rather than the engine - it
# recomputed `keyword and not vector` in the test and passed whatever the
# engine did. A test that cannot see the bug it was written for still reads as
# coverage, which is how this project lost a day to `--help`.

DIM = 384


def encoder(texts):
    return [l2_normalise([math.sin(abs(hash(t)) % 997 + i) for i in range(DIM)])
            for t in texts]


@pytest.fixture()
def engine(tmp_path):
    """Keyword search works; the vector store is empty. The reported state."""
    store = SqliteStore(tmp_path / "index.db").connect()
    for n in range(8):
        file_id = store.upsert_file(path=f"/docs/report{n}.txt", size_bytes=100,
                                    mtime_ns=n, source_kind="file")
        store.replace_chunks(file_id, [
            {"text": "the northern pump station was commissioned in March"}])
    vectors = VectorStore(tmp_path / "vectors", dim=DIM).connect()
    vectors.ensure_table()

    built = SearchEngine(store, vectors, Embedder(dim=DIM, encoder=encoder))
    yield built
    built.close()
    store.close()
    vectors.close()


def test_a_word_left_out_as_too_common_is_said_on_the_page(engine, monkeypatch):
    """2026-10-04. Past `keyword.SCORED_MATCHES` a word in a tenth of the index
    is left out of the search - which changes the answer, so it is said."""
    from app.search import keyword
    from app.search.engine import NOTICE_LEFT_OUT

    file_id = engine.store.upsert_file(path="/docs/spare.txt", size_bytes=1,
                                       mtime_ns=9, source_kind="file")
    engine.store.replace_chunks(file_id, [{"text": "a spare valve"}])
    monkeypatch.setattr(keyword, "SCORED_MATCHES", 1)

    response = engine.search("pump valve")

    message = next(n.message for n in response.notices if n.code == NOTICE_LEFT_OUT)
    assert "pump" in message and "valve" not in message


def test_a_common_word_is_kept_when_the_other_is_in_nothing(engine, monkeypatch):
    """Leaving `pump` out of `pump petrrabigh` would search for a word in
    nothing: the pumps are found and the missing word is named instead."""
    from app.search import keyword
    from app.search.engine import NOTICE_LEFT_OUT

    monkeypatch.setattr(keyword, "SCORED_MATCHES", 1)

    response = engine.search("pump petrrabigh")

    assert NOTICE_LEFT_OUT not in codes(response)
    assert NOTICE_UNMATCHED_TERMS in codes(response)
    assert response.keyword_count > 0


def test_keyword_hits_with_no_vectors_produces_a_notice(engine):
    """The reported failure, end to end.

    Keyword search finds plenty; the vector store is empty. Before this, the
    only symptom outside a console was results that felt worse than they
    should.
    """
    response = engine.search("pump station")

    assert response.results, "the fixture is wrong - keyword search found nothing"
    assert response.vector_count == 0
    assert NOTICE_NO_VECTORS in codes(response)


def test_the_notice_says_what_still_works_and_how_to_fix_it(engine):
    r"""A warning without an action is a warning somebody has to research.

    **Asked for the technical register explicitly**, because that is the one
    naming `reembed` - and naming a command is exactly what the plain register
    must not do. Since the search-experience order the engine's default is
    plain, so a test that wants the developer's wording has to say so; before
    that it got it by accident, which is why it started failing rather than
    silently checking the wrong thing.
    """
    from app.search.policy import CODE, for_surface

    response = engine.search("pump station", policy=for_surface(CODE))

    message = next(n.message for n in response.notices
                   if n.code == NOTICE_NO_VECTORS)
    assert "keyword" in message.lower(), "it must say what still works"
    assert "reembed" in message, "it must name the remedy"


def test_the_same_notice_in_plain_words_names_no_command(engine):
    """The universal surface's half of the same fact. A child who reads it
    learns that something is off and that nothing is broken - not that the fix
    involves a command line she does not have."""
    response = engine.search("pump station")

    message = next(n.message for n in response.notices
                   if n.code == NOTICE_NO_VECTORS)
    assert "reembed" not in message and "app.cli" not in message
    assert "word matches" in message.lower(), "it still says what still works"


def test_a_query_that_matches_nothing_gets_no_vector_notice(engine):
    """Zero vector hits on a query that found nothing anyway means nothing.

    Reporting it there would train people to ignore the message that matters.
    """
    response = engine.search("zzzznotawordanywhere")

    assert response.keyword_count == 0
    assert NOTICE_NO_VECTORS not in codes(response)


def test_unmatched_terms_are_explained(engine):
    """Terms are ORed, so one word matching nothing silently turns the query
    into a search for its most common words - twenty confident, irrelevant
    results with nothing to say why."""
    response = engine.search("pump petrrabigh")

    assert "petrrabigh" in response.unmatched
    assert NOTICE_UNMATCHED_TERMS in codes(response)
    message = next(n.message for n in response.notices
                   if n.code == NOTICE_UNMATCHED_TERMS)
    assert "petrrabigh" in message


def test_the_notice_names_the_coverage_percent_while_still_embedding(engine):
    r"""Work order 0b §6d: "the existing NOTICE_NO_VECTORS wording extends
    to '...still embedding, N% done'" - the reassuring story, distinct from
    "the vector store looks broken, run reembed."

    `vector_coverage` reads its row count from `self.vectors.count()`
    rather than from the table it searched, so monkeypatching just that one
    number reports genuine partial progress without needing the ANN search
    itself to find anything - it stays a real, empty-table `[]`, exactly
    the precondition this notice already requires.
    """
    from app.search.policy import CODE, for_surface

    engine.vectors.count = lambda: 4              # 4 of 8 fixture chunks

    response = engine.search("pump station", policy=for_surface(CODE))

    message = next(n.message for n in response.notices
                   if n.code == NOTICE_NO_VECTORS)
    assert "50%" in message, "it must name the coverage percentage"
    assert "still embedding" in message.lower()


def test_the_notice_does_not_claim_progress_from_a_wholly_empty_store(engine):
    r"""0% is not "still embedding" - it is the other story this same
    notice already tells, and the two must not collide. A blank vector
    store reads as broken, not as in-progress, and saying "0% done" would
    be a worse message than the one it replaced."""
    from app.search.policy import CODE, for_surface

    response = engine.search("pump station", policy=for_surface(CODE))

    message = next(n.message for n in response.notices
                   if n.code == NOTICE_NO_VECTORS)
    assert "still embedding" not in message.lower()
    assert "reembed" in message


def test_every_notice_code_is_distinct_and_prefixed():
    """A renamed code is a silently-dropped notice - this file's own failure
    mode, one level up."""
    seen = {NOTICE_NO_VECTORS, NOTICE_UNMATCHED_TERMS, NOTICE_RERANK_UNAVAILABLE}

    assert len(seen) == 3
    assert all(code.startswith("NOTICE_") for code in seen)
