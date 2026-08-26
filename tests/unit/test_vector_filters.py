"""Filtered ANN search must return the eligible top-k, not the global top-k.

Regression for the large-filter defect: when `allowed_file_ids` exceeded
MAX_PREFILTER_IDS the old code searched the global top `limit` and discarded
ineligible rows afterwards. An eligible chunk ranked 101st globally simply
vanished, so a broad filter like `type:pdf` could return zero semantic hits
while thousands of PDFs matched.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence

import pytest

from app.search import vector as vector_search
from app.search.query import ParsedQuery


class FakeEmbedder:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


class FakeVectorStore:
    """Rows pre-sorted nearest-first. Honors `file_id IN (...)` where clauses
    the way LanceDB's prefilter does: filter first, then take k."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        vector: Sequence[float],
        *,
        k: int = 100,
        where: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self.calls.append({"k": k, "where": where})
        rows = self.rows
        if where is not None:
            match = re.search(r"file_id IN \(([^)]*)\)", where)
            assert match, f"unexpected where clause: {where}"
            allowed = {int(x) for x in match.group(1).split(",")}
            rows = [r for r in rows if r["file_id"] in allowed]
        return [dict(r) for r in rows[:k]]


def _rows(spec: list[tuple[int, int]]) -> list[dict[str, Any]]:
    """spec: [(chunk_id, file_id), ...] nearest-first."""
    return [
        {"chunk_id": cid, "file_id": fid, "_distance": i / 1000.0}
        for i, (cid, fid) in enumerate(spec)
    ]


def _query() -> ParsedQuery:
    return ParsedQuery(raw="pumps", text="pumps", terms=("pumps",))


LIMIT = 100
BIG_FILTER = set(range(10_000, 10_000 + vector_search.MAX_PREFILTER_IDS + 1))


def test_small_filter_is_pushed_down():
    store = FakeVectorStore(_rows([(1, 5), (2, 6), (3, 5)]))
    hits = vector_search.search(
        store, FakeEmbedder(), _query(), limit=LIMIT, allowed_file_ids={5}
    )
    assert [h["chunk_id"] for h in hits] == [1, 3]
    assert store.calls[0]["where"] is not None  # filtered inside the ANN query


def test_large_filter_finds_hit_ranked_past_global_top_k():
    """The defect: an eligible chunk at global rank > limit must still be found."""
    # 250 ineligible rows first, then the only eligible one at rank 251.
    eligible_fid = 10_000
    spec = [(i, 1) for i in range(250)] + [(999, eligible_fid)]
    store = FakeVectorStore(_rows(spec))

    hits = vector_search.search(
        store, FakeEmbedder(), _query(), limit=LIMIT, allowed_file_ids=BIG_FILTER
    )

    assert [h["chunk_id"] for h in hits] == [999]
    # Found on the first over-fetch rung (k = limit * 4), no pushdown needed.
    assert store.calls[0] == {"k": LIMIT * 4, "where": None}


def test_large_filter_escalates_to_full_pushdown_when_overfetch_misses():
    """Eligible rows buried past every over-fetch rung are still returned,
    via the final full-prefilter query - never silently dropped."""
    deepest = LIMIT * max(vector_search.OVERFETCH_FACTORS)  # 1600
    eligible_fid = 10_001
    spec = [(i, 1) for i in range(deepest + 50)] + [(7777, eligible_fid)]
    store = FakeVectorStore(_rows(spec))

    hits = vector_search.search(
        store, FakeEmbedder(), _query(), limit=LIMIT, allowed_file_ids=BIG_FILTER
    )

    assert [h["chunk_id"] for h in hits] == [7777]
    assert store.calls[-1]["where"] is not None  # last resort pushed the ids down
    assert len(store.calls) == len(vector_search.OVERFETCH_FACTORS) + 1


def test_large_filter_stops_when_table_exhausted():
    """A short table means the post-filtered result is already complete:
    no pointless escalation, no pushdown query."""
    spec = [(1, 10_000), (2, 1), (3, 10_001)]  # entire table, 3 rows
    store = FakeVectorStore(_rows(spec))

    hits = vector_search.search(
        store, FakeEmbedder(), _query(), limit=LIMIT, allowed_file_ids=BIG_FILTER
    )

    assert [h["chunk_id"] for h in hits] == [1, 3]
    assert len(store.calls) == 1


def test_large_filter_full_page_short_circuits():
    """When the first rung fills the page, no further queries are made."""
    eligible = [(i, 10_000) for i in range(LIMIT)]
    store = FakeVectorStore(_rows(eligible + [(500, 1)] * 5))

    hits = vector_search.search(
        store, FakeEmbedder(), _query(), limit=LIMIT, allowed_file_ids=BIG_FILTER
    )

    assert len(hits) == LIMIT
    assert len(store.calls) == 1


