"""Keeping the indexer a good citizen on a machine somebody is working on.

Layer: L3

**The premise this module exists for: nobody is watching this run.** Indexing
100GB takes hours, so it happens in the background while its owner is in a
meeting, on a call, or trying to open a spreadsheet. An indexer that is merely
*fast* is a bad indexer here. One that makes Excel stutter will be turned off and
never turned back on, and then the whole application is worth nothing.

So every limit below is a **ceiling, not a target**. The job is allowed to go
slower. It is not allowed to be the reason the machine feels broken.

**Four things are governed, and they fail in different ways:**

| | Symptom when ungoverned | What happens here |
|---|---|---|
| Memory | the machine swaps, everything stops | pause, drain, resume |
| CPU | fans, stutter, dropped calls | fewer workers, low priority, yield |
| Disk space | the index or the user's own work fails to write | stop cleanly, keep what is written |
| Battery | an hour of runtime disappears | pause until mains |

**The decision is a pure function.** `verdict()` takes a snapshot of the machine
and the limits and returns what to do. Reading the machine is behind
`probe`, the same seam pattern used for COM and Qt, so every threshold, every
hysteresis boundary and every recovery path is tested with invented numbers on
a machine that is doing nothing.

**psutil is optional.** Without it, disk space and the worker cap still work -
those need only the standard library - and the memory and CPU governors report
that they are unavailable rather than pretending. `doctor.py` says which.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from app.core.logging import logger

__all__ = [
    "ResourceLimits",
    "Snapshot",
    "Verdict",
    "ResourceGovernor",
    "verdict",
    "default_workers",
    "psutil_available",
]

log = logger.bind(component="index.resources")


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """What the indexer is allowed to use. Every field is configurable.

    The defaults are chosen for **a machine somebody is using**, not for the
    fastest possible index. On a dedicated box they are all worth raising.
    """

    #: 0 means "decide from the CPU count" - see `default_workers`.
    workers: int = 0

    #: How much memory **indexing** may add, in MB - measured as growth above
    #: whatever the process was already using when the run started.
    #:
    #: **Not an absolute ceiling, and that distinction is the whole point.** The
    #: CLI starts at roughly 200MB; the GUI starts above 1.2GB before a single
    #: file is read, because Qt, the ONNX runtime and both stores are already
    #: resident. An absolute 1500MB cap therefore paused GUI indexing on its
    #: first check, every time, and never resumed - the run reported
    #: `seen: 1, indexed: 0` after 96 seconds of doing nothing.
    #:
    #: Growth is also the number that actually matters: the question is not "how
    #: big is this process" but "is indexing running away".
    memory_mb: int = 1500

    #: Pause while *system-wide* CPU is above this. Measured across all cores,
    #: so 80 still leaves the indexer plenty of room on an idle machine and
    #: gets out of the way the moment something else needs the processor.
    #: 0 disables the check.
    cpu_percent: int = 80

    #: How long the machine must be busy before pausing, and quiet before
    #: resuming. Without hysteresis the indexer oscillates on every transient
    #: spike, which costs more than it saves and looks like a hang.
    busy_seconds: float = 5.0
    quiet_seconds: float = 10.0

    #: Stop the run below this much free space on the index drive. The index is
    #: rebuildable; the user's own documents on the same drive are not, and
    #: filling a system drive breaks things far outside this application.
    min_free_gb: int = 5

    #: Pause when the machine is on battery. On a laptop in a meeting this is
    #: the difference between a flat battery and a delayed index.
    pause_on_battery: bool = True

    #: Run below normal priority. The single cheapest and most effective
    #: courtesy available: the scheduler simply prefers whatever the person is
    #: actually doing, with no throttling logic at all.
    low_priority: bool = True

    #: Seconds to sleep between checks while paused.
    poll_seconds: float = 2.0

    def resolved_workers(self) -> int:
        return self.workers if self.workers > 0 else default_workers()


def default_workers(cpu_count: Optional[int] = None) -> int:
    """Half the cores, at least one, never more than four.

    **Deliberately not `cpu_count - 1`.** That is the right answer for a batch
    job on a build server and the wrong one here: on an 8-core laptop it hands
    seven cores to a background task and leaves one for the person. Half is
    generous, and the cap matters because extraction is I/O-bound long before
    it is CPU-bound - past four workers the disk is the wall and the extra
    threads only add contention and memory.
    """
    cores = cpu_count if cpu_count is not None else (os.cpu_count() or 2)
    return max(1, min(4, cores // 2))


# ---------------------------------------------------------------------------
# What the machine looks like right now
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Snapshot:
    """One reading. `None` means "could not measure", never "zero"."""

    rss_mb: Optional[float] = None
    system_cpu_percent: Optional[float] = None
    #: This process's own share, normalised the same way as the system figure.
    #: Subtracted before the busy check - see `other_cpu_percent`.
    own_cpu_percent: Optional[float] = None
    #: What the process was using before indexing started. The ceiling applies
    #: to growth above this, not to the absolute figure.
    baseline_mb: Optional[float] = None
    free_disk_gb: Optional[float] = None
    on_battery: Optional[bool] = None
    at: float = 0.0

    @property
    def growth_mb(self) -> Optional[float]:
        """How much memory indexing has added since it started.

        With no baseline this is the absolute figure, which is the safe
        direction: an unknown baseline should not silently disable the ceiling.
        """
        if self.rss_mb is None:
            return None
        return max(0.0, self.rss_mb - (self.baseline_mb or 0.0))

    @property
    def other_cpu_percent(self) -> Optional[float]:
        """How busy the machine is *because of somebody else*.

        **The indexer must not count its own load as a reason to stop.** With
        four workers on a four-core laptop it can push system CPU past any
        sensible threshold entirely on its own, then pause, watch CPU fall,
        resume, and spike again - throttling itself to a crawl while the machine
        is in fact completely free. The governor exists to yield to *other*
        work, and this is the number that measures that.

        Its own load is already handled, and better: below-normal priority means
        the scheduler hands the CPU to anything else that wants it, without any
        arithmetic at all.
        """
        if self.system_cpu_percent is None:
            return None
        return max(0.0, self.system_cpu_percent - (self.own_cpu_percent or 0.0))


@dataclass(frozen=True, slots=True)
class Verdict:
    """What to do, and a sentence saying why - which is the whole point.

    A background job that silently slows down is indistinguishable from one that
    has hung. Every pause carries a reason the UI can show.
    """

    action: str          # "run" | "pause" | "stop"
    reason: str = ""

    @property
    def running(self) -> bool:
        return self.action == "run"


def verdict(
    snapshot: Snapshot,
    limits: ResourceLimits,
    *,
    busy_since: Optional[float] = None,
    now: Optional[float] = None,
) -> Verdict:
    """The whole decision, as a pure function of numbers.

    Order matters and is not arbitrary. Disk is checked first and is the only
    one that *stops*: running out of space is the failure that can damage
    something outside this application, and it does not resolve itself while the
    indexer keeps writing. Everything else is temporary, so everything else
    pauses.

    An unmeasurable resource never blocks the run. Refusing to index because
    psutil is missing would be a self-inflicted outage; the correct response to
    "I cannot tell" is to carry on and say so once.
    """
    now = now if now is not None else time.monotonic()

    if snapshot.free_disk_gb is not None and snapshot.free_disk_gb < limits.min_free_gb:
        return Verdict("stop", (
            f"Only {snapshot.free_disk_gb:.1f}GB free on the index drive, "
            f"below the {limits.min_free_gb}GB floor. "
            "Everything indexed so far is saved; free some space and run again."
        ))

    growth = snapshot.growth_mb
    if growth is not None and growth > limits.memory_mb:
        return Verdict("pause", (
            f"Indexing has added {growth:,.0f}MB "
            f"(now {snapshot.rss_mb:,.0f}MB), above the {limits.memory_mb:,}MB "
            "it is allowed to add. Pausing to let memory settle."
        ))

    if limits.pause_on_battery and snapshot.on_battery:
        return Verdict("pause", "On battery. Indexing resumes on mains power.")

    others = snapshot.other_cpu_percent
    if (
        limits.cpu_percent > 0
        and others is not None
        and others > limits.cpu_percent
        and busy_since is not None
        and (now - busy_since) >= limits.busy_seconds
    ):
        return Verdict("pause", (
            f"The machine is busy ({others:.0f}% CPU used by other programs). "
            "Indexing waits until it is free."
        ))

    return Verdict("run")


# ---------------------------------------------------------------------------
# Reading the machine - the only part that is not testable
# ---------------------------------------------------------------------------

def psutil_available() -> bool:
    try:
        import psutil  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


class SystemProbe:
    """The seam. Everything above works against `Snapshot`; this makes one."""

    def __init__(
        self,
        index_path: Optional[Path] | Callable[[], object] = None,
        *,
        baseline_mb: Optional[float] = None,
    ) -> None:
        self._baseline_mb = baseline_mb
        #: A path, or a callable returning one. Callable because the pipeline
        #: builds its governor before the vector store is necessarily usable,
        #: and a probe that fails at construction takes the whole run with it.
        self._index_path = index_path
        self._process = None
        self._warned = False

    @property
    def index_path(self) -> Optional[Path]:
        target = self._index_path() if callable(self._index_path) else self._index_path
        if target is None:
            return None
        try:
            return Path(str(target))
        except (TypeError, ValueError):
            return None

    def _psutil(self):
        try:
            import psutil  # noqa: PLC0415
        except ImportError:
            if not self._warned:
                self._warned = True
                log.info(
                    "psutil is not installed: the memory, CPU and battery governors "
                    "are inactive. Disk space and the worker cap still apply. "
                    "Install it with: pip install psutil"
                )
            return None
        return psutil

    def read(self) -> Snapshot:
        """Never raises. A probe that can fail is a probe nobody can rely on."""
        free_gb = None
        if self.index_path is not None:
            try:
                free_gb = shutil.disk_usage(self.index_path).free / 1_073_741_824
            except OSError:
                free_gb = None

        psutil = self._psutil()
        if psutil is None:
            return Snapshot(free_disk_gb=free_gb, at=time.monotonic())

        rss = cpu = own = battery = None
        try:
            if self._process is None:
                self._process = psutil.Process()
            rss = self._process.memory_info().rss / 1_048_576
        except Exception:                       # noqa: BLE001 - see the docstring
            rss = None
        try:
            # interval=None returns the value since the previous call, which is
            # what a polling loop wants. A blocking interval here would add its
            # own delay to every check.
            cpu = psutil.cpu_percent(interval=None)
            if self._process is not None:
                # `Process.cpu_percent` is per-core (200% means two full cores),
                # while `cpu_percent` is already averaged across them. Divide, or
                # the indexer's own load is overstated by the core count and it
                # concludes the machine is busy whenever it is working.
                cores = psutil.cpu_count() or 1
                own = self._process.cpu_percent(interval=None) / cores
        except Exception:                       # noqa: BLE001
            cpu = own = None
        try:
            state = psutil.sensors_battery()
            battery = None if state is None else not state.power_plugged
        except Exception:                       # noqa: BLE001
            battery = None

        # The first reading becomes the baseline: whatever the process already
        # weighed before indexing began is not indexing's fault.
        if self._baseline_mb is None and rss is not None:
            self._baseline_mb = rss

        return Snapshot(
            rss_mb=rss, system_cpu_percent=cpu, own_cpu_percent=own,
            free_disk_gb=free_gb, on_battery=battery,
            baseline_mb=self._baseline_mb, at=time.monotonic(),
        )

    def lower_priority(self) -> bool:
        """Drop below normal priority. Returns whether it worked.

        On Windows this also asks for background I/O priority, which matters as
        much as CPU: the indexer reads constantly, and a foreground application
        waiting behind it for the disk feels exactly like a slow computer.
        """
        psutil = self._psutil()
        if psutil is None:
            return False
        try:
            process = psutil.Process()
            if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
                process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
                try:
                    process.ionice(psutil.IOPRIO_LOW)
                except Exception:               # noqa: BLE001 - not on every Windows build
                    pass
            else:
                process.nice(10)                # POSIX
            return True
        except Exception as exc:                # noqa: BLE001
            log.debug("could not lower process priority: {}", exc)
            return False


# ---------------------------------------------------------------------------
# The governor the pipeline actually calls
# ---------------------------------------------------------------------------

class ResourceGovernor:
    """Ask before each batch: may I carry on?

    Used from the pipeline's consumer loop, which is the only place that commits
    anything - so a pause always lands on a clean boundary and never mid-write.
    """

    def __init__(
        self,
        limits: Optional[ResourceLimits] = None,
        *,
        probe: Optional[Callable[[], Snapshot]] = None,
        sleep: Callable[[float], None] = time.sleep,
        on_state_change: Optional[Callable[[Verdict], None]] = None,
    ) -> None:
        self.limits = limits or ResourceLimits()
        self._probe = probe or SystemProbe().read
        self._sleep = sleep
        self._on_state_change = on_state_change
        self._busy_since: Optional[float] = None
        self._last_action = "run"
        self.paused_seconds = 0.0
        self.pauses = 0

    def check(self, now: Optional[float] = None) -> Verdict:
        """One decision, with the busy-timer maintained across calls."""
        snapshot = self._probe()
        moment = now if now is not None else snapshot.at or time.monotonic()

        others = snapshot.other_cpu_percent
        over_cpu = (
            self.limits.cpu_percent > 0
            and others is not None
            and others > self.limits.cpu_percent
        )
        if over_cpu:
            if self._busy_since is None:
                self._busy_since = moment
        elif self._busy_since is not None and (
            moment - self._busy_since
        ) >= self.limits.quiet_seconds:
            # Hysteresis: stay pessimistic for `quiet_seconds` after the spike
            # ends. Resuming the instant CPU dips means resuming into the gap
            # between two bursts of somebody's build, over and over.
            self._busy_since = None

        found = verdict(snapshot, self.limits, busy_since=self._busy_since, now=moment)
        if found.action != self._last_action:
            self._last_action = found.action
            if found.action != "run":
                self.pauses += 1
            log.info("resource governor: {} - {}", found.action, found.reason or "clear")
            if self._on_state_change is not None:
                self._on_state_change(found)
        return found

    def wait_while_throttled(self, should_stop: Callable[[], bool] = lambda: False) -> Verdict:
        """Block until it is reasonable to continue, or until told to stop.

        Returns the verdict that ended the wait: `run` to carry on, `stop` to
        end the run. `should_stop` is checked every poll so a user pressing
        Stop is never left waiting out a battery pause.
        """
        while True:
            found = self.check()
            if found.action == "run" or found.action == "stop":
                return found
            if should_stop():
                return Verdict("stop", "Stopped at your request.")
            self.pauses = self.pauses            # state kept for reporting
            self.paused_seconds += self.limits.poll_seconds
            self._sleep(self.limits.poll_seconds)

    def apply_priority(self) -> bool:
        if not self.limits.low_priority:
            return False
        probe = getattr(self._probe, "__self__", None)
        if isinstance(probe, SystemProbe):
            return probe.lower_priority()
        return SystemProbe().lower_priority()

    def summary(self) -> dict[str, object]:
        return {
            "workers": self.limits.resolved_workers(),
            "memory_mb_cap": self.limits.memory_mb,
            "cpu_percent_cap": self.limits.cpu_percent,
            "min_free_gb": self.limits.min_free_gb,
            "pause_on_battery": self.limits.pause_on_battery,
            "low_priority": self.limits.low_priority,
            "pauses": self.pauses,
            "paused_seconds": round(self.paused_seconds, 1),
            "governor_active": psutil_available(),
        }


def limits_from_settings(settings: object) -> ResourceLimits:
    """Build limits from a `Settings`, falling back to the defaults per field.

    Reads with `getattr` so an older `.env` that predates any of these keys
    still loads. A configuration file that must be regenerated to open the app
    is not configuration, it is a migration.
    """
    base = ResourceLimits()
    return replace(
        base,
        workers=int(getattr(settings, "index_workers", base.workers) or 0),
        memory_mb=int(getattr(settings, "index_memory_mb", base.memory_mb)),
        cpu_percent=int(getattr(settings, "index_cpu_percent", base.cpu_percent)),
        min_free_gb=int(getattr(settings, "min_free_gb", base.min_free_gb)),
        pause_on_battery=bool(getattr(settings, "index_pause_on_battery", base.pause_on_battery)),
        low_priority=bool(getattr(settings, "index_low_priority", base.low_priority)),
    )
