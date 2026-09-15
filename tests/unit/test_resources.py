"""The limits that stop the indexer ruining the machine it runs on.

Layer: L3

None of this can be tested by actually exhausting memory or saturating a CPU -
that would be slow, flaky, and would take the test runner down with it. So the
decision is a pure function of a `Snapshot`, and every threshold, hysteresis
boundary and recovery path below is driven with invented numbers.

The bias throughout: **an unmeasurable resource never blocks the run.** Refusing
to index because psutil is missing would be a self-inflicted outage.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.index.resources import (
    ResourceGovernor,
    ResourceLimits,
    Snapshot,
    Verdict,
    default_workers,
    verdict,
)

LIMITS = ResourceLimits(
    memory_mb=1000, cpu_percent=80, min_free_gb=5,
    busy_seconds=5.0, quiet_seconds=10.0, poll_seconds=0.0,
)


def snap(**kwargs) -> Snapshot:
    base = dict(rss_mb=200.0, system_cpu_percent=10.0, own_cpu_percent=0.0,
                free_disk_gb=100.0, on_battery=False)
    base.update(kwargs)
    return Snapshot(**base)


# -- how many workers --------------------------------------------------------

@pytest.mark.parametrize(("cores", "expected"), [
    (1, 1), (2, 1), (4, 2), (8, 4), (16, 4), (64, 4),
])
def test_workers_are_half_the_cores_capped_at_four(cores, expected):
    """Deliberately not `cores - 1`.

    That is right for a batch job on a build server and wrong here: on an
    8-core laptop it hands seven cores to a background task and leaves one for
    the person. The cap exists because extraction is I/O-bound long before it
    is CPU-bound - past four the disk is the wall and more threads only add
    contention and memory.
    """
    assert default_workers(cores) == expected


def test_an_explicit_worker_count_is_honoured():
    assert ResourceLimits(workers=7).resolved_workers() == 7


def test_zero_workers_means_decide_for_me():
    assert ResourceLimits(workers=0).resolved_workers() == default_workers()


# -- disk: the one that stops rather than pauses -----------------------------

def test_running_out_of_disk_stops_the_run():
    """Not a pause. It does not resolve itself while the indexer keeps writing,
    and filling a drive breaks things far outside this application."""
    found = verdict(snap(free_disk_gb=2.0), LIMITS)
    assert found.action == "stop"


def test_the_disk_message_says_nothing_was_lost():
    found = verdict(snap(free_disk_gb=1.0), LIMITS)
    assert "saved" in found.reason
    assert "5GB" in found.reason


def test_disk_is_checked_before_everything_else():
    """A machine that is short of disk AND memory must stop, not pause forever."""
    found = verdict(snap(free_disk_gb=1.0, rss_mb=99_999), LIMITS)
    assert found.action == "stop"


# -- memory ------------------------------------------------------------------

def test_exceeding_the_memory_ceiling_pauses_rather_than_aborts():
    """A pause costs minutes and loses nothing. An abort loses the run."""
    found = verdict(snap(rss_mb=1200.0), LIMITS)
    assert found.action == "pause"
    assert "1,200MB" in found.reason


def test_memory_below_the_ceiling_runs():
    assert verdict(snap(rss_mb=999.0), LIMITS).running


def test_the_ceiling_is_not_a_target():
    """Sitting exactly at the limit is allowed; only above it pauses."""
    assert verdict(snap(rss_mb=1000.0), LIMITS).running


# -- battery -----------------------------------------------------------------

def test_battery_pauses_when_asked_to():
    assert verdict(snap(on_battery=True), LIMITS).action == "pause"


def test_battery_is_ignored_when_the_user_has_turned_it_off():
    limits = ResourceLimits(pause_on_battery=False)
    assert verdict(snap(on_battery=True), limits).running


def test_a_desktop_with_no_battery_is_not_treated_as_on_battery():
    """`sensors_battery()` returns None on a desktop, which must not read as True."""
    assert verdict(snap(on_battery=None), LIMITS).running


# -- CPU, and why it needs hysteresis ----------------------------------------

def test_a_busy_machine_alone_is_not_enough_to_pause():
    """One reading above the line is a transient, not a busy machine."""
    assert verdict(snap(system_cpu_percent=95.0), LIMITS, busy_since=None, now=100.0).running


def test_a_machine_busy_for_long_enough_pauses():
    found = verdict(snap(system_cpu_percent=95.0), LIMITS, busy_since=100.0, now=110.0)
    assert found.action == "pause"
    assert "95% CPU" in found.reason


def test_a_brief_spike_does_not_pause():
    found = verdict(snap(system_cpu_percent=95.0), LIMITS, busy_since=100.0, now=102.0)
    assert found.running, "a two-second spike should not stop the indexer"


def test_a_cpu_limit_of_zero_disables_the_check():
    limits = ResourceLimits(cpu_percent=0)
    found = verdict(snap(system_cpu_percent=100.0), limits, busy_since=0.0, now=9999.0)
    assert found.running


# -- what happens when nothing can be measured -------------------------------

def test_an_unmeasurable_machine_is_allowed_to_run():
    """Without psutil every reading is None. Refusing to index would be an
    outage this application inflicted on itself."""
    assert verdict(Snapshot(), LIMITS).running


def test_a_missing_reading_does_not_read_as_zero():
    """`None` must not be compared as a number anywhere."""
    assert verdict(Snapshot(rss_mb=None, free_disk_gb=None), LIMITS).running


# -- the governor, including the state it carries between calls --------------

class FakeProbe:
    """Returns a scripted sequence of snapshots, repeating the last one."""

    def __init__(self, *snapshots: Snapshot):
        self.snapshots = list(snapshots)
        self.calls = 0

    def __call__(self) -> Snapshot:
        index = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return self.snapshots[index]


def test_the_governor_pauses_then_resumes_when_the_machine_frees_up():
    probe = FakeProbe(snap(rss_mb=2000.0), snap(rss_mb=2000.0), snap(rss_mb=100.0))
    slept: list[float] = []
    governor = ResourceGovernor(LIMITS, probe=probe, sleep=slept.append)

    found = governor.wait_while_throttled()

    assert found.running
    assert slept, "it should have waited rather than spinning"
    assert governor.pauses == 1


def test_waiting_ends_immediately_when_the_user_presses_stop():
    """Nobody should have to sit out a battery pause to cancel a run."""
    probe = FakeProbe(snap(on_battery=True))
    governor = ResourceGovernor(LIMITS, probe=probe, sleep=lambda _s: None)

    found = governor.wait_while_throttled(should_stop=lambda: True)

    assert found.action == "stop"
    assert "your request" in found.reason


def test_a_full_disk_ends_the_wait_rather_than_looping_forever():
    probe = FakeProbe(snap(free_disk_gb=0.5))
    governor = ResourceGovernor(LIMITS, probe=probe, sleep=lambda _s: None)

    assert governor.wait_while_throttled().action == "stop"


def test_the_state_change_callback_fires_once_per_transition():
    """The UI needs to know *why* it went quiet, and needs telling once."""
    probe = FakeProbe(snap(rss_mb=2000.0), snap(rss_mb=2000.0), snap(rss_mb=10.0))
    seen: list[Verdict] = []
    governor = ResourceGovernor(
        LIMITS, probe=probe, sleep=lambda _s: None, on_state_change=seen.append
    )

    governor.wait_while_throttled()

    assert [found.action for found in seen] == ["pause", "run"]


def test_the_summary_reports_what_the_limits_actually_were():
    governor = ResourceGovernor(LIMITS, probe=FakeProbe(snap()), sleep=lambda _s: None)
    summary = governor.summary()
    assert summary["memory_mb_cap"] == 1000
    assert summary["cpu_percent_cap"] == 80
    assert summary["min_free_gb"] == 5


def test_limits_come_from_settings_but_survive_an_older_env():
    """An .env written before these keys existed must still load.

    A configuration file that has to be regenerated to open the app is not
    configuration, it is a migration.
    """
    from app.index.resources import limits_from_settings

    class Old:
        min_free_gb = 9                      # the only key that existed

    limits = limits_from_settings(Old())
    assert limits.min_free_gb == 9
    assert limits.memory_mb == ResourceLimits().memory_mb


# ---------------------------------------------------------------------------
# Self-throttling: the indexer must not treat its own work as a busy machine.
# ---------------------------------------------------------------------------

def test_the_indexers_own_load_is_not_a_reason_to_pause():
    """Four workers on a four-core laptop saturate the CPU by themselves.

    Counting that as "the machine is busy" makes the indexer pause, watch CPU
    fall, resume, spike, and pause again - throttling itself to a crawl while
    the machine is in fact completely free. The governor exists to yield to
    *other* work.
    """
    mine = snap(system_cpu_percent=95.0, own_cpu_percent=90.0)
    assert mine.other_cpu_percent == pytest.approx(5.0)
    assert verdict(mine, LIMITS, busy_since=0.0, now=9999.0).running


def test_somebody_elses_load_still_pauses():
    theirs = snap(system_cpu_percent=95.0, own_cpu_percent=5.0)
    found = verdict(theirs, LIMITS, busy_since=100.0, now=110.0)
    assert found.action == "pause"
    assert "other programs" in found.reason


def test_own_cpu_is_never_allowed_to_make_the_figure_negative():
    """Two readings taken microseconds apart can disagree."""
    assert snap(system_cpu_percent=10.0, own_cpu_percent=40.0).other_cpu_percent == 0.0


def test_an_unmeasurable_cpu_stays_unmeasurable():
    assert snap(system_cpu_percent=None, own_cpu_percent=None).other_cpu_percent is None


def test_a_missing_own_reading_falls_back_to_the_system_figure():
    """Better to be conservative than to divide by a number that is not there."""
    assert snap(system_cpu_percent=70.0, own_cpu_percent=None).other_cpu_percent == 70.0


# ---------------------------------------------------------------------------
# The ceiling is on GROWTH, not on the absolute figure.
#
# Reported from the first GUI run: it paused on its very first check at
# 1,597MB and never resumed, finishing `seen: 1, indexed: 0` after 96 seconds.
# ---------------------------------------------------------------------------

def test_a_gui_baseline_does_not_trip_the_ceiling_before_any_work():
    """The CLI starts at ~200MB; the GUI starts above 1.2GB with Qt, the ONNX
    runtime and both stores already resident. An absolute cap therefore made
    indexing from the window impossible - it paused instantly, every time."""
    gui = snap(rss_mb=1597.0, baseline_mb=1400.0)
    assert verdict(gui, ResourceLimits(memory_mb=1500)).running


def test_real_growth_still_pauses():
    runaway = snap(rss_mb=12_056.0, baseline_mb=1400.0)
    found = verdict(runaway, ResourceLimits(memory_mb=1500))
    assert found.action == "pause"
    assert "added" in found.reason
    assert "10,656MB" in found.reason, "it should name the growth, not just the total"


def test_growth_is_never_negative():
    """Memory can fall below where it started - a model unloaded, a cache
    dropped - and a negative reading must not read as a huge number."""
    assert snap(rss_mb=800.0, baseline_mb=1200.0).growth_mb == 0.0


def test_an_unknown_baseline_falls_back_to_the_absolute_figure():
    """The safe direction: not knowing the baseline must not silently disable
    the ceiling altogether."""
    assert snap(rss_mb=2000.0, baseline_mb=None).growth_mb == 2000.0
    assert verdict(snap(rss_mb=2000.0, baseline_mb=None), LIMITS).action == "pause"


def test_the_probe_adopts_its_first_reading_as_the_baseline():
    """Whatever the process already weighed is not indexing's fault."""
    from app.index.resources import SystemProbe

    probe = SystemProbe()
    first = probe.read()
    if first.rss_mb is None:
        pytest.skip("psutil is not available here")

    assert first.baseline_mb == pytest.approx(first.rss_mb, rel=0.5)
    assert probe.read().growth_mb is not None


