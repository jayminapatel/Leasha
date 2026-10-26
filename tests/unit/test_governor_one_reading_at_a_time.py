r"""The resource governor takes one machine reading at a time, over a real window.

Layer: L3

2026-10-26: a run paused about once a minute on "97% CPU used by other programs" while
the programs the log named came to under 20%. The run asks the probe from the walker,
every reader and the consumer, and `cpu_percent(interval=None)` answers for the time
since it was last asked: interleaved asks put the machine figure and Leasha's own figure
in different windows, and back-to-back asks in a window under Windows' clock tick, where
Leasha's own share reads 0.0. `SystemProbe.read` now holds a lock and hands an ask inside
`min_interval_s` the last reading.
"""

from __future__ import annotations

import threading

from app.index import resources
from app.index.resources import Snapshot, SystemProbe


def _counting_probe(monkeypatch, *, min_interval_s=resources.READ_MIN_S):
    clock = [100.0]
    monkeypatch.setattr(resources.time, "monotonic", lambda: clock[0])
    probe = SystemProbe(min_interval_s=min_interval_s)
    reads = []

    def read_now():
        reads.append(clock[0])
        return Snapshot(system_cpu_percent=90.0, own_cpu_percent=50.0, at=clock[0])

    probe._read_now = read_now
    return probe, clock, reads


def test_an_ask_inside_the_window_gets_the_last_reading(monkeypatch):
    probe, clock, reads = _counting_probe(monkeypatch)
    first = probe.read()
    clock[0] += resources.READ_MIN_S / 2
    assert probe.read() is first, "a fresh read here would cover a window too short to mean anything"
    assert len(reads) == 1
    clock[0] += resources.READ_MIN_S
    assert probe.read() is not first
    assert len(reads) == 2


def test_zero_reads_on_every_ask(monkeypatch):
    probe, _clock, reads = _counting_probe(monkeypatch, min_interval_s=0.0)
    for _ in range(3):
        probe.read()
    assert len(reads) == 3


def test_asks_from_many_threads_share_one_reading(monkeypatch):
    """The run's shape: the walker, the readers and the consumer, all at once."""
    probe, _clock, reads = _counting_probe(monkeypatch)
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(probe.read())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(reads) == 1
    assert len({id(s) for s in seen}) == 1
    assert seen[0].other_cpu_percent == 40.0


def test_the_window_is_half_a_second():
    assert resources.READ_MIN_S == 0.5
    assert SystemProbe().min_interval_s == resources.READ_MIN_S