def test_empty_filter_short_circuits():
    store = FakeVectorStore(_rows([(1, 5)]))
    hits = vector_search.search(
        store, FakeEmbedder(), _query(), limit=LIMIT, allowed_file_ids=set()
    )
    assert hits == []
    assert store.calls == []


def test_results_are_normalised():
    store = FakeVectorStore(_rows([(1, 5)]))
    hits = vector_search.search(store, FakeEmbedder(), _query(), limit=LIMIT)
    assert "distance" in hits[0] and "_distance" not in hits[0]


# ---------------------------------------------------------------------------
# H5's remainder: the eligible set is not built when it is not wanted
#
# `file_ids_matching` materialised every matching file id before either
# retriever started. Measured on 500,000 files, 2026-08-27: `type:pdf` matched
# half of them and cost **362ms and 45.6MB**, on the critical path of every
# filtered search, against a 300ms budget for the whole search. `Eligibility`
# stops at `ELIGIBLE_CAP`: the same query costs **10.9ms and 0.2MB**, which is
# 33x faster and 228x less memory, and a narrow filter is unchanged.
# ---------------------------------------------------------------------------

from app.search.keyword import ELIGIBLE_CAP, Eligibility   # noqa: E402
from app.search.vector import MAX_PREFILTER_IDS            # noqa: E402


def _store_with(count: int, ext: str = "pdf"):
    from app.storage.sqlite_store import SqliteStore

    import tempfile
    from pathlib import Path as _Path

    store = SqliteStore(_Path(tempfile.mkdtemp()) / "e.db").connect()
    store.conn.execute("BEGIN IMMEDIATE")
    store.conn.executemany(
        "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, "
        "status, source_kind) VALUES (?, 'C:/d', ?, 1, 1, 'INDEXED', 'file')",
        [(f"C:/d/{i}.{ext}", ext) for i in range(count)],
    )
    store.conn.execute("COMMIT")
    return store


def test_the_cap_matches_the_point_where_pushdown_stops() -> None:
    """Listing more ids than can be pushed down is work nothing uses.

    Stated as a literal in `keyword` because importing `vector` there would be
    a cycle - so this is what keeps the two numbers the same one.
    """
    assert ELIGIBLE_CAP == MAX_PREFILTER_IDS


def test_a_narrow_filter_is_still_listed_and_pushed_down() -> None:
    """The fast path must survive the fix: a small filter is the best case for
    the ANN search and nothing about it should change."""
    from app.search.query import parse_query

    store = _store_with(50)
    try:
        eligible = Eligibility(store, parse_query("type:pdf"))
        assert eligible.ids is not None
        assert len(eligible.ids) == 50
    finally:
        store.close()


def test_a_broad_filter_is_not_listed_at_all() -> None:
    from app.search.query import parse_query

    store = _store_with(ELIGIBLE_CAP + 500)
    try:
        eligible = Eligibility(store, parse_query("type:pdf"))
        assert eligible.ids is None, (
            "the whole point is not building this list")
        assert eligible.filtered
        assert not eligible.excludes_everything
    finally:
        store.close()


def test_a_broad_filter_still_answers_the_question_correctly() -> None:
    """**Not listing them must not mean not filtering.** The pushdown is an
    optimisation; `keeps` is what enforces the filter, and it is asked in both
    shapes."""
    from app.search.query import parse_query

    store = _store_with(ELIGIBLE_CAP + 500)
    try:
        store.upsert_file("C:/d/odd.txt", size_bytes=1, mtime_ns=1,
                          status="INDEXED", source_kind="file")
        odd = store.get_file("C:/d/odd.txt")
        eligible = Eligibility(store, parse_query("type:pdf"))

        candidates = [1, 2, 3, odd.id]
        kept = eligible.keeps(candidates)
        assert odd.id not in kept, "a .txt survived a type:pdf filter"
        assert {1, 2, 3} <= kept
    finally:
        store.close()


def test_no_filters_keeps_everything() -> None:
    """None means no restriction, which is not the same as an empty set - and
    conflating the two turns every unfiltered search into one returning
    nothing."""
    from app.search.query import parse_query

    store = _store_with(5)
    try:
        eligible = Eligibility(store, parse_query("invoice"))
        assert eligible.ids is None
        assert not eligible.filtered
        assert eligible.keeps([1, 2, 3]) == {1, 2, 3}
        assert not eligible.excludes_everything
    finally:
        store.close()


def test_a_filter_matching_nothing_still_says_so() -> None:
    from app.search.query import parse_query

    store = _store_with(5)
    try:
        eligible = Eligibility(store, parse_query("type:zzz"))
        assert eligible.ids == set()
        assert eligible.excludes_everything
    finally:
        store.close()