# ---------------------------------------------------------------------------
# 2026-09-08: the converters are our load too, and a pause names its cause.
#
# From `logs/runs/run-20260908-055844-window.log`: ~25 pause/resume cycles in
# 22 minutes on "81-95% CPU used by other programs", on a machine where the
# "other programs" were largely the indexer's own converter subprocesses.
# ---------------------------------------------------------------------------

class FakeChild:
    def __init__(self, pid: int, percent: float, *, raises: type | None = None):
        self.pid = pid
        self._percent = percent
        self._raises = raises
        self.info = {"pid": pid, "name": f"child{pid}.exe"}

    def cpu_percent(self, interval=None) -> float:
        if self._raises is not None:
            raise self._raises(self.pid)
        return self._percent


class FakeProcess:
    """Enough of `psutil.Process` for the probe."""

    def __init__(self, own_percent: float, children: list, pid: int = 4242):
        self.pid = pid
        self._own = own_percent
        self._children = children
        self.info = {"pid": pid, "name": "Leasha.exe"}

    class _Mem:
        rss = 300 * 1_048_576

    def memory_info(self):
        return self._Mem()

    def cpu_percent(self, interval=None) -> float:
        return self._own

    def children(self, recursive: bool = False) -> list:
        return list(self._children)


class FakePsutil:
    """A psutil module with a scripted process table and no real machine."""

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    def __init__(self, process: FakeProcess, table: list | None = None,
                 cores: int = 4, iter_raises: bool = False):
        self._process = process
        self._table = table or []
        self._cores = cores
        self._iter_raises = iter_raises

    def Process(self, pid=None):
        return self._process

    def cpu_count(self, logical: bool = True) -> int:
        return self._cores

    def cpu_percent(self, interval=None) -> float:
        return 90.0

    def sensors_battery(self):
        return None

    def process_iter(self, attrs=None):
        if self._iter_raises:
            raise RuntimeError("the process table is not available")
        return iter(self._table)


