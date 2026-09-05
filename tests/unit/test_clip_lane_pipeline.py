r"""Work order 0h §1a/§1b/§1c: the CLIP image lane, wired into the pipeline.

`Pipeline.__init__` gained two optional, keyword-only parameters -
`image_embedder` and `image_vectors` - both `None` by default. Backward
compatibility is the point: every existing caller (`app.cli`, the window,
every other test in this suite) constructs a `Pipeline` without them and must
see exactly the behaviour it saw before this order. The first test below is
that guarantee; the rest exercise the lane once it is actually configured.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

import pytest

from app.extract import ocr as ocr_module
from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore
from app.storage.vector_store import ImageVectorStore

pytest.importorskip("lancedb")

LONG_AGO = 3600
IMG_DIM = 8


class NullVectors:
    """The text table, faked out - this order's tests care about the image
    table, not the one every other pipeline test already covers."""

    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _text_embedder(dim: int = 8) -> Embedder:
    def encode(texts):
        return [
            l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(dim)])
            for t in texts
        ]

    return Embedder(dim=dim, encoder=encode)


class FakeClipImageEmbedder:
    """`ClipImageEmbedder`'s public shape, with no model and no download."""

    def __init__(self, *, fail_on: frozenset[str] = frozenset()):
        self.calls: list[str] = []
        self._fail_on = fail_on
        self.loaded = True

    def embed(self, paths):
        out = []
        for path in paths:
            self.calls.append(str(path))
            if Path(path).name in self._fail_on:
                raise RuntimeError(f"simulated CLIP failure on {path}")
            seed = abs(hash(str(path))) % 100
            out.append(l2_normalise([math.cos(seed + i) for i in range(IMG_DIM)]))
        return out


@pytest.fixture(autouse=True)
def _ocr_that_always_reads(monkeypatch):
    def engine(_source):
        return ([([0, 0, 1, 1], "INVOICE 4471 Barnsley Dairy", 0.94)], 0.01)

    monkeypatch.setattr(ocr_module, "available", lambda: True)
    monkeypatch.setattr(ocr_module, "_load_engine", lambda: engine)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - LONG_AGO
    os.utime(path, (stamp, stamp))
    return path


def _png(path: Path) -> Path:
    return _write(path, b"\x89PNG\r\n\x1a\n" + b"x" * 900)


def _run(store, root, *, image_embedder=None, image_vectors=None, **config):
    pipeline = Pipeline(
        store, NullVectors(), _text_embedder(),
        PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, ocr_mode="images",
                        **config),
        image_embedder=image_embedder, image_vectors=image_vectors,
    )
    stats = pipeline.run()
    return pipeline, stats


# --- backward compatibility: the lane is off unless configured --------------


def test_unconfigured_pipeline_behaves_exactly_as_before(tmp_path):
    """No `image_embedder`, no `image_vectors` - every existing caller."""
    root = tmp_path / "docs"
    _png(root / "photo.png")

    with SqliteStore(tmp_path / "index.db") as store:
        _, stats = _run(store, root)

    assert stats.indexed == 1
    assert "ERR_CLIP_EMBED" not in stats.warned_by_code
    assert "ERR_CLIP_STORE" not in stats.warned_by_code


def test_constructing_a_pipeline_with_neither_argument_still_works():
    """The plain, two-positional-plus-config constructor every other test in
    this suite already uses must not have changed shape."""
    pipeline = Pipeline(
        object(), NullVectors(), _text_embedder(),
        PipelineConfig(walk=WalkConfig(roots=[])),
    )
    assert pipeline.image_embedder is None
    assert pipeline.image_vectors is None


# --- the lane, configured -----------------------------------------------------


def test_every_ladder_passed_image_gets_a_clip_vector(tmp_path):
    root = tmp_path / "docs"
    _png(root / "one.png")
    _png(root / "two.png")

    clip = FakeClipImageEmbedder()
    with SqliteStore(tmp_path / "index.db") as store, \
         ImageVectorStore(tmp_path / "img_vectors", dim=IMG_DIM) as images:
        _run(store, root, image_embedder=clip, image_vectors=images)

        assert images.count() == 2
        for name in ("one.png", "two.png"):
            file_row = store.get_file(str(root / name))
            assert file_row is not None
            hits = images.search([0.0] * IMG_DIM, k=10)
            assert file_row.id in {h["file_id"] for h in hits}

    assert len(clip.calls) == 2


