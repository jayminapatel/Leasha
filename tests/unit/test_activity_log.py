r"""The run's own timestamped story: the buffer, and what the pipeline puts in it.

Layer: L3. Work order 0w §2a, with the §2e tests that ride on it.

**The Indexing page could say how far a run had got, never what it was doing.**
Its only list was the notices, plain strings with no time. The run now keeps a
bounded log of `(time, kind, text)` entries - phases, large files started,
pauses and why, warnings, notices - and these pin:

* the buffer is bounded, and `since` returns exactly what is new;
* four threads writing at once lose nothing and never repeat a sequence number;
* a snapshot's copy is frozen and carries the run's identity;
* the large-file rule, and that its suffix lists agree with the extractors';
* a notice keeps its exact text and gains a time, however it was added;
* a small real run records its entries in order, first phase to last line;
* a pause is said once however many threads notice it, and so is its end;
* a warning code is said once a run, except a partial archive, said each time.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from app.index import activity as activity_module
from app.index import pipeline as pipeline_module
from app.index.activity import (
    KIND_FINISHED,
    KIND_LARGE_FILE,
    KIND_NOTICE,
    KIND_PAUSE,
    KIND_PHASE,
    KIND_RESUME,
    KIND_STOPPING,
    KIND_WARNING,
    ActivityLog,
    large_file_kind,
)
from app.index.embedder import Embedder
from app.index.pipeline import IndexStats, Pipeline, PipelineConfig
from app.index.resources import MANUAL_PAUSE_REASON, ResourceLimits, Verdict
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore

# ---------------------------------------------------------------------------
# The buffer
# ---------------------------------------------------------------------------


def test_the_buffer_is_bounded_and_keeps_the_newest() -> None:
    log = ActivityLog(limit=5)
    for index in range(12):
        log.record(KIND_NOTICE, f"n{index}")
    assert len(log) == 5
    assert [entry.text for entry in log.entries()] == [f"n{i}" for i in range(7, 12)]
    assert log.last_seq == 12, "the counter counts what was said, not what is held"


def test_the_default_bound_holds_on_a_long_run() -> None:
    log = ActivityLog()
    for index in range(activity_module.ACTIVITY_LIMIT * 3):
        log.record(KIND_WARNING, str(index))
    assert len(log) == activity_module.ACTIVITY_LIMIT


def test_since_returns_only_what_is_new_oldest_first() -> None:
    log = ActivityLog()
    for index in range(4):
        log.record(KIND_NOTICE, str(index))
    assert [entry.text for entry in log.since(0)] == ["0", "1", "2", "3"]
    assert [entry.text for entry in log.since(2)] == ["2", "3"]
    assert log.since(4) == []
    assert log.since(99) == []


def test_a_reader_further_behind_than_the_buffer_gets_the_newest() -> None:
    log = ActivityLog(limit=3)
    for index in range(10):
        log.record(KIND_NOTICE, str(index))
    assert [entry.text for entry in log.since(1)] == ["7", "8", "9"]


def test_recording_never_raises() -> None:
    log = ActivityLog()
    log.record(KIND_NOTICE, None, size="not a number")        # type: ignore[arg-type]
    assert len(log) == 0 or log.entries()[0].kind == KIND_NOTICE


def test_concurrent_writers_lose_nothing_and_never_share_a_number() -> None:
    """The walker, the extraction workers, the consumer and the window all
    record into the one log. One lock covers the counter and the append."""
    log = ActivityLog(limit=10_000)
    threads, per_thread = 8, 500
    start = threading.Barrier(threads)

    def write(name: int) -> None:
        start.wait()
        for index in range(per_thread):
            log.record(KIND_WARNING, f"{name}:{index}")

    workers = [threading.Thread(target=write, args=(n,)) for n in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    entries = log.entries()
    assert len(entries) == threads * per_thread
    seqs = [entry.seq for entry in entries]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert seqs == list(range(1, threads * per_thread + 1))
    # Each writer's own lines stay in the order it wrote them.
    for name in range(threads):
        mine = [int(e.text.split(":")[1]) for e in entries if e.text.startswith(f"{name}:")]
        assert mine == list(range(per_thread))


def test_reading_while_writing_is_safe() -> None:
    log = ActivityLog(limit=50)
    stop = threading.Event()
    problems: list[BaseException] = []

    def write() -> None:
        while not stop.is_set():
            log.record(KIND_NOTICE, "x")

    def read() -> None:
        seen = 0
        try:
            for _ in range(2_000):
                fresh = log.since(seen)
                if fresh:
                    assert [e.seq for e in fresh] == sorted(e.seq for e in fresh)
                    seen = fresh[-1].seq
                log.copy().entries()
        except BaseException as exc:          # noqa: BLE001 - reported below
            problems.append(exc)

    writers = [threading.Thread(target=write) for _ in range(3)]
    for writer in writers:
        writer.start()
    read()
    stop.set()
    for writer in writers:
        writer.join(timeout=5)
    assert not problems


def test_a_copy_is_frozen_and_belongs_to_the_same_run() -> None:
    log = ActivityLog()
    log.record(KIND_NOTICE, "first")
    frozen = log.copy()
    log.record(KIND_NOTICE, "second")
    assert [entry.text for entry in frozen.entries()] == ["first"]
    assert frozen.run == log.run
    assert ActivityLog().run != log.run, "a new run must be told apart"


def test_a_tick_with_nothing_new_reuses_the_last_copy() -> None:
    """Every progress tick copies; most add nothing, and those cost nothing."""
    log = ActivityLog()
    log.record(KIND_NOTICE, "a")
    assert log.copy() is log.copy()
    before = log.copy()
    log.record(KIND_NOTICE, "b")
    assert log.copy() is not before


# ---------------------------------------------------------------------------
# What counts as a large file
# ---------------------------------------------------------------------------


def test_the_large_file_rule() -> None:
    big = activity_module.LARGE_CONTAINER_BYTES
    huge = activity_module.LARGE_FILE_BYTES
    assert large_file_kind(Path("Mail.PST"), big) == "mail"
    assert large_file_kind(Path("old.mbox"), big) == "mail"
    assert large_file_kind(Path("bundle.zip"), big) == "archive"
    assert large_file_kind(Path("talk.mp4"), big) == "recording"
    assert large_file_kind(Path("memo.m4a"), big) == "recording"
    assert large_file_kind(Path("Mail.pst"), big - 1) == ""
    assert large_file_kind(Path("report.pdf"), big) == "", "a document needs the higher bar"
    assert large_file_kind(Path("report.pdf"), huge) == "file"
    assert large_file_kind(Path("notes.txt"), 10) == ""


def test_the_suffix_lists_agree_with_the_extractors() -> None:
    """Copied rather than imported, to keep the pipeline's import light - so
    held to the extractors' own lists here."""
    from app.extract.archive import ARCHIVE_EXTENSIONS
    from app.extract.email_mbox import MboxExtractor
    from app.extract.email_pst import OUTLOOK_EXTENSIONS
    from app.extract.media import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS

    assert activity_module.RECORDING_EXTENSIONS == VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
    assert activity_module.ARCHIVE_EXTENSIONS == frozenset(ARCHIVE_EXTENSIONS)
    assert OUTLOOK_EXTENSIONS <= activity_module.MAIL_ARCHIVE_EXTENSIONS
    assert ".mbox" in MboxExtractor.extensions