def probe_with(fake: FakePsutil):
    from app.index.resources import SystemProbe

    probe = SystemProbe()
    probe._psutil = lambda: fake                # the seam, exactly as `read` uses it
    probe._process = fake.Process()
    return probe


def test_child_processes_count_as_our_own_load():
    """LibreOffice, the DWG and RTF converters run as children. Their CPU was
    landing on the "other programs" side, so the governor paused the indexer
    because of the indexer's own converter, resumed, spawned the next one and
    paused again."""
    me = FakeProcess(own_percent=40.0, children=[FakeChild(1, 120.0), FakeChild(2, 40.0)])
    snapshot = probe_with(FakePsutil(me, cores=4)).read()

    # (40 + 120 + 40) per-core-summed, over four cores.
    assert snapshot.own_cpu_percent == pytest.approx(50.0)
    assert snapshot.other_cpu_percent == pytest.approx(40.0)


def test_a_child_that_exits_mid_read_is_skipped_and_the_probe_still_answers():
    """Children exit between being listed and being read - a converter
    finishing is the ordinary case. The probe never raises."""
    me = FakeProcess(own_percent=40.0, children=[
        FakeChild(1, 80.0),
        FakeChild(2, 999.0, raises=FakePsutil.NoSuchProcess),
        FakeChild(3, 999.0, raises=FakePsutil.AccessDenied),
    ])
    snapshot = probe_with(FakePsutil(me, cores=4)).read()

    assert isinstance(snapshot, Snapshot)
    assert snapshot.own_cpu_percent == pytest.approx(30.0)


