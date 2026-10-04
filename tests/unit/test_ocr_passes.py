r"""OCR as a separate pass, because at a terabyte OCR *is* the schedule.

Layer: L3

Measured here at **3.6 seconds a page** - about eight times what embedding a
passage costs. 100,000 scanned pages is 100 hours on its own, before any other
file is touched. So a single pass that reads text and images together means
nothing in the corpus is searchable until everything is, and the whole run is
hostage to the slowest tenth of it.

Two passes fix that: text first, images behind it. The point is that **search
becomes useful after the first pass** - in a day or two rather than a fortnight
- and nobody waits for the second.

**The held files are a queue, not a failure**, and that distinction is a
one-word difference in a skip code and the whole difference on screen: 40,000
rows saying "held for the images pass" against 40,000 rows saying something
broke.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

import pytest

from app.extract import ocr as ocr_module
from app.extract.base import reads_by_ocr
from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import OCR_MODES, Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore

LONG_AGO = 3600
HELD = "ERR_OCR_HELD"


class NullVectors:
    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _embedder(dim: int = 8) -> Embedder:
    def encode(texts):
        return [
            l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(dim)])
            for t in texts
        ]

    return Embedder(dim=dim, encoder=encode)


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    stamp = time.time() - LONG_AGO
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture(autouse=True)
def _ocr_that_always_reads(monkeypatch):
    """The engine is a seam; this is the fake behind it.

    Every path here is exercised on a machine with no OCR installed, which is
    the point of the seam - and means these tests do not take 3.6 seconds a
    page themselves.
    """
    def engine(_source):
        return ([([0, 0, 1, 1], "INVOICE 4471 Barnsley Dairy", 0.94)], 0.01)

    monkeypatch.setattr(ocr_module, "available", lambda: True)
    monkeypatch.setattr(ocr_module, "_load_engine", lambda: engine)


@pytest.fixture()
def corpus(tmp_path):
    root = tmp_path / "docs"
    _write(root / "notes.txt", "Barnsley Dairy notes.")
    _write(root / "report.txt", "The HACCP review.")
    _write(root / "scan.png", b"\x89PNG\r\n\x1a\n" + b"x" * 900)
    return root


def _run(store, root, **config):
    pipeline = Pipeline(
        store, NullVectors(), _embedder(),
        PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, **config),
    )
    return pipeline.run()


# --- which tier a file belongs to ------------------------------------------

def test_an_image_is_recognised_as_the_expensive_tier():
    """Asked of the resolved extractor rather than a hard-coded list, so a
    route added in `extractors.toml` that lands on the OCR reader counts."""
    assert reads_by_ocr(Path("scan.png"))
    assert reads_by_ocr(Path("photo.TIFF"))
    assert not reads_by_ocr(Path("notes.txt"))
    assert not reads_by_ocr(Path("report.pdf"))


def test_the_three_modes_are_named_once():
    assert OCR_MODES == ("both", "text", "images")


# --- pass one: everything except OCR ---------------------------------------

def test_the_text_pass_indexes_the_documents_and_queues_the_image(tmp_path, corpus):
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _run(store, corpus, ocr_mode="text")

        held = store.get_file(str(corpus / "scan.png"))

    assert stats.indexed == 2                      # the two text files
    assert stats.skipped_by_code == {HELD: 1}
    assert held is not None
    assert held.status == FileStatus.SKIPPED
    assert held.skip_code == HELD


def test_the_held_file_says_it_is_held_rather_than_broken():
    r"""**The wording is the feature.**

    A held file gets an ordinary skip row - which is what makes it findable
    later - so the code and its sentence are the only thing separating "40,000
    images waiting" from "40,000 files failed" on the Indexing page. Somebody
    who reads the second deletes their index.
    """
    from app.core.errors import make_error

    error = make_error(HELD, "test", path=r"D:\Scans\invoice.png")

    assert "Held" in error.message
    assert "queued rather than read" in error.suggestion
    assert "nothing has been lost" in error.suggestion
    assert "--only-ocr" in (error.action_payload or "")


def test_the_skip_ledger_shows_it_as_its_own_row_with_a_count():
    """*"so the queue is visible rather than looking like 40,000 failures."*"""
    from app.ui.presenter import group_skips

    groups = group_skips({HELD: 40_000, "ERR_FILE_LOCKED": 3})

    first = groups[0]
    assert first.code == HELD                      # sorted by count, biggest first
    assert first.count == 40_000
    assert "Held" in first.message
    # Not retryable: retrying the *text* pass will hold it again. The action on
    # this row is to run the images pass, which the payload names.
    assert not first.retryable


# --- pass two: OCR only -----------------------------------------------------

def test_the_images_pass_fills_in_exactly_what_the_first_one_held(tmp_path, corpus):
    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, corpus, ocr_mode="text")
        stats = _run(store, corpus, ocr_mode="images")

        image = store.get_file(str(corpus / "scan.png"))

    assert stats.indexed == 1
    assert not stats.skipped_by_code
    assert image.status == FileStatus.INDEXED


def test_the_images_pass_does_not_walk_the_rest_of_the_corpus(tmp_path, corpus):
    r"""**Narrowing the walk is the saving, not filtering the results.**

    Otherwise the second pass `stat`s every one of a terabyte's millions of
    files to discard all but the pictures - the same fruitless walk that
    archival roots exist to avoid, paid a second time.
    """
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _run(store, corpus, ocr_mode="images")

    assert stats.seen == 1                         # the .png, and nothing else


def test_the_images_pass_does_not_prune_the_documents_it_never_looked_at(tmp_path, corpus):
    """Its walk saw only pictures, so "missing" would mean "not an image" for
    every document in the corpus."""
    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, corpus, ocr_mode="text")
        stats = _run(store, corpus, ocr_mode="images")
        survivors = [r.path for r in store.iter_files(source_kind="file")]

    assert stats.deleted == 0
    assert str(corpus / "notes.txt") in survivors


def test_a_narrower_walk_is_narrowed_further_never_widened(tmp_path, corpus):
    """A run restricted to `.png` must not become a run over every image type
    just because it is the images pass."""
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = Pipeline(
            store, NullVectors(), _embedder(),
            PipelineConfig(
                walk=WalkConfig(roots=[corpus], extensions=frozenset({".txt"})),
                workers=1, ocr_mode="images"),
        )
        pipeline.run()

        assert pipeline.config.walk.extensions == frozenset()


# --- the default, and both together ----------------------------------------

def test_both_is_the_default_and_reads_everything(tmp_path, corpus):
    """Unchanged behaviour for anybody whose corpus is 100GB, which is where
    reading images inline is the right answer."""
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _run(store, corpus)

    assert stats.indexed == 3
    assert not stats.skipped_by_code


def test_a_held_file_is_picked_up_again_rather_than_left_for_ever(tmp_path, corpus):
    r"""A held row is `SKIPPED`, not `INDEXED` - so change detection re-queues
    it on every later run. That is what makes the queue a queue: switching
    Settings back to "together" later fills them in with no extra step and no
    `--force`."""
    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, corpus, ocr_mode="text")
        stats = _run(store, corpus)                # back to both

        image = store.get_file(str(corpus / "scan.png"))

    assert stats.indexed == 1                      # only the held one was left
    assert image.status == FileStatus.INDEXED


# --- an archive folder of photos (2026-10-05) ----------------------------------

def test_an_archive_held_by_the_text_pass_is_read_by_the_images_pass(tmp_path, corpus):
    """The owner: "i ran the index few times no faces nothing keeps skipping".
    `PhotosMaster` was marked as an archive; the text pass held all 15,010
    photos and then recorded the folder as fully indexed, so the images pass -
    and every run after it - skipped the whole folder unread."""
    from app.index.archives import (
        ARCHIVE, MODE_STATE_KEY, RECORD_STATE_KEY, dump_modes, load_records, normalise,
    )

    with SqliteStore(tmp_path / "index.db") as store:
        store.set_state(MODE_STATE_KEY, dump_modes({str(corpus): ARCHIVE}))
        _run(store, corpus, ocr_mode="text")
        records = load_records(store.get_state(RECORD_STATE_KEY, "") or "")
        assert normalise(corpus) not in records, \
            "a pass that held a picture is not a full pass of the folder"

        stats = _run(store, corpus, ocr_mode="images")
        image = store.get_file(str(corpus / "scan.png"))

    assert not stats.skipped_roots, "the images pass does not take the archive shortcut"
    assert image.status == FileStatus.INDEXED


def test_an_images_pass_reads_an_archive_already_recorded_as_done(tmp_path, corpus):
    """The owner's index as it stands: the record was written before the fix.
    The images pass reads the held photos anyway - no repair needed."""
    from app.index.archives import (
        ARCHIVE, MODE_STATE_KEY, RECORD_STATE_KEY, ArchiveRecord, directory_mtime,
        dump_modes, dump_records, normalise,
    )

    with SqliteStore(tmp_path / "index.db") as store:
        store.set_state(MODE_STATE_KEY, dump_modes({str(corpus): ARCHIVE}))
        _run(store, corpus, ocr_mode="text")
        stale = ArchiveRecord(root=str(corpus), archived_at=time.time(), files=3,
                              mtime_ns=directory_mtime(corpus))
        store.set_state(RECORD_STATE_KEY, dump_records({normalise(corpus): stale}))

        stats = _run(store, corpus, ocr_mode="images")
        image = store.get_file(str(corpus / "scan.png"))

    assert image.status == FileStatus.INDEXED and stats.indexed == 1


def test_a_with_the_run_pass_reads_an_archive_whose_photos_are_still_held(tmp_path, corpus):
    """Not only the images pass: while anything is waiting, no run trusts a
    record that says the folder is done - "with the run" included."""
    from app.index.archives import (
        ARCHIVE, MODE_STATE_KEY, RECORD_STATE_KEY, ArchiveRecord, directory_mtime,
        dump_modes, dump_records, normalise,
    )

    with SqliteStore(tmp_path / "index.db") as store:
        store.set_state(MODE_STATE_KEY, dump_modes({str(corpus): ARCHIVE}))
        _run(store, corpus, ocr_mode="text")
        stale = ArchiveRecord(root=str(corpus), archived_at=time.time(), files=3,
                              mtime_ns=directory_mtime(corpus))
        store.set_state(RECORD_STATE_KEY, dump_records({normalise(corpus): stale}))

        stats = _run(store, corpus, ocr_mode="both")
        image = store.get_file(str(corpus / "scan.png"))

    assert not stats.skipped_roots
    assert image.status == FileStatus.INDEXED
