r"""What this machine actually did, as opposed to what its specification implies.

Layer: L0 — a record, with no policy in it.

**This is the entire difference between Defaults and Auto-tune.** Defaults
divides core counts by rules of thumb; Auto-tune divides them by numbers this
machine produced. Whether 96 Iris Xe execution units beat a particular
processor on a small embed model is not knowable from a specification sheet -
§0 says so - and this is where the answer lives once something has measured it.

**Stored beside the profile and keyed by its fingerprint.** Rates measured on a
laptop are wrong for the desktop the same index folder is later opened on, and
silently using them would be worse than having none: a wrong number is acted on
with confidence, an absent one is not acted on at all. The fingerprint check is
the whole safety property, and `for_profile` is the only way in.

**A rate that is stale is not the same as a rate that is absent.** `Measured`
carries when it was taken and under which pipeline version, so §5d's re-tune
triggers - a machine that aged, an app update, a new antivirus - can notice
without anything else having to remember.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from app.core.logging import logger

__all__ = [
    "Measured",
    "STATE_KEY",
    "PIPELINE_VERSION",
    "for_profile",
    "remember",
    "drifted",
]

_log = logger.bind(component="core.measured")

STATE_KEY = "compute:measured"

#: Bumped when a pipeline change would make old rates misleading.
#:
#: **A version rather than a date.** "Measured three weeks ago" says nothing
#: about whether the code that produced the number still behaves that way;
#: "measured under pipeline 1" against a running pipeline 2 says exactly that,
#: and §5d's trigger (b) is that comparison.
PIPELINE_VERSION = 1

#: How far a fresh measurement may differ from the stored one before the stored
#: one is treated as wrong. 30% per §5d - wide enough that ordinary background
#: load does not trip it, narrow enough to catch a machine that has genuinely
#: changed.
DRIFT = 0.30

#: Consecutive drifting runs before a re-tune. One deviant run is a busy
#: afternoon; three in a row is a machine that has changed.
DRIFT_RUNS = 3

#: The most chunks a second the embedding model could plausibly manage on any
#: machine this runs on - **a ceiling on believing a measurement, not a target**.
#:
#: 2026-09-19: the stored rate on the owner's machine was 106,666,662 a second.
#: `index_bench` timed a generator it never iterated, so the "measurement" was
#: how long it takes to create one, and `embed_batch_from_rates` read anything
#: over 20 as "fast enough to double the batch". A number that cannot be true
#: is not a number, and it must be refused where it is *read*, because one is
#: already sitting in a stored record.
MAX_PLAUSIBLE_EMBED_PER_SECOND = 5_000.0


def plausible_embed_rates(rates: Any) -> dict[str, float]:
    """The entries of `rates` that could have come from a real measurement.

    Drops anything non-numeric, non-finite, zero, negative or above
    `MAX_PLAUSIBLE_EMBED_PER_SECOND`. Never raises: what it cannot read it
    leaves out, and leaving a rate out only makes the caller use its
    heuristic - which is what Defaults does.
    """
    kept: dict[str, float] = {}
    if not isinstance(rates, dict):
        return kept
    for name, value in rates.items():
        try:
            rate = float(value)
        except (TypeError, ValueError):
            continue
        if rate != rate or rate <= 0 or rate > MAX_PLAUSIBLE_EMBED_PER_SECOND:
            _log.warning("ignoring an implausible {} embedding rate of {:,.0f} "
                         "a second", name, rate if rate == rate else 0.0)
            continue
        kept[str(name)] = rate
    return kept


@dataclass
class Measured:
    """Rates this machine produced, and enough context to distrust them later."""

    #: The `ComputeProfile.fingerprint()` these were taken on. **Never rates
    #: without one**: unlabelled numbers get used on the wrong machine.
    fingerprint: str = ""
    pipeline_version: int = PIPELINE_VERSION
    taken_at: float = field(default_factory=time.time)
    #: What produced them: `bench` (the synthetic workload) or `run` (a real
    #: index run). Kept because they are not equally trustworthy - a bench is
    #: controlled, a run is whatever corpus somebody happened to index.
    source: str = "bench"

    #: Chunks a second through the model, per device (`cpu`, `gpu`).
    embed_per_second: dict[str, float] = field(default_factory=dict)
    #: Files a second through extraction, per worker.
    extract_per_second: float = 0.0
    #: Rows a second into SQLite.
    write_per_second: float = 0.0
    #: Where a real run's time went - `stages.seconds()`. Absent for a bench.
    stages: dict[str, float] = field(default_factory=dict)
    #: How many consecutive runs have disagreed with these by more than DRIFT.
    drifting_runs: int = 0

    # -- the settings these justify -----------------------------------------

    def suggests(self) -> dict[str, int]:
        r"""`{registry key: value}` these rates argue for - **or nothing**.

        Empty is the common and correct answer. A measurement that does not
        clearly point at a change should produce no change: §5c's adjustments
        apply themselves while the mode is Auto, so a rule that fires on noise
        would move somebody's settings for no reason and they would never know
        why their machine got slower.

        Only the one rule the evidence genuinely supports is here. More will
        arrive when there are numbers to justify them, and not before - which
        is the same discipline §6 applies to the speed work itself.
        """
        total = sum(self.stages.values())
        if total <= 0:
            return {}

        waiting = self.stages.get("waiting", 0.0) / total
        # **Half the run spent waiting for files means the readers are the
        # bottleneck**, and that is the one inference this project has the
        # evidence to make: the consumer was idle, so more producers would
        # have filled it. The envelope still clamps the result, so this can
        # never propose more workers than the machine has.
        if waiting >= 0.5:
            return {"INDEX_WORKERS": 0}          # 0 = let the envelope decide
        return {}

    # -- serialisation ------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Plain data for `index_state`; the inverse of `from_dict`."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> Optional["Measured"]:
        """Rebuild from `as_dict`, or None if it is not one. Never raises.

        Unknown keys are dropped (an older or newer build wrote them) and the
        embedding rates go through `plausible_embed_rates`, so a stored number
        that cannot be true is refused where it is read.
        """
        if not isinstance(payload, dict):
            return None
        try:
            fields = {key: value for key, value in payload.items()
                      if key in cls.__dataclass_fields__}
            if "embed_per_second" in fields:
                fields["embed_per_second"] = plausible_embed_rates(
                    fields["embed_per_second"])
            return cls(**fields)
        except Exception:                        # noqa: BLE001 - a cache
            return None

    def stale_against(self, profile: Any) -> str:
        """Why these rates should not be used, or `""` when they should be.

        Four reasons, and they are §5d's four triggers. Each returns a sentence
        rather than a boolean because the tuning screen says which one applies -
        "this is a different computer" and "the app was updated" send somebody
        to different conclusions about whether anything is wrong.
        """
        if not self.fingerprint:
            return "these rates were recorded without saying which machine on"
        wanted = getattr(profile, "fingerprint", None)
        if callable(wanted) and self.fingerprint != wanted():
            return "this is not the machine they were measured on"
        if self.pipeline_version != PIPELINE_VERSION:
            return "the app has been updated since they were measured"
        if self.drifting_runs >= DRIFT_RUNS:
            return (f"the last {self.drifting_runs} runs disagreed with them, "
                    f"so something about this machine has changed")
        return ""


def for_profile(store: Any, profile: Any) -> Optional[Measured]:
    """The stored rates, **only if they belong to this machine**.

    `None` for every reason they might not: nothing stored, unreadable, a
    different machine, an older pipeline, or three runs of drift. The caller
    then uses the heuristics, which is what Defaults does - so the failure mode
    of this whole feature is "no better than before", never "wrong".
    """
    try:
        stored = Measured.from_dict(
            json.loads(store.get_state(STATE_KEY, "") or "null"))
    except Exception as exc:                     # noqa: BLE001 - a cache
        _log.debug("the measured rates could not be read: {}", exc)
        return None
    if stored is None:
        return None

    why = stored.stale_against(profile)
    if why:
        _log.info("ignoring the stored rates: {}", why)
        return None
    return stored


def remember(store: Any, measured: Measured) -> bool:
    """Store rates. Returns whether it worked; never raises.

    A cache that cannot be written is not a reason to fail a benchmark or an
    index run - it is a reason to do the same work again next time.
    """
    try:
        store.set_state(STATE_KEY, json.dumps(measured.as_dict()))
        return True
    except Exception as exc:                     # noqa: BLE001
        _log.debug("could not store the measured rates: {}", exc)
        return False


def drifted(stored: Optional[Measured], fresh: Optional[float],
            key: str = "extract_per_second") -> bool:
    """Has this run disagreed with the stored rate by more than `DRIFT`?

    **Compared as a ratio, not a difference**, because the interesting change
    is proportional: 2 files a second becoming 1 is the same event as 200
    becoming 100, and an absolute threshold would fire on one and never on the
    other.
    """
    if stored is None or not fresh or fresh <= 0:
        return False
    was = float(getattr(stored, key, 0.0) or 0.0)
    if was <= 0:
        return False
    return abs(fresh - was) / was > DRIFT
