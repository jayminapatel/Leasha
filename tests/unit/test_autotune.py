r"""Index tuning §5: what Auto-tune learns, and when it stops trusting it.

**The failure mode this whole feature must not have is "wrong".** A rate
measured on a laptop and used on the desktop the same index folder was later
opened on is worse than having no rate at all: a wrong number is acted on with
confidence, an absent one falls back to the heuristics and nobody is any worse
off than before Auto-tune existed. Most of what is asserted here is that the
absent case is reached whenever there is the slightest doubt.

The second rule is §5c's, and it is a product rule rather than a technical one:
**in Auto an accepted adjustment applies itself and says so in plain words.** A
non-technical person must never be handed a decision in order to get the
benefit. In Manual it waits, because somebody who took manual control asked to
make these choices.
"""

from __future__ import annotations

import json
import time

import pytest

from app.core import measured as m
from app.index import autotune


class Machine:
    """A profile, invented, with only the method the rates are keyed by."""

    def __init__(self, name: str = "machine-a") -> None:
        self._name = name

    def fingerprint(self) -> str:
        return self._name


class Store:
    """The two state methods, and a switch to make writing fail."""

    def __init__(self, writable: bool = True) -> None:
        self.state: dict = {}
        self.writable = writable

    def get_state(self, key, default=""):
        return self.state.get(key, default)

    def set_state(self, key, value):
        if not self.writable:
            raise RuntimeError("the index is read-only")
        self.state[key] = value


class Stats:
    def __init__(self, stages, indexed=100, elapsed_s=10.0, workers=1):
        self.stages = stages
        self.indexed = indexed
        self.elapsed_s = elapsed_s
        self.workers = workers


# --- rates belong to one machine and to one version -------------------------


def test_rates_come_back_for_the_machine_they_were_taken_on() -> None:
    store, machine = Store(), Machine()
    m.remember(store, m.Measured(fingerprint="machine-a",
                                 extract_per_second=12.0))

    found = m.for_profile(store, machine)

    assert found is not None and found.extract_per_second == 12.0


def test_rates_from_another_machine_are_refused() -> None:
    """The same index folder opened on a different box. Silently using the old
    machine's numbers is the failure this key exists to prevent."""
    store = Store()
    m.remember(store, m.Measured(fingerprint="a-laptop", extract_per_second=2.0))

    assert m.for_profile(store, Machine("a-desktop")) is None


def test_rates_with_no_machine_named_are_refused() -> None:
    """Unlabelled numbers get used on the wrong machine sooner or later."""
    store = Store()
    m.remember(store, m.Measured(fingerprint="", extract_per_second=2.0))

    assert m.for_profile(store, Machine()) is None


def test_rates_from_an_older_pipeline_are_refused() -> None:
    """§5d trigger (b). "Measured three weeks ago" says nothing about whether
    the code that produced the number still behaves that way; "measured under
    pipeline 1" against a running pipeline 2 says exactly that."""
    store = Store()
    m.remember(store, m.Measured(fingerprint="machine-a",
                                 pipeline_version=m.PIPELINE_VERSION - 1))

    assert m.for_profile(store, Machine()) is None


def test_three_drifting_runs_retire_the_rates() -> None:
    """§5d trigger (c): a machine that aged, a new antivirus, a full disk."""
    store = Store()
    m.remember(store, m.Measured(fingerprint="machine-a",
                                 drifting_runs=m.DRIFT_RUNS))

    assert m.for_profile(store, Machine()) is None


def test_nothing_stored_is_not_an_error() -> None:
    assert m.for_profile(Store(), Machine()) is None


def test_unreadable_rates_are_not_an_error() -> None:
    """A cache is allowed to be corrupt. Falling back to the heuristics is the
    behaviour from before Auto-tune existed, which is a fine place to land."""
    store = Store()
    store.state[m.STATE_KEY] = "{not json"

    assert m.for_profile(store, Machine()) is None


def test_a_store_that_cannot_be_written_is_not_an_error() -> None:
    """A benchmark whose result cannot be cached is a benchmark to run again,
    not a failed benchmark."""
    assert m.remember(Store(writable=False), m.Measured()) is False