# ---------------------------------------------------------------------------
# Notices keep their words and gain a time
# ---------------------------------------------------------------------------


def test_a_notice_keeps_its_text_and_gains_a_time() -> None:
    stats = IndexStats()
    before = time.time()
    stats.add_notice("40GB free on the index drive.")
    assert stats.notices == ["40GB free on the index drive."]
    assert len(stats.notice_times) == 1 and stats.notice_times[0] >= before
    (entry,) = stats.activity.entries()
    assert (entry.kind, entry.text, entry.at) == (
        KIND_NOTICE, "40GB free on the index drive.", stats.notice_times[0])


def test_a_notice_appended_directly_is_timed_at_the_next_snapshot() -> None:
    """`notices` is a public list; a path that appends to it directly must not
    leave a notice with no time and no line in the log."""
    stats = IndexStats()
    stats.notices.append("appended the old way")
    snap = stats.snapshot()
    assert len(snap.notice_times) == 1
    assert [e.text for e in snap.activity.entries()] == ["appended the old way"]
    stats.snapshot()
    assert len(stats.activity) == 1, "stamped once, not on every tick"


def test_a_snapshot_copies_the_log_and_the_times() -> None:
    live = IndexStats()
    live.add_notice("first")
    snap = live.snapshot()
    live.add_notice("second")
    assert snap.notices == ["first"] and len(snap.notice_times) == 1
    assert [e.text for e in snap.activity.entries()] == ["first"]
    assert snap.activity.run == live.activity.run


def test_the_run_record_does_not_carry_the_log() -> None:
    stats = IndexStats()
    stats.add_notice("n")
    assert "activity" not in stats.as_dict()
    assert stats.as_dict()["notices"] == ["n"]


