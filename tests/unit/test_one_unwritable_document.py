"""One document whose text cannot be written never ends the run.

Layer: L1/L3

The owner's run on 2026-10-08, ten minutes in: a `.doc` whose text held a lone
UTF-16 surrogate (a pair split across two pieces of the piece table, decoded
one piece at a time) reached `replace_chunks`; SQLite stores UTF-8 and Python's
encoder refused it; the `UnicodeEncodeError` came straight out of `_consume`
and the run ended with "an unexpected error occurred". Non-negotiable 3 says
one bad file never halts a run, and non-negotiable 6 says SQLite is the
authority, so there are two guards here and a test for each: the store makes
text UTF-8-safe at its boundary, and the consumer turns any failed write into
a recorded skip and goes on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.extract import base
from app.extract.base import Document
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.resources import ResourceLimits
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore, utf8_safe
from tests.unit.test_index_freshness import write_aged
from tests.unit.test_stop_mid_batch import Model, RecordingVectors

LONE = "\ud83d"                      # the high half of an emoji, on its own
SPLIT_PAIR = "\ud83d" + "\ude00"     # both halves, as two code points - the .doc shape
REPLACEMENT = chr(0xFFFD)            # U+FFFD, spelled so this file stays ASCII


# --- the store boundary -----------------------------------------------------------

def test_utf8_safe_repairs_a_split_pair_and_replaces_a_stray_half():
    assert utf8_safe("ok") == "ok"
    assert utf8_safe("a" + SPLIT_PAIR + "b") == "a\U0001f600b"
    assert utf8_safe("a" + LONE + "b") == "a" + REPLACEMENT + "b"
    assert utf8_safe(None) is None and utf8_safe(7) == 7


def test_a_passage_with_a_lone_surrogate_is_stored_not_refused(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        file_id = store.upsert_file(str(tmp_path / "plan.doc"), size_bytes=10, mtime_ns=5,
                                    status="PENDING", source_kind="file")
        ids = store.replace_chunks(file_id, [
            {"ordinal": 0, "text": "the pump seal " + LONE + " failed", "label": "p" + LONE},
            {"ordinal": 1, "text": "fine " + SPLIT_PAIR},
        ])
        assert len(ids) == 2
        with store.write() as conn:              # the test's own read; nothing is written
            rows = conn.execute("SELECT text, label FROM chunks WHERE file_id = ? ORDER BY ordinal",
                                (file_id,)).fetchall()
        assert rows[0][0] == "the pump seal " + REPLACEMENT + " failed"
        assert rows[0][1] == "p" + REPLACEMENT
        assert rows[1][0] == "fine \U0001f600"


# --- the consumer -------------------------------------------------------------------

class TwoDocs:
    """A reader for `.twodoc` files: one wholesome document each."""

    name = "twodoc"
    extensions = (".twodoc",)
    reads_externally = False

    def extract(self, path: Path):
        yield Document(path=path, text=f"Notes in {path.stem} about the dairy audit.")


@pytest.fixture(autouse=True)
def _register():
    before = dict(base.REGISTRY)
    base.REGISTRY.pop(".twodoc", None)
    base.register(TwoDocs())
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(before)


def _run(tmp_path: Path, store: SqliteStore):
    root = tmp_path / "docs"
    root.mkdir()
    for name in ("alpha", "broken", "gamma"):
        write_aged(root / f"{name}.twodoc", f"{name} body")
    config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, embed_batch=1000,
                            limits=ResourceLimits(pause_on_battery=False, cpu_percent=0))
    model = Model()
    pipeline = Pipeline(store, RecordingVectors(), model.embedder(), config)
    model.pipeline = pipeline
    return pipeline.run()


def test_a_write_that_raises_is_a_skipped_file_not_a_dead_run(tmp_path, monkeypatch):
    """The exact shape of 2026-10-08, with the store refusing one document."""
    with SqliteStore(tmp_path / "i.db") as store:
        real = store.replace_chunks

        def refusing(file_id, chunks):
            if any("broken" in c["text"] for c in chunks):
                raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")
            return real(file_id, chunks)

        monkeypatch.setattr(store, "replace_chunks", refusing)
        stats = _run(tmp_path, store)

        assert stats.indexed == 2, "the two good documents are written"
        assert stats.skipped == 1 and stats.skipped_by_code.get("ERR_UNEXPECTED") == 1
        counts = store.stats()
        statuses = counts.get("files") or {}
        assert statuses.get("FAILED", 0) == 1, statuses
        failed = [r for r in store.iter_files(source_kind="file") if "broken" in str(r.path)]
        assert failed and failed[0].status == "FAILED"


def test_a_lone_surrogate_in_a_document_is_indexed_through_the_whole_pipeline(tmp_path,
                                                                           monkeypatch):
    """Belt and braces: with the store boundary doing its job, the document is
    not even skipped - it is indexed, its stray half as U+FFFD."""
    def one_with_a_stray_half(self, path):
        yield Document(path=path, text=f"seal {LONE} failed in {path.stem}")

    monkeypatch.setattr(TwoDocs, "extract", one_with_a_stray_half)
    with SqliteStore(tmp_path / "i.db") as store:
        stats = _run(tmp_path, store)
        assert stats.indexed == 3 and stats.skipped == 0
        with store.write() as conn:              # the test's own read; nothing is written
            texts = [r[0] for r in conn.execute("SELECT text FROM chunks").fetchall()]
        assert texts and all(REPLACEMENT in t and LONE not in t for t in texts)


def test_even_the_skip_row_failing_does_not_end_the_run(tmp_path, monkeypatch):
    """The last line of defence: the store refuses the document *and* refuses to
    record it as skipped. Logged in full, the file keeps its status, the other
    documents are indexed, and the run ends normally."""
    with SqliteStore(tmp_path / "i.db") as store:
        real_replace, real_mark = store.replace_chunks, store.mark_skipped

        def refusing(file_id, chunks):
            if any("broken" in c["text"] for c in chunks):
                raise RuntimeError("the store will not have it")
            return real_replace(file_id, chunks)

        def refusing_mark(file_id, error):
            if "store will not have it" in (error.details or ""):
                raise RuntimeError("nor the skip row")
            return real_mark(file_id, error)

        monkeypatch.setattr(store, "replace_chunks", refusing)
        monkeypatch.setattr(store, "mark_skipped", refusing_mark)
        stats = _run(tmp_path, store)

        assert stats.indexed == 2 and stats.skipped == 1
        pending = [r for r in store.iter_files(source_kind="file") if "broken" in str(r.path)]
        assert pending and pending[0].status != "INDEXED", "it is read again next run"
