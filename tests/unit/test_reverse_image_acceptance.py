r"""Work order 0h §4's pHash acceptance sentence, demonstrated end to end:
**"reverse-image finds the original from a recompressed copy."**

The real `PhashComputer` (real `imagehash.phash`, real Pillow decode) computes
both the indexed original's hash and the query photo's hash - nothing about
pHash matching is faked here, unlike `test_engine_image_lane.py`'s
`FakePhashComputer`, which exists to test the *wiring* rather than the
algorithm. What *is* faked is the CLIP half (`image_vectors`, `image_embedder`)
- real CLIP models are measured and proven elsewhere (`test_clip_embedder.py`,
the work order's own §1a measurements) and re-downloading one here would make
this test slow and network-dependent for no additional proof; the ANN "found
it at all" step is a fake row exactly as `test_engine_image_lane.py` already
uses, and the `photo_match` labelling this test is actually about happens
after that, against the real stored and real query hashes.
"""

from __future__ import annotations

import math

import pytest

from app.index.phash import PhashComputer
from app.search.engine import SearchEngine
from app.storage.sqlite_store import SqliteStore

pytest.importorskip("imagehash")
pytest.importorskip("PIL")


def _natural_photo(w: int = 320, h: int = 240) -> "object":
    """Broadband synthetic photo-like image - see `test_phash.py`'s `_natural`
    for why a flat gradient or a fine checkerboard cannot stand in for this."""
    from PIL import Image

    img = Image.new("RGB", (w, h))
    px = img.load()
    for x in range(w):
        for y in range(h):
            r = 128 + 60 * math.sin(x / 37 + 1) + 30 * math.sin(y / 23 + 2) \
                + 20 * math.sin((x + y) / 51 + 0.3)
            g = 128 + 50 * math.sin(x / 29 + 0.7) + 40 * math.sin(y / 41 + 1.4) \
                + 15 * math.sin((x - y) / 61 + 2.1)
            b = 128 + 45 * math.sin(x / 19 + 2.2) + 35 * math.sin(y / 33 + 0.5) \
                + 25 * math.sin((x * y) % 97 / 17.0)
            px[x, y] = (int(max(0, min(255, r))), int(max(0, min(255, g))),
                        int(max(0, min(255, b))))
    return img


class FakeImageVectorStore:
    """The ANN half, faked - see this file's module docstring for why."""

    def __init__(self, rows):
        self.rows = rows

    def search(self, vector, *, k=100, where=None):
        return [dict(r) for r in self.rows[:k]]


class FakeImageEmbedder:
    def embed(self, paths):
        return [[0.1, 0.2, 0.3] for _ in paths]


class _NoVectors:
    def search(self, *_a, **_k):
        return []


class _NoModel:
    def embed(self, texts):
        return [[0.0] for _ in texts]


def test_a_recompressed_copy_finds_the_full_res_original(tmp_path):
    r"""The scene: a full-resolution photo is already indexed (`Sources\
    DriveA\holiday.jpg`). A WhatsApp-compressed copy of the *same* photo is
    dropped into the search box. Reverse image search must find the
    original and say, via `photo_match == "exact"`, that this is the same
    picture - not merely something similar - which is what lets a UI built
    on this say "the full-res original is on DriveA"."""
    original_path = r"D:\Sources\DriveA\holiday.jpg"
    original_file = tmp_path / "original.jpg"
    query_file = tmp_path / "whatsapp_copy.jpg"

    photo = _natural_photo()
    photo.save(original_file, quality=95)
    photo.resize((160, 120)).save(query_file, quality=60)  # WhatsApp-style

    computer = PhashComputer()
    original_hash = computer.compute(original_file)

    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            original_path, size_bytes=original_file.stat().st_size,
            mtime_ns=1, ext="jpg", parent_dir=r"D:\Sources\DriveA",
            status="INDEXED", source_kind="file",
        )
        store.set_phashes({file_id: original_hash})

        image_vectors = FakeImageVectorStore(
            [{"chunk_id": file_id, "file_id": file_id, "distance": 0.03}])

        engine = SearchEngine(
            store, _NoVectors(), _NoModel(),
            image_vectors=image_vectors, image_embedder=FakeImageEmbedder(),
            phash_computer=computer,
        )
        try:
            response = engine.search_by_image(query_file)
        finally:
            engine.close()

    assert response.image_count == 1
    result = response.results[0]
    assert result.path == original_path, (
        "reverse image search must find the original file, not merely "
        "return a hit"
    )
    assert result.photo_match == "exact", (
        "a recompressed copy of the same photo must be labelled 'exact', "
        "not merely 'similar' - this is the fact a UI needs to say "
        "'the full-res original is on <source>'"
    )


def test_an_unrelated_photo_is_labelled_similar_not_exact(tmp_path):
    """The negative case: CLIP might still surface an unrelated photo as
    visually close (that is what CLIP-only similarity means), but pHash
    must not call it the same picture."""
    from PIL import Image

    original_path = r"D:\Sources\DriveA\holiday.jpg"
    original_file = tmp_path / "original.jpg"
    unrelated_file = tmp_path / "unrelated.jpg"

    _natural_photo().save(original_file, quality=95)
    Image.new("RGB", (320, 240), color=(10, 10, 10)).save(unrelated_file, quality=95)

    computer = PhashComputer()
    original_hash = computer.compute(original_file)

    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            original_path, size_bytes=original_file.stat().st_size,
            mtime_ns=1, ext="jpg", parent_dir=r"D:\Sources\DriveA",
            status="INDEXED", source_kind="file",
        )
        store.set_phashes({file_id: original_hash})

        image_vectors = FakeImageVectorStore(
            [{"chunk_id": file_id, "file_id": file_id, "distance": 0.4}])

        engine = SearchEngine(
            store, _NoVectors(), _NoModel(),
            image_vectors=image_vectors, image_embedder=FakeImageEmbedder(),
            phash_computer=computer,
        )
        try:
            response = engine.search_by_image(unrelated_file)
        finally:
            engine.close()

    assert response.results[0].photo_match == "similar"
