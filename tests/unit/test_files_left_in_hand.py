"""A file the indexing process died on is recorded, not read again.

Layer: L3

2026-10-09, the owner's overnight run: the indexing process died with an access
violation inside the PDF library on a PDF inside a zip, six and a half hours
in. A native fault cannot be caught by any Python guard; the window stayed up
and said "press Start" - and the next run would have walked straight back into
the same file, because nothing recorded which file it was. Now each reader
thread writes down the file it has in hand just before reading it and clears
the note when done. A note still there when a run starts is the file the
previous process died on: it is recorded as `ERR_FILE_CRASHED_READER`, settled
like any other skip, and the run goes on without it.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.extract import base
from app.extract.base import Document
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.resources import ResourceLimits
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_index_freshness import write_aged
from tests.unit.test_stop_mid_batch import Model, RecordingVectors

READ: list[str] = []


class Counting:
    """A reader for `.inhand` files that remembers which ones it read."""

    name = "inhand"
    extensions = (".inhand",)
    reads_externally = False

    def extract(self, path: Path):
        READ.append(path.name)
        yield Document(path=path, text=f"Notes in {path.stem} about the dairy audit.")


@pytest.fixture(autouse=True)
def _register():
    before = dict(base.REGISTRY)
    base.REGISTRY.pop(".inhand", None)
    READ.clear()
    base.register(Counting())
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(before)


def _corpus(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    for name in ("alpha", "cursed", "gamma"):
        write_aged(root / f"{name}.inhand", f"{name} body")
    return root


def _run(store: SqliteStore, root: Path):
    config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, embed_batch=1000,
                            limits=ResourceLimits(pause_on_battery=False, cpu_percent=0))
    model = Model()
    pipeline = Pipeline(store, RecordingVectors(), model.embedder(), config)
    model.pipeline = pipeline
    return pipeline, pipeline.run()


def test_a_note_left_by_a_dead_run_marks_the_file_failed_and_leaves_it_unread(tmp_path):
    root = _corpus(tmp_path)
    db = tmp_path / "i.db"
    with SqliteStore(db) as store:
        # What a reader thread leaves behind when the process dies mid-file.
        notes = db.parent / Pipeline.IN_HAND_DIR
        notes.mkdir()
        (notes / "4242.txt").write_text(str(root / "cursed.inhand"), encoding="utf-8")

        _pipeline, stats = _run(store, root)

        assert sorted(READ) == ["alpha.inhand", "gamma.inhand"], "the cursed file is never opened"
        assert stats.indexed == 2
        assert stats.skipped_by_code.get("ERR_FILE_CRASHED_READER") == 1
        record = store.get_file(str(root / "cursed.inhand"))
        assert record is not None and record.status == "FAILED"
        assert record.skip_code == "ERR_FILE_CRASHED_READER"
        assert not list(notes.glob("*.txt")), "the note is consumed"


def test_a_run_that_finishes_leaves_no_notes_behind(tmp_path):
    root = _corpus(tmp_path)
    db = tmp_path / "i.db"
    with SqliteStore(db) as store:
        _pipeline, stats = _run(store, root)
        assert stats.indexed == 3
        notes = db.parent / Pipeline.IN_HAND_DIR
        assert not notes.is_dir() or not list(notes.glob("*.txt"))


def test_the_note_is_there_while_a_file_is_read_and_gone_after(tmp_path):
    db = tmp_path / "i.db"
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[tmp_path]), workers=1,
                                limits=ResourceLimits(pause_on_battery=False, cpu_percent=0))
        pipeline = Pipeline(store, RecordingVectors(), Model().embedder(), config)
        note = db.parent / Pipeline.IN_HAND_DIR / f"{threading.get_ident()}.txt"

        pipeline._note_in_hand(tmp_path / "big.pdf")
        assert note.read_text(encoding="utf-8") == str(tmp_path / "big.pdf")
        pipeline._clear_in_hand()
        assert not note.exists()


def test_a_store_without_a_database_file_switches_the_notes_off():
    """A bare stand-in store has no `db_path`; the notes must not fail the run."""
    from types import SimpleNamespace

    pipeline = Pipeline.__new__(Pipeline)
    pipeline.store = SimpleNamespace()
    assert pipeline._in_hand_dir() is None
    pipeline._note_in_hand("anything")        # no folder, no error
    pipeline._clear_in_hand()
