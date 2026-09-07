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


# --------------------------------------------------------------------------
# Work order 0h §2d: `find_similar_images` - "more like this" for photos
# --------------------------------------------------------------------------


def test_find_similar_images_returns_relatives_not_the_source(store) -> None:
    file_id = _photo_file_id(store)
    other_id = store.upsert_file(
        path=r"D:\Photos\IMG_20260102.jpg", size_bytes=1, mtime_ns=2,
        ext="jpg", source_kind="file",
    )
    image_vectors = FakeImageVectorStore([
        {"chunk_id": file_id, "file_id": file_id, "distance": 0.0},
        {"chunk_id": other_id, "file_id": other_id, "distance": 0.05},
    ])
    # `find_similar_images` needs `vector_for`, which `FakeImageVectorStore`
    # (built for `search_images`) does not have - added here rather than to
    # the shared fake, since only this lane's "more like this" needs it.
    image_vectors.vector_for = lambda chunk_id: [0.1, 0.2, 0.3]

    engine = SearchEngine(
        store, _EmptyVectorStore(), _NoOpEmbedder(), image_vectors=image_vectors,
    )
    try:
        response = engine.find_similar_images(file_id)
    finally:
        engine.close()

    assert response.image_count == 1
    assert response.results[0].file_id == other_id
    assert response.results[0].path == r"D:\Photos\IMG_20260102.jpg"


def test_find_similar_images_is_off_without_image_vectors(store) -> None:
    engine = SearchEngine(store, _EmptyVectorStore(), _NoOpEmbedder())
    try:
        response = engine.find_similar_images(999)
    finally:
        engine.close()
    assert response.results == []
    assert response.image_count == 0


def test_find_similar_images_of_an_unembedded_photo_is_empty(store) -> None:
    """No stored vector for this file id - the ordinary state of a photo not
    yet CLIP-embedded, not an error."""
    image_vectors = FakeImageVectorStore([])
    image_vectors.vector_for = lambda chunk_id: None

    engine = SearchEngine(
        store, _EmptyVectorStore(), _NoOpEmbedder(), image_vectors=image_vectors,
    )
    try:
        response = engine.find_similar_images(12345)
    finally:
        engine.close()
    assert response.results == []


# --------------------------------------------------------------------------
# Work order 0h §2c: `search_by_image` - reverse image search
# --------------------------------------------------------------------------


class FakeImageEmbedder:
    """Stands in for `app.index.clip_embedder.ClipImageEmbedder` - embeds a
    path, not a string, but the fakes in `test_search_images.py` already
    accept either since they just record whatever they were given."""

    def __init__(self, vector=(0.4, 0.5, 0.6)):
        self.vector = list(vector)
        self.calls: list[str] = []

    def embed(self, paths):
        self.calls.extend(str(p) for p in paths)
        return [self.vector for _ in paths]


class FakePhashComputer:
    def __init__(self, values: dict):
        self.values = values

    def compute(self, path):
        key = str(path)
        if key not in self.values:
            raise RuntimeError(f"no fake pHash configured for {key}")
        return self.values[key]


def test_search_by_image_finds_the_exact_photo_and_labels_it(store) -> None:
    """The acceptance demo, at the engine level: a recompressed copy of a
    photo already in the index comes back labelled `"exact"`, not merely
    found."""
    file_id = _photo_file_id(store)
    store.set_phashes({file_id: "0000000000000000"})
    image_vectors = FakeImageVectorStore(
        [{"chunk_id": file_id, "file_id": file_id, "distance": 0.01}])

    engine = SearchEngine(
        store, _EmptyVectorStore(), _NoOpEmbedder(),
        image_vectors=image_vectors, image_embedder=FakeImageEmbedder(),
        phash_computer=FakePhashComputer({"query.jpg": "0000000000000003"}),
    )
    try:
        response = engine.search_by_image("query.jpg")
    finally:
        engine.close()

    assert response.image_count == 1
    assert response.results[0].photo_match == "exact"


def test_search_by_image_labels_a_clip_only_match_as_similar(store) -> None:
    file_id = _photo_file_id(store)
    store.set_phashes({file_id: "0000000000000000"})
    image_vectors = FakeImageVectorStore(
        [{"chunk_id": file_id, "file_id": file_id, "distance": 0.2}])

    engine = SearchEngine(
        store, _EmptyVectorStore(), _NoOpEmbedder(),
        image_vectors=image_vectors, image_embedder=FakeImageEmbedder(),
        # A totally different pHash - far past the threshold.
        phash_computer=FakePhashComputer({"query.jpg": "ffffffffffffffff"}),
    )
    try:
        response = engine.search_by_image("query.jpg")
    finally:
        engine.close()

    assert response.results[0].photo_match == "similar"


def test_search_by_image_without_a_phash_computer_still_finds_it(store) -> None:
    """H4: `phash_computer` is optional. Absent means no exact/similar
    distinction, never a broken search."""
    file_id = _photo_file_id(store)
    image_vectors = FakeImageVectorStore(
        [{"chunk_id": file_id, "file_id": file_id, "distance": 0.01}])

    engine = SearchEngine(
        store, _EmptyVectorStore(), _NoOpEmbedder(),
        image_vectors=image_vectors, image_embedder=FakeImageEmbedder(),
    )
    try:
        response = engine.search_by_image("query.jpg")
    finally:
        engine.close()

    assert response.image_count == 1
    assert response.results[0].photo_match == "similar"