def test_the_probe_stays_cheap_with_children():
    """The cost of the children walk, stated rather than assumed: with eight
    fake children a full `read()` stays well under a millisecond here, so the
    real `children(recursive=True)` call - one process-table walk - is the
    only cost added, and psutil's own is a few hundred microseconds."""
    import time as _time

    me = FakeProcess(own_percent=10.0, children=[FakeChild(i, 5.0) for i in range(8)])
    probe = probe_with(FakePsutil(me, cores=4))
    probe.read()                                # warm

    started = _time.perf_counter()
    for _ in range(200):
        probe.read()
    per_read = (_time.perf_counter() - started) / 200

    assert per_read < 0.005, f"{per_read * 1000:.2f}ms per read with eight children"


# -- who is busy -------------------------------------------------------------

@pytest.fixture
def captured():
    """INFO and above from the resources logger, as plain messages."""
    from app.core.logging import logger

    lines: list[str] = []
    handle = logger.add(lambda message: lines.append(message.record["message"]),
                        level="INFO", format="{message}")
    yield lines
    logger.remove(handle)


def test_a_cpu_pause_names_the_three_busiest_programs(captured):
    """The "plant logging" move: the next log answers "busy with what" instead
    of a guess between antivirus, Ollama, Windows Search and OneDrive."""
    from app.index.resources import busiest_processes

    me = FakeProcess(own_percent=0.0, children=[FakeChild(7, 400.0)], pid=4242)
    table = [
        FakeChild(10, 160.0), FakeChild(11, 40.0), FakeChild(12, 90.0),
        FakeChild(13, 8.0), me, FakeChild(7, 400.0),   # us, and our child
    ]
    table[0].info["name"] = "MsMpEng.exe"
    table[1].info["name"] = "chrome.exe"
    table[2].info["name"] = "ollama.exe"
    slept: list[float] = []
    fake = FakePsutil(me, table=table, cores=4)

    top = busiest_processes(psutil_module=fake, sleep=slept.append)

    assert top == [("MsMpEng.exe", 40.0), ("ollama.exe", 22.5), ("chrome.exe", 10.0)]
    assert slept == [0.5], "two readings half a second apart, at a pause boundary"