def test_image_vector_is_written_regardless_of_ocr_text(tmp_path, monkeypatch):
    """The whole point: a photo with no OCR text at all must still get a CLIP
    vector, because that is precisely the photo a caption-based search
    cannot find."""
    monkeypatch.setattr(ocr_module, "_load_engine", lambda: (lambda _s: ([], 0.0)))

    root = tmp_path / "docs"
    _png(root / "blank.png")

    clip = FakeClipImageEmbedder()
    with SqliteStore(tmp_path / "index.db") as store, \
         ImageVectorStore(tmp_path / "img_vectors", dim=IMG_DIM) as images:
        _run(store, root, image_embedder=clip, image_vectors=images)

        assert images.count() == 1
        assert clip.calls


# --- H4: a broken image degrades that photo, never the run -------------------


def test_a_failing_image_does_not_break_the_run_or_the_file(tmp_path):
    root = tmp_path / "docs"
    _png(root / "good.png")
    _png(root / "bad.png")

    clip = FakeClipImageEmbedder(fail_on=frozenset({"bad.png"}))
    with SqliteStore(tmp_path / "index.db") as store, \
         ImageVectorStore(tmp_path / "img_vectors", dim=IMG_DIM) as images:
        _, stats = _run(store, root, image_embedder=clip, image_vectors=images)

        bad_row = store.get_file(str(root / "bad.png"))
        good_row = store.get_file(str(root / "good.png"))

        # Both files are still fully indexed - the failure cost only the
        # CLIP vector for one of them, never the file's own INDEXED status.
        assert bad_row.status == FileStatus.INDEXED
        assert good_row.status == FileStatus.INDEXED
        assert stats.indexed == 2
        assert stats.warned_by_code.get("ERR_CLIP_EMBED") == 1
        assert images.count() == 1               # only the good one has a vector


def test_no_image_embedder_configured_means_ocr_still_works(tmp_path):
    """Half of §1a/§1c missing (no CLIP embedder given) must not touch the
    OCR ladder this order sits on top of."""
    root = tmp_path / "docs"
    _png(root / "photo.png")

    with SqliteStore(tmp_path / "index.db") as store:
        _, stats = _run(store, root, image_vectors=None, image_embedder=None)
        row = store.get_file(str(root / "photo.png"))

    assert row.status == FileStatus.INDEXED
    assert stats.indexed == 1


# --- H7: batched writes, not one Lance version per image ----------------------


def test_many_images_flush_in_a_batch_not_one_delete_per_file(tmp_path):
    root = tmp_path / "docs"
    for i in range(10):
        _png(root / f"img{i}.png")

    clip = FakeClipImageEmbedder()
    with SqliteStore(tmp_path / "index.db") as store, \
         ImageVectorStore(tmp_path / "img_vectors", dim=IMG_DIM) as images:
        images.ensure_table()
        before = len(list(images._table.list_versions()))

        _run(store, root, image_embedder=clip, image_vectors=images,
             embed_batch=256)          # every image fits in one flush

        after = len(list(images._table.list_versions()))

        # One flush at end-of-run: at most a handful of versions (delete-of-
        # nothing on first index + one add), never one per image.
        assert after - before <= 3, (
            "ten images must not cost ten dataset versions - the H7 pathology"
        )
        assert images.count() == 10


# --- M6: vectors exist before the file can read as complete -------------------


def test_pending_images_are_flushed_before_the_run_ends(tmp_path):
    """`_flush_pending_images` runs synchronously in `_consume`'s final flush,
    before `run()` returns - so by the time a caller can observe the run as
    finished, every successfully embedded image's vector already exists."""
    root = tmp_path / "docs"
    _png(root / "solo.png")

    clip = FakeClipImageEmbedder()
    with SqliteStore(tmp_path / "index.db") as store, \
         ImageVectorStore(tmp_path / "img_vectors", dim=IMG_DIM) as images:
        pipeline, _ = _run(store, root, image_embedder=clip, image_vectors=images)

        assert pipeline._pending_images == [], "must be flushed, not left pending"
        assert images.count() == 1
