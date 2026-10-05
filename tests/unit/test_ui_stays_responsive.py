r"""The window stays responsive while an index run is going.

Layer: L5

The window and the indexer share one process, so "indexing is on its own
threads" is necessary and not sufficient: the same CPUs, the same interpreter
lock and the same process priority are shared with it. Seven measures, one
group of tests each:

1. `LagMonitor` - the window's lateness is measured, and a stall is described
   while it is happening.
2. The indexer's threads lower themselves; the process, and so the window, do
   not.
3. The interpreter's thread switch interval is tightened.
4. Progress is handed to the window as a copy, and repainted at a limited rate.
5. The database was already in WAL mode with a busy timeout - pinned, so a
   change to it is a decision rather than an accident.
6. A slow reading of the window makes the run yield.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import priority
from app.index.pipeline import (
    UI_LAG_YIELD_S, UI_YIELD_MAX_S, IndexStats, Pipeline, PipelineConfig,
)
from app.index.resources import ResourceGovernor, ResourceLimits
from app.ui.lag_monitor import (
    BEAT_MS, DUMP_INTERVAL_S, LagMonitor, tighten_switch_interval,
)

windows_only = pytest.mark.skipif(sys.platform != "win32",
                                  reason="thread priority is a Windows call here")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _monitor() -> tuple[LagMonitor, Clock]:
    clock = Clock()
    return LagMonitor(clock=clock), clock


# ---------------------------------------------------------------------------
# 1. The instrument
# ---------------------------------------------------------------------------

def test_a_beat_on_time_is_not_late():
    monitor, clock = _monitor()
    clock.advance(BEAT_MS / 1000)
    monitor.beat()

    assert monitor.recent_lag_s() == pytest.approx(0.0, abs=1e-9)
    assert monitor.stalls == 0


def test_a_late_beat_is_measured_beyond_its_interval():
    monitor, clock = _monitor()
    clock.advance(0.05 + 0.4)
    monitor.beat()

    assert monitor.worst_s == pytest.approx(0.4)
    assert monitor.stalls == 1
    assert monitor.summary()["worst_ms"] == 400


def test_a_stall_in_progress_counts_before_the_beat_arrives():
    r"""**The moment the answer is needed is the moment the beat is missing.**"""
    monitor, clock = _monitor()
    clock.advance(0.05)
    monitor.beat()
    clock.advance(0.05 + 0.6)                    # ...and no beat has come

    assert monitor.recent_lag_s() == pytest.approx(0.6)


def test_the_lag_reading_recovers_once_the_window_does():
    monitor, clock = _monitor()
    clock.advance(0.05 + 0.5)
    monitor.beat()
    assert monitor.recent_lag_s() == pytest.approx(0.5)

    for _ in range(40):                          # two seconds of healthy beats
        clock.advance(0.05)
        monitor.beat()

    assert monitor.recent_lag_s() < UI_LAG_YIELD_S,         "the run must stop yielding soon after the window recovers"


def test_the_watcher_describes_a_stall_once_and_names_what_the_window_is_running():
    monitor, clock = _monitor()
    clock.advance(0.05 + 1.0)                    # overdue, no beat

    report = monitor.check()

    assert report is not None
    assert "has not responded for" in report
    assert "it is executing" in report
    assert "test_the_watcher_describes_a_stall_once" in report, \
        "the report must contain the frame the window thread is actually in"
    assert monitor.check() is None, "one stall is one report"


def test_stack_dumps_are_rate_limited_across_stalls():
    monitor, clock = _monitor()
    clock.advance(1.0)
    assert monitor.check() is not None
    clock.advance(0.05)
    monitor.beat()                               # recovered
    clock.advance(1.0)                           # a second stall, straight away

    assert monitor.check() is None, "a log full of identical dumps is not read"
    clock.advance(DUMP_INTERVAL_S)
    assert monitor.check() is not None


def test_percentiles_are_over_the_beats_seen():
    monitor, clock = _monitor()
    for _ in range(99):
        clock.advance(0.05)
        monitor.beat()
    clock.advance(0.05 + 0.5)
    monitor.beat()

    assert monitor.percentile(0.50) == pytest.approx(0.0, abs=1e-6)
    assert monitor.percentile(1.0) == pytest.approx(0.5)
    assert monitor.summary()["beats"] == 100


def test_the_real_watcher_thread_reports_a_real_stall():
    r"""End to end with the wall clock: block this thread and the watcher, on its
    own thread, writes the report while the block is still in force."""
    monitor = LagMonitor(beat_s=0.02, stall_s=0.1)
    reports: list[str] = []
    real = monitor._describe                     # noqa: SLF001
    monitor._describe = lambda overdue: reports.append(real(overdue)) or reports[-1]  # noqa: SLF001
    monitor.start_watcher()
    try:
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline and not reports:
            time.sleep(0.02)                     # never beats
    finally:
        monitor.stop()

    assert reports, "the watcher never noticed a beat that never came"


# ---------------------------------------------------------------------------
# 2. Priority belongs to the indexer's threads, not the process
# ---------------------------------------------------------------------------

@windows_only
def test_lowering_a_thread_leaves_the_calling_thread_alone_and_restores():
    seen: dict[str, object] = {}

    def body() -> None:
        seen["before"] = priority.current_thread_priority()
        previous = priority.lower_this_thread()
        seen["during"] = priority.current_thread_priority()
        priority.restore_this_thread(previous)
        seen["after"] = priority.current_thread_priority()

    here = priority.current_thread_priority()
    worker = threading.Thread(target=body)
    worker.start()
    worker.join()

    assert seen["before"] == 0
    assert seen["during"] == priority.THREAD_PRIORITY_LOWEST
    assert seen["after"] == 0
    assert priority.current_thread_priority() == here, \
        "another thread's priority must never move this one's"


@windows_only
def test_lowering_a_thread_does_not_lower_the_process():
    psutil = pytest.importorskip("psutil")
    before = psutil.Process().nice()

    def body() -> None:
        with priority.background_thread(True):
            pass

    worker = threading.Thread(target=body)
    worker.start()
    worker.join()

    assert psutil.Process().nice() == before


def test_children_are_created_below_normal_only_while_a_run_is_lowered():
    assert priority.child_creationflags() == 0
    with priority.background_thread(True):
        flags = priority.child_creationflags()
    assert priority.child_creationflags() == 0
    if sys.platform == "win32":
        assert flags == priority.BELOW_NORMAL_PRIORITY_CLASS


def test_background_thread_restores_on_an_exception():
    with pytest.raises(RuntimeError):
        with priority.background_thread(True):
            raise RuntimeError("the run failed")

    assert priority.child_creationflags() == 0


def test_disabled_background_thread_changes_nothing():
    with priority.background_thread(False) as lowered:
        assert lowered is False
        assert priority.child_creationflags() == 0


def test_the_converters_pass_the_child_flags():
    # 2026-09-20: the spawn sites changed under this test - `run_media_tool` is gone
    # (PyAV replaced ffmpeg), the cold path is `_run_tree`, and the warm LibreOffice
    # session starts its own helper. Every place a converter process is started must
    # pass the flags, or a converter runs at full priority under a lowered window run.
    root = Path(__file__).resolve().parents[2] / "app" / "extract"
    for name in ("converter.py", "lo_session.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert source.count("child_creationflags()") >= 1, (
            f"{name} starts a process without the below-normal flags")


def _pipeline(tmp_path: Path, *, low_priority: bool = True) -> Pipeline:
    from app.index.embedder import Embedder
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    for index in range(6):
        (tmp_path / "docs" / f"f{index}.txt").write_text(
            f"pump station {index} commissioning report", encoding="utf-8")

    class Vectors:
        def ensure_table(self) -> None: ...
        def delete_by_file_ids(self, ids) -> None: ...
        def add(self, *, chunk_ids, file_ids, vectors) -> int:
            return len(list(chunk_ids))
        def maybe_compact(self, **_k) -> bool:
            return False
        def maybe_create_index(self, **_k) -> bool:
            return False
        def count(self) -> int:
            return 0

    limits = ResourceLimits(low_priority=low_priority, cpu_percent=0,
                            pause_on_battery=False)
    config = PipelineConfig(walk=WalkConfig(roots=[tmp_path / "docs"]), workers=2,
                            limits=limits, min_free_gb=0, required_free_gb=0)
    store = SqliteStore(tmp_path / "index.db").connect()
    embedder = Embedder(dim=4, encoder=lambda texts: [[1.0, 0, 0, 0] for _ in texts])
    return Pipeline(store, Vectors(), embedder, config)


def test_every_thread_the_run_starts_lowers_itself_when_the_window_shares_the_process(
        tmp_path, monkeypatch):
    import app.index.pipeline as module

    lowered: list[str] = []
    monkeypatch.setattr(module, "lower_this_thread",
                        lambda: lowered.append(threading.current_thread().name))
    pipeline = _pipeline(tmp_path)
    pipeline.thread_priority_only = True

    stats = pipeline.run()

    assert stats.indexed == 6
    assert "walker" in lowered
    assert "feeder" in lowered
    assert any(name.startswith("extract-") for name in lowered)


def test_the_process_is_lowered_only_when_nobody_else_lives_in_it(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(ResourceGovernor, "apply_priority",
                        lambda self: calls.append("process") or True)
    monkeypatch.setattr(ResourceGovernor, "apply_io_priority",
                        lambda self: calls.append("io") or True)

    _pipeline(tmp_path / "cli").run()
    shared = _pipeline(tmp_path / "window")
    shared.thread_priority_only = True
    shared.run()

    assert calls == ["process", "io"], \
        "the command line lowers the process; the window's run lowers I/O only"


def test_a_run_that_asked_not_to_be_low_priority_is_left_at_normal(tmp_path, monkeypatch):
    import app.index.pipeline as module

    lowered: list[str] = []
    monkeypatch.setattr(module, "lower_this_thread", lambda: lowered.append("x"))
    governor_calls: list[str] = []
    monkeypatch.setattr(ResourceGovernor, "apply_io_priority",
                        lambda self: governor_calls.append("io") or False)
    pipeline = _pipeline(tmp_path, low_priority=False)

    class Worker:                                # what IndexWorker decides
        polite = bool(pipeline.config.limits.low_priority)

    pipeline.thread_priority_only = Worker.polite
    pipeline.run()

    assert lowered == []


def test_the_index_worker_lowers_its_own_thread_and_tells_the_pipeline(monkeypatch):
    pytest.importorskip("PySide6")
    from app.ui import workers

    seen: dict[str, object] = {}

    class FakePipeline:
        store = None
        config = SimpleNamespace(limits=SimpleNamespace(low_priority=True))
        thread_priority_only = False

        def run(self, *, on_progress):
            seen["flag"] = self.thread_priority_only
            seen["children"] = priority.child_creationflags()
            on_progress(IndexStats(indexed=3))
            return IndexStats(indexed=3)

    worker = workers.IndexWorker(FakePipeline())
    progress: list[object] = []
    worker.signals.progress.connect(progress.append, type=_direct())
    worker.run()

    assert seen["flag"] is True
    if sys.platform == "win32":
        assert seen["children"] == priority.BELOW_NORMAL_PRIORITY_CLASS
    assert priority.child_creationflags() == 0, "restored once the run ends"


@pytest.fixture(autouse=True)
def _no_real_run_lock(monkeypatch):
    r"""`IndexWorker` takes the machine-wide run mutex. A test must never fight a
    real run (or another session's) for it, so a dummy stands in."""
    import contextlib

    from app.core import run_lock

    class Free:
        def __init__(self, *_a, **_k) -> None: ...
        def __enter__(self):
            return self
        def __exit__(self, *_exc) -> None:
            return None

    monkeypatch.setattr(run_lock, "IndexRunLock", Free)
    yield


def _direct():
    from PySide6.QtCore import Qt

    return Qt.ConnectionType.DirectConnection


# ---------------------------------------------------------------------------
# 3. The interpreter lock is handed over sooner
# ---------------------------------------------------------------------------

def test_the_switch_interval_is_tightened_and_can_be_overridden(monkeypatch):
    original = sys.getswitchinterval()
    try:
        monkeypatch.delenv("LEASHA_SWITCH_INTERVAL_MS", raising=False)
        assert tighten_switch_interval() == pytest.approx(0.001)

        monkeypatch.setenv("LEASHA_SWITCH_INTERVAL_MS", "2")
        assert tighten_switch_interval() == pytest.approx(0.002)

        monkeypatch.setenv("LEASHA_SWITCH_INTERVAL_MS", "nonsense")
        assert tighten_switch_interval() == pytest.approx(0.001)
    finally:
        sys.setswitchinterval(original)


# ---------------------------------------------------------------------------
# 4. Progress reaches the window as a copy, and at a limited rate
# ---------------------------------------------------------------------------

def test_a_snapshot_is_independent_of_the_live_stats():
    live = IndexStats(indexed=5)
    live.skipped_by_code["ERR_A"] = 1
    live.notices.append("first")
    live.recent.append((1.0, 2, 3))

    snap = live.snapshot()
    live.skipped_by_code["ERR_B"] = 2
    live.notices.append("second")
    live.indexed = 99

    assert snap.indexed == 5
    assert snap.skipped_by_code == {"ERR_A": 1}
    assert snap.notices == ["first"]
    assert snap.recent == [(1.0, 2, 3)]
    assert snap.files_per_minute == 0.0          # properties still work on a copy


def test_a_snapshot_can_be_read_while_the_run_writes_to_it():
    r"""The failure this prevents: `dictionary changed size during iteration`
    inside a slot, which PyQt turns into an abort."""
    live = IndexStats()
    stop = threading.Event()

    def writer() -> None:
        index = 0
        while not stop.is_set():
            live.skipped_by_code[f"ERR_{index}"] = index     # grows...
            live.skipped_by_code.pop(f"ERR_{index - 300}", None)   # ...and shrinks
            index += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(200):
            snap = live.snapshot()
            for _key, _value in snap.skipped_by_code.items():
                pass
    finally:
        stop.set()
        thread.join()


def test_the_index_worker_hands_the_window_a_copy(monkeypatch):
    pytest.importorskip("PySide6")
    from app.ui import workers

    live = IndexStats(indexed=1)

    class FakePipeline:
        store = None
        config = SimpleNamespace(limits=SimpleNamespace(low_priority=False))

        def run(self, *, on_progress):
            on_progress(live)
            return live

    worker = workers.IndexWorker(FakePipeline())
    received: list[object] = []
    worker.signals.progress.connect(received.append, type=_direct())
    worker.run()

    assert len(received) == 1
    assert received[0] is not live
    assert received[0].indexed == 1


def _view():
    from PySide6.QtWidgets import QApplication

    from app.ui.indexing_view import IndexingView

    QApplication.instance() or QApplication([])
    return IndexingView()


def test_progress_ticks_are_repainted_at_a_limited_rate():
    pytest.importorskip("PySide6")
    view = _view()
    painted: list[int] = []
    view.progressed.connect(lambda *args: painted.append(args[1]))

    for indexed in range(20):                    # twenty ticks in a few microseconds
        view._on_progress(IndexStats(indexed=indexed, seen=100))   # noqa: SLF001

    assert len(painted) == 1, "a burst of ticks must not repaint the page each time"
    assert painted == [0]


def test_a_pause_is_never_dropped_by_the_limiter():
    pytest.importorskip("PySide6")
    view = _view()
    painted: list[bool] = []
    view.progressed.connect(lambda *args: painted.append(args[4]))

    view._on_progress(IndexStats(indexed=1, seen=10))                     # noqa: SLF001
    view._on_progress(IndexStats(indexed=2, seen=10, paused=True))        # noqa: SLF001

    assert painted == [False, True], "the tick that explains a stopped bar always paints"


def test_progress_paints_again_once_the_interval_has_passed():
    pytest.importorskip("PySide6")
    from app.ui import indexing_view

    view = _view()
    painted: list[int] = []
    view.progressed.connect(lambda *args: painted.append(args[1]))

    view._on_progress(IndexStats(indexed=1, seen=10))                     # noqa: SLF001
    view._last_paint -= indexing_view.PROGRESS_PAINT_MIN_S + 0.01         # noqa: SLF001
    view._on_progress(IndexStats(indexed=2, seen=10))                     # noqa: SLF001

    assert painted == [1, 2]


# ---------------------------------------------------------------------------
# 5. The database was already built for a reader that is not the writer
# ---------------------------------------------------------------------------

def test_the_metadata_store_keeps_wal_and_a_busy_timeout(tmp_path):
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(tmp_path / "index.db").connect()
    conn = store.conn
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 6. A slow window makes the run yield
# ---------------------------------------------------------------------------

def _yielding(monkeypatch, lag) -> list[float]:
    import app.index.pipeline as module

    slept: list[float] = []
    monkeypatch.setattr(module.time, "sleep", slept.append)
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.ui_lag = lag
    pipeline._yield_to_ui()                                               # noqa: SLF001
    return slept


def test_the_run_does_not_yield_when_nothing_watches_the_window(monkeypatch):
    assert _yielding(monkeypatch, None) == []


def test_the_run_does_not_yield_while_the_window_keeps_up(monkeypatch):
    assert _yielding(monkeypatch, lambda: UI_LAG_YIELD_S / 2) == []


def test_the_run_yields_for_about_as_long_as_the_window_is_late(monkeypatch):
    assert _yielding(monkeypatch, lambda: 0.22) == [0.22]


def test_a_yield_is_capped_so_the_run_cannot_crawl(monkeypatch):
    assert _yielding(monkeypatch, lambda: 30.0) == [UI_YIELD_MAX_S]


def test_a_broken_lag_probe_never_stops_the_run(monkeypatch):
    def broken() -> float:
        raise RuntimeError("the monitor is gone")

    assert _yielding(monkeypatch, broken) == []


def test_a_real_run_yields_between_items_when_the_window_is_slow(tmp_path, monkeypatch):
    import app.index.pipeline as module

    slept: list[float] = []
    real_sleep = time.sleep
    monkeypatch.setattr(module.time, "sleep",
                        lambda s: (slept.append(s), real_sleep(0.001))[-1]
                        if s >= UI_LAG_YIELD_S else real_sleep(s))
    pipeline = _pipeline(tmp_path)
    pipeline.ui_lag = lambda: 0.3

    stats = pipeline.run()

    assert stats.indexed == 6, "yielding must never cost a document"
    assert len(slept) >= 6
    assert set(slept) == {UI_YIELD_MAX_S}
