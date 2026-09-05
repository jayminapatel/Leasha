r"""Work order 0h §1c: the image lane, wired into `SearchEngine.search()`.

Standalone with an injected `encoder`, `test_search_images.py` already proves
`search_images`/`hydrate_images` themselves; `test_backends.py` and
`test_fusion.py` already prove the machinery each of those calls into. What
none of those prove - because none of them touch `SearchEngine` - is the
actual wiring this file adds: a third future submitted alongside keyword and
text-vector, `image_hits` reaching `fuse_hits` as a third list, a namespaced
`"img:<file_id>"` chunk id surviving all the way to a `SearchResult` without
crashing, and `NOTICE_NO_IMAGES` firing on a broken lane without breaking the
other two.

This is the acceptance sentence's search half: "type a description and the
photo appears" - the corpus below has **no text and no matching filename**
that could explain a keyword or text-vector hit, so a result appearing at all
proves the image lane, not a coincidence in the other two.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.search.engine import NOTICE_NO_IMAGES, SearchEngine
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_search_images import (
    ExplodingEmbedder,
    FakeClipTextEmbedder,
    FakeImageVectorStore,
)


class _EmptyVectorStore:
    """Stands in for the text-vector `VectorStore` - always empty, so any
    result that comes back is provably not from this lane."""

    def search(self, vector, *, k: int = 100, where=None):
        return []


class _NoOpEmbedder:
    """Stands in for the FastEmbed text `Embedder` used by the text-vector
    lane - never asked to do anything real, since `_EmptyVectorStore` never
    needs a query vector to compare against."""

    def embed(self, texts):
        return [[0.0] for _ in texts]

    def warm_up(self) -> None:
        pass


@pytest.fixture()
def store(tmp_path: Path):
    db = SqliteStore(tmp_path / "index.db").connect()
    # One photo, no chunks - a photo has no chunks row, exactly as
    # `hydrate_images`'s docstring describes. Named so a keyword match would
    # be an obvious tell that the wrong lane found it.
    db.upsert_file(
        path=r"D:\Photos\IMG_20260101.jpg", size_bytes=1_000_000, mtime_ns=1,
        ext="jpg", source_kind="file",
    )
    yield db
    db.close()


def _photo_file_id(db: SqliteStore) -> int:
    row = db.conn.execute("SELECT id FROM files LIMIT 1").fetchone()
    return int(row["id"])


def test_a_photo_with_no_text_is_found_by_the_image_lane(store) -> None:
    """The acceptance sentence's search half. Nothing about this photo's
    filename or (non-existent) text describes "kids blowing out birthday
    candles" - the only way it can appear is through the CLIP lane."""
    file_id = _photo_file_id(store)
    image_vectors = FakeImageVectorStore(
        [{"chunk_id": file_id, "file_id": file_id, "distance": 0.02}])

    engine = SearchEngine(
        store, _EmptyVectorStore(), _NoOpEmbedder(),
        image_vectors=image_vectors, clip_text_embedder=FakeClipTextEmbedder(),
    )
    try:
        response = engine.search("kids blowing out birthday candles")
    finally:
        engine.close()

    assert response.image_count == 1
    assert len(response.results) == 1
    result = response.results[0]
    assert result.path == r"D:\Photos\IMG_20260101.jpg"
    # The namespaced fused key ("img:<file_id>") must not have reached here
    # as a string - `SearchResult.chunk_id` is typed `int` and every caller
    # downstream (the run log, `as_dict()`, the UI) expects a plain one.
    assert result.chunk_id == file_id
    assert result.file_id == file_id
    # **Not "no notices at all".** `NOTICE_UNMATCHED_TERMS` is correct and
    # expected here - none of these words are in any chunk, because there
    # are no chunks; that is the whole premise of the test, not a lane
    # failure. What must be absent is any complaint about the image lane,
    # which worked.
    assert not any(n.code == NOTICE_NO_IMAGES for n in response.notices)


def test_the_image_lane_is_off_when_either_argument_is_none(store) -> None:
    """H4, restated as the default: absent means off, exactly as it did
    before this lane existed. No third future, no image hits, no notice."""
    engine = SearchEngine(store, _EmptyVectorStore(), _NoOpEmbedder())
    try:
        response = engine.search("kids blowing out birthday candles")
    finally:
        engine.close()

    assert response.image_count == 0
    assert response.results == []
    assert not any(n.code == NOTICE_NO_IMAGES for n in response.notices)


def test_a_broken_image_lane_degrades_with_a_notice_not_a_crash(store) -> None:
    """H4: the picture lane failing must not take keyword/text-vector with
    it, and must say so - the same discipline `NOTICE_NO_VECTORS` already
    has, under its own code so the two failures are never conflated."""
    file_id = _photo_file_id(store)
    image_vectors = FakeImageVectorStore(
        [{"chunk_id": file_id, "file_id": file_id, "distance": 0.02}])

    engine = SearchEngine(
        store, _EmptyVectorStore(), _NoOpEmbedder(),
        image_vectors=image_vectors, clip_text_embedder=ExplodingEmbedder(),
    )
    try:
        response = engine.search("kids blowing out birthday candles")
    finally:
        engine.close()

    assert response.image_count == 0
    assert response.results == []
    codes = [n.code for n in response.notices]
    assert NOTICE_NO_IMAGES in codes, "a broken picture lane must say so"
    assert "NOTICE_NO_VECTORS" not in codes, (
        "the text-vector half never ran into trouble - the two lanes must "
        "not be conflated under one notice code")


def test_the_pool_has_a_worker_for_all_three_lanes(store) -> None:
    """Regression guard for the pool-sizing fix alongside this wiring: three
    lanes submitted to a two-worker pool would serialise the third behind
    the other two on every single search, not just fail loudly - the kind of
    slow-down `RETRIEVER_TIMEOUT_S`'s docstring warns never to reintroduce
    silently."""
    engine = SearchEngine(store, _EmptyVectorStore(), _NoOpEmbedder())
    try:
        assert engine._pool._max_workers >= 3
    finally:
        engine.close()
