"""The conductor inside an index run: passes side by side, the face pool, resources.

Layer: L3

Work order `indexing-cockpit-and-conductor` 5b, 5c, 5d and the cockpit's
resource strip. Held here:

* the run's end passes run side by side, each photo still described before
  its text is read, pages read, every new passage given meaning in the run,
  and the document cards after all of them;
* the picture worker runs photos on a pool when faces are on the processor
  and one at a time on the graphics card, and a flush waits for every photo
  handed over before it (M6);
* the CLIP batch and the pending lists lose nothing with several threads;
* the decode cache holds one picture per pool thread, plus one;
* `IndexStats.resources` is filled from the governor's own reading and
  crosses the process boundary like the lanes;
* the reader ceiling reaches the pipeline's config.
"""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.extract import florence_tagger
from app.index import run_events as ev
from app.index.conductor import Conductor, budget_for
from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import IndexStats, Pipeline, PipelineConfig
from app.index.stages import StageClock
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore

OWNER = SimpleNamespace(physical_cores=10, logical_processors=12,
                        performance_cores=2, efficiency_cores=8)


@pytest.fixture(autouse=True)
def _not_deferred_after():
    yield
    florence_tagger.defer(False)


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _waiting(store, name, code):
    from app.core.errors import make_error

    file_id = store.upsert_file(path=f"/p/{name}", size_bytes=1, mtime_ns=1, ext="jpg",
                                source_kind="file")
    store.mark_skipped(file_id, make_error(code, "extract.ocr", path=f"/p/{name}"))
    return file_id


def _bare(store):
    """A pipeline as the run's end finds it, built without `__init__`."""
    built = Pipeline.__new__(Pipeline)
    built.store = store
    built._stop = threading.Event()
    built._stop.set()                  # as it is at every run's end
    built._interrupted = False
    built.thread_priority_only = False
    built.governor = SimpleNamespace(
        wait_while_throttled=lambda should_stop=None: SimpleNamespace(action="go"),
        paused_seconds=0.0, pauses=0, paused=False, pause_reason="")
    built._log = __import__("app.core.logging", fromlist=["logger"]).logger
    built.config = SimpleNamespace(people_recognition_enabled=True, embed_batch=64,
                                   two_phase=True, walk=None, dedup_chunks=False)
    built._stats_ref = IndexStats()
    built._budget = budget_for(OWNER, readers=4)
    built._conductor = Conductor(built._budget.cpu_slots)
    return built


# --- 5b: the run's end side by side ------------------------------------------------

def test_the_end_passes_run_side_by_side_and_each_photo_keeps_its_order(store, monkeypatch):
    from app.extract import ocr

    photos = [_waiting(store, f"beach{n}.jpg", "ERR_PICTURE_TEXT_LATER") for n in range(4)]
    _waiting(store, "letter.jpg", "ERR_PAGE_TEXT_LATER")
    events: list[tuple[str, str]] = []
    lock = threading.Lock()

    def tag(path):
        time.sleep(0.15)                          # a description takes a while
        with lock:
            events.append(("describe", Path(path).name))
        return florence_tagger.FlorenceResult(caption=f"A view of {Path(path).stem}",
                                              tags=["sea"], elapsed_s=0.15)

    def read(path):
        with lock:
            events.append(("text", Path(path).name))
        text = "Dear Sir" if Path(path).name == "letter.jpg" else "SEA VIEW"
        return SimpleNamespace(text=text, empty=False, engine_missing=False)

    monkeypatch.setattr(florence_tagger, "available", lambda: True)
    monkeypatch.setattr(florence_tagger, "tag_image", tag)
    monkeypatch.setattr(ocr, "ocr_image", read)
    built = _bare(store)
    embedded: list[int] = []
    built._embed_pending = lambda pending: (embedded.extend(f for _c, f, _t in pending),
                                            pending.clear())
    stats = built._stats_ref

    built._passes_side_by_side(stats, None, cluster=False)

    names = [f"beach{n}.jpg" for n in range(4)]
    for name in names:
        assert events.index(("describe", name)) < events.index(("text", name)), \
            f"{name} is described before its text is read"
    last_describe = max(events.index(("describe", n)) for n in names)
    assert events.index(("text", "letter.jpg")) < last_describe, \
        "the page was read while the photos were still being described - side by side"
    assert sorted(n for step, n in events if step == "text") == sorted(names + ["letter.jpg"])
    # Four descriptions and five texts, counted once each, from two threads.
    assert stats.enrichment_counts["picture_text"] == 9
    assert stats.chunks == 9
    assert set(photos) <= set(embedded), "every new passage has its meaning in the run"
    assert len(embedded) == 9, "each passage embedded once, by the one meaning thread"
    assert "describe" not in stats.board.lane_board.lanes(), "a finished pass's lane goes"


