"""Work order 0h §1c: the third retrieval lane.

`search_images` is deliberately a thin wrapper around `app.search.vector.search`
- see that function's docstring - so these tests pin down exactly the two
things that are new: the CLIP text tower is a distinct embedder from the
FastEmbed text model, and the `chunk_id` rewrite that keeps an image hit from
colliding with a real passage's chunk id once both reach `fuse_hits`.

H4 (a broken lane degrades, never breaks search) is `search()`'s own
contract and is exercised again here only to confirm it survives being called
through this wrapper unchanged.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

import pytest

from app.core.errors import AppErrorException, make_error
from app.search import vector as vector_search
from app.search.fusion import fuse_hits
from app.search.query import parse_query


class FakeClipTextEmbedder:
    """Stands in for `Embedder(model_name="Qdrant/clip-ViT-B-32-text", dim=512)`."""

    def __init__(self, vector=(0.1, 0.2, 0.3)):
        self.vector = list(vector)
        self.calls: list[str] = []

    def embed(self, texts):
        self.calls.extend(texts)
        return [self.vector for _ in texts]


class ExplodingEmbedder:
    def embed(self, texts):
        raise AppErrorException(make_error(
            "ERR_MODEL_LOAD", "index.clip_embedder", details="model missing"))


class FakeImageVectorStore:
    """Rows pre-sorted nearest-first, `chunk_id` already equal to `file_id` -
    exactly what `ImageVectorStore.add_images` writes."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def search(self, vector: Sequence[float], *, k: int = 100,
               where: Optional[str] = None) -> list[dict[str, Any]]:
        self.calls.append({"k": k, "where": where})
        return [dict(r) for r in self.rows[:k]]


def parsed(text: str):
    return parse_query(text)


# --- the CLIP text tower is used, not the FastEmbed text model --------------


def test_uses_the_given_embedder_not_a_default() -> None:
    embedder = FakeClipTextEmbedder()
    store = FakeImageVectorStore([
        {"chunk_id": 7, "file_id": 7, "distance": 0.1},
    ])

    vector_search.search_images(store, embedder, parsed("kids blowing out birthday candles"))

    assert embedder.calls == ["kids blowing out birthday candles"]


# --- the chunk_id rewrite: no collision with a real chunk id ----------------


def test_image_hits_are_namespaced_before_fusion() -> None:
    """`ImageVectorStore` writes `chunk_id == file_id`. A bare file_id of 7
    would collide with an unrelated passage whose real `chunks.id` is also 7 -
    two independent SQLite sequences that both start at 1."""
    embedder = FakeClipTextEmbedder()
    store = FakeImageVectorStore([{"chunk_id": 7, "file_id": 7, "distance": 0.1}])

    hits = vector_search.search_images(store, embedder, parsed("a photo"))

    assert hits[0]["chunk_id"] == "img:7"
    assert hits[0]["file_id"] == 7


def test_fusing_keyword_text_and_image_lanes_does_not_collide() -> None:
    """The whole point of the rewrite, demonstrated with `fuse_hits` exactly as
    keyword+vector already fuse: a text passage with chunk_id 7 and a photo
    whose file_id is also 7 must land as two distinct fused rows, not one."""
    keyword_hits = [{"chunk_id": 1, "file_id": 100}]
    text_vector_hits = [{"chunk_id": 7, "file_id": 200}]

    embedder = FakeClipTextEmbedder()
    image_store = FakeImageVectorStore([{"chunk_id": 7, "file_id": 7, "distance": 0.05}])
    image_hits = vector_search.search_images(image_store, embedder, parsed("a photo"))

    fused = fuse_hits([keyword_hits, text_vector_hits, image_hits])

    fused_keys = {row["chunk_id"] for row in fused}
    assert fused_keys == {1, 7, "img:7"}, (
        "the text chunk (id 7) and the photo (file_id 7) must both survive "
        "fusion as distinct rows"
    )
    assert len(fused) == 3


# --- H4: a broken CLIP model degrades this lane, never breaks search --------


def test_a_broken_clip_text_model_returns_no_hits_and_a_notice() -> None:
    store = FakeImageVectorStore([{"chunk_id": 1, "file_id": 1, "distance": 0.1}])
    problems: list[str] = []

    hits = vector_search.search_images(
        store, ExplodingEmbedder(), parsed("a photo"), problems=problems)

    assert hits == []
    assert problems, "a lane failure must leave a notice, not fail silently"


def test_an_empty_query_embeds_nothing() -> None:
    embedder = FakeClipTextEmbedder()
    store = FakeImageVectorStore([{"chunk_id": 1, "file_id": 1, "distance": 0.1}])

    hits = vector_search.search_images(store, embedder, parsed(""))

    assert hits == []
    assert embedder.calls == []