def test_each_reason_reads_differently() -> None:
    """The tuning screen shows it. "This is a different computer" and "the app
    was updated" lead somebody to different conclusions."""
    machine = Machine()
    reasons = {
        m.Measured(fingerprint="").stale_against(machine),
        m.Measured(fingerprint="other").stale_against(machine),
        m.Measured(fingerprint="machine-a",
                   pipeline_version=99).stale_against(machine),
        m.Measured(fingerprint="machine-a",
                   drifting_runs=9).stale_against(machine),
    }

    assert len(reasons) == 4, reasons
    assert "" not in reasons
    assert m.Measured(fingerprint="machine-a").stale_against(machine) == ""


# --- drift is proportional --------------------------------------------------


def test_drift_is_measured_as_a_ratio_not_a_difference() -> None:
    """2 files a second becoming 1 is the same event as 200 becoming 100. An
    absolute threshold fires on one and never on the other."""
    small = m.Measured(extract_per_second=2.0)
    large = m.Measured(extract_per_second=200.0)

    assert m.drifted(small, 1.0)
    assert m.drifted(large, 100.0)
    assert not m.drifted(large, 190.0), "5% is an ordinary busy afternoon"


def test_drift_against_nothing_is_not_drift() -> None:
    for stored, fresh in ((None, 5.0), (m.Measured(), 5.0),
                          (m.Measured(extract_per_second=5.0), 0.0)):
        assert not m.drifted(stored, fresh)


# --- what a measurement is allowed to argue for -----------------------------


def test_a_waiting_bound_run_argues_for_more_readers() -> None:
    found = m.Measured(stages={"waiting": 80.0, "embed": 20.0}).suggests()

    assert found == {"INDEX_WORKERS": 0}, "0 means: let the envelope decide"


def test_a_balanced_run_argues_for_nothing() -> None:
    """**Empty is the common and correct answer.** In Auto these apply
    themselves, so a rule firing on noise would move somebody's settings for no
    reason and they would never learn why the machine got slower."""
    assert m.Measured(stages={"waiting": 30.0, "embed": 40.0,
                              "write": 30.0}).suggests() == {}
    assert m.Measured().suggests() == {}


def test_the_automatic_change_never_names_a_number() -> None:
    """It hands the decision to the bounded, tested envelope rather than
    proposing a count of its own, so nothing here can exceed the machine."""
    for key, value in m.Measured(stages={"waiting": 90.0}).suggests().items():
        assert key in autotune.ACCEPTED
        assert value == 0


# --- §5c: Auto applies, Manual proposes -------------------------------------


def test_auto_applies_the_change_and_says_so_in_plain_words() -> None:
    store, machine = Store(), Machine()

    found = autotune.learn(store, machine, Stats({"waiting": 90.0, "embed": 10.0}),
                           mode="auto")

    assert found is not None and found.applied
    assert "sped up" in found.message
    # Jargon means the vocabulary of this codebase, not ordinary English: the
    # sentence may say "waiting for files to be read", which is what anybody
    # would say, and must not say "waiting: 90%" or name a settings key.
    for jargon in ("%", "INDEX_WORKERS", "envelope", "embed", "chunk",
                   "worker", "stage"):
        assert jargon not in found.message.lower(), jargon


def test_manual_proposes_rather_than_applying() -> None:
    """Somebody who took manual control asked to make these choices."""
    found = autotune.learn(Store(), Machine(),
                           Stats({"waiting": 90.0}), mode="manual")

    assert found is not None and not found.applied


def test_defaults_changes_nothing() -> None:
    """It already resolves from the envelope every run, so there is nothing to
    change - it is *already* whatever the envelope now says."""
    assert autotune.learn(Store(), Machine(),
                          Stats({"waiting": 90.0}), mode="defaults") is None


def test_a_run_that_measured_nothing_teaches_nothing() -> None:
    assert autotune.learn(Store(), Machine(), Stats({}), mode="auto") is None


def test_every_run_is_recorded_whatever_the_mode() -> None:
    """A person in Manual still benefits from the drift detector noticing their
    machine has changed. What they do not get is their settings moved."""
    store = Store()

    autotune.learn(store, Machine(), Stats({"embed": 50.0, "write": 50.0}),
                   mode="manual")

    assert m.STATE_KEY in store.state