def test_a_photo_whose_text_read_failed_is_not_taken_twice(store, monkeypatch):
    from app.core.errors import make_error
    from app.extract import ocr

    _waiting(store, "beach.jpg", "ERR_PICTURE_TEXT_LATER")
    reads = []
    monkeypatch.setattr(florence_tagger, "available", lambda: False)
    monkeypatch.setattr(ocr, "ocr_image", lambda path: (
        reads.append(path), SimpleNamespace(
            error=make_error("ERR_OCR_HELPER", "index.ocr_process", path=str(path)),
            text="", empty=True, engine_missing=False))[1])
    built = _bare(store)
    built._embed_pending = lambda pending: pending.clear()
    stats = built._stats_ref
    built._passes_side_by_side(stats, None, cluster=False)
    assert len(reads) == 1, "it waits for the next run, as it always did"
    assert stats.warned_by_code.get("ERR_OCR_HELPER") == 1


def test_a_stop_ends_the_side_by_side_passes(store, monkeypatch):
    from app.extract import ocr

    for n in range(6):
        _waiting(store, f"p{n}.jpg", "ERR_PICTURE_TEXT_LATER")
    built = _bare(store)
    described = []

    def tag(path):
        described.append(path)
        built._interrupted = True                # the person pressed Stop
        return None

    monkeypatch.setattr(florence_tagger, "available", lambda: True)
    monkeypatch.setattr(florence_tagger, "tag_image", tag)
    monkeypatch.setattr(ocr, "ocr_image", lambda p: SimpleNamespace(
        text="", empty=True, engine_missing=False))
    built._embed_pending = lambda pending: pending.clear()
    started = time.monotonic()
    built._passes_side_by_side(built._stats_ref, None, cluster=False)
    assert time.monotonic() - started < 5
    assert len(described) == 1, "the stop is met at the next photo"


def test_the_cards_come_after_the_photo_passes_in_a_whole_run(tmp_path, monkeypatch):
    from PIL import Image

    from app.extract import ocr as ocr_module

    root = tmp_path / "docs"
    root.mkdir()
    Image.new("RGB", (320, 240), (40, 90, 160)).save(root / "beach.png")
    (root / "letter.txt").write_text(
        "Dear Mr Patel, the Barnsley Dairy invoice 4471 is attached for the "
        "HACCP review of March. Please confirm the delivery dates. " * 4, encoding="utf-8")
    stamp = time.time() - 3600
    for path in root.iterdir():
        os.utime(path, (stamp, stamp))
    events: list[str] = []

    def engine(_source):
        return ([([0, 0, 1, 1], "SEA VIEW", 0.94)], 0.01)

    monkeypatch.setattr(ocr_module, "available", lambda: True)
    monkeypatch.setattr(ocr_module, "_load_engine", lambda: engine)
    real_ocr = ocr_module.ocr_image
    monkeypatch.setattr(ocr_module, "ocr_image",
                        lambda p, **kw: (events.append("text"), real_ocr(p, **kw))[1])
    monkeypatch.setattr(florence_tagger, "available", lambda: True)
    monkeypatch.setattr(florence_tagger, "tag_image", lambda p: (
        events.append("describe"),
        florence_tagger.FlorenceResult(caption="A beach", tags=["sea"], elapsed_s=0.0))[1])

    class Model:
        model = "fake"

        def has_model(self):
            return True

        def generate(self, *args, **kwargs):
            events.append("card")
            return SimpleNamespace(text="{}")

    class Vectors:
        def __getattr__(self, name):
            return lambda *a, **k: None

        def add(self, **kwargs):
            return len(kwargs.get("chunk_ids") or ())

    embedder = Embedder(dim=8, encoder=lambda texts: [
        l2_normalise([math.sin(len(t) + i) for i in range(8)]) for t in texts])
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = Pipeline(store, Vectors(), embedder, PipelineConfig(
            walk=WalkConfig(roots=[root]), workers=1, doc_cards_enabled=True,
            card_model=lambda: Model()))
        stats = pipeline.run()

    assert "card" in events and "describe" in events and "text" in events
    assert events.index("card") > max(i for i, e in enumerate(events) if e != "card"), \
        "the cards read what the photo passes wrote, so they come last"
    assert stats.resolved["conductor"]["cpu_slots"] >= 2