# --- hydrate_images -----------------------------------------------------------


def test_hydrate_images_joins_files_by_file_id() -> None:
    class FakeConn:
        def execute(self, sql, ids):
            rows = [
                {"file_id": 7, "path": r"D:\Photos\bday.jpg", "ext": "jpg",
                 "mtime_ns": 0, "content_hash": None},
            ]
            return _Cursor([r for r in rows if r["file_id"] in ids])

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeStore:
        conn = FakeConn()

    hits = [{"chunk_id": "img:7", "file_id": 7, "distance": 0.02}]
    hydrated = vector_search.hydrate_images(FakeStore(), hits)

    assert len(hydrated) == 1
    assert hydrated[0]["path"] == r"D:\Photos\bday.jpg"
    assert hydrated[0]["chunk_id"] == "img:7"
    assert hydrated[0]["distance"] == 0.02


def test_hydrate_images_drops_a_hit_whose_file_is_gone() -> None:
    class FakeConn:
        def execute(self, sql, ids):
            return self

        def fetchall(self):
            return []

    class FakeStore:
        conn = FakeConn()

    hits = [{"chunk_id": "img:9", "file_id": 9, "distance": 0.5}]
    assert vector_search.hydrate_images(FakeStore(), hits) == []


def test_hydrate_images_of_an_empty_list_is_empty() -> None:
    assert vector_search.hydrate_images(object(), []) == []


def test_hydrate_images_carries_the_phash_through() -> None:
    """Work order 0h §2a/§2b: `phash` has to survive `hydrate_images` the
    same way `content_hash` already does, so folding and reverse-image
    search can read it off a hydrated hit."""
    class FakeConn:
        def execute(self, sql, ids):
            rows = [
                {"file_id": 7, "path": r"D:\Photos\bday.jpg", "ext": "jpg",
                 "mtime_ns": 0, "content_hash": None, "phash": "abc123abc123abc1"},
            ]
            return _Cursor([r for r in rows if r["file_id"] in ids])

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeStore:
        conn = FakeConn()

    hits = [{"chunk_id": "img:7", "file_id": 7, "distance": 0.02}]
    hydrated = vector_search.hydrate_images(FakeStore(), hits)

    assert hydrated[0]["phash"] == "abc123abc123abc1"


# --- work order 0h §2c: reverse image search ---------------------------------


def test_search_by_image_embeds_the_photo_not_a_string() -> None:
    embedder = FakeClipTextEmbedder()
    store = FakeImageVectorStore([{"chunk_id": 5, "file_id": 5, "distance": 0.01}])

    vector_search.search_by_image(store, embedder, r"D:\Photos\query.jpg")

    assert embedder.calls == [r"D:\Photos\query.jpg"]


def test_search_by_image_namespaces_the_chunk_id() -> None:
    """Same collision-avoidance reasoning as `search_images` - the query
    changed shape, the fusion hazard did not."""
    embedder = FakeClipTextEmbedder()
    store = FakeImageVectorStore([{"chunk_id": 9, "file_id": 9, "distance": 0.03}])

    hits = vector_search.search_by_image(store, embedder, "query.jpg")

    assert hits[0]["chunk_id"] == "img:9"
    assert hits[0]["file_id"] == 9


def test_search_by_image_returns_nothing_when_either_argument_is_none() -> None:
    store = FakeImageVectorStore([{"chunk_id": 1, "file_id": 1, "distance": 0.1}])
    assert vector_search.search_by_image(None, FakeClipTextEmbedder(), "q.jpg") == []
    assert vector_search.search_by_image(store, None, "q.jpg") == []


def test_search_by_image_degrades_with_a_notice_on_a_broken_embedder() -> None:
    store = FakeImageVectorStore([{"chunk_id": 1, "file_id": 1, "distance": 0.1}])
    problems: list[str] = []

    hits = vector_search.search_by_image(
        store, ExplodingEmbedder(), "q.jpg", problems=problems)

    assert hits == []
    assert problems, "a broken lane must leave a notice, not fail silently"


def test_search_by_image_degrades_with_a_notice_on_a_broken_store() -> None:
    class ExplodingStore:
        def search(self, vector, *, k=100, where=None):
            raise RuntimeError("LanceDB is unavailable")

    problems: list[str] = []
    hits = vector_search.search_by_image(
        ExplodingStore(), FakeClipTextEmbedder(), "q.jpg", problems=problems)

    assert hits == []
    assert problems


# --- work order 0r item 1c, second clause: mid-life cache-empty progress ----


