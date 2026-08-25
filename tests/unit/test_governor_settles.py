"""A memory pause that frees nothing is waiting for something that never comes.

Layer: L3

Observed on a real run, indexing the project folder for ten minutes:

    pause - Indexing has added 1,501MB (now 1,601MB), above the 1,500MB...
    run - clear
    pause - Indexing has added 1,539MB (now 1,638MB), above the 1,500MB...
    run - clear
    pause - Indexing has added 1,540MB ...

The ceiling is growth above a baseline taken on the **first probe**, seconds
into the run. The embedding model, the reranker and the OCR engine all load
*after* that - the run's own log said `OCR engine loaded in 3.0s`, seven
seconds in - and together they are roughly a gigabyte that is never released.

So growth sat permanently just over the ceiling, and the governor paused,
resumed and paused again for as long as it was left alone. It was making
progress, but paying a pause cycle for it.

The fix is not to raise the ceiling. It is to notice that **pausing only helps
if the memory can actually be released**, and that a pause which does not move
RSS has proved the memory is resident. The floor is then raised to accept it -
bounded by `MAX_SETTLES`, and logged, because a ceiling that quietly moves
itself is precisely the sort of thing that must never happen silently.
"""

from __future__ import annotations

import pytest

from app.index.resources import (
    MAX_SETTLES,
    SETTLE_POLLS,
    ResourceGovernor,
    ResourceLimits,
    Snapshot,
    SystemProbe,
    verdict,
)

LIMITS = ResourceLimits(memory_mb=1500, cpu_percent=0, pause_on_battery=False,
                        min_free_gb=0, poll_seconds=0.0)


class FakeProbe(SystemProbe):
    """A `SystemProbe` whose readings are scripted.

    Subclasses the real one rather than duck-typing it, because
    `_accept_resident` deliberately refuses to adjust anything that is not a
    `SystemProbe` - an injected fake has no baseline worth moving, and silently
    doing nothing there would be a worse bug than the one being fixed.
    """

    def __init__(self, readings):
        super().__init__(baseline_mb=100.0)
        self._readings = list(readings)
        self.reads = 0

    def read(self) -> Snapshot:            # type: ignore[override]
        rss = self._readings[min(self.reads, len(self._readings) - 1)]
        self.reads += 1
        return Snapshot(rss_mb=rss, baseline_mb=self.baseline_mb,
                        free_disk_gb=999.0, on_battery=False, at=float(self.reads))


def governor(readings):
    probe = FakeProbe(readings)
    return ResourceGovernor(LIMITS, probe=probe.read, sleep=lambda _s: None), probe


# --- the bug ----------------------------------------------------------------

def test_a_pause_that_frees_nothing_stops_pausing():
    """The ten-minute oscillation, in one test.

    RSS never moves: the models are resident. Without the settle rule this
    loops forever.
    """
    engine, probe = governor([1700.0] * 200)

    found = engine.wait_while_throttled()

    assert found.action == "run"
    assert probe.baseline_mb == 1700.0, "the floor was not raised"
    assert probe.reads < 20, "it kept polling long after waiting proved useless"


def test_it_waits_before_deciding_the_memory_is_resident():
    """A pause must be given a fair chance to work.

    Transient buffers - a large PDF being parsed - do come back, and adjusting
    on the first poll would turn a working ceiling into no ceiling at all.
    """
    engine, probe = governor([1700.0] * SETTLE_POLLS + [1700.0] * 50)

    engine.wait_while_throttled()

    assert probe.reads > SETTLE_POLLS, "it gave up waiting immediately"


def test_memory_that_is_released_is_never_accepted_as_resident():
    """The case the pause exists for, which must keep working untouched."""
    engine, probe = governor([1700.0, 1690.0, 1200.0, 1200.0])

    found = engine.wait_while_throttled()

    assert found.action == "run"
    assert probe.baseline_mb == 100.0, "the floor moved for memory that was freed"


def test_the_ceiling_still_applies_above_the_new_floor():
    """Not "the limit is off now" - the limit is measured from a higher floor."""
    probe = FakeProbe([1700.0])
    probe.baseline_mb = 1700.0          # as a settle would leave it

    assert verdict(Snapshot(rss_mb=1700.0, baseline_mb=1700.0, free_disk_gb=999.0,
                            on_battery=False), LIMITS).action == "run"
    # ...and a genuine runaway above it still trips.
    assert verdict(Snapshot(rss_mb=3300.0, baseline_mb=1700.0, free_disk_gb=999.0,
                            on_battery=False), LIMITS).action == "pause"


def test_a_real_leak_still_trips_after_the_settles_run_out():
    """Memory that keeps climbing is not resident, it is a leak.

    Each settle buys one more ceiling's worth of headroom, so a leak is
    tolerated for longer - but `MAX_SETTLES` bounds it and then the pause
    stands.
    """
    climbing = [1700.0 + step * 400 for step in range(200)]
    engine, probe = governor(climbing)

    found = engine.wait_while_throttled(should_stop=lambda: probe.reads > 150)

    assert found.action == "stop", "a runaway was never stopped"
    assert engine._settles <= MAX_SETTLES


def test_a_cpu_pause_is_not_treated_as_a_memory_pause():
    """Only memory pauses settle. A busy machine gets quiet again on its own."""
    limits = ResourceLimits(memory_mb=100_000, cpu_percent=50, busy_seconds=0.0,
                            quiet_seconds=0.0, pause_on_battery=False,
                            min_free_gb=0, poll_seconds=0.0)
    readings = iter([90.0, 90.0, 10.0, 10.0])

    def probe():
        busy = next(readings, 10.0)
        return Snapshot(rss_mb=200.0, baseline_mb=100.0, system_cpu_percent=busy,
                        own_cpu_percent=0.0, free_disk_gb=999.0, on_battery=False,
                        at=0.0)

    engine = ResourceGovernor(limits, probe=probe, sleep=lambda _s: None)

    assert engine.wait_while_throttled().action == "run"


# --- the cause field, which the settle rule depends on ----------------------

@pytest.mark.parametrize("snapshot, expected", [
    (Snapshot(rss_mb=200.0, baseline_mb=100.0, free_disk_gb=0.5, on_battery=False), "disk"),
    (Snapshot(rss_mb=9000.0, baseline_mb=100.0, free_disk_gb=999.0, on_battery=False), "memory"),
    (Snapshot(rss_mb=200.0, baseline_mb=100.0, free_disk_gb=999.0, on_battery=True), "battery"),
])
def test_every_throttle_says_which_limit_produced_it(snapshot, expected):
    """Matching on the reason *text* would break when the wording changed, and
    the wording is meant to be free to change - it is what the user reads."""
    limits = ResourceLimits(memory_mb=1500, cpu_percent=0, min_free_gb=5,
                            pause_on_battery=True)

    assert verdict(snapshot, limits).cause == expected


def test_running_carries_no_cause():
    healthy = Snapshot(rss_mb=200.0, baseline_mb=100.0, free_disk_gb=999.0,
                       on_battery=False)

    found = verdict(healthy, LIMITS)
    assert found.action == "run"
    assert found.cause == ""
