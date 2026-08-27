r"""Learning from a real run, and knowing when to stop trusting what was learned.

Layer: L3 — decisions, no I/O of its own. The store is passed in.

§5c and §5d. Two rules, and the difference between them is the whole design:

**In Auto, an accepted adjustment applies itself and says so in plain words.**
The product rule is explicit - a non-technical person must never be handed a
decision in order to get the benefit. "Indexing sped up: this computer handles
6 files at once" is the whole of what they see. In Manual it is proposed with a
button instead, because somebody who has taken manual control has said they
want to make these choices.

**Only adjustments in the accepted class apply themselves**, and the class is
deliberately tiny: one rule, backed by the one inference this project has the
evidence to make. A self-tuning system that changes things on weak evidence is
worse than none, because the person cannot tell a bad automatic change from a
machine that has got slower on its own.

Re-tune triggers are §5d's four, and all of them are cheap to check: a
different machine, an app update, three runs of drift, or nothing ever
measured. Each schedules the bench - **never mid-run, never on battery, never
as a question.**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.core.logging import logger
from app.core.measured import (
    DRIFT_RUNS,
    Measured,
    drifted,
    for_profile,
    remember,
)

__all__ = ["Adjustment", "learn", "should_bench", "status_line"]

_log = logger.bind(component="index.autotune")

#: Settings an Auto-mode run may change on its own.
#:
#: **One entry, and it is a floor rather than a value.** `INDEX_WORKERS = 0`
#: means "let the envelope decide", so the automatic adjustment hands the
#: decision back to the bounded, tested function rather than proposing a number
#: of its own. Nothing here can exceed what the machine allows, because it does
#: not name a number at all.
ACCEPTED = ("INDEX_WORKERS",)


@dataclass
class Adjustment:
    """A change a run's measurements argue for, and how to say it."""

    values: dict
    #: What the person reads. Plain words: no stage names, no percentages.
    message: str
    #: True when it applied itself (Auto), False when it is waiting (Manual).
    applied: bool = False

    def __bool__(self) -> bool:
        return bool(self.values)


def learn(store: Any, profile: Any, stats: Any, *, mode: str = "defaults",
          device: str = "cpu") -> Optional[Adjustment]:
    r"""Record what this run measured, and return what it argues for.

    **Recording happens whatever the mode is**, and proposing does not. A
    person in Manual still benefits from the drift detector noticing their
    machine has changed; what they do not get is their settings moved under
    them.

    Returns None when the run says nothing worth acting on - which is the
    common case and the correct one.
    """
    stages = dict(getattr(stats, "stages", {}) or {})
    if not stages:
        return None                              # too short to have measured

    stored = for_profile(store, profile)
    fresh = _extract_rate(stats)
    is_drifting = bool(stored) and drifted(stored, fresh)

    # **A drifting run must not overwrite the baseline it drifted from**, and
    # the first version did. Storing the deviant rate moved the goalposts to
    # meet it, so the *next* run was then compared against the slow afternoon
    # rather than against the machine's real rate - the counter could never
    # settle, and a single bad run permanently redefined "normal".
    #
    # The baseline is replaced only by a run that agrees with it, or when there
    # was none. Three disagreements retire it (`stale_against`), which is the
    # proper way for a baseline to be abandoned: deliberately, after evidence.
    keep = stored.extract_per_second if (stored and is_drifting) else fresh
    updated = Measured(
        fingerprint=_fingerprint(profile),
        source="run",
        stages=stages,
        extract_per_second=keep or (stored.extract_per_second if stored else 0.0),
        write_per_second=stored.write_per_second if stored else 0.0,
        embed_per_second=dict(stored.embed_per_second) if stored else {},
        # **Drift is counted, not latched.** One slow afternoon is not a
        # changed machine; three consecutive ones are, and a counter that
        # resets on agreement is what tells them apart.
        drifting_runs=((stored.drifting_runs + 1) if is_drifting else 0),
    )
    remember(store, updated)

    if updated.drifting_runs and updated.drifting_runs < DRIFT_RUNS:
        _log.info("this run disagreed with the stored rates ({} in a row)",
                  updated.drifting_runs)

    wanted = updated.suggests()
    if not wanted:
        return None

    message = _plain_words(wanted, stages, device)
    if str(mode) == "auto" and all(key in ACCEPTED for key in wanted):
        # §5c's exception: it applies itself, and the notice is past tense.
        return Adjustment(wanted, message, applied=True)
    if str(mode) == "manual":
        return Adjustment(wanted, message, applied=False)
    # Defaults mode already resolves from the envelope every run, so there is
    # nothing to change - it is *already* whatever the envelope now says.
    return None


def should_bench(store: Any, profile: Any) -> str:
    """Why the bench should run, or `""` when it should not.

    §5d's four triggers, as one sentence each. A sentence rather than a flag
    because the tuning screen shows it: "this is a different computer" and
    "the app was updated" lead somebody to different conclusions about whether
    anything is wrong, and a single "re-tuning" would answer neither.
    """
    stored = for_profile(store, profile)
    if stored is None:
        # `for_profile` already logged which of the reasons applied; from here
        # they are the same instruction.
        return "this machine has not been timed yet"
    return ""


def status_line(store: Any, profile: Any) -> str:
    """The Index Tuning status line, in every mode.

    "Tuned for this computer · last checked 14 Aug 2026" - §5d asks for it in
    every mode, including Manual, because *when this was last checked* is a
    fact about the machine rather than about the mode.
    """
    from datetime import datetime

    stored = for_profile(store, profile)
    if stored is None:
        return "Not yet timed on this computer."
    when = datetime.fromtimestamp(stored.taken_at)
    # `%-d` is glibc-only and raises on Windows - see the date-hint fix.
    return (f"Tuned for this computer · last checked "
            f"{when.day} {when:%b %Y}")


# --- internals --------------------------------------------------------------


def _fingerprint(profile: Any) -> str:
    wanted = getattr(profile, "fingerprint", None)
    try:
        return str(wanted()) if callable(wanted) else ""
    except Exception:                            # noqa: BLE001
        return ""


def _extract_rate(stats: Any) -> float:
    """Files a second through extraction, per worker, from a real run.

    Divided by the worker count, so it is comparable with the bench's
    single-threaded number. Comparing a four-worker run against a
    single-threaded bench would report a 4x speed-up every time and trip the
    drift detector on the first run after benching.
    """
    seconds = float(getattr(stats, "elapsed_s", 0.0) or 0.0)
    indexed = float(getattr(stats, "indexed", 0) or 0)
    workers = float(getattr(stats, "workers", 0) or 0) or 1.0
    if seconds <= 0 or indexed <= 0:
        return 0.0
    return indexed / seconds / workers


def _plain_words(values: dict, stages: dict, device: str) -> str:
    """The sentence somebody actually reads.

    No stage names, no percentages, no jargon - §5c's rule. "Indexing sped up:
    this computer handles 6 files at once" tells a non-technical person what
    happened and implies it was good, which is the entire brief.
    """
    from app.index.stages import advice

    if "INDEX_WORKERS" in values:
        return ("Indexing sped up: most of the last run was spent waiting for "
                "files to be read, so more of them are read at once now.")
    said = advice(stages, on_gpu=str(device) == "gpu")
    return said or "Indexing settings were adjusted for this computer."
