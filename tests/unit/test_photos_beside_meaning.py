r"""The photo passes run beside the meaning model when they are on another
processor.

Layer: L3

2026-10-10, a review of the run's tail. The end of a text-first run waits for
the meaning model's backlog - days on the owner's laptop (5.9 million passages
at 5-13 a second) - and the photo passes (Florence-2 descriptions, text read
from pictures) waited behind it, although the meaning model is fastest on the
processor there and the photo models on the graphics card. They now run beside
it when every photo model is on a different processor from the meaning model,
and after it, as before, when one is not. Their own passages are embedded once
at the run's end (`_drain_unembedded(at_run_end=True)`, from another change).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.core.errors import make_error
from app.extract import florence_tagger
from app.index.embedder import Embedder
from app.index.pipeline import (
    PHASE_MEANING, PHASE_PHOTO_TAGS, Pipeline, PipelineConfig,
)
from app.index.resources import ResourceLimits
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore

NO_PACING = ResourceLimits(pause_on_battery=False, cpu_percent=0)


class Vectors:
    """Like the real store where it matters: a delete by file removes that
    file's rows, and a chunk embedded twice without one is an error."""

    def __init__(self) -> None:
        self.rows: dict[int, int] = {}
        self.calls: dict[str, int] = {}

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        gone = {int(one) for one in file_ids}
        self.rows = {cid: fid for cid, fid in self.rows.items() if fid not in gone}

    def add(self, *, chunk_ids, file_ids, vectors, **_kw) -> int:
        for cid, fid in zip(list(chunk_ids), list(file_ids), strict=True):
            assert int(cid) not in self.rows, f"chunk {cid} was embedded twice"
            self.rows[int(cid)] = int(fid)
        return len(list(chunk_ids))

    def maybe_create_index(self, **_kw) -> bool:
        self.calls["create_index"] = self.calls.get("create_index", 0) + 1
        return False

    def maybe_compact(self, *, force: bool = False) -> bool:
        key = "compact_forced" if force else "compact"
        self.calls[key] = self.calls.get(key, 0) + 1
        return False

    def __getattr__(self, name):
        return lambda *args, **kwargs: False


@pytest.fixture(autouse=True)
def _florence_restored():
    yield
    florence_tagger.defer(False)


def _docs(root: Path, count: int) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for n in range(count):
        (root / f"f{n:02d}.txt").write_text(
            f"pump station {n} commissioning report", encoding="utf-8")
    return root


def _waiting_photos(store: SqliteStore, folder: Path, count: int) -> list[int]:
    """Photos an earlier run read with no text, waiting for their description.
    Outside the walked folder, so only the end-of-run pass touches them."""
    folder.mkdir(parents=True, exist_ok=True)
    ids = []
    for n in range(count):
        path = folder / f"p{n:02d}.jpg"
        path.write_bytes(b"not really a jpeg")
        file_id = store.upsert_file(path=str(path), size_bytes=1, mtime_ns=1,
                                    ext="jpg", source_kind="file")
        store.mark_skipped(file_id, make_error("ERR_NO_TEXT_LAYER", "extract.ocr",
                                               path=str(path)))
        ids.append(file_id)
    return ids


def _florence(monkeypatch, *, delay_s: float = 0.0,
              started: "list[float] | None" = None) -> list[str]:
    asked: list[str] = []

    def tag(path):
        asked.append(Path(path).name)
        if started is not None:
            started.append(time.perf_counter())
        if delay_s:
            time.sleep(delay_s)
        return florence_tagger.FlorenceResult(
            caption=f"A pump station photo {Path(path).stem}", tags=["pump"], elapsed_s=0.0)

    monkeypatch.setattr(florence_tagger, "available", lambda: True)
    monkeypatch.setattr(florence_tagger, "tag_image", tag)
    return asked


def _encoder(delay_s: float = 0.0):
    def encode(texts):
        if delay_s:
            time.sleep(delay_s)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]
    return encode


def _scalar(store: SqliteStore, sql: str, *args) -> int:
    return int(store.conn.execute(sql, args).fetchone()[0])


# --- T2: the photo passes beside the meaning model ----------------------------------

