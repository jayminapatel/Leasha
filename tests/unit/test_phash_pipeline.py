r"""Work order 0h §2a: pHash computation wired into the images pass.

Same shape as `test_clip_lane_pipeline.py`, which this file sits beside
rather than inside - `phash_computer` is a wholly independent optional
argument from `image_embedder`/`image_vectors`, gated and flushed on its own
(see `Pipeline._maybe_compute_phash`'s docstring for why), so its own tests
belong in their own file rather than growing every existing CLIP test a
second, unrelated assertion.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

import pytest

from app.core.errors import AppErrorException, make_error
from app.extract import ocr as ocr_module
from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore

LONG_AGO = 3600


class NullVectors:
    """The text table, faked out - these tests care about pHashes, not
    text vectors."""

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


class FakePhashComputer:
    """`PhashComputer`'s public shape, with no Pillow decode and no model."""

    def __init__(self, *, fail_on: frozenset[str] = frozenset()):
        self.calls: list[str] = []
        self._fail_on = fail_on

    def compute(self, path):
        self._calls_append(path)
        name = Path(path).name
        if name in self._fail_on:
            raise AppErrorException(make_error(
                "ERR_PHASH", "index.phash", details=f"simulated failure on {path}"))
        # **Derived from the file's bytes, not its path** - the real
        # `PhashComputer` hashes pixels, so two files with identical content
        # at two different paths (§4's "duplicate fixture across two roots")
        # must fake-hash equal here too, exactly as they would for real.
        data = Path(path).read_bytes()
        seed = abs(hash(data)) % (2 ** 63)
        return format(seed & 0xFFFFFFFFFFFFFFFF, "016x")

    def _calls_append(self, path):
        self.calls.append(str(path))


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


def _run(store, root, *, phash_computer=None, **config):
    pipeline = Pipeline(
        store, NullVectors(), _text_embedder(),
        PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, ocr_mode="images",
                        **config),
        phash_computer=phash_computer,
    )
    stats = pipeline.run()
    return pipeline, stats


# --- backward compatibility: off unless configured ---------------------------


def test_unconfigured_pipeline_behaves_exactly_as_before(tmp_path):
    root = tmp_path / "docs"
    _png(root / "photo.png")

    with SqliteStore(tmp_path / "index.db") as store:
        _, stats = _run(store, root)
        file_row = store.get_file(str(root / "photo.png"))

    assert stats.indexed == 1
    assert file_row.phash is None
    assert "ERR_PHASH" not in stats.warned_by_code


def test_constructing_a_pipeline_with_no_phash_computer_still_works():
    pipeline = Pipeline(
        object(), NullVectors(), _text_embedder(),
        PipelineConfig(walk=WalkConfig(roots=[])),
    )
    assert pipeline.phash_computer is None


# --- the lane, configured ------------------------------------------------


def test_every_ladder_passed_image_gets_a_phash(tmp_path):
    root = tmp_path / "docs"
    _png(root / "one.png")
    _png(root / "two.png")

    computer = FakePhashComputer()
    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, root, phash_computer=computer)

        for name in ("one.png", "two.png"):
            row = store.get_file(str(root / name))
            assert row.phash is not None
            assert len(row.phash) == 16

    assert len(computer.calls) == 2


def test_a_phash_is_written_regardless_of_ocr_text(tmp_path, monkeypatch):
    """Mirrors `test_image_vector_is_written_regardless_of_ocr_text`: a photo
    with no OCR text at all must still get a pHash."""
    monkeypatch.setattr(ocr_module, "_load_engine", lambda: (lambda _s: ([], 0.0)))

    root = tmp_path / "docs"
    _png(root / "blank.png")

    computer = FakePhashComputer()
    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, root, phash_computer=computer)
        row = store.get_file(str(root / "blank.png"))

    assert row.phash is not None
    assert computer.calls


def test_phash_and_clip_are_computed_independently(tmp_path):
    """Neither lane's success or failure implies anything about the other -
    a pipeline with only `phash_computer` configured writes pHashes and no
    CLIP vectors, and vice versa (already covered by
    `test_clip_lane_pipeline.py`)."""
    from app.storage.vector_store import ImageVectorStore

    root = tmp_path / "docs"
    _png(root / "photo.png")

    with SqliteStore(tmp_path / "index.db") as store, \
         ImageVectorStore(tmp_path / "img_vectors", dim=8) as images:
        _run(store, root, phash_computer=FakePhashComputer())
        row = store.get_file(str(root / "photo.png"))
        assert row.phash is not None
        assert images.count() == 0, "no image_embedder/image_vectors given"


