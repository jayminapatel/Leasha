"""Every thread an index run uses says what it is doing (2026-10-14).

Layer: L3

"Nothing silent": the readers had a line each (`IndexStats.workers`), but the
picture worker, the meaning model's feeder, the writer, the walker and the
run's own end-of-run passes did their work with nothing on the page - a run
that was only looking for faces read as stuck. `IndexStats.lanes` gives each
of them a line. These tests hold:

* the lane API is safe from many threads at once and costs no clock per tick;
* the picture worker names "Looking for faces" and the photo while the face
  step runs, and goes idle once it has nothing in hand;
* the face backfill at a run's edge says so on the "pass" lane;
* lanes survive `snapshot()` as a deep copy and cross the process boundary;
* the heartbeat sees a lane move when nothing else does.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from app.index import live_progress
from app.index import run_events as ev
from app.index.embedder import Embedder
from app.index.live_progress import (
    LANE_DONE,
    LANE_IDLE,
    LANE_PASS,
    LANE_PICTURES,
    LANE_WORKING,
    LaneBoard,
    TASK_FACE_BACKFILL,
    TASK_FACES,
)
from app.index.pipeline import IndexStats, Pipeline, PipelineConfig
from app.index.resources import ResourceLimits
from app.index.walker import Candidate, WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore


class _Vectors:
    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        pass

    def add(self, *, chunk_ids, file_ids, vectors) -> int:
        return len(list(chunk_ids))


class _Governor:
    """Never throttles: these tests are about what is said, not pacing."""

    paused_seconds = 0.0
    pauses = 0
    paused = False
    pause_reason = ""

    def wait_while_throttled(self, should_stop=lambda: False):
        return SimpleNamespace(action="run")


def _pipeline(store, **config) -> Pipeline:
    pipeline = Pipeline(
        store, _Vectors(), Embedder(dim=4, encoder=lambda ts: [[0.1] * 4 for _ in ts]),
        PipelineConfig(walk=WalkConfig(roots=[]),
                       limits=ResourceLimits(pause_on_battery=False), **config))
    pipeline.governor = _Governor()
    return pipeline


def _wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# The lane API
# ---------------------------------------------------------------------------

def test_lanes_from_many_threads_lose_no_tick_and_keep_their_shape():
    board = LaneBoard()
    lanes = ("walker", "pictures", "meaning", "writer")

    def hammer(lane_id: str) -> None:
        lane = board.lane(lane_id)
        for n in range(2000):
            lane.set(task="Working", item=f"file {n % 7}.txt")
            lane.tick()
        lane.idle()

    threads = [threading.Thread(target=hammer, args=(lane_id,))
               for lane_id in lanes for _ in range(3)]
    for thread in threads:
        thread.start()
    # A reader taking snapshots throughout, as the run's thread does.
    while any(thread.is_alive() for thread in threads):
        json.dumps(board.lanes())
    for thread in threads:
        thread.join()

    out = board.lanes()
    assert list(out) == list(lanes), "lanes come out in the fixed order"
    for lane_id in lanes:
        assert out[lane_id]["done"] == 3 * 2000
        assert out[lane_id]["state"] == LANE_IDLE
        assert out[lane_id]["started_at"] == 0.0
        assert set(out[lane_id]) == {"lane", "label", "state", "task", "item", "path",
                                     "started_at", "done", "total", "device"}
    assert out["walker"]["label"] == "Finding files"


def test_a_path_item_keeps_its_name_and_its_whole_path_and_a_tick_reads_no_clock(
        monkeypatch):
    calls = []
    monkeypatch.setattr(live_progress, "now_wall", lambda: calls.append(1) or 100.0)
    lane = LaneBoard().lane(LANE_PICTURES)
    photo = Path("C:/Photos/2019/beach.jpg")
    lane.set(task=TASK_FACES, item=photo)
    lane.set(task=TASK_FACES, item=photo)          # the same words again
    for _ in range(50):
        lane.tick()
    plain = lane.as_dict()
    assert plain["item"] == "beach.jpg"
    assert plain["path"] == str(photo)
    assert plain["started_at"] == 100.0
    assert plain["done"] == 50
    assert len(calls) == 1, "only a change of task or item reads the clock"


def test_a_lane_that_was_dropped_is_gone_and_the_null_lane_takes_any_call():
    board = LaneBoard()
    board.lane("beside").set(task="x")
    board.drop("beside")
    assert "beside" not in board.lanes()
    live_progress.NULL_LANE.set(task="x", item="y", done=1)
    live_progress.NULL_LANE.tick(3)
    live_progress.NULL_LANE.idle()


# ---------------------------------------------------------------------------
# The picture worker
# ---------------------------------------------------------------------------

def test_the_picture_worker_names_the_face_scan_and_the_photo_then_goes_idle(
        tmp_path, monkeypatch):
    photo = tmp_path / "family.jpg"
    photo.write_bytes(b"not really a jpeg")
    inside = threading.Event()
    release = threading.Event()
    seen: dict = {}

    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, people_recognition_enabled=True)
        stats = IndexStats()
        pipeline._stats_ref = stats
        for name in ("_maybe_embed_image", "_maybe_compute_phash", "_maybe_embed_video"):
            monkeypatch.setattr(pipeline, name, lambda *a, **k: None)

        def slow_faces(candidate, file_id):
            seen["lanes"] = stats.snapshot().lanes
            inside.set()
            release.wait(5)

        monkeypatch.setattr(pipeline, "_maybe_detect_faces", slow_faces)
        pipeline._start_picture_work()
        try:
            candidate = Candidate(path=photo, size_bytes=1, mtime_ns=1)
            assert pipeline._picture_put(("photo", candidate, 1, None))
            assert inside.wait(5), "the face step never ran"
            during = seen["lanes"][LANE_PICTURES]
            assert during["task"] == TASK_FACES == "Looking for faces"
            assert during["item"] == "family.jpg"
            assert during["path"] == str(photo)
            assert during["state"] == LANE_WORKING
            assert during["started_at"] > 0
            release.set()
            assert _wait_for(lambda: stats.snapshot().lanes[LANE_PICTURES]["state"]
                             == LANE_IDLE), "the lane went on naming the last photo"
            after = stats.snapshot().lanes[LANE_PICTURES]
            assert after["item"] == "" and after["task"] == ""
            assert after["done"] == 1
        finally:
            release.set()
            pipeline._finish_picture_work()
        assert stats.snapshot().lanes[LANE_PICTURES]["state"] == LANE_DONE


# ---------------------------------------------------------------------------
# The run's own passes
# ---------------------------------------------------------------------------

def test_the_face_backfill_says_so_on_the_pass_lane(tmp_path, monkeypatch):
    from app.extract import face_detect

    photo = tmp_path / "old.jpg"
    photo.write_bytes(b"not really a jpeg")
    seen: list = []

    with SqliteStore(tmp_path / "index.db") as store:
        store.upsert_file(str(photo), size_bytes=1, mtime_ns=1,
                          status=FileStatus.INDEXED, ext="jpg")
        pipeline = _pipeline(store, people_recognition_enabled=True)
        stats = pipeline._stats_ref

        def fake_detect(path, data=None):
            seen.append(stats.snapshot().lanes[LANE_PASS])
            return []

        monkeypatch.setattr(face_detect, "available", lambda: True)
        monkeypatch.setattr(face_detect, "detect_faces", fake_detect)
        pipeline._drain_face_backfill(stats)

    assert len(seen) == 1, "the backfill did not look at the photo"
    lane = seen[0]
    assert lane["task"] == TASK_FACE_BACKFILL == "Looking for faces in photos read before"
    assert lane["item"] == "old.jpg"
    assert lane["label"] == "Run's end"
    assert lane["state"] == LANE_WORKING


def test_a_phase_said_by_the_run_is_on_the_pass_lane_in_the_progress_words(tmp_path):
    from app.index.pipeline import PHASE_READING, PHASE_VECTOR_INDEX
    from app.ui.presenter import PHASE_WORDS

    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store)
        stats = pipeline._stats_ref
        pipeline._announce_phase(stats, None, PHASE_VECTOR_INDEX)
        lane = stats.snapshot().lanes[LANE_PASS]
        assert lane["task"] == PHASE_WORDS[PHASE_VECTOR_INDEX]
        pipeline._announce_phase(stats, None, PHASE_READING)
        assert stats.snapshot().lanes[LANE_PASS]["state"] == LANE_IDLE


# ---------------------------------------------------------------------------
# Snapshots, the pipe and the heartbeat
# ---------------------------------------------------------------------------

def test_lanes_in_a_snapshot_share_nothing_with_the_live_run():
    stats = IndexStats()
    stats.board.lane(LANE_PICTURES).set(task=TASK_FACES, item="a.jpg")
    shot = stats.snapshot()
    assert shot.lanes[LANE_PICTURES]["item"] == "a.jpg"
    shot.lanes[LANE_PICTURES]["item"] = "changed"
    assert stats.lanes[LANE_PICTURES]["item"] == "a.jpg"
    assert shot.lanes[LANE_PICTURES] is not stats.lanes[LANE_PICTURES]
    assert shot.board is None
    # A refresh of the snapshot keeps what it was taken with.
    shot.refresh_live()
    assert shot.lanes[LANE_PICTURES]["task"] == TASK_FACES


def test_lanes_cross_the_process_boundary_as_they_were():
    stats = IndexStats()
    stats.board.lane(LANE_PICTURES).set(task=TASK_FACES, item=Path("C:/p/a.jpg"),
                                        device="graphics card")
    stats.board.lane(LANE_PASS).set(task=TASK_FACE_BACKFILL, total=40, done=3)
    original = stats.snapshot()
    payload, _ = ev.encode_stats(original)
    across = json.loads(json.dumps(payload, ensure_ascii=True, allow_nan=False))
    rebuilt = ev.StatsRebuilder().rebuild(across)
    assert rebuilt.lanes == original.lanes
    # And it stays a snapshot on the far side: no board refreshes it away.
    assert rebuilt.snapshot().lanes == original.lanes


def test_the_heartbeat_moves_when_only_a_lane_moves():
    stats = IndexStats()
    lane = stats.board.lane(LANE_PASS)
    lane.set(task=TASK_FACE_BACKFILL, item="one.jpg")
    stats.refresh_live(now=10.0)
    stats.refresh_live(now=20.0)
    assert stats.last_activity == 10.0, "nothing moved, so the heart did not beat"
    lane.set(item="two.jpg")
    stats.refresh_live(now=30.0)
    assert stats.last_activity == 30.0, "a lane moved and the heartbeat missed it"
    lane.tick()
    stats.refresh_live(now=40.0)
    assert stats.last_activity == 40.0
