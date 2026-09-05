r"""Section 3 of the review-remediation order: search correctness and semantics.

Layer: L1 and L4

`docs/WORKORDER-202626082352-review-remediation.md` §3. Every item here is a
**wrong answer that looks like a right one** - which is the worst category this
application has, because nothing on screen distinguishes it from working. A
filter that silently inverts, a date range that excludes the year it names, a
sender search that misses an accented name: each returns a plausible page of
results and no indication that the question asked was not the question answered.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.search.query import parse_query
from app.storage.filters import file_filter_sql
from app.storage.sqlite_store import SqliteStore

TODAY = date(2026, 6, 15)


# ---------------------------------------------------------------------------
# M4 - a negation means what it says
# ---------------------------------------------------------------------------

def _corpus(store):
    """Two files and two messages, differing in every way a filter can see."""
    report = store.upsert_file(r"D:\work\report.pdf", size_bytes=10, mtime_ns=1,
                               ext="pdf", parent_dir=r"D:\work")
    notes = store.upsert_file(r"D:\work\notes.txt", size_bytes=10, mtime_ns=1,
                              ext="txt", parent_dir=r"D:\work")
    for file_id in (report, notes):
        store.replace_chunks(file_id, [{"ordinal": 0, "text": "pump station",
                                        "page": None, "char_start": 0,
                                        "char_end": 12}])
        store.mark_indexed(file_id)
    return report, notes


def _matching(store, query: str) -> set:
    sql, params = file_filter_sql(parse_query(query, today=TODAY))
    rows = store.conn.execute(
        f"SELECT f.id FROM files f WHERE 1=1 {sql}", params).fetchall()
    return {row[0] for row in rows}


def test_a_negated_type_excludes_rather_than_includes(tmp_path):
    r"""**The finding, in one line.** `\\b` matched the operator with the minus
    still outside it, the minus was dropped as punctuation, and `-type:pdf`
    returned nothing but PDFs - the exact opposite of what was asked, with
    nothing on screen to say so."""
    with SqliteStore(tmp_path / "i.db") as store:
        report, notes = _corpus(store)

        assert _matching(store, "-type:pdf") == {notes}
        assert _matching(store, "type:pdf") == {report}


def test_a_negated_name_and_path_exclude_too(tmp_path):
    """Every filter that can express a negation honours it, not just the one
    that got reported."""
    with SqliteStore(tmp_path / "i.db") as store:
        report, notes = _corpus(store)

        assert _matching(store, "-name:report") == {notes}
        assert _matching(store, r"-path:work") == set()


def test_excluding_a_repository_keeps_the_files_that_have_none(tmp_path):
    r"""**`repo_id NOT IN (...)` is NULL for every file outside a repository**,
    and NULL is not true - so a plain `NOT IN` would have excluded the entire
    rest of the corpus along with the one checkout named. A far larger wrong
    answer than the one being fixed, and in the same direction: silent."""
    with SqliteStore(tmp_path / "i.db") as store:
        _report, notes = _corpus(store)
        repo = store.upsert_repo(r"D:\code\tools", kind="work")
        inside = store.upsert_file(r"D:\code\tools\main.py", size_bytes=1,
                                   mtime_ns=1, ext="py",
                                   parent_dir=r"D:\code\tools", repo_id=repo)
        store.mark_indexed(inside)

        found = _matching(store, "-repo:tools")

        assert inside not in found, "the excluded repository is still there"
        assert notes in found, (
            "excluding one repository also excluded every file that belongs to "
            "no repository at all")


def test_a_negated_phrase_is_excluded_from_the_match_expression():
    r"""`-"annual report"` asked for the phrase to be *absent* and was read as
    requiring it. FTS5 excludes the sequence, not each word in it."""
    from app.search.query import to_fts_match

    parsed = parse_query('-"annual report" pump')

    assert parsed.not_phrases == ("annual report",)
    assert parsed.phrases == ()
    expression = to_fts_match(parsed)
    assert "NOT" in expression
    assert '"annual report"' in expression


def test_a_negation_on_a_field_that_cannot_express_one_is_reported():
    """`-after:2024` is a confusing way of writing `before:`, not a filter.

    Reported rather than dropped: acting on half of what somebody typed,
    silently, is the failure this file exists to prevent - and guessing at the
    intent would be worse than saying it was not understood.
    """
    parsed = parse_query("-after:2024 pump", today=TODAY)

    assert parsed.after is None
    assert "-after:2024" in parsed.unknown_operators


def test_a_hyphen_inside_a_word_is_not_a_negation():
    """The lookbehind exists so `some-type:pdf` keeps behaving exactly as it
    always did. A fix that changes an unrelated input is two changes."""
    assert parse_query("some-type:pdf").ext == ("pdf",)


# ---------------------------------------------------------------------------
# The partial-date range
# ---------------------------------------------------------------------------

def test_before_a_bare_year_includes_that_whole_year():
    r"""**`before:2024` excluded all of 2024 but New Year's Day.**

    A partial date names a *period*, and which edge is meant depends on which
    side of the range it sits on. Both used to resolve to the first day.
    """
    assert parse_query("before:2024", today=TODAY).before == date(2024, 12, 31)
    assert parse_query("after:2024", today=TODAY).after == date(2024, 1, 1)
    assert parse_query("before:2024-06", today=TODAY).before == date(2024, 6, 30)


def test_a_year_to_year_range_covers_both_years():
    """`after:2024 before:2024` read as "everything in 2024" and matched only
    1 January - a range that is empty for 364 days of the year it names."""
    parsed = parse_query("after:2024 before:2024", today=TODAY)

    assert (parsed.after, parsed.before) == (date(2024, 1, 1), date(2024, 12, 31))


def test_a_full_date_is_untouched():
    """A day is already a single day. The fix must not widen anything that was
    already precise."""
    assert parse_query("before:2024-01-15", today=TODAY).before == date(2024, 1, 15)


# ---------------------------------------------------------------------------
# M20 - folding that matches the rest of the search
# ---------------------------------------------------------------------------

def test_an_accented_sender_is_found_however_it_was_typed(tmp_path):
    r"""**SQLite cannot do this and neither can `LOWER()`.**

        SELECT 'JOSÉ@x' LIKE '%josé%'  -> 0
        SELECT lower('JOSÉ@x')         -> 'josÉ@x'

    `LIKE` folds ASCII only, and so does the built-in `lower()`, so no
    expression over the stored column could fix it - while FTS5's `unicode61`
    tokeniser folded correctly and the two halves of one search disagreed.
    """
    with SqliteStore(tmp_path / "i.db") as store:
        file_id = store.upsert_file(r"D:\m\one.msg", size_bytes=1, mtime_ns=1,
                                    ext="msg", parent_dir=r"D:\m",
                                    source_kind="pst_message")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": "hello",
                                        "page": None, "char_start": 0,
                                        "char_end": 5}])
        store.mark_indexed(file_id)
        store.set_message(file_id, sender="JOSÉ Ramírez <JOSE@acme.com>",
                          subject="Añejo Report")

        assert _matching(store, "from:josé") == {file_id}
        assert _matching(store, "from:JOSÉ") == {file_id}
        assert _matching(store, "subject:añejo") == {file_id}
        assert _matching(store, "from:dave") == set()


def test_a_message_indexed_before_the_fold_is_still_found(tmp_path):
    r"""**`COALESCE(sender_lc, sender)` is what makes the migration safe.**

    The backfill runs inside `connect()` over a table that can hold tens of
    millions of rows. If it is interrupted, the folded column is NULL for the
    rest - and the filter has to degrade to exactly the old behaviour rather
    than stop finding those messages at all.
    """
    with SqliteStore(tmp_path / "i.db") as store:
        file_id = store.upsert_file(r"D:\m\old.msg", size_bytes=1, mtime_ns=1,
                                    ext="msg", parent_dir=r"D:\m",
                                    source_kind="pst_message")
        store.mark_indexed(file_id)
        store.set_message(file_id, sender="Dave Smith <dave@acme.com>")
        # Exactly what an interrupted backfill leaves behind.
        with store.write() as conn:
            conn.execute("UPDATE messages SET sender_lc = NULL WHERE file_id = ?",
                         (file_id,))

        assert _matching(store, "from:dave") == {file_id}


# ---------------------------------------------------------------------------
# M2 / the cache
# ---------------------------------------------------------------------------

def test_a_cache_exists_by_default():
    """`cache=` was passed at none of the four constructions, so the machinery
    documented across two reviews had never once run."""
    from app.search.engine import SearchEngine

    engine = SearchEngine(store=object(), vectors=object(), embedder=object())

    assert engine.cache is not None, "still no warm search"


def test_the_cache_can_still_be_switched_off():
    from app.search.engine import SearchEngine

    engine = SearchEngine(store=object(), vectors=object(), embedder=object(),
                          cache=False)

    assert engine.cache is None


def test_the_cache_evicts_rather_than_growing_without_end():
    from app.search.engine import CACHE_ENTRIES, _LruCache

    cache = _LruCache(limit=3)
    for number in range(10):
        cache.set(str(number), number)

    assert len(cache) == 3
    assert cache.get("9") == 9
    assert cache.get("0") is None
    assert CACHE_ENTRIES >= 1


# ---------------------------------------------------------------------------
# The two permanent latches
# ---------------------------------------------------------------------------

def test_one_bad_rerank_does_not_disable_reranking_for_the_session():
    r"""A single transient - a model file mid-write, one malformed passage -
    used to turn reranking off until the application was restarted, with every
    later search quietly returning weaker ordering."""
    from app.search.rerank import RERANK_FAILURE_BUDGET, Reranker

    ranker = Reranker("model", enabled=True)
    hits = [{"chunk_id": 1, "text": "pump station"}]

    def angry(_query, _passages):
        raise RuntimeError("scorer fell over")

    ranker._scorer = angry
    ranker.rerank("pump", hits, terms=["pump"])

    assert ranker.available, "one failure disabled reranking for the session"

    for _ in range(RERANK_FAILURE_BUDGET):
        ranker.rerank("pump", hits, terms=["pump"])
    assert not ranker.available, "the budget must eventually be spent"


# ---------------------------------------------------------------------------
# Reranking an image hit is a wrong opinion, not no opinion
# ---------------------------------------------------------------------------

class _SpyScorer:
    """A fake cross-encoder that records exactly what it was asked to score.

    `scores` is returned in full on every call - these tests only ever call
    `rerank()` once, so a fixed list keyed by call position is enough to prove
    both what reached the scorer and how its verdict reordered the head.
    """

    def __init__(self, scores):
        self.scores = list(scores)
        self.calls: list[tuple] = []

    def __call__(self, query, passages):
        self.calls.append((query, list(passages)))
        return list(self.scores)


def test_textless_hits_are_excluded_from_reranking_and_reinserted_in_place():
    r"""**The finding, in one line.** `hydrate_images` (`app/search/vector.py`)
    gives a photo hit no `text_key` at all - confirmed by
    `grep -n "text" app/search/vector.py` around `hydrate_images`, which shows
    it filling in only `path`, `ext`, `mtime_ns`, `content_hash`, `distance`
    and `chunk_id`, never `text`. The reranker used to read that gap as
    `hit.get("text", "")` and score the empty string, which is not "no
    opinion" about the photo - it is a confident wrong one, and it buried
    every image hit at the bottom of the reordered head."""
    from app.search.rerank import Reranker

    hits = [
        {"chunk_id": 1, "text": "pump station alpha, commissioning report"},
        {"chunk_id": 2, "path": r"D:\photos\pump.jpg"},                 # no "text" key at all
        {"chunk_id": 3, "text": "pump valve beta, inspection notes"},
        {"chunk_id": 4, "text": "", "path": r"D:\photos\valve.jpg"},    # blank "text"
        {"chunk_id": 5, "text": "pump station gamma, longer maintenance log entry"},
    ]

    # One score per texted passage, in the order the scorer will see them
    # (chunk 1, then 3, then 5 - their fused order). Deliberately not
    # monotonic with that order, so a reorder among the texted hits alone
    # is visible in the assertions below.
    spy = _SpyScorer([10.0, 30.0, 20.0])
    ranker = Reranker("model", enabled=True, scorer=spy, top_n=10)

    ranked = ranker.rerank("pump", hits, terms=["pump"])

    assert len(ranked) == len(hits), "reranking must never change how many hits come back"
    assert ranked[1] is hits[1], "the textless hit must land back at its own fused index"
    assert ranked[3] is hits[3], "the blank-text hit must land back at its own fused index"

    assert len(spy.calls) == 1
    _query, passages = spy.calls[0]
    assert len(passages) == 3, "only the three texted hits should ever reach the scorer"
    assert all(passage.strip() for passage in passages), (
        "a textless row's empty passage must never reach the scorer"
    )

    texted_order = [ranked[0]["chunk_id"], ranked[2]["chunk_id"], ranked[4]["chunk_id"]]
    assert texted_order == [3, 5, 1], (
        "texted hits reorder among themselves by the scorer's verdict, "
        "independent of where the textless hits sit"
    )


def test_an_all_image_result_set_is_returned_unchanged():
    r"""Nothing to rerank means nothing to score - and nothing to load the
    1.1GB model for, either."""
    from app.search.rerank import Reranker

    hits = [
        {"chunk_id": 1, "path": r"D:\photos\a.jpg"},
        {"chunk_id": 2, "text": "", "path": r"D:\photos\b.jpg"},
        {"chunk_id": 3, "text": "   ", "path": r"D:\photos\c.jpg"},   # whitespace-only
    ]

    spy = _SpyScorer([])
    ranker = Reranker("model", enabled=True, scorer=spy, top_n=10)

    ranked = ranker.rerank("pump", hits, terms=["pump"])

    assert ranked == hits
    assert ranked[0] is hits[0] and ranked[1] is hits[1] and ranked[2] is hits[2]
    assert spy.calls == [], "the scorer must never be invoked for an all-image result set"


def test_an_ocr_engine_that_will_not_load_is_not_reported_as_a_blank_image():
    r"""**`ERR_NO_TEXT_LAYER` is a claim about the file.**

    A latched engine failure recorded every image with it, so a corpus of
    scanned documents came back as thousands of blank photographs - and the OCR
    pass, which takes its work from exactly that code, would never look at them
    again.
    """
    from app.extract.ocr import OcrResult, ocr_image

    result = ocr_image(b"not an image", engine=None)

    assert isinstance(result, OcrResult)
    if result.engine_missing:
        assert result.empty, "an engine that did not run cannot have read text"
