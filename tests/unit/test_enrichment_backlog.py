r"""The unified enrichment backlog. Work order 0i section 2a.

Layer: L3

Two kinds are real and proven here: unembedded_chunk (a genuine drain,
migrated from the old _drain_unembedded call site into the new
_run_enrichment_drains loop with no behaviour change - test_embedding_gap.py
and test_ocr_passes.py already pin the underlying mechanisms and both stay
green) and ocr_pending (an existing requeue mechanism, now additionally
counted). A third kind, image_tag, is declared but not drained - see the
dated note on this item in the work order for why guessing at its semantics
would be inventing product behaviour rather than migrating it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.index.embedder import Embedder
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore

DIM = 8


def _encode(texts):
    out = []
    for text in texts:
        seed = sum(ord(ch) for ch in text) or 1
        raw = list(((seed * (i + 3)) % 17) / 17 for i in range(DIM))
        length = sum(value * value for value in raw) ** 0.5 or 1.0
        out.append(list(value / length for value in raw))
    return out


def _embedder():
    return Embedder(dim=DIM, encoder=_encode)


class FakeVectors:
    def __init__(self):
        self.rows = dict()

    def ensure_table(self):
        pass

    def delete_by_file_ids(self, file_ids):
        wanted = list(int(one) for one in file_ids)
        for chunk_id, file_id in list(self.rows.items()):
            if file_id in wanted:
                del self.rows[chunk_id]

    def add(self, *, chunk_ids, file_ids, vectors):
        for chunk_id, file_id in zip(chunk_ids, file_ids, strict=True):
            self.rows[int(chunk_id)] = int(file_id)
        return len(list(chunk_ids))

    def maybe_compact(self, **_kwargs):
        return False

    def maybe_create_index(self, **_kwargs):
        return False

    def count(self):
        return len(self.rows)


def _corpus(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "doc0.txt").write_text(
        "Pump station commissioning report for the northern site.",
        encoding="utf-8")
    return root


def _run(store, root, vectors, embedder, **config):
    pipeline = Pipeline(
        store, vectors, embedder,
        PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, **config),
    )
    return pipeline.run()


def test_unembedded_chunk_kind_is_recorded(tmp_path):
    """A migrated behaviour, not a new one - matches vectors_repaired exactly."""
    root = _corpus(tmp_path / "docs")
    vectors = FakeVectors()

    with SqliteStore(tmp_path / "index.db") as store:
        first = _run(store, root, vectors, _embedder())
        assert first.chunks and vectors.count() == first.chunks

        store.mark_all_unembedded()

        second = _run(store, root, vectors, _embedder(), force=False)

        assert second.enrichment_counts.get("unembedded_chunk") == first.chunks
        assert second.vectors_repaired == first.chunks


def test_a_healthy_run_records_zero_not_absence(tmp_path):
    """0 means 'ran, found nothing outstanding' - present in the dict, not
    missing from it. See the comment on IndexStats.enrichment_counts."""
    root = _corpus(tmp_path / "docs")
    vectors = FakeVectors()

    with SqliteStore(tmp_path / "index.db") as store:
        stats = _run(store, root, vectors, _embedder())

    assert stats.enrichment_counts.get("unembedded_chunk") == 0


def test_a_failing_kind_does_not_stop_the_run(tmp_path, monkeypatch):
    """Bounded and never fatal - the same promise _drain_unembedded already
    made, now extended to the dispatcher wrapping every registered kind."""
    root = _corpus(tmp_path / "docs")
    vectors = FakeVectors()

    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = Pipeline(
            store, vectors, _embedder(),
            PipelineConfig(walk=WalkConfig(roots=[root]), workers=1),
        )

        def explode(_stats):
            raise RuntimeError("deliberate")

        monkeypatch.setattr(pipeline, "_drain_unembedded", explode)
        stats = pipeline.run()

    assert stats.indexed >= 1, "one kind failing must not stop the whole run"


def test_ocr_pending_kind_is_counted_from_held_pdfs(tmp_path):
    """_no_text_layer_candidates already existed; this only proves the new
    additive count matches what it actually requeues - no behaviour change
    to what gets requeued or when."""
    root = tmp_path / "docs"
    root.mkdir()
    pdf = root / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 not a real pdf but present on disk")

    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            path=str(pdf), size_bytes=pdf.stat().st_size,
            mtime_ns=pdf.stat().st_mtime_ns, source_kind="file")
        from app.core.errors import make_error
        error = make_error("ERR_NO_TEXT_LAYER", "extract.pdf", path=str(pdf))
        store.mark_skipped(file_id, error)

        pipeline = Pipeline(
            store, FakeVectors(), _embedder(),
            PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                            ocr_mode="images"),
        )
        stats = pipeline.run()

    assert stats.enrichment_counts.get("ocr_pending") == 1


def test_print_enrichment_counts_is_silent_when_empty(capsys):
    from app.cli import _print_enrichment_counts

    class Stats:
        enrichment_counts = dict()

    _print_enrichment_counts(Stats())
    assert capsys.readouterr().out == ""


def test_print_enrichment_counts_formats_plain_words(capsys):
    from app.cli import _print_enrichment_counts

    class Stats:
        enrichment_counts = dict(unembedded_chunk=214, ocr_pending=0, image_tag=30)

    _print_enrichment_counts(Stats())
    out = capsys.readouterr().out
    assert "214 vector(s) repaired" in out
    assert "30 photo(s) tagged" in out
    assert "ocr_pending" not in out