def _timed_run(tmp_path: Path, monkeypatch, *, beside: "tuple[bool, ...]",
               phases: "list[str] | None" = None, docs: int = 12, pictures: int = 12):
    """A text-first run with a meaning backlog and photos waiting, both slow.

    A description is one short passage, so its batch is quick for the model;
    a document batch is the slow thing - as on the owner's machine, where a
    batch is hundreds of passages and a photo's description is one."""
    photo_started: list[float] = []
    document_done: list[float] = []
    _florence(monkeypatch, delay_s=0.25, started=photo_started)

    def encode(texts):
        quick = all(text.startswith("AI description") for text in texts)
        time.sleep(0.01 if quick else 0.15)
        if not quick:
            document_done.append(time.perf_counter())
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(Pipeline, "_picture_models_on_gpu", lambda self: beside)
    vectors = Vectors()
    with SqliteStore(tmp_path / "index.db") as store:
        photos = _waiting_photos(store, tmp_path / "photos", pictures)
        config = PipelineConfig(walk=WalkConfig(roots=[_docs(tmp_path / "docs", docs)]),
                                workers=1, embed_batch=1, limits=NO_PACING,
                                prune_missing=False)
        pipeline = Pipeline(store, vectors, Embedder(dim=4, encoder=encode), config)
        began = time.perf_counter()
        stats = pipeline.run(
            on_progress=None if phases is None else (lambda s: phases.append(s.phase)))
        took = time.perf_counter() - began
        # The documents' passages; the descriptions' own vectors are the
        # run-end embedding's (`_drain_unembedded(at_run_end=True)`).
        left = _scalar(store, "SELECT COUNT(*) FROM chunks WHERE embedded = 0 "
                              "AND COALESCE(label, '') != 'AI description'")
        indexed = _scalar(store, "SELECT COUNT(*) FROM files WHERE status = 'INDEXED'")
    # How many document batches the model finished after the first photo was
    # being described: the overlap, measured rather than inferred from a clock
    # a loaded test machine stretches.
    overlap = sum(1 for done in document_done if photo_started and done > photo_started[0])
    pipeline.overlap = overlap
    return took, stats, left, indexed, len(photos), pipeline


def test_photo_passes_run_beside_the_meaning_model_on_another_processor(tmp_path, monkeypatch):
    """Meaning on the processor (the fake has no graphics card), the photo models
    on the card: the two waits overlap instead of adding up."""
    after, stats_a, left_a, indexed_a, photos, first = _timed_run(
        tmp_path / "after", monkeypatch, beside=(False, False))
    beside, stats_b, left_b, indexed_b, _n, pipeline = _timed_run(
        tmp_path / "beside", monkeypatch, beside=(True, True))

    for stats, left, indexed in ((stats_a, left_a, indexed_a), (stats_b, left_b, indexed_b)):
        assert stats.enrichment_counts["photo_tags"] == photos
        assert left == 0, "a passage was left without its vector"
        assert indexed == 12 + photos
    assert pipeline._pictures_beside is not None and not pipeline._pictures_beside.is_alive()
    assert pipeline._beside_view.wrote, "the passes beside the model wrote nothing"
    # 12 document batches (0.15 s, plus the feeder's 0.1 s look for a parked
    # one) and 12 photos at 0.25 s: ~6.3 s one after the other, ~4.3 s beside.
    # The wall time is printed, not asserted - four other test runs shared this
    # machine's processor when it was written and stretched both ways at random;
    # the overlap is what the change is, and it does not depend on the clock.
    print(f"T2 wall time: after {after:.2f}s, beside {beside:.2f}s, "
          f"document batches finished while photos were described: "
          f"after {first.overlap}, beside {pipeline.overlap}")
    assert first.overlap == 0, "the photo passes started before the backlog ended"
    assert pipeline.overlap >= 3, (
        f"only {pipeline.overlap} document batch(es) overlapped the photo pass")


def test_on_the_same_processor_they_stay_one_after_the_other(tmp_path, monkeypatch):
    started = []
    original = Pipeline._start_pictures_beside_meaning

    def spy(self, stats):
        thread = original(self, stats)
        started.append(thread)
        return thread

    monkeypatch.setattr(Pipeline, "_start_pictures_beside_meaning", spy)
    _took, stats, left, _indexed, photos, _p = _timed_run(
        tmp_path, monkeypatch, beside=(False, True))      # OCR shares the processor
    assert started == [None]
    assert stats.enrichment_counts["photo_tags"] == photos and left == 0


