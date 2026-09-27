"""Work order 0x §2a: the event protocol between the window and its indexer process.

Layer: L3

`app/index/run_events.py` is the language the indexer's child process and the
window speak over a pipe: one JSON object per line. These tests hold it to
three promises:

* **Faithful.** An `IndexStats` sent across and rebuilt answers every question
  the page asks of it exactly as the original would - every field, the
  activity log, the `AppError` a stopped run carries.
* **Generic.** A field added to `IndexStats` later (order 0x §3 is adding
  some) crosses without this module being touched.
* **Safe to read.** Anything on the pipe that is not a complete event is
  ignored, never misread; and a writer never blocks the run behind a slow
  reader.
"""

from __future__ import annotations

import dataclasses
import enum
import io
import json
import threading
import time
from pathlib import Path

import pytest

from app.core.errors import AppError, make_error
from app.index import run_events as ev
from app.index.activity import KIND_LARGE_FILE, KIND_PAUSE, KIND_PHASE
from app.index.pipeline import IndexStats


def _busy_stats() -> IndexStats:
    """An IndexStats with something in every kind of field the page reads."""
    stats = IndexStats(seen=120, indexed=95, unchanged=10, unchanged_documents=4,
                       skipped=3, deleted=1, chunks=400, bytes_read=12_345_678,
                       elapsed_s=61.5, paused_seconds=2.5, pauses=1, paused=True,
                       pause_reason="the computer is busy", paused_by_person=False,
                       walk_complete=True, phase="reading", current="big.pst",
                       current_since=time.monotonic() - 12.0, current_item=812,
                       ocr_mode="text", vectors=399, chunks_deduped=7)
    stats.skipped_by_code = {"ERR_FILE_LOCKED": 2, "ERR_FILE_CORRUPT": 1}
    stats.name_only_by_ext = {"mp4": 3}
    stats.root_problems = {"D:\\Gone": "missing"}
    stats.skipped_roots = [{"root": "D:\\Archive", "reason": "archived", "files": 9}]
    stats.stages = {"write": 1.5, "embed": 0.25}
    stats.resolved = {"workers": 2, "device": "cpu"}
    stats.stopped_early = make_error("ERR_DISK_FULL", "index.pipeline", path="D:\\")
    now = time.monotonic()
    stats.recent = [(now - 30.0, 10, 1000), (now, 95, 12_345_678)]
    stats.add_notice("40GB free on the index drive")
    stats.activity.record(KIND_PHASE, "reading")
    stats.activity.record(KIND_LARGE_FILE, "big.pst", size=90_000_000, detail="mail")
    stats.activity.record(KIND_PAUSE, "the computer is busy", detail="cpu")
    return stats


def _across(payload: dict) -> dict:
    """Through real JSON text and back, as the pipe carries it."""
    return json.loads(json.dumps(payload, ensure_ascii=True, allow_nan=False))


# --- to_json_safe -------------------------------------------------------------

class _Colour(enum.Enum):
    RED = "red"


@dataclasses.dataclass
class _Point:
    x: int
    where: Path


def test_every_value_kind_becomes_something_json_takes() -> None:
    safe = ev.to_json_safe({
        "path": Path("a") / "b.txt", "set": {3}, "tuple": (1, 2),
        "nan": float("nan"), "inf": float("inf"), "enum": _Colour.RED,
        "point": _Point(1, Path("x")), 4: "int key", "odd": object(),
    })
    json.dumps(safe, allow_nan=False)                 # must not raise
    assert safe["path"] == str(Path("a") / "b.txt")
    assert safe["set"] == [3] and safe["tuple"] == [1, 2]
    assert safe["nan"] is None and safe["inf"] is None
    assert safe["enum"] == "red"
    assert safe["point"] == {"x": 1, "where": "x"}
    assert safe["4"] == "int key"
    assert isinstance(safe["odd"], str)


def test_an_app_error_crosses_as_an_app_error() -> None:
    error = make_error("ERR_INDEX_RUNNING", "core.run_lock", holder="the window")
    back = ev.from_json_safe(_across(ev.to_json_safe(error)))
    assert isinstance(back, AppError)
    assert back == error


# --- IndexStats, both ways ------------------------------------------------------