def test_the_busiest_sample_never_raises(captured):
    from app.index.resources import busiest_processes

    me = FakeProcess(own_percent=0.0, children=[])
    fake = FakePsutil(me, iter_raises=True)

    assert busiest_processes(psutil_module=fake, sleep=lambda _s: None) == []


def test_the_governor_logs_the_busiest_on_the_way_into_a_cpu_pause(captured):
    calls: list[int] = []

    def busiest():
        calls.append(1)
        return [("MsMpEng.exe", 41.0), ("ollama.exe", 22.0), ("chrome.exe", 9.0)]

    governor = ResourceGovernor(LIMITS, probe=FakeProbe(snap(system_cpu_percent=95.0)),
                                sleep=lambda _s: None, busiest=busiest)
    governor.check(now=100.0)                   # starts the busy timer
    found = governor.check(now=110.0)           # busy long enough: pause

    assert found.action == "pause" and found.cause == "cpu"
    assert calls == [1]
    line = next(l for l in captured if "busiest right now" in l)
    assert line.endswith("MsMpEng.exe 41%, ollama.exe 22%, chrome.exe 9%")


def test_the_busiest_sample_is_not_repeated_within_thirty_seconds(captured):
    """A flapping governor must not spend half a second per flap."""
    calls: list[int] = []
    busy, quiet = snap(system_cpu_percent=95.0), snap(system_cpu_percent=5.0)
    probe = FakeProbe(busy, busy, quiet, quiet, busy, busy, quiet, quiet, busy, busy)
    governor = ResourceGovernor(LIMITS, probe=probe, sleep=lambda _s: None,
                                busiest=lambda: calls.append(1) or [("x.exe", 50.0)])

    governor.check(now=0.0); governor.check(now=6.0)        # pause 1 at t=6
    governor.check(now=7.0); governor.check(now=18.0)       # run again at t=18
    governor.check(now=19.0); governor.check(now=25.0)      # pause 2 at t=25: <30s, skipped
    governor.check(now=26.0); governor.check(now=37.0)      # run
    governor.check(now=38.0); governor.check(now=44.0)      # pause 3 at t=44: 38s, sampled

    assert governor.pauses == 3
    assert calls == [1, 1]


def test_a_memory_pause_does_not_sample_the_process_table(captured):
    calls: list[int] = []
    governor = ResourceGovernor(LIMITS, probe=FakeProbe(snap(rss_mb=2000.0)),
                                sleep=lambda _s: None,
                                busiest=lambda: calls.append(1) or [])

    assert governor.check(now=0.0).cause == "memory"
    assert calls == []
    assert not any("busiest" in l for l in captured)