def test_the_page_says_meaning_then_photos_and_never_flickers(tmp_path, monkeypatch):
    phases: list[str] = []
    # A short meaning backlog and many photos: the model is done long before
    # the photo pass, so the page must move on to naming it.
    _timed_run(tmp_path, monkeypatch, beside=(True, True), phases=phases,
               docs=4, pictures=16)
    said = [phase for n, phase in enumerate(phases) if n == 0 or phases[n - 1] != phase]
    assert PHASE_MEANING in said and PHASE_PHOTO_TAGS in said, str(said)
    assert said.index(PHASE_MEANING) < said.index(PHASE_PHOTO_TAGS)
    assert said.count(PHASE_MEANING) == 1 and said.count(PHASE_PHOTO_TAGS) == 1, said


def test_a_stop_ends_both_and_leaves_nothing_running(tmp_path, monkeypatch):
    _florence(monkeypatch, delay_s=0.1)
    monkeypatch.setattr(Pipeline, "_picture_models_on_gpu", lambda self: (True, True))
    holder: dict = {}

    def encode(texts):
        time.sleep(0.1)
        beside = getattr(holder.get("p"), "_pictures_beside", None)
        if beside is not None and beside.is_alive() and not holder.get("stopped"):
            holder["stopped"] = time.perf_counter()
            holder["p"].request_stop()
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    with SqliteStore(tmp_path / "index.db") as store:
        _waiting_photos(store, tmp_path / "photos", 30)
        config = PipelineConfig(walk=WalkConfig(roots=[_docs(tmp_path / "docs", 20)]),
                                workers=1, embed_batch=1, limits=NO_PACING,
                                prune_missing=False)
        pipeline = Pipeline(store, Vectors(), Embedder(dim=4, encoder=encode), config)
        holder["p"] = pipeline
        pipeline.run()
        ended = time.perf_counter()
        described = _scalar(store, "SELECT COUNT(*) FROM chunks WHERE label = 'AI description'")
    assert "stopped" in holder, "the photo passes never started beside the model"
    assert not pipeline._pictures_beside.is_alive()
    assert ended - holder["stopped"] < 5.0
    assert described < 30, "the stop did not reach the photo passes"


def test_what_they_wrote_is_embedded_once_at_the_run_end(tmp_path, monkeypatch):
    """Not by the passes beside the model - their `_embed_pending` would race the
    feeder's - but once, by the run's end, in the form that embeds there."""
    calls: list[dict] = []

    def drain(self, stats, *, at_run_end=False):
        calls.append({"at_run_end": at_run_end,
                      "feeder_alive": self._feeder_thread.is_alive()
                      if getattr(self, "_feeder_thread", None) else False,
                      "thread": threading.current_thread().name})

    monkeypatch.setattr(Pipeline, "_drain_unembedded", drain)
    _timed_run(tmp_path, monkeypatch, beside=(True, True), docs=6, pictures=4)
    at_end = [call for call in calls if call["at_run_end"]]
    assert len(at_end) == 1, calls
    assert not at_end[0]["feeder_alive"]
    assert all(call["thread"] != "pictures-beside-meaning" for call in calls), calls


def test_without_the_run_end_form_nothing_is_called_with_it(tmp_path):
    built = Pipeline.__new__(Pipeline)
    built._drain_unembedded = lambda stats: None
    assert not built._drain_takes_run_end()
    built._drain_unembedded = lambda stats, *, at_run_end=False: None
    assert built._drain_takes_run_end()


def test_a_meaning_model_failure_ends_the_photo_passes_too(tmp_path, monkeypatch):
    """The run raises the model's error, as it always has - and the photo passes
    beside it are told, not left describing photos for a run that has ended."""
    _florence(monkeypatch, delay_s=0.1)
    monkeypatch.setattr(Pipeline, "_picture_models_on_gpu", lambda self: (True, True))
    holder: dict = {}

    def encode(texts):
        time.sleep(0.1)
        beside = getattr(holder.get("p"), "_pictures_beside", None)
        if beside is not None and beside.is_alive():
            raise RuntimeError("the model broke")
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    with SqliteStore(tmp_path / "index.db") as store:
        _waiting_photos(store, tmp_path / "photos", 40)
        config = PipelineConfig(walk=WalkConfig(roots=[_docs(tmp_path / "docs", 12)]),
                                workers=1, embed_batch=1, limits=NO_PACING,
                                prune_missing=False)
        pipeline = Pipeline(store, Vectors(), Embedder(dim=4, encoder=encode), config)
        holder["p"] = pipeline
        with pytest.raises(Exception):          # the model's error, as the embedder words it
            pipeline.run()
    assert pipeline._pictures_beside is not None
    assert not pipeline._pictures_beside.is_alive()