# ---------------------------------------------------------------------------
# A real run, in order
# ---------------------------------------------------------------------------


class _Vectors:
    """A vector store that counts, not writes. As `test_phase_progress.py`."""

    def __init__(self) -> None:
        self.rows: dict[int, int] = {}

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        wanted = {int(one) for one in file_ids}
        for cid, fid in list(self.rows.items()):
            if fid in wanted:
                del self.rows[cid]

    def add(self, *, chunk_ids, file_ids, vectors) -> int:
        for cid, fid in zip(chunk_ids, file_ids, strict=True):
            self.rows[int(cid)] = int(fid)
        return len(list(chunk_ids))

    def maybe_compact(self, **_k) -> bool:
        return False

    def maybe_create_index(self, **_k) -> bool:
        return False

    def count(self) -> int:
        return len(self.rows)


def _limits() -> ResourceLimits:
    return ResourceLimits(pause_on_battery=False, cpu_percent=0,
                          min_free_gb=0, low_priority=False)


def _embedder() -> Embedder:
    return Embedder(dim=4, encoder=lambda texts: [[1.0, 0.0, 0.0, 0.0] for _ in texts])


def _pipeline(store, roots) -> Pipeline:
    return Pipeline(store, _Vectors(), _embedder(), PipelineConfig(
        walk=WalkConfig(roots=list(roots)), workers=1, min_free_gb=0,
        required_free_gb=0, limits=_limits()))


def test_a_real_run_records_its_story_in_order(tmp_path: Path, monkeypatch) -> None:
    corpus = tmp_path / "docs"
    corpus.mkdir()
    for index in range(4):
        (corpus / f"f{index}.txt").write_text(
            f"pump station {index} commissioning report", encoding="utf-8")
    (corpus / "big.txt").write_text("pump station " * 50, encoding="utf-8")
    # A missing folder, for a notice; a low bar, so a small file is "large".
    missing = tmp_path / "not-mounted"
    monkeypatch.setattr(activity_module, "LARGE_FILE_BYTES", 400)

    ticks: list[IndexStats] = []
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, [corpus, missing])
        stats = pipeline.run(on_progress=lambda s: ticks.append(s.snapshot()))

    assert stats.indexed == 5
    entries = stats.activity.entries()
    phases = [entry.text for entry in entries if entry.kind == KIND_PHASE]
    from tests.unit.test_phase_progress import EXPECTED_ORDER

    assert phases == EXPECTED_ORDER

    # In order: sequence strictly rising, times never going backwards.
    assert [e.seq for e in entries] == sorted(e.seq for e in entries)
    stamps = [e.at for e in entries]
    assert stamps == sorted(stamps)

    # The large file is started while reading, and named.
    large = [e for e in entries if e.kind == KIND_LARGE_FILE]
    assert [e.text for e in large] == ["big.txt"]
    assert large[0].detail == "file" and large[0].size > 400
    reading = next(i for i, e in enumerate(entries)
                   if e.kind == KIND_PHASE and e.text == pipeline_module.PHASE_READING)
    tidying = next(i for i, e in enumerate(entries)
                   if e.kind == KIND_PHASE and e.text == pipeline_module.PHASE_TIDYING)
    assert reading < entries.index(large[0]) < tidying

    # The missing folder's notice, word for word, with the same time the
    # notice itself carries.
    notices = [e for e in entries if e.kind == KIND_NOTICE]
    assert [e.text for e in notices] == stats.notices
    assert [e.at for e in notices] == stats.notice_times
    assert "not-mounted" in notices[0].text

    # The last word is that it finished.
    assert entries[-1].kind == KIND_FINISHED and entries[-1].text == ""

    # Every tick's copy belonged to this run and only ever grew.
    assert {tick.activity.run for tick in ticks} == {stats.activity.run}
    seqs = [tick.activity.last_seq for tick in ticks]
    assert seqs == sorted(seqs)


def test_a_stopped_run_says_so(tmp_path: Path) -> None:
    corpus = tmp_path / "docs"
    corpus.mkdir()
    for index in range(3):
        (corpus / f"f{index}.txt").write_text(f"report {index}", encoding="utf-8")

    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, [corpus])
        stats = pipeline.run(
            on_progress=lambda s: s.indexed and pipeline.request_stop())

    kinds = [entry.kind for entry in stats.activity.entries()]
    assert kinds.count(KIND_STOPPING) == 1, "said once, however often it is asked"
    assert stats.activity.entries()[-1].kind == KIND_FINISHED
    assert stats.activity.entries()[-1].text == "stopped"


