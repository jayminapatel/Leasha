r"""The media tail's own run leaves the end of the run to the run that
started it.

Layer: L3

2026-10-10, a review of the run's tail:

* The media tail (`app/index/media_backlog.py`) runs a whole second
  `Pipeline.run` over the same store near the outer run's end, and that run did
  the outer run's end-of-run work - the photo passes, the forced vector
  rewrite, the word-index merge, the completions file, the `last_run` record -
  a moment before the outer run did all of it again.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import make_error
from app.extract import florence_tagger
from app.index import pipeline as module
from app.index.embedder import Embedder
from app.index.pipeline import (
    Pipeline, PipelineConfig,
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


# --- T3 and T5: the media tail's run leaves the end to the outer run -----------------

def _spied_run(tmp_path: Path, monkeypatch, *, cls=Pipeline, light: bool = False):
    calls: dict[str, int] = {}

    def count(name):
        def spy(*_a, **_k):
            calls[name] = calls.get(name, 0) + 1
        return spy

    for name in ("_drain_photo_tags", "_drain_picture_text", "_write_completions",
                 "_drain_face_cluster"):
        monkeypatch.setattr(cls, name, lambda self, *a, _n=name, **k: count(_n)())
    vectors = Vectors()
    with SqliteStore(tmp_path / "index.db") as store:
        store.optimize_fts = lambda: count("optimize_fts")() or True
        real_set_state = store.set_state

        def set_state(key, value):
            if key in ("last_run", "last_run_stats"):
                count(key)()
            return real_set_state(key, value)

        store.set_state = set_state
        config = PipelineConfig(walk=WalkConfig(roots=[_docs(tmp_path / "docs", 2)]),
                                workers=1, limits=NO_PACING, prune_missing=False,
                                bulk_fts="on", light=light)
        pipeline = cls(store, vectors, Embedder(dim=4, encoder=_encoder()), config)
        pipeline._faces_since_cluster = 0
        pipeline.run()
    calls.update(vectors.calls)
    return calls, pipeline


def test_an_ordinary_run_does_its_end_once_each(tmp_path, monkeypatch):
    calls, _p = _spied_run(tmp_path, monkeypatch)
    for name in ("_drain_photo_tags", "_drain_picture_text", "_write_completions",
                 "optimize_fts", "last_run", "last_run_stats",
                 "create_index", "compact_forced"):
        assert calls.get(name) == 1, (name, calls)


def test_the_media_tails_run_leaves_the_end_to_the_run_that_started_it(tmp_path, monkeypatch):
    from app.index import media_backlog

    tail = media_backlog._backlog_pipeline_class()
    assert tail.outer_run_finishes is True and Pipeline.outer_run_finishes is False

    class Tail(Pipeline):
        outer_run_finishes = True

    calls, _p = _spied_run(tmp_path, monkeypatch, cls=Tail)
    for name in ("_drain_photo_tags", "_drain_picture_text", "_write_completions",
                 "optimize_fts", "last_run", "last_run_stats",
                 "create_index", "compact_forced", "compact"):
        assert name not in calls, (name, calls)


def test_faces_the_tail_left_are_grouped_by_the_outer_run(tmp_path, monkeypatch):
    import dataclasses

    from app.index import media_backlog
    from app.index.walker import Candidate

    class FakeTail:
        def __init__(self, *args, **kwargs):
            self._faces_since_cluster = 0

        def request_stop(self):
            pass

        def run(self, on_progress=None):
            self._faces_since_cluster = 3
            return module.IndexStats()

    monkeypatch.setattr(media_backlog, "_backlog_pipeline_class", lambda: FakeTail)
    monkeypatch.setattr(media_backlog, "applies", lambda config: True)
    monkeypatch.setattr(media_backlog, "queued_candidates", lambda store: iter([
        Candidate(path=tmp_path / "film.mp4", size_bytes=1, mtime_ns=1, priority=0)]))
    monkeypatch.setattr(media_backlog, "queued_paths", lambda store: [])
    outer = SimpleNamespace(
        config=dataclasses.replace(PipelineConfig(walk=WalkConfig(roots=[]))),
        store=None, vectors=None, embedder=None, governor=None, image_embedder=None,
        image_vectors=None, phash_computer=None, _interrupted=False,
        _faces_since_cluster=2, _announce_phase=lambda *a: None)
    media_backlog.drain(outer, module.IndexStats())
    assert outer._faces_since_cluster == 5
