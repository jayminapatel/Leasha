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