def test_every_field_survives_the_round_trip() -> None:
    """Field by field, generically: the list of fields is the dataclass's own."""
    original = _busy_stats().snapshot()
    payload, _ = ev.encode_stats(original)
    rebuilt = ev.StatsRebuilder().rebuild(_across(payload))

    for spec in dataclasses.fields(IndexStats):
        if spec.name == "activity":
            continue
        assert getattr(rebuilt, spec.name) == getattr(original, spec.name), spec.name
    # The properties and methods the presenter uses work on the copy too.
    assert rebuilt.files_per_minute == original.files_per_minute
    assert rebuilt.recent_files_per_minute == pytest.approx(original.recent_files_per_minute)
    assert rebuilt.as_dict() == original.as_dict()
    assert isinstance(rebuilt.stopped_early, AppError)
    assert rebuilt.snapshot().indexed == 95


def test_the_activity_log_crosses_whole_and_in_order() -> None:
    original = _busy_stats().snapshot()
    payload, last = ev.encode_stats(original)
    rebuilt = ev.StatsRebuilder().rebuild(_across(payload))

    assert [tuple(e) for e in rebuilt.activity.entries()] == \
        [tuple(e) for e in original.activity.entries()]
    assert last == original.activity.last_seq
    assert rebuilt.activity.since(0) == rebuilt.activity.entries()


def test_only_new_log_lines_are_sent_and_the_window_keeps_the_rest() -> None:
    stats = IndexStats()
    stats.activity.record(KIND_PHASE, "model")
    rebuilder = ev.StatsRebuilder()
    first, sent = ev.encode_stats(stats.snapshot(), since_seq=0)
    rebuilder.rebuild(_across(first))

    stats.activity.record(KIND_PHASE, "reading")
    second, sent = ev.encode_stats(stats.snapshot(), since_seq=sent)
    assert [row[3] for row in second["activity"]["entries"]] == ["reading"]
    rebuilt = rebuilder.rebuild(_across(second))
    assert [e.text for e in rebuilt.activity.entries()] == ["model", "reading"]

    # The same event twice (a reader that asks again) adds nothing twice.
    rebuilt = rebuilder.rebuild(_across(second))
    assert [e.text for e in rebuilt.activity.entries()] == ["model", "reading"]


def test_a_new_run_in_the_child_starts_a_new_log_in_the_window() -> None:
    rebuilder = ev.StatsRebuilder()
    one = IndexStats()
    one.activity.record(KIND_PHASE, "model")
    first = rebuilder.rebuild(_across(ev.encode_stats(one.snapshot())[0]))
    two = IndexStats()
    two.activity.record(KIND_PHASE, "planning")
    second = rebuilder.rebuild(_across(ev.encode_stats(two.snapshot())[0]))

    assert [e.text for e in second.activity.entries()] == ["planning"]
    assert second.activity.run != first.activity.run      # the page clears on this


def test_a_field_added_later_crosses_without_this_module_knowing(monkeypatch) -> None:
    """Order 0x §3 adds fields to IndexStats; they must not need a change here."""
    import app.index.pipeline as pipeline

    @dataclasses.dataclass
    class Wider(IndexStats):
        archive_position: dict = dataclasses.field(default_factory=dict)
        worker_lines: list = dataclasses.field(default_factory=list)

    monkeypatch.setattr(pipeline, "IndexStats", Wider)
    stats = Wider(indexed=3)
    stats.archive_position = {"path": "backup.zip", "member": 812, "of": 900}
    stats.worker_lines = ["reading a.pdf", "reading b.docx"]

    payload, _ = ev.encode_stats(stats.snapshot())
    rebuilt = ev.StatsRebuilder().rebuild(_across(payload))
    assert rebuilt.archive_position == {"path": "backup.zip", "member": 812, "of": 900}
    assert rebuilt.worker_lines == ["reading a.pdf", "reading b.docx"]
    assert rebuilt.indexed == 3


def test_unknown_and_malformed_fields_cost_nothing() -> None:
    rebuilt = ev.StatsRebuilder().rebuild({"indexed": 4, "from_a_newer_child": 1,
                                           "activity": {"run": 1, "entries": [["bad"]]}})
    assert rebuilt.indexed == 4
    assert not hasattr(rebuilt, "from_a_newer_child")
    assert ev.StatsRebuilder().rebuild(None).indexed == 0