def test_search_images_sets_on_progress_onto_the_embedder_before_calling_it() -> None:
    """`search_images`'s `on_progress` is not consumed here - it is handed to
    `text_embedder` (whatever object it is) immediately before the call that
    might need it, so that whatever `Embedder._ensure_encoder` does with its
    own `_on_progress` attribute sees the caller's callback, not `None`."""
    embedder = FakeClipTextEmbedder()
    store = FakeImageVectorStore([{"chunk_id": 7, "file_id": 7, "distance": 0.1}])
    seen: list[float] = []

    vector_search.search_images(
        store, embedder, parsed("a photo"), on_progress=seen.append)

    assert embedder._on_progress == seen.append


def test_search_images_on_progress_defaults_to_untouched() -> None:
    """No caller asked for progress reporting - the embedder must not gain
    an `_on_progress` attribute it never had, matching every other H4
    default in this module: absent means exactly what it meant before this
    existed."""
    embedder = FakeClipTextEmbedder()
    store = FakeImageVectorStore([{"chunk_id": 7, "file_id": 7, "distance": 0.1}])

    vector_search.search_images(store, embedder, parsed("a photo"))

    assert not hasattr(embedder, "_on_progress")


def test_search_images_on_progress_never_breaks_the_search() -> None:
    """H4: an embedder that refuses the attribute (a `__slots__` object, a
    frozen dataclass, anything unexpected) must still search - the whole
    point of guarding this the way `app/index/embedder.py` guards every call
    to its own `_on_progress`."""
    class SlottedEmbedder:
        __slots__ = ("calls",)

        def __init__(self):
            self.calls: list[str] = []

        def embed(self, texts):
            self.calls.extend(texts)
            return [[0.1, 0.2, 0.3] for _ in texts]

    embedder = SlottedEmbedder()
    store = FakeImageVectorStore([{"chunk_id": 7, "file_id": 7, "distance": 0.1}])

    hits = vector_search.search_images(
        store, embedder, parsed("a photo"), on_progress=lambda pct: None)

    assert hits[0]["chunk_id"] == "img:7"
    assert embedder.calls == ["a photo"]


def test_search_images_reports_a_mid_life_cache_empty_download(
    tmp_path, monkeypatch,
) -> None:
    r"""The scenario this item exists for: the model cache has been emptied
    mid-life (a moved index, a re-staged data directory) and the first image
    search after that is what next tries to load the CLIP text tower. There
    is no splash by then - the window has been open for a while - so
    `search_images`'s own `on_progress` is what has to carry a real download
    signal at all.

    Uses the same fake-slow-download technique `test_embedder.py`'s
    `test_on_progress_reports_a_simulated_download` already proves the
    watcher itself with. What is new here is going through `search_images`,
    not `Embedder` directly, and building the `Embedder` the way
    `clip_text_embedder_from_settings` actually does in production: with no
    `on_progress` at construction, because `app/main.py` builds this embedder
    before the window - and therefore before there is anywhere to report to
    - exists. The callback only shows up later, at `search_images` call
    time, which is the whole point of this seam.
    """
    import time
    from pathlib import Path

    import app.index.embedder as embedder_module
    from app.index.embedder import Embedder

    class FakeTextEmbedding:
        """Simulates a slow download by growing the cache dir over time."""

        def __init__(self, model_name, cache_dir=None, **_kwargs):
            target = Path(cache_dir)
            target.mkdir(parents=True, exist_ok=True)
            for chunk in range(3):
                (target / f"part{chunk}.bin").write_bytes(b"x" * 4_000_000)
                time.sleep(0.35)

        def embed(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr("fastembed.TextEmbedding", FakeTextEmbedding)
    monkeypatch.setitem(
        embedder_module._APPROX_MODEL_BYTES, "Qdrant/clip-ViT-B-32-text", 12_000_000)

    # No `on_progress` yet - exactly as `clip_text_embedder_from_settings`
    # builds this embedder in the real app, before the window exists.
    text_embedder = Embedder(
        "Qdrant/clip-ViT-B-32-text", dim=3, cache_dir=str(tmp_path))
    image_vectors = FakeImageVectorStore(
        [{"chunk_id": 7, "file_id": 7, "distance": 0.1}])

    progress: list[float] = []
    hits = vector_search.search_images(
        image_vectors, text_embedder, parsed("kids on the beach"),
        on_progress=progress.append,
    )

    assert hits and hits[0]["chunk_id"] == "img:7"
    assert progress, ("a mid-life cache-empty download must report progress "
                      "somewhere reachable - exactly the gap this item closes")
    assert progress[-1] == 100.0, "the final call must report completion"
