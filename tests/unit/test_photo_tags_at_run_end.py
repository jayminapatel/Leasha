r"""Florence-2 tags photos at the end of a run, not as each photo is read.

Layer: L2

2026-10-04, the owner ("pictures very slow"): per photo on an idle laptop,
Florence-2 10.4 s, OCR 2.3, faces 0.9, CLIP 0.3. Tagging was three quarters
of a photo library's time and everything else waited behind it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import make_error
from app.extract import florence_tagger
from app.storage.sqlite_store import SqliteStore


@pytest.fixture(autouse=True)
def _not_deferred_after():
    yield
    florence_tagger.defer(False)


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _no_text_photo(store, name):
    file_id = store.upsert_file(path=f"/photos/{name}", size_bytes=1, mtime_ns=1,
                                ext="jpg", source_kind="file")
    store.mark_skipped(file_id, make_error("ERR_NO_TEXT_LAYER", "extract.ocr",
                                           path=f"/photos/{name}"))
    return file_id


def test_a_photo_read_during_a_run_is_not_tagged_there(tmp_path, monkeypatch):
    """*2026-10-04, later*: while the run reads pictures, neither tagging nor text
    reading happens there - the owner's order moved both to the run's end
    (`test_pictures_faces_then_text.py`). This asserted that text was read in
    place; it is not any more."""
    from app.extract import ocr
    from app.core.errors import AppErrorException

    asked, read = [], []
    monkeypatch.setattr(ocr, "ocr_image", lambda path: read.append(path))
    monkeypatch.setattr(florence_tagger, "available", lambda: True)
    monkeypatch.setattr(florence_tagger, "tag_image", lambda path: asked.append(path))
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"not really a jpeg")

    florence_tagger.defer(True)
    with pytest.raises(AppErrorException):
        list(ocr.OcrExtractor().extract(photo))
    assert asked == [] and read == []


def _pipeline(store):
    from app.index import pipeline as module

    built = module.Pipeline.__new__(module.Pipeline)
    built.store = store
    built._stop = threading.Event()
    built._stop.set()                  # as it is at every run's end
    built._interrupted = False         # nobody pressed Stop
    built.governor = SimpleNamespace(
        wait_while_throttled=lambda should_stop: SimpleNamespace(action="go"),
        paused_seconds=0.0, pauses=0, paused=False, pause_reason="")
    built._log = __import__("app.core.logging", fromlist=["logger"]).logger
    built._announce_phase = lambda stats, on_progress, phase: None
    built._drain_unembedded = lambda stats, **_how: None
    return built


def test_the_run_end_tags_them_and_stops_asking_about_the_rest(store, monkeypatch):
    described = _no_text_photo(store, "dog.jpg")
    blank = _no_text_photo(store, "wall.jpg")
    asked = []

    def tag(path):
        asked.append(Path(path).name)
        if Path(path).name == "dog.jpg":
            return florence_tagger.FlorenceResult(caption="A dog on a beach",
                                                  tags=["dog", "beach"], elapsed_s=1.0)
        return None

    monkeypatch.setattr(florence_tagger, "available", lambda: True)
    monkeypatch.setattr(florence_tagger, "tag_image", tag)
    stats = SimpleNamespace(enrichment_counts={}, current="")
    built = _pipeline(store)

    built._drain_photo_tags(stats)

    assert sorted(asked) == ["dog.jpg", "wall.jpg"]
    assert stats.enrichment_counts["photo_tags"] == 1
    status = dict(store.conn.execute("SELECT id, status FROM files").fetchall())
    assert status[described] == "INDEXED" and status[blank] == "SKIPPED"
    text = store.conn.execute("SELECT text, label FROM chunks WHERE file_id = ?",
                              (described,)).fetchone()
    assert text["label"] == "AI description" and "Tags: dog, beach" in text["text"]

    asked.clear()
    built._drain_photo_tags(stats)               # the next run
    assert asked == [], "neither is offered again"


def test_the_run_end_gives_its_descriptions_meaning_in_the_same_run(store, monkeypatch):
    """2026-10-10, found reviewing the model sequencing: with "Make text searchable
    first" on (the default), the run-end steps parked their new passages for a
    feeder the teardown had already stopped, so a description waited for the next
    run for its vector. Both steps now embed them where they are."""
    import collections

    photo = _no_text_photo(store, "dog.jpg")
    monkeypatch.setattr(florence_tagger, "available", lambda: True)
    monkeypatch.setattr(florence_tagger, "tag_image", lambda path: florence_tagger.FlorenceResult(
        caption="A dog on a beach", tags=["dog"], elapsed_s=1.0))
    built = _pipeline(store)
    del built._drain_unembedded                  # the real one, this time
    built.config = SimpleNamespace(two_phase=True, embed_batch=8)
    built._parked = collections.deque()
    built._stats_ref = SimpleNamespace(embed_batches=0)
    embedded = []
    built._embed_pending = lambda pending: embedded.extend(fid for _cid, fid, _t in pending)
    stats = SimpleNamespace(enrichment_counts={}, current="", embed_batch=0)

    built._drain_photo_tags(stats)

    assert embedded == [photo], "the description has its vector before the run ends"
    assert not built._parked, "nothing is left for a feeder that is gone"