# --- 5d: the face pool -------------------------------------------------------------

def _loop_pipeline(pool_size):
    built = Pipeline.__new__(Pipeline)
    built.config = SimpleNamespace(embed_batch=10_000, people_recognition_enabled=True)
    built._stop = threading.Event()
    built.thread_priority_only = False
    built.governor = SimpleNamespace()
    built._log = __import__("app.core.logging", fromlist=["logger"]).logger
    built._pending_images = []
    built._picture_queue = queue.Queue(maxsize=Pipeline.PICTURE_QUEUE_SIZE)
    built._face_pool_size = lambda: pool_size
    state = {"now": 0, "peak": 0, "done": set(), "flushed": []}
    lock = threading.Lock()

    def steps(candidate, file_id, meta, *, video=True):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
            state["done"].add(file_id)

    built._picture_steps = steps
    built._flush_pending_images = lambda: state["flushed"].append(set(state["done"]))
    return built, state


@pytest.mark.parametrize("pool_size, peak", [(3, 3), (1, 1)])
def test_photos_run_on_a_pool_on_the_processor_and_one_at_a_time_on_the_card(pool_size, peak):
    from app.extract import picture

    built, state = _loop_pipeline(pool_size)
    thread = threading.Thread(target=built._picture_loop, daemon=True)
    thread.start()
    for n in range(9):
        built._picture_queue.put(("photo", SimpleNamespace(path=Path(f"{n}.jpg")), n, None))
    flushed = threading.Event()
    built._picture_queue.put(("flush", flushed))
    assert flushed.wait(5)
    for n in range(9, 12):
        built._picture_queue.put(("photo", SimpleNamespace(path=Path(f"{n}.jpg")), n, None))
    built._picture_queue.put(("finish",))
    thread.join(5)
    assert not thread.is_alive()
    assert state["peak"] == peak
    assert state["flushed"][0] >= set(range(9)), \
        "a flush waits for every photo handed over before it (M6)"
    assert state["flushed"][-1] == set(range(12))
    assert picture.kept_limit() == picture.KEEP, "the decode cache is put back"


def test_the_pool_size_follows_where_the_face_model_loaded(store, monkeypatch):
    import sys

    from app.extract import face_detect

    built = _bare(store)
    monkeypatch.setattr(face_detect, "_engine", None)
    assert built._face_pool_size() == 1, "not known until the model has loaded"
    monkeypatch.setattr(face_detect, "_engine", object())
    monkeypatch.setattr(face_detect, "_engine_is_gpu", False)
    assert sys.modules["app.extract.face_detect"] is face_detect
    assert built._face_pool_size() == 4
    monkeypatch.setattr(face_detect, "_engine_is_gpu", True)
    assert built._face_pool_size() == 1
    built._budget = None
    monkeypatch.setattr(face_detect, "_engine_is_gpu", False)
    assert built._face_pool_size() == 1, "no conductor, no pool"


def test_the_decode_cache_holds_one_picture_per_pool_thread_plus_one(tmp_path):
    from PIL import Image

    from app.extract import picture

    paths = []
    for n in range(6):
        path = tmp_path / f"{n}.png"
        Image.new("RGB", (8, 8), (n, n, n)).save(path)
        paths.append(path)
    picture.clear()
    try:
        assert picture.keep_at_least(5) == 5
        first = [picture.decoded(p) for p in paths[:5]]
        assert all(picture.decoded(p) is pic for p, pic in zip(paths[:5], first)), \
            "five threads' photos are all still decoded"
        picture.keep_default()
        assert picture.kept_limit() == picture.KEEP
        assert picture.decoded(paths[0]) is not first[0], "back to two kept"
    finally:
        picture.keep_default()
        picture.clear()