def test_search_by_image_is_off_without_both_arguments(store) -> None:
    engine = SearchEngine(store, _EmptyVectorStore(), _NoOpEmbedder())
    try:
        response = engine.search_by_image("query.jpg")
    finally:
        engine.close()
    assert response.results == []
    assert response.image_count == 0


# --------------------------------------------------------------------------
# Work order 0r item 1c, second clause: mid-life cache-empty progress reaches
# `status_callback`, not just the splash's own (already-done) first clause.
# --------------------------------------------------------------------------


class _MidLifeDownloadEmbedder:
    """Stands in for the real CLIP text-tower `Embedder` mid-download: its
    `embed` call reports progress through whatever `_on_progress` was set
    onto it - exactly the seam `vector.search_images` writes to - before
    returning a vector, the same way the real `Embedder._ensure_encoder`
    reports through its `_DownloadProgressWatcher` before the constructor
    call it is watching returns."""

    def __init__(self, vector=(0.1, 0.2, 0.3)):
        self.vector = list(vector)

    def embed(self, texts):
        on_progress = getattr(self, "_on_progress", None)
        if on_progress is not None:
            on_progress(42.0)
        return [self.vector for _ in texts]


def test_a_mid_life_cache_empty_download_reaches_the_status_callback(store) -> None:
    """The gap this item closes: a search reaching the image lane after the
    model cache has been emptied mid-life must not report progress nowhere -
    `SearchEngine.status_callback`, wired by `app/ui/shell.py` to the
    window's notices bar, is where it has to land."""
    file_id = _photo_file_id(store)
    image_vectors = FakeImageVectorStore(
        [{"chunk_id": file_id, "file_id": file_id, "distance": 0.02}])

    messages: list[str] = []
    engine = SearchEngine(
        store, _EmptyVectorStore(), _NoOpEmbedder(),
        image_vectors=image_vectors, clip_text_embedder=_MidLifeDownloadEmbedder(),
        status_callback=messages.append,
    )
    try:
        response = engine.search("kids on the beach")
    finally:
        engine.close()

    assert response.image_count == 1, "the download report must not have cost the search"
    assert messages, "a mid-life download must reach the status callback"
    assert "42" in messages[0]
    assert "Downloading" in messages[0]
    # §0.8's plain-words discipline is for splash strings specifically, but a
    # notices-bar line naming a real download fact is exactly what that rule
    # already allows for the 130MB figure - a fabricated number would not be.
    assert "%" in messages[0]


def test_without_a_status_callback_nothing_is_wired_or_attempted(store) -> None:
    """H4, restated for this wiring: `status_callback` absent is exactly the
    state before this item existed - no `on_progress` reaches the embedder at
    all, matching `Embedder`'s own "only when someone is listening" gate for
    its download watcher thread."""
    file_id = _photo_file_id(store)
    image_vectors = FakeImageVectorStore(
        [{"chunk_id": file_id, "file_id": file_id, "distance": 0.02}])
    embedder = _MidLifeDownloadEmbedder()

    engine = SearchEngine(
        store, _EmptyVectorStore(), _NoOpEmbedder(),
        image_vectors=image_vectors, clip_text_embedder=embedder,
    )
    try:
        response = engine.search("kids on the beach")
    finally:
        engine.close()

    assert response.image_count == 1
    assert not hasattr(embedder, "_on_progress")


def test_a_broken_status_callback_never_breaks_the_image_search(store) -> None:
    """H4: a status callback that raises (a window mid-teardown, a broken
    test double) must not be the reason an image search fails."""
    file_id = _photo_file_id(store)
    image_vectors = FakeImageVectorStore(
        [{"chunk_id": file_id, "file_id": file_id, "distance": 0.02}])

    def exploding_callback(message):
        raise RuntimeError("the window is already gone")

    engine = SearchEngine(
        store, _EmptyVectorStore(), _NoOpEmbedder(),
        image_vectors=image_vectors, clip_text_embedder=_MidLifeDownloadEmbedder(),
        status_callback=exploding_callback,
    )
    try:
        response = engine.search("kids on the beach")
    finally:
        engine.close()

    assert response.image_count == 1
    assert not any(n.code == NOTICE_NO_IMAGES for n in response.notices)


def test_a_broken_reverse_image_search_degrades_with_notice_no_images(store) -> None:
    """H4: mirrors `NOTICE_NO_IMAGES`, deliberately, per the work order's own
    instruction - the same CLIP lane failing, not a genuinely different
    failure mode."""
    image_vectors = FakeImageVectorStore(
        [{"chunk_id": 1, "file_id": 1, "distance": 0.1}])

    engine = SearchEngine(
        store, _EmptyVectorStore(), _NoOpEmbedder(),
        image_vectors=image_vectors, image_embedder=ExplodingEmbedder(),
    )
    try:
        response = engine.search_by_image("query.jpg")
    finally:
        engine.close()

    assert response.results == []
    codes = [n.code for n in response.notices]
    assert NOTICE_NO_IMAGES in codes
