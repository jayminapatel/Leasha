r"""Pictures in the owner's order: faces first, then descriptions, text last.

Layer: L2/L3

2026-10-04, the owner: "faces then description then ocr", then (a): "fast,
efficient and comprehensive". As a picture is read, its faces, picture search
and hash are done and it is sorted, by the OCR ladder's free rungs (name,
thumbnail whiteness), into a photo or a page. The run's end describes photos
- pages are not described - then reads text last: pages first, then photos.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import AppErrorException
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


# --- read time: sorted, not read ------------------------------------------------------

def _code_for(path, monkeypatch):
    from app.extract import ocr

    read = []
    monkeypatch.setattr(ocr, "ocr_image", lambda p: read.append(p))
    florence_tagger.defer(True)
    with pytest.raises(AppErrorException) as raised:
        list(ocr.OcrExtractor().extract(path))
    assert read == [], "no text is read while the run reads pictures"
    return raised.value.error.code


def test_a_white_page_is_a_page_and_a_photo_is_a_photo(tmp_path, monkeypatch):
    from PIL import Image

    page = tmp_path / "letter.png"
    Image.new("RGB", (400, 560), (255, 255, 255)).save(page)
    photo = tmp_path / "beach.png"
    Image.new("RGB", (400, 300), (40, 90, 160)).save(photo)

    assert _code_for(page, monkeypatch) == "ERR_PAGE_TEXT_LATER"
    assert _code_for(photo, monkeypatch) == "ERR_PICTURE_TEXT_LATER"


def test_both_wait_as_deferred_not_skipped():
    from app.core.file_state import DEFERRED_CODES

    assert {"ERR_PICTURE_TEXT_LATER", "ERR_PAGE_TEXT_LATER"} <= DEFERRED_CODES


# --- read time: faces, picture search, hash -------------------------------------------

def _pipeline(store):
    from app.index import pipeline as module

    built = module.Pipeline.__new__(module.Pipeline)
    built.store = store
    built.config = SimpleNamespace(people_recognition_enabled=True)
    built._stop = threading.Event()
    built._stop.set()                  # as it is at every run's end
    built._interrupted = False         # nobody pressed Stop
    built.governor = SimpleNamespace(
        wait_while_throttled=lambda should_stop: SimpleNamespace(action="go"))
    built._log = __import__("app.core.logging", fromlist=["logger"]).logger
    built._announce_phase = lambda stats, on_progress, phase: None
    built._drain_unembedded = lambda stats: None
    return built


@pytest.mark.parametrize("code", ["ERR_PICTURE_TEXT_LATER", "ERR_PAGE_TEXT_LATER",
                                  "ERR_NO_TEXT_LAYER"])
def test_a_picture_waiting_for_its_text_still_gets_faces_and_picture_search(
        store, monkeypatch, code):
    from app.core.errors import make_error
    from app.index import pipeline as module

    built = _pipeline(store)
    done = []
    built._photo_taken_at = lambda candidate: (None, False)
    built._photo_place = lambda candidate: None
    built._maybe_embed_image = lambda c, f: done.append("picture search")
    built._maybe_compute_phash = lambda c, f: done.append("hash")
    built._maybe_detect_faces = lambda c, f: done.append("faces")
    candidate = SimpleNamespace(path=Path("/p/a.jpg"), size_bytes=1, mtime_ns=1,
                                volume_id=None, relative_path=None)
    monkeypatch.setattr(module, "_candidate_row_key", lambda c: str(c.path))
    monkeypatch.setattr(module, "_candidate_parent_dir", lambda c: "/p")
    item = SimpleNamespace(candidate=candidate, content_hash=None,
                           error=make_error(code, "extract.ocr", path="/p/a.jpg"))

    built._record_skip(item)

    assert done == ["picture search", "hash", "faces"]


def test_faces_are_not_stored_twice_when_a_picture_is_read_again(store, monkeypatch):
    from app.extract import face_detect
    from app.index import face_clustering as fc

    built = _pipeline(store)
    built._face_stats = None
    found = face_detect.FaceDetection(bbox=(0, 0, 1, 1),
                                      embedding=fc.to_bytes([1.0, 0.0]), confidence=0.9)
    monkeypatch.setattr(face_detect, "detect_faces", lambda path: [found])
    file_id = store.upsert_file(path="/p/a.jpg", size_bytes=1, mtime_ns=1, ext="jpg",
                                source_kind="file")
    candidate = SimpleNamespace(path=Path("/p/a.jpg"))
    built._maybe_detect_faces(candidate, file_id)
    built._maybe_detect_faces(candidate, file_id)        # the next run, again
    assert store.conn.execute("SELECT count(*) FROM faces").fetchone()[0] == 1


# --- the run's end: describe photos, then text, pages first --------------------------

def _waiting(store, name, code):
    from app.core.errors import make_error

    file_id = store.upsert_file(path=f"/p/{name}", size_bytes=1, mtime_ns=1, ext="jpg",
                                source_kind="file")
    store.mark_skipped(file_id, make_error(code, "extract.ocr", path=f"/p/{name}"))
    return file_id


def test_the_end_describes_photos_then_reads_pages_first_then_photos(store, monkeypatch):
    from app.extract import ocr

    photo = _waiting(store, "beach.jpg", "ERR_PICTURE_TEXT_LATER")
    page = _waiting(store, "letter.jpg", "ERR_PAGE_TEXT_LATER")
    blank = _waiting(store, "fog.jpg", "ERR_PICTURE_TEXT_LATER")
    order = []

    def tag(path):
        order.append(("describe", Path(path).name))
        if Path(path).name == "beach.jpg":
            return florence_tagger.FlorenceResult(caption="A beach", tags=["sea"],
                                                  elapsed_s=1.0)
        return None

    def read(path):
        order.append(("text", Path(path).name))
        text = "Dear Sir" if Path(path).name == "letter.jpg" else ""
        return SimpleNamespace(text=text, empty=not text, engine_missing=False)

    monkeypatch.setattr(florence_tagger, "available", lambda: True)
    monkeypatch.setattr(florence_tagger, "tag_image", tag)
    monkeypatch.setattr(ocr, "ocr_image", read)
    stats = SimpleNamespace(enrichment_counts={}, current="")

    _pipeline(store)._drain_picture_text(stats)

    described = [name for step, name in order if step == "describe"]
    read_text = [name for step, name in order if step == "text"]
    assert sorted(described) == ["beach.jpg", "fog.jpg"], "pages are not described"
    assert order.index(("text", "letter.jpg")) > max(
        order.index(("describe", n)) for n in described), "text is read last"
    assert read_text[0] == "letter.jpg", "pages first"
    assert sorted(read_text) == ["beach.jpg", "fog.jpg", "letter.jpg"]

    state = {row[0]: (row[1], row[2]) for row in store.conn.execute(
        "SELECT id, status, skip_code FROM files")}
    assert state[photo] == ("INDEXED", None)
    assert state[page] == ("INDEXED", None)
    assert state[blank] == ("SKIPPED", "ERR_NO_TEXT_LAYER"), "nothing to say, settled"
    labels = sorted(r[0] for r in store.conn.execute("SELECT label FROM chunks"))
    assert labels == ["AI description", "Text read from the image"]


def test_the_runs_end_steps_run_although_the_threads_were_told_to_unwind(store, monkeypatch):
    """*Found 2026-10-04 by the pipeline tests*: `Pipeline._stop` is set at the end
    of every run to unwind its threads, so the end-of-run steps that checked it -
    photo tags, picture text and the last face grouping - quit at once in every
    real run. They answer to a real Stop (`_interrupted`) only."""
    from app.extract import ocr

    _waiting(store, "letter.jpg", "ERR_PAGE_TEXT_LATER")
    monkeypatch.setattr(florence_tagger, "available", lambda: False)
    monkeypatch.setattr(ocr, "ocr_image", lambda p: SimpleNamespace(
        text="Dear Sir", empty=False, engine_missing=False))
    built = _pipeline(store)
    assert built._stop.is_set() and not built._interrupted
    built._drain_picture_text(SimpleNamespace(enrichment_counts={}, current=""))
    assert store.conn.execute("SELECT status FROM files").fetchone()[0] == "INDEXED"

    built._interrupted = True                    # and a real Stop is still honoured
    later = _waiting(store, "memo.jpg", "ERR_PAGE_TEXT_LATER")
    built._drain_picture_text(SimpleNamespace(enrichment_counts={}, current=""))
    assert store.conn.execute("SELECT status FROM files WHERE id = ?",
                              (later,)).fetchone()[0] == "SKIPPED"
