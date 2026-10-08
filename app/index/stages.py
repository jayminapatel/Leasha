r"""Where an index run's time actually goes.

Layer: L3

**§6a of the index-tuning order, and it gates every other item in that
section.** Nothing else there may be ticked without before/after numbers from
here, because every speed idea in that list sounds plausible and at most two of
them are worth building on any given corpus. A feeder thread is wasted work on
a run that spends its time waiting for the disk; more workers are wasted on a
run that is embedding-bound. The only way to know which is to measure.

**The critical path is the consumer thread, and that is what is timed.**
Extraction runs on N workers in parallel, so adding up their seconds
double-counts: four workers busy for a minute is four worker-minutes and one
wall minute, and a percentage built from the first is meaningless. What the
consumer *waits* for is the honest measure - it is the thing that would go
faster if the stage behind it went faster, which is exactly the question a
tuning screen is asked.

So the stages are the consumer's own:

  * `waiting` - blocked on the results queue. **Extraction is the bottleneck**,
    and more readers would help.
  * `write` - SQLite: rows, chunks, FTS.
  * `embed` - the model. Large here means a bigger batch, or the graphics card.
  * `vectors` - the Lance write.
  * `walk` - the producer's own wall time, recorded separately because it
    overlaps everything and is not on the consumer's path.

`worker_seconds` is kept alongside, unweighted and clearly named, because
"extraction cost 40 worker-minutes" is a real fact worth having - just not a
percentage of anything.

Thread-safe, because the extraction workers write to it while the consumer
reads. Cheap: one `perf_counter` pair and a dictionary add per stage entry, so
timing a stage that runs a hundred thousand times costs a few hundredths of a
second in total.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

__all__ = ["StageClock", "CRITICAL_PATH", "WAITING"]

#: Blocked on the results queue: the name for "extraction has not kept up".
WAITING = "waiting"

#: The consumer's own stages, in the order they read most naturally. `walk`
#: is deliberately absent: it overlaps everything and is not on the path.
CRITICAL_PATH = (WAITING, "write", "embed", "vectors")


class StageClock:
    """Seconds per stage, accumulated from several threads.

    Deliberately **not** a general profiler. It knows a handful of stage names
    and adds seconds to them, because a profiler's answer - a thousand
    functions - is not the answer a tuning screen can act on. The question is
    "which of five things should I change", and five buckets answer it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seconds: dict[str, float] = {}
        self._worker_seconds: dict[str, float] = {}

    # -- recording ----------------------------------------------------------

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time a block on the critical path.

        `perf_counter`, not `time()`: this measures durations, and a clock the
        operating system may step backwards over an NTP correction would
        produce negative stage times in a report somebody is trying to trust.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - started)

    @contextmanager
    def worker(self, name: str) -> Iterator[None]:
        """Time a block running on one of several parallel threads.

        Kept in its own tally so it can never be mistaken for wall time. Four
        workers busy for a minute is four worker-minutes, and dividing that by
        the run's length would give 400%.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self.add_worker(name, time.perf_counter() - started)

    def add(self, name: str, seconds: float) -> None:
        """Add to a critical-path stage. Zero and negative are dropped: a clock
        that read the same tick twice must not create a stage out of nothing."""
        if seconds <= 0:
            return
        with self._lock:
            self._seconds[name] = self._seconds.get(name, 0.0) + seconds

    def add_worker(self, name: str, seconds: float) -> None:
        """Add to a parallel-thread tally (`worker`), never to the critical path."""
        if seconds <= 0:
            return
        with self._lock:
            self._worker_seconds[name] = (
                self._worker_seconds.get(name, 0.0) + seconds)

    # -- reading ------------------------------------------------------------

    def seconds(self) -> dict[str, float]:
        """The critical-path stages, rounded, largest first.

        Largest first because the one worth acting on should not need looking
        for - the same reason the tuning footer orders them that way.
        """
        with self._lock:
            found = dict(self._seconds)
        return {name: round(value, 2) for name, value in
                sorted(found.items(), key=lambda pair: -pair[1])}

    def worker_seconds(self) -> dict[str, float]:
        with self._lock:
            found = dict(self._worker_seconds)
        return {name: round(value, 2) for name, value in
                sorted(found.items(), key=lambda pair: -pair[1])}

    def total(self) -> float:
        with self._lock:
            return sum(self._seconds.values())

    def share(self, name: str) -> float:
        """This stage's fraction of the critical path, 0.0 when nothing ran.

        **The number the auto-tuner acts on.** "embed took 41 seconds" needs a
        denominator before it means anything; "embed was 8% of the run" is
        immediately actionable - it says the graphics card would buy almost
        nothing here.
        """
        total = self.total()
        if total <= 0:
            return 0.0
        with self._lock:
            return self._seconds.get(name, 0.0) / total

    def dominant(self) -> Optional[str]:
        """The stage worth changing, or None when nothing was measured."""
        found = self.seconds()
        return next(iter(found), None) if found else None

    def as_dict(self) -> dict[str, Any]:
        """What the run summary carries. Empty dicts are omitted, so a run that
        measured nothing says nothing rather than printing a row of zeroes."""
        payload: dict[str, Any] = {}
        stages = self.seconds()
        workers = self.worker_seconds()
        if stages:
            payload["stages"] = stages
        if workers:
            payload["worker_seconds"] = workers
        return payload


def advice(stages: Any, on_gpu: bool = False) -> str:
    r"""One sentence saying what this run's shape suggests changing.

    **Plain words, because the person reading it may not know what "embed"
    means.** §5c's rule: a non-technical user must never have to interpret a
    measurement to get the benefit of it. The auto-tuner acts on `share`; this
    is what it says while doing so.

    Returns `""` when nothing measured, or when no stage is clearly dominant -
    an even split is a balanced run, and inventing a recommendation for one
    would send somebody off to change a setting for no reason.
    """
    try:
        found = {str(name): float(value) for name, value in dict(stages).items()}
    except (TypeError, ValueError):
        return ""
    total = sum(found.values())
    if total <= 0:
        return ""

    name, seconds = max(found.items(), key=lambda pair: pair[1])
    share = seconds / total
    if share < 0.5:
        return ""

    if name == WAITING:
        return ("Most of this run was spent waiting for files to be read, so "
                "reading more of them at once would finish sooner.")
    if name == "embed":
        return ("Most of this run was spent working out what the text means. "
                "A graphics card, or a larger batch, is what speeds that up."
                if not on_gpu else
                "Most of this run was spent working out what the text means, "
                "on the graphics card. A larger batch is the remaining lever.")
    if name in ("write", "vectors"):
        return ("Most of this run was spent writing to disk, so a faster drive "
                "for the index is what would help - not more of the machine.")
    return ""