def test_clock_readings_are_moved_onto_the_readers_clock() -> None:
    """`current_since` is the child's monotonic time; "n seconds on this file"
    must come out the same in the window whatever the two clocks' origins."""
    stats = IndexStats(current="big.pst")
    child_now = 5_000.0
    stats.current_since = child_now - 12.0
    stats.recent = [(child_now - 30.0, 10, 100), (child_now, 40, 400)]
    payload, _ = ev.encode_stats(stats)

    here = time.monotonic()
    rebuilt = ev.StatsRebuilder().rebuild(_across(payload), sent_at=child_now,
                                          received_at=here)
    assert here - rebuilt.current_since == pytest.approx(12.0)
    assert rebuilt.recent[-1][0] == pytest.approx(here)
    assert rebuilt.recent_files_per_minute == pytest.approx(60.0)


# --- reading lines ----------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "", "not json", "[1, 2]", '{"no_event": 1}', '{"event": 3}',
    '{"event": "progress", "stats": {"seen"',            # a line cut short
    "Traceback (most recent call last):",
])
def test_anything_that_is_not_an_event_is_ignored(line) -> None:
    assert ev.parse_line(line) is None


def test_an_event_line_reads_back() -> None:
    line = ev.event_line(ev.EVENT_HEARTBEAT)
    assert "\n" not in line and line.isascii()
    found = ev.parse_line(line.encode("ascii") + b"\n")
    assert found["event"] == ev.EVENT_HEARTBEAT and isinstance(found["t"], float)


def test_a_file_name_in_any_script_is_sent_as_plain_ascii() -> None:
    payload, _ = ev.encode_stats(IndexStats(current="Rapport \u00e9t\u00e9 \u4e2d.pdf"))
    line = ev.event_line(ev.EVENT_PROGRESS, stats=payload)
    assert line.isascii()
    assert ev.parse_line(line)["stats"]["current"] == "Rapport \u00e9t\u00e9 \u4e2d.pdf"


@pytest.mark.parametrize("text,word", [
    ("pause\n", "pause"), (" RESUME ", "resume"), ("stop", "stop"),
    ("delete everything", None), ("", None),
])
def test_only_the_three_commands_are_commands(text, word) -> None:
    assert ev.parse_command(text) == word


# --- writing lines ------------------------------------------------------------------

class _Stream(io.StringIO):
    """A stream that records complete lines, and can be made to fail."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = False
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        if self.fail:
            raise BrokenPipeError("the window has gone")
        with self.lock:
            return super().write(text)

    def events(self) -> list[dict]:
        with self.lock:
            text = self.getvalue()
        return [ev.parse_line(line) for line in text.splitlines()]


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_the_writer_says_hello_heartbeats_and_finishes() -> None:
    stream = _Stream()
    writer = ev.EventWriter(stream, heartbeat_s=0.05)
    writer.start()
    _wait_for(lambda: sum(e["event"] == "heartbeat" for e in stream.events()) >= 3)
    stats = IndexStats(indexed=7)
    writer.finish(stats=stats, exit_code=0)

    events = stream.events()
    assert events[0]["event"] == "hello" and events[0]["protocol"] == ev.PROTOCOL
    assert events[-1]["event"] == "finished"
    assert events[-1]["stats"]["indexed"] == 7 and events[-1]["exit"] == 0


def test_a_burst_of_ticks_is_coalesced_and_the_last_one_always_arrives() -> None:
    stream = _Stream()
    writer = ev.EventWriter(stream, heartbeat_s=10.0, progress_min_s=0.2)
    writer.start()
    stats = IndexStats(phase="reading")
    for n in range(500):
        stats.indexed = n
        writer.offer(stats)
    # The trailing tick is sent when the throttle opens, with nothing more
    # offered - the page never sits on a stale number.
    _wait_for(lambda: any(e["event"] == "progress" and e["stats"]["indexed"] == 499
                          for e in stream.events()))
    progress = [e for e in stream.events() if e["event"] == "progress"]
    assert len(progress) <= 3, f"{len(progress)} lines for one burst"
    writer.finish(stats=stats)


def test_a_change_of_phase_or_pause_is_sent_at_once() -> None:
    stream = _Stream()
    writer = ev.EventWriter(stream, heartbeat_s=10.0, progress_min_s=30.0)
    writer.start()
    stats = IndexStats(phase="model")
    writer.offer(stats)
    _wait_for(lambda: any(e["event"] == "progress" for e in stream.events()))
    stats.phase = "reading"
    writer.offer(stats)
    _wait_for(lambda: any(e["event"] == "progress" and e["stats"]["phase"] == "reading"
                          for e in stream.events()), timeout=2.0)
    stats.paused_by_person = True
    stats.paused = True
    writer.offer(stats)
    _wait_for(lambda: any(e["event"] == "progress" and e["stats"]["paused_by_person"]
                          for e in stream.events()), timeout=2.0)
    writer.finish(stats=stats)


def test_offer_never_waits_for_a_reader_that_is_not_reading() -> None:
    """The pipeline's callback returns at once even if every write blocks."""
    release = threading.Event()

    class Blocked(_Stream):
        def write(self, text: str) -> int:
            if '"progress"' in text:
                release.wait(5)
            return super().write(text)

    writer = ev.EventWriter(Blocked(), heartbeat_s=10.0, progress_min_s=0.0)
    writer.start()
    stats = IndexStats(phase="reading")
    started = time.monotonic()
    for n in range(200):
        stats.indexed = n
        writer.offer(stats)
    assert time.monotonic() - started < 0.5
    release.set()
    writer.finish(stats=stats)