def test_a_failing_busiest_sample_does_not_break_the_check(captured):
    def broken():
        raise RuntimeError("no process table")

    governor = ResourceGovernor(LIMITS, probe=FakeProbe(snap(system_cpu_percent=95.0)),
                                sleep=lambda _s: None, busiest=broken)
    governor.check(now=0.0)

    assert governor.check(now=10.0).action == "pause"


# ---------------------------------------------------------------------------
# Priority is per-thread, never per-process
#
# **2026-09: `apply_priority` used to call `psutil.Process().nice(...)`
# with no PID - the whole process, GUI thread included.** Non-negotiable #1
# runs the window and the indexer in one process, so this silently ran the
# window below normal priority for as long as an index was going, and on a
# real machine under real competing load that produced multi-second frozen
# windows the instant Start was pressed - measured, not assumed: heartbeat
# gaps up to 7.3s with the old process-wide call under ~75% external CPU
# load, versus sub-20ms with the per-thread fix under the same load. See
# `SystemProbe.lower_current_thread_priority`.
#
# Not tested by actually spawning threads and saturating a CPU - the module
# docstring's own rule against slow, flaky tests that could take the runner
# down applies here as much as anywhere else in this file. What is tested:
# the real Windows call is reached through one seam, and nothing here ever
# reaches for a whole-process priority API again.
# ---------------------------------------------------------------------------

def test_lower_current_thread_priority_goes_through_the_one_seam(monkeypatch):
    """`SystemProbe` never touches the OS directly - it calls the module-level
    function a test can replace, so no test run ever changes its own thread's
    real priority."""
    import app.index.resources as resources
    from app.index.resources import SystemProbe

    calls = []
    monkeypatch.setattr(resources, "_lower_this_thread_to_background",
                        lambda: calls.append(1) or True)

    assert SystemProbe().lower_current_thread_priority() is True
    assert calls == [1]


def test_apply_priority_lowers_the_calling_thread_only(monkeypatch):
    """`ResourceGovernor.apply_priority()` is what `Pipeline.run()` and each
    of its own threads call - assert it reaches the per-thread seam, not a
    process-wide one, and that turning `low_priority` off skips it entirely."""
    import app.index.resources as resources

    calls = []
    monkeypatch.setattr(resources, "_lower_this_thread_to_background",
                        lambda: calls.append(1) or True)

    on = ResourceGovernor(replace(LIMITS, low_priority=True), probe=FakeProbe(snap()))
    assert on.apply_priority() is True
    assert calls == [1]

    off = ResourceGovernor(replace(LIMITS, low_priority=False), probe=FakeProbe(snap()))
    assert off.apply_priority() is False
    assert calls == [1]                          # unchanged - never called


def test_nothing_in_resources_calls_the_whole_process_priority_api():
    """A guard, in the spirit of `tests/unit/test_ui_never_blocks.py`, against
    a regression that no runtime test on an idle CI machine would ever catch -
    the freeze this replaces only shows up under real competing load.

    Checked by introspection, not a source substring: the fix's own docstring
    has to *describe* the old `psutil.Process().nice(BELOW_NORMAL_PRIORITY_
    CLASS)` call in prose to explain why it was wrong, and a plain substring
    check cannot tell that explanation from the call itself.
    """
    from app.index.resources import SystemProbe

    assert not hasattr(SystemProbe, "lower_priority"), (
        "SystemProbe still has the old whole-process lower_priority() method - "
        "see lower_current_thread_priority's docstring for why it was replaced"
    )
    assert hasattr(SystemProbe, "lower_current_thread_priority")


def test_a_real_windows_thread_priority_call_only_touches_the_calling_thread():
    """Runs the real `ctypes` call - safe because `THREAD_MODE_BACKGROUND_
    BEGIN` is, by definition, scoped to the thread that asks for it, so this
    is the one place in the suite allowed to call it directly rather than
    through the seam. Skipped everywhere but a real Windows interpreter."""
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows-only API")

    import threading

    from app.index.resources import _lower_this_thread_to_background

    result: dict = {}

    def worker() -> None:
        result["ok"] = _lower_this_thread_to_background()

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)

    assert result.get("ok") is True