def test_the_clip_batch_loses_nothing_with_four_picture_threads(tmp_path):
    from PIL import Image

    built = Pipeline.__new__(Pipeline)
    built._clock = StageClock()
    built._stats_ref = IndexStats()
    built._pending_images, built._pending_pictures = [], []
    built._pending_frames, built._pending_phashes = {}, {}
    built._log = __import__("app.core.logging", fromlist=["logger"]).logger
    written: list[int] = []

    class Clip:
        choice = None

        def embed(self, pictures):
            time.sleep(0.002)
            return [[1.0, 0.0] for _ in pictures]

    class Images:
        def delete_by_file_ids(self, ids):
            pass

        def add_images(self, file_ids, vectors, **_kw):
            written.extend(file_ids)
            return len(file_ids)

    built.image_embedder, built.image_vectors = Clip(), Images()
    candidates = []
    for n in range(60):
        path = tmp_path / f"{n}.png"
        Image.new("RGB", (16, 16), (n, 0, 0)).save(path)
        candidates.append((SimpleNamespace(path=path, mtime_ns=1), n))

    def worker(share):
        for candidate, file_id in share:
            built._maybe_embed_image(candidate, file_id)
            if file_id % 7 == 0:
                built._flush_pending_images()      # a flush while others add

    threads = [threading.Thread(target=worker, args=(candidates[i::4],)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    built._flush_pending_images()
    assert sorted(written) == list(range(60)), "every photo's vector, written once"


# --- the cockpit's resource strip -----------------------------------------------------

def test_resources_come_from_the_governors_own_reading_at_most_every_two_seconds(store):
    from app.index.resources import Snapshot

    built = _bare(store)
    probes = []
    reading = Snapshot(rss_mb=812.4, system_cpu_percent=37.25, free_disk_gb=120.44)
    built.governor = SimpleNamespace(
        last_snapshot=reading, last_snapshot_at=time.monotonic(),
        probe_now=lambda: probes.append(1) or reading)
    with built._conductor.gpu("describe"):
        out = built._resources_reading()
    assert out["cpu_percent"] == 37.2 or out["cpu_percent"] == 37.3
    assert out["memory_mb"] == 812 and out["disk_free_gb"] == 120.4
    assert out["gpu_lane"] == "describe"
    assert out["memory_total_mb"] and out["memory_total_mb"] > 0
    assert probes == [], "a fresh governor reading is used as it is"
    built.governor.last_snapshot_at = time.monotonic() - 10
    built._resources_reading()
    built._resources_reading()
    assert probes == [1], "a stale one is refreshed, once in two seconds"
    json.dumps(out)


def test_resources_cross_the_process_boundary_like_the_lanes():
    stats = IndexStats()
    stats.board.resources = lambda: {"cpu_percent": 12.5, "memory_mb": 900,
                                     "memory_total_mb": 16000, "disk_free_gb": 80.0,
                                     "gpu_lane": "faces"}
    snap = stats.snapshot()
    assert snap.resources["gpu_lane"] == "faces"
    payload, _seq = ev.encode_stats(snap)
    line = json.loads(json.dumps(payload))
    rebuilt = ev.StatsRebuilder().rebuild(line)
    assert rebuilt.resources == snap.resources


# --- the reader ceiling -------------------------------------------------------------

def test_the_reader_ceiling_reaches_the_pipeline_config():
    from app.index.resolve import resolve_for_run
    from app.index.run_setup import build_pipeline_config

    settings = SimpleNamespace(index_tuning_mode="defaults", index_workers=0,
                               onnx_intra_op_threads=0, embed_batch=0,
                               embed_device="cpu")
    tuned = resolve_for_run(settings, profile=OWNER)
    assert tuned.worker_ceiling == 8 and tuned.workers == 4
    config = build_pipeline_config(settings, [Path(".")], tuned=tuned)
    assert config.worker_ceiling == 8
