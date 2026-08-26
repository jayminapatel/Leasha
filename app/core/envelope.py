r"""What each tunable is allowed to be **on this machine**.

Layer: L0 — one pure function per knob, taking a `ComputeProfile` and returning
`(floor, ceiling, auto, why)`.

**Two kinds of bound, and they are not the same thing.** The registry's static
`minimum`/`maximum` are the absolute limits - values outside them are wrong on
any machine, and they stay declarative and import-free. The envelope narrows
them *per machine*: eight index workers is legal in the abstract and absurd on
a dual-core laptop. So the registry keeps the hard bounds and this file keeps
the local ones, and nothing here can widen what the registry set.

**Cores are weighted for inference and counted for everything else**, and the
difference is not a detail. The owner's 13th-gen i7 reports ten cores: two
P-cores with hyper-threading and eight E-cores. An ONNX thread on an E-core
delivers a fraction of a P-core's throughput, so `onnx_threads` divides
`capacity()` rather than a flat count - which is the crudeness §0 identifies.

An *extraction* worker is different work: it opens a file, decodes it and hands
text on, waiting on the disk as much as it computes, and an E-core does that at
very nearly a P-core's rate. Weighting it produced two workers on the owner's
machine against the four in use today - a silent halving of his throughput,
justified by an estimate borrowed from the wrong kind of work. So
`index_workers` counts cores, and this paragraph exists because the first
version of this file did not make that distinction.

**Every bound carries its reason.** `why` is shown beside the control and
printed by `doctor`, because a ceiling somebody cannot account for is a ceiling
they route around by editing a file.

Pure: no store, no settings, no I/O. Testable entirely with invented profiles,
which is the point - the machines that matter are not the one running the test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "Bounds",
    "AUTO",
    "capacity",
    "index_workers",
    "onnx_threads",
    "embed_batch",
    "index_memory_mb",
    "oversubscription_warning",
    "for_setting",
    "ENVELOPES",
]

#: The sentinel a setting holds when it has not been decided by hand.
#:
#: **Intent, not a number.** Storing the derived value would freeze this
#: machine's answer into the index, and the same index opened on a different
#: box would run on the old one's arithmetic - which is the whole failure §3b
#: exists to prevent.
AUTO = "auto"

#: How much of a P-core's work one E-core thread does.
#:
#: **0.4, and it is an estimate flagged as one.** Published single-thread
#: comparisons for Alder-Lake-and-later E-cores against their P-core siblings
#: land between roughly a third and a half on integer-heavy work; ONNX
#: inference is in that family. It is used only to *weight a core count*, so
#: being wrong by a tenth moves a worker count by less than one - and 5a's
#: measured auto-tune replaces the estimate with the machine's own number the
#: moment it has run.
E_CORE_WEIGHT = 0.4


@dataclass(frozen=True)
class Bounds:
    """What a knob may be here, what it would be left alone, and why."""

    floor: int
    ceiling: int
    auto: int
    why: str

    def clamp(self, value: Any) -> tuple[int, Optional[str]]:
        """`(value, notice)`. **A clamp is never silent.**

        The notice names the old value, the new one and the reason, because
        "your setting was changed" without those three is a message that
        teaches nobody anything - and this fires exactly when somebody has
        carried an index to a smaller machine and is wondering why.
        """
        try:
            wanted = int(value)
        except (TypeError, ValueError):
            return self.auto, None
        if wanted < self.floor:
            return self.floor, (f"{wanted} raised to {self.floor}: {self.why}")
        if wanted > self.ceiling:
            return self.ceiling, (f"{wanted} lowered to {self.ceiling}: {self.why}")
        return wanted, None

    def resolve(self, value: Any) -> tuple[int, Optional[str]]:
        """The number to use for a stored intent - `auto` or an explicit one."""
        if value is None or str(value).strip().lower() == AUTO:
            return self.auto, None
        return self.clamp(value)


def capacity(profile: Any) -> float:
    """Usable parallel capacity, in P-core-equivalents.

    A flat core count is a lie on a hybrid part. This is the number the
    formulas below divide up, and it is deliberately conservative: a machine
    that under-uses its E-cores is slower than it could be, while one that
    over-schedules its P-cores is slower than it *was*.
    """
    performance = int(getattr(profile, "performance_cores", 0) or 0)
    efficiency = int(getattr(profile, "efficiency_cores", 0) or 0)
    if performance and efficiency:
        return performance + efficiency * E_CORE_WEIGHT

    physical = int(getattr(profile, "physical_cores", 0) or 0)
    logical = int(getattr(profile, "logical_processors", 0) or 0)
    return float(physical or logical or 1)


def index_workers(profile: Any, *, hard_max: int = 32) -> Bounds:
    r"""Extraction workers.

    **Counted on physical cores, not on weighted capacity - and that is the
    correction.** The first version divided `capacity()` and produced *two*
    workers on the owner's 2P+8E laptop, against the four `default_workers`
    has been using. Halving somebody's throughput silently, on the strength of
    an E-core weighting invented for a different kind of work, is exactly the
    sort of "improvement" this order's §0 warns about.

    An extraction worker opens a file, decodes it and hands text on: it waits
    on the disk as much as it computes, and an E-core does that work at very
    nearly a P-core's rate. The weighting belongs to `onnx_threads`, where the
    work really is inference and really does scale with core class - which is
    also what §0 says the crude arithmetic got wrong.

    Half the cores, capped at four, floor one: `resources.default_workers`'
    reasoning kept, with a per-machine ceiling added. The person indexing is
    usually also using the computer, and an indexer that takes every core is
    one they turn off.
    """
    physical = int(getattr(profile, "physical_cores", 0) or 0)
    logical = int(getattr(profile, "logical_processors", 0) or 0)
    cores = physical or logical or 1

    auto = max(1, min(4, cores // 2))
    ceiling = max(1, min(hard_max, cores))
    why = (f"{cores} core(s): half of them, capped at four, leaves the machine "
           f"usable while it indexes - extraction waits on the disk as much as "
           f"it computes, so this counts cores rather than weighting them")
    return Bounds(1, ceiling, min(auto, ceiling), why)


def onnx_threads(profile: Any, workers: Optional[int] = None) -> Bounds:
    """Intra-op threads for one ONNX session.

    Sized against **what the workers have not already taken**, because the two
    numbers multiply: the oversubscription this avoids is the commonest way a
    tuning screen makes a machine slower while every control reads "faster".
    """
    # **Here the weighting is the point.** Inference threads are the work that
    # really does run at a fraction of the speed on an E-core, which is the
    # distinction §0 draws and the reason `capacity` exists at all.
    usable = capacity(profile)
    taken = float(workers if workers is not None else index_workers(profile).auto)
    spare = max(1.0, usable - taken)
    auto = max(1, int(spare))
    logical = int(getattr(profile, "logical_processors", 0) or 0) or auto
    return Bounds(
        1, max(1, logical), auto,
        f"about {spare:.1f} core(s) are left once {int(taken)} worker(s) are "
        f"running; threads beyond that contend rather than help",
    )


def embed_batch(profile: Any) -> Bounds:
    """Chunks per embedding call.

    Bounded by **memory**, not by cores: a batch is text held at once, and the
    failure it prevents is an indexer that gets itself killed on a small
    machine rather than one that runs slowly on a large one.
    """
    ram_mb = int(getattr(profile, "ram_mb", 0) or 0)
    if ram_mb >= 16_000:
        auto, ceiling = 256, 1024
    elif ram_mb >= 8_000:
        auto, ceiling = 128, 512
    else:
        auto, ceiling = 64, 256
    return Bounds(
        8, ceiling, auto,
        f"{ram_mb / 1024:.0f}GB of RAM: a batch is text held in memory all at "
        f"once, and the ceiling is what keeps a large one from ending the run"
        if ram_mb else "memory unknown, so the conservative default applies",
    )


def index_memory_mb(profile: Any) -> Bounds:
    """The governor's ceiling on the indexer's own footprint.

    Half of what is installed, floored at 512MB. Half rather than more because
    the point of the ceiling is that the machine stays usable - a governor set
    to nearly all of memory is a governor that never fires until it is too late.
    """
    ram_mb = int(getattr(profile, "ram_mb", 0) or 0)
    if not ram_mb:
        return Bounds(512, 8_000, 4_000,
                      "memory could not be detected, so the default stands")
    auto = max(512, int(ram_mb * 0.5))
    ceiling = max(auto, int(ram_mb * 0.8))
    return Bounds(
        512, ceiling, auto,
        f"half of {ram_mb / 1024:.0f}GB, so the machine stays usable while it "
        f"indexes; above {ceiling / 1024:.0f}GB the ceiling stops protecting "
        f"anything",
    )


def oversubscription_warning(profile: Any, workers: Any,
                             threads: Any) -> Optional[str]:
    """§3c's one rule worth a warning. None when there is nothing to say.

    **Warn, do not block.** A suboptimal machine configuration is the person's
    right, informed - what is not their right is being slower and not knowing.
    Illegal states are unreachable at the control; this is the legal one that
    disappoints.
    """
    logical = int(getattr(profile, "logical_processors", 0) or 0)
    try:
        total = int(workers) + int(threads)
    except (TypeError, ValueError):
        return None
    if not logical or total <= logical + 2:
        return None
    return (f"{int(workers)} workers + {int(threads)} ONNX threads "
            f"oversubscribe your {logical} threads - slower, not faster")


#: The knobs this file bounds, by the name the registry uses for them.
#:
#: Named rather than discovered, so a setting gains an envelope when somebody
#: decides what it should be here rather than by being added elsewhere.
ENVELOPES = {
    "INDEX_WORKERS": index_workers,
    "ONNX_INTRA_OP_THREADS": onnx_threads,
    "EMBED_BATCH": embed_batch,
    "INDEX_MEMORY_MB": index_memory_mb,
}


def for_setting(key: str, profile: Any) -> Optional[Bounds]:
    """The envelope for one setting, or None when it has no per-machine bound.

    None is the ordinary answer for most settings - a theme or a schedule does
    not depend on the hardware - and the caller shows the registry's static
    bounds for those.
    """
    builder = ENVELOPES.get(str(key or "").upper())
    return builder(profile) if builder is not None else None