def test_a_broken_pipe_is_reported_once_and_writing_stops() -> None:
    stream = _Stream()
    calls = []
    writer = ev.EventWriter(stream, heartbeat_s=0.02, on_broken=lambda: calls.append(1))
    writer.start()
    stream.fail = True
    _wait_for(lambda: writer.broken)
    time.sleep(0.1)
    writer.finish(stats=IndexStats())            # must not raise
    assert calls == [1]


def test_the_error_of_a_run_that_could_not_start_is_an_event() -> None:
    stream = _Stream()
    writer = ev.EventWriter(stream, heartbeat_s=10.0)
    writer.start()
    error = make_error("ERR_INDEX_RUNNING", "core.run_lock", holder="the command line")
    writer.finish(error=error, exit_code=1)
    last = stream.events()[-1]
    assert last["event"] == "finished" and last["exit"] == 1
    assert ev.from_json_safe(last["error"]) == error


# --- order 0x §3's reader lines, across the pipe --------------------------------

def test_the_reader_lines_read_the_same_from_the_child_as_in_process() -> None:
    """Child JSON -> rebuilt stats -> `live_headline` / `worker_lines` must say
    exactly what the same snapshot says in the window's own process - and keep
    saying it after `IndexWorker` takes its own `snapshot()` of the copy."""
    from app.extract import progress
    from app.ui.presenter.live_progress import heartbeat_line, live_headline, worker_lines

    stats = IndexStats(phase="reading", indexed=40, embed_batch=2, embed_batches=5,
                       stage="writing")
    pst = stats.board.open_slot()
    pst.begin(Path("Archive2019.pst"))
    frame = progress.Frame("pst", "Archive2019.pst", unit="message", total=18_300)
    frame.n, frame.where = 4512, "Inbox/Projects"
    pst.frames.append(frame)
    zipped = stats.board.open_slot()
    zipped.begin(Path("backup.zip"))
    outer = progress.Frame("zip", "backup.zip", unit="member", total=900)
    outer.n = 12
    inner = progress.Frame("mbox", "mail.mbox", unit="message", total=2000)
    inner.n = 812
    zipped.frames.extend([outer, inner])
    stats.board.open_slot()                      # a third reader, waiting

    here = stats.snapshot()
    payload, _ = ev.encode_stats(here)
    there = ev.StatsRebuilder().rebuild(_across(payload))
    again = there.snapshot()                     # what IndexWorker hands the page

    now = here.last_activity + 3.0
    for copy in (there, again):
        assert copy.workers == here.workers
        assert copy.last_activity == here.last_activity
        assert live_headline(copy) == live_headline(here)
        assert worker_lines(copy, now=now) == worker_lines(here, now=now)
        assert heartbeat_line(copy, now=now) == heartbeat_line(here, now=now)
    assert "message 4,512 of 18,300" in live_headline(there)
    assert len(worker_lines(there, now=now)) == 3