def test_each_run_has_its_own_log(tmp_path: Path) -> None:
    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "a.txt").write_text("pump station report", encoding="utf-8")
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, [corpus])
        first = pipeline.run()
        second = pipeline.run()
    assert first.activity.run != second.activity.run
    assert second.activity.entries()[0].text == pipeline_module.PHASE_MODEL


def test_the_media_tail_reads_into_the_outer_runs_log(tmp_path: Path) -> None:
    """The recordings read after everything else are part of the same run, so
    the page's log carries straight on instead of emptying."""
    from app.index import media_backlog

    corpus = tmp_path / "docs"
    corpus.mkdir()
    with SqliteStore(tmp_path / "index.db") as store:
        outer = _pipeline(store, [corpus])
        stats = IndexStats()
        outer._stats_ref = stats
        sub = media_backlog._backlog_pipeline_class()(
            store, _Vectors(), _embedder(), outer.config, outer.governor, queued=[])
        sub.activity_into = stats.activity
        done = sub.run()
    assert done.activity is stats.activity
    assert all(e.kind != KIND_FINISHED for e in stats.activity.entries()), (
        "the tail is not the end of the run")


# ---------------------------------------------------------------------------
# Pauses, stops and warnings
# ---------------------------------------------------------------------------


def _bare_log_pipeline(tmp_path: Path):
    store = SqliteStore(tmp_path / "index.db")
    pipeline = _pipeline(store, [tmp_path])
    pipeline._stats_ref = IndexStats()
    return store, pipeline


def test_the_persons_pause_is_said_once_with_its_reason(tmp_path: Path) -> None:
    store, pipeline = _bare_log_pipeline(tmp_path)
    try:
        pipeline.pause()
        # Every waiter then asks the governor, which reports the same pause.
        manual = Verdict("pause", MANUAL_PAUSE_REASON, cause="manual")
        pipeline._on_throttle(manual)
        pipeline._on_throttle(manual)
        pipeline.resume()
        pipeline._on_throttle(Verdict("run"))
    finally:
        store.close()
    entries = pipeline._stats_ref.activity.entries()
    assert [e.kind for e in entries] == [KIND_PAUSE, KIND_RESUME]
    assert entries[0].text == MANUAL_PAUSE_REASON and entries[0].detail == "manual"


def test_the_machines_pause_is_said_with_the_governors_reason(tmp_path: Path) -> None:
    store, pipeline = _bare_log_pipeline(tmp_path)
    try:
        battery = Verdict("pause", "On battery. Indexing resumes on mains power.",
                          cause="battery")
        for _ in range(3):
            pipeline._on_throttle(battery)
        pipeline._on_throttle(Verdict("run"))
        pipeline._on_throttle(Verdict("run"))
        pipeline._on_throttle(Verdict("stop", "Only 1.0GB free on the index drive.",
                                      cause="disk"))
    finally:
        store.close()
    entries = pipeline._stats_ref.activity.entries()
    assert [e.kind for e in entries] == [KIND_PAUSE, KIND_RESUME, KIND_WARNING]
    assert entries[0].text.startswith("On battery") and entries[0].detail == "machine"
    assert entries[2].text.startswith("Only 1.0GB free")


def test_a_warning_code_is_said_once_but_each_partial_archive_is_said(tmp_path: Path) -> None:
    store, pipeline = _bare_log_pipeline(tmp_path)

    def item(code: str, message: str):
        return SimpleNamespace(warnings=[SimpleNamespace(
            code=code, message=message, suggestion="")])

    try:
        for n in range(4):
            pipeline._note_warnings(item("ERR_MOSTLY_PICTURES", f"deck {n}"), log=False)
        pipeline._note_warnings(item("ERR_PST_PARTIAL", "Only part of 'a.pst'"), log=False)
        pipeline._note_warnings(item("ERR_PST_PARTIAL", "Only part of 'b.pst'"), log=False)
    finally:
        store.close()
    warned = [e.text for e in pipeline._stats_ref.activity.entries()]
    assert warned == ["deck 0", "Only part of 'a.pst'", "Only part of 'b.pst'"]
    assert pipeline._stats_ref.warned_by_code["ERR_MOSTLY_PICTURES"] == 4, (
        "the count is unchanged: only the log is spared the repeats")


def test_a_bare_pipeline_can_still_be_stopped() -> None:
    """Several tests build a pipeline without `__init__`; recording must not be
    the reason one of them fails."""
    bare = Pipeline.__new__(Pipeline)
    bare._stop = threading.Event()
    bare._interrupted = False
    bare._release_pause = lambda: None
    bare.request_stop()
    assert bare._stop.is_set()