def test_drift_counts_up_and_resets_on_agreement() -> None:
    """One slow afternoon is not a changed machine; three in a row is. A latch
    could not tell them apart."""
    store, machine = Store(), Machine()
    m.remember(store, m.Measured(fingerprint="machine-a",
                                 extract_per_second=10.0))

    autotune.learn(store, machine, Stats({"write": 9.0}, indexed=10, elapsed_s=10.0))
    assert json.loads(store.state[m.STATE_KEY])["drifting_runs"] == 1

    autotune.learn(store, machine, Stats({"write": 9.0}, indexed=100, elapsed_s=10.0))
    assert json.loads(store.state[m.STATE_KEY])["drifting_runs"] == 0


def test_a_multi_worker_run_is_compared_per_worker() -> None:
    """A four-worker run against a single-threaded bench would look 4x faster
    every time, and trip the drift detector on the first run after benching."""
    store, machine = Store(), Machine()
    m.remember(store, m.Measured(fingerprint="machine-a",
                                 extract_per_second=10.0))

    autotune.learn(store, machine,
                   Stats({"write": 9.0}, indexed=400, elapsed_s=10.0, workers=4))

    assert json.loads(store.state[m.STATE_KEY])["drifting_runs"] == 0


# --- the status line --------------------------------------------------------


def test_the_status_line_says_when_it_was_last_checked() -> None:
    store = Store()
    m.remember(store, m.Measured(fingerprint="machine-a",
                                 taken_at=time.time()))

    line = autotune.status_line(store, Machine())

    assert line.startswith("Tuned for this computer")
    assert "last checked" in line


def test_the_status_line_says_when_nothing_has_been_measured() -> None:
    """Rather than nothing, which is indistinguishable from a broken panel."""
    assert "Not yet timed" in autotune.status_line(Store(), Machine())


def test_the_bench_is_asked_for_when_there_is_nothing_to_trust() -> None:
    assert autotune.should_bench(Store(), Machine())
    store = Store()
    m.remember(store, m.Measured(fingerprint="machine-a"))
    assert autotune.should_bench(store, Machine()) == ""


# --- the envelope is where a measured rate lands ----------------------------


def test_a_measured_rate_can_widen_the_batch() -> None:
    from app.core import envelope
    from app.core.compute_profile import ComputeProfile

    machine = ComputeProfile(logical_processors=8, physical_cores=8,
                             ram_mb=32 * 1024)
    heuristic = envelope.for_setting("EMBED_BATCH", machine)
    fast = envelope.for_setting(
        "EMBED_BATCH", machine,
        m.Measured(embed_per_second={"gpu": 400.0}))

    assert fast.auto > heuristic.auto
    assert "measured at 400" in fast.why


def test_a_slow_machine_keeps_the_heuristic() -> None:
    """Below roughly twenty chunks a second the per-call overhead is already
    noise against the matrix work, and a bigger batch buys nothing while
    holding more text in memory."""
    from app.core import envelope
    from app.core.compute_profile import ComputeProfile

    machine = ComputeProfile(logical_processors=4, physical_cores=4,
                             ram_mb=8 * 1024)

    heuristic = envelope.for_setting("EMBED_BATCH", machine)
    slow = envelope.for_setting("EMBED_BATCH", machine,
                                m.Measured(embed_per_second={"cpu": 4.0}))

    assert slow.auto == heuristic.auto


def test_no_measurement_gives_exactly_the_old_behaviour() -> None:
    """Which is what Defaults mode wants, and what every caller gets until it
    asks otherwise."""
    from app.core import envelope
    from app.core.compute_profile import ComputeProfile

    machine = ComputeProfile(logical_processors=8, physical_cores=8,
                             ram_mb=16 * 1024)

    assert (envelope.for_setting("EMBED_BATCH", machine)
            == envelope.for_setting("EMBED_BATCH", machine, None))


@pytest.mark.parametrize("key", ["INDEX_WORKERS", "INDEX_MEMORY_MB",
                                 "ONNX_INTRA_OP_THREADS"])
def test_a_measurement_does_not_touch_the_bounds_it_cannot_improve(key) -> None:
    """Most bounds are about what the machine *has* - memory, cores, disk - and
    no amount of measuring changes those."""
    from app.core import envelope
    from app.core.compute_profile import ComputeProfile

    machine = ComputeProfile(logical_processors=8, physical_cores=8,
                             ram_mb=16 * 1024)
    rates = m.Measured(embed_per_second={"gpu": 900.0})

    assert (envelope.for_setting(key, machine)
            == envelope.for_setting(key, machine, rates))