# --- H4: a broken hash costs only itself --------------------------------


def test_a_failing_hash_does_not_break_the_run_or_the_file(tmp_path):
    root = tmp_path / "docs"
    _png(root / "good.png")
    _png(root / "bad.png")

    computer = FakePhashComputer(fail_on=frozenset({"bad.png"}))
    with SqliteStore(tmp_path / "index.db") as store:
        _, stats = _run(store, root, phash_computer=computer)

        bad_row = store.get_file(str(root / "bad.png"))
        good_row = store.get_file(str(root / "good.png"))

        assert bad_row.status == FileStatus.INDEXED
        assert good_row.status == FileStatus.INDEXED
        assert stats.indexed == 2
        assert stats.warned_by_code.get("ERR_PHASH") == 1
        assert bad_row.phash is None
        assert good_row.phash is not None


def test_no_phash_computer_configured_means_ocr_still_works(tmp_path):
    root = tmp_path / "docs"
    _png(root / "photo.png")

    with SqliteStore(tmp_path / "index.db") as store:
        _, stats = _run(store, root, phash_computer=None)
        row = store.get_file(str(root / "photo.png"))

    assert row.status == FileStatus.INDEXED
    assert stats.indexed == 1


# --- H7-shaped: batched writes, not one UPDATE per file -----------------


def test_many_photos_flush_their_hashes_in_one_batch(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    for i in range(10):
        _png(root / f"img{i}.png")

    write_calls: list[int] = []
    with SqliteStore(tmp_path / "index.db") as store:
        original = store.set_phashes

        def counted(phashes):
            write_calls.append(len(phashes))
            return original(phashes)

        monkeypatch.setattr(store, "set_phashes", counted)
        _run(store, root, phash_computer=FakePhashComputer(), embed_batch=256)

    assert sum(write_calls) == 10
    assert len(write_calls) <= 2, (
        "ten photos must not cost ten separate UPDATE transactions - the "
        "H7 pathology, over SQLite instead of LanceDB"
    )


# --- M6-shaped: written before a file can read as complete ---------------


def test_pending_phashes_are_flushed_before_the_run_ends(tmp_path):
    root = tmp_path / "docs"
    _png(root / "solo.png")

    with SqliteStore(tmp_path / "index.db") as store:
        pipeline, _ = _run(store, root, phash_computer=FakePhashComputer())
        assert pipeline._pending_phashes == {}, "must be flushed, not left pending"
        row = store.get_file(str(root / "solo.png"))
        assert row.phash is not None


# --- work order 0h §4: the pHash acceptance scenario, at this layer ------


def test_the_same_photo_indexed_from_two_roots_gets_the_same_phash(tmp_path):
    r"""§4's acceptance sentence, the indexing half: "duplicate fixture
    across two roots folds". This proves the ingredient that makes folding
    possible - the *same bytes*, read from two different roots (simulating
    two drives), produce the *same* pHash, so `app.search.folding.fold`'s
    burst pass (proven separately in `test_folding.py`) has something to
    group. The fold logic itself is not re-proven here - only that indexing
    genuinely produces equal hashes for equal images, which is the one fact
    this layer is responsible for."""
    payload = b"\x89PNG\r\n\x1a\n" + b"same-bytes" * 200

    root_a = tmp_path / "driveA" / "Photos"
    root_b = tmp_path / "driveB" / "Backup"
    _write(root_a / "holiday.png", payload)
    _write(root_b / "holiday_copy.png", payload)

    computer = FakePhashComputer()
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = Pipeline(
            store, NullVectors(), _text_embedder(),
            PipelineConfig(walk=WalkConfig(roots=[root_a, root_b]), workers=1,
                            ocr_mode="images"),
            phash_computer=computer,
        )
        pipeline.run()

        row_a = store.get_file(str(root_a / "holiday.png"))
        row_b = store.get_file(str(root_b / "holiday_copy.png"))

    assert row_a.phash is not None and row_b.phash is not None
    assert row_a.phash == row_b.phash, (
        "identical image bytes, read from two roots, must produce the same "
        "pHash - the fact the burst-fold pass relies on"
    )
