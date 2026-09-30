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
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from app.core.logging import logger
from app.core.osbridge.priority import lower_process_priority

__all__ = [
    "ResourceLimits",
    "Snapshot",
    "Verdict",
    "ResourceGovernor",
    "verdict",
    "default_workers",
    "psutil_available",
    "busiest_processes",
    "MANUAL_PAUSE_REASON",
]

log = logger.bind(component="index.resources")

#: Least seconds between two walks of the process table for this process's
#: children (`SystemProbe._children_cpu_percent`, which has the measurements).
#: A constant (non-negotiable 11): the walk is the probe's whole cost, a run
#: asks four times a second, and nothing a person could notice depends on a
#: new child being counted within a quarter of a second rather than one.
CHILD_LIST_S = 1.0

#: Consecutive memory polls with no meaningful drop before the level being
#: waited for is accepted as resident rather than transient.
SETTLE_POLLS = 3

#: How far RSS must fall for a pause to count as having achieved something.
#: Below this is measurement noise, not memory being released.
SETTLE_DROP_MB = 50.0

#: Most times per run the memory floor may be raised. Bounds the damage if the
#: growth really is a leak: it still trips, just later and loudly.
MAX_SETTLES = 3

#: 2026-09-20. Why the *person's* pause runs through this module at all.
#:
#: Everything above pauses the run **for the machine** - memory, battery, other
#: people's CPU - and it already owns the only place a run waits without ending
#: (`wait_while_throttled`), the only live "we are stopped, here is why" pair
#: (`paused`/`pause_reason`), and the accounting the summaries print. A second
#: waiting mechanism beside it would mean two flags the UI has to merge, two
#: places a stop has to be noticed, and two answers to "why is nothing
#: happening". So a manual pause is one more reason this one wait can be
#: waiting - with its own `cause`, so the reason shown is always the true one:
#: the person's while they hold it, and the machine's the moment they let go
#: and the machine is still busy.
MANUAL_PAUSE_REASON = (
    "Paused at your request. Nothing is lost - press Resume to carry on."
)


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
    memory_mb: int = 4000

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
    #: 2026-09-08: includes the child processes (converters) - see
    #: `SystemProbe._children_cpu_percent`.
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
    #: Which limit produced this, for callers that must treat one differently.
    #: Matching on `reason` text would break the moment the wording changed,
    #: and the wording is meant to be free to change.
    cause: str = ""      # "memory" | "disk" | "battery" | "cpu" | ""

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
        ), cause="disk")

    growth = snapshot.growth_mb
    if growth is not None and growth > limits.memory_mb:
        return Verdict("pause", (
            f"Indexing has added {growth:,.0f}MB "
            f"(now {snapshot.rss_mb:,.0f}MB), above the {limits.memory_mb:,}MB "
            "it is allowed to add. Pausing to let memory settle."
        ), cause="memory")

    if limits.pause_on_battery and snapshot.on_battery:
        return Verdict("pause", "On battery. Indexing resumes on mains power.",
                       cause="battery")

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
        ), cause="cpu")

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
        #: Public, because the governor raises it when a memory pause proves
        #: that the level being waited for is resident rather than transient.
        #: See `ResourceGovernor._accept_resident`.
        self.baseline_mb = baseline_mb
        #: A path, or a callable returning one. Callable because the pipeline
        #: builds its governor before the vector store is necessarily usable,
        #: and a probe that fails at construction takes the whole run with it.
        self._index_path = index_path
        self._process = None
        self._warned = False
        #: This process's children, **kept from one read to the next**, keyed
        #: by pid and creation time. See `_children_cpu_percent` for why they
        #: must be kept and not asked for afresh.
        self._children: dict = {}
        #: `time.monotonic()` before which the process table is not walked
        #: again for the list of children (`CHILD_LIST_S`).
        self._children_listed_until = 0.0

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
                # 2026-09-08: the converters are our load too. LibreOffice,
                # the DWG converters and the RTF converter run as child
                # processes, and their CPU was landing on the "other programs"
                # side of the subtraction - so the governor paused the indexer
                # because of the indexer's own converter, waited, resumed,
                # spawned the next one and paused again: ~25 pause/resume
                # cycles in 22 minutes on a 12-core machine. A child's first
                # reading is 0.0 by psutil's contract (nothing to compare
                # against yet); it is accurate from the second reading, which
                # is soon enough for a converter that runs for seconds.
                #
                # 2026-09-30: "from the second reading" needs the *same object*
                # to be read twice, and it never was - so none of this had
                # ever subtracted anything. See `_children_cpu_percent`.
                own += self._children_cpu_percent(psutil) / cores
        except Exception:                       # noqa: BLE001
            cpu = own = None
        try:
            state = psutil.sensors_battery()
            # **`power_plugged` can be None: "cannot tell".** psutil says so,
            # and a Windows virtual machine reports it that way. `not None` is
            # True, so "cannot tell" read as "on battery" and the governor
            # paused the run for ever - heartbeats still flowing, nothing
            # indexed (2026-09-29, the first Windows CI run). Unknown is unknown.
            plugged = None if state is None else state.power_plugged
            battery = None if plugged is None else not plugged
        except Exception:                       # noqa: BLE001
            battery = None

        # The first reading becomes the baseline: whatever the process already
        # weighed before indexing began is not indexing's fault.
        if self.baseline_mb is None and rss is not None:
            self.baseline_mb = rss

        return Snapshot(
            rss_mb=rss, system_cpu_percent=cpu, own_cpu_percent=own,
            free_disk_gb=free_gb, on_battery=battery,
            baseline_mb=self.baseline_mb, at=time.monotonic(),
        )

    def _children_cpu_percent(self, psutil) -> float:
        """Per-core-summed CPU of every child process, or 0.0. Never raises.

        Children exit between being listed and being read - a converter
        finishing is the ordinary case, not an error - so each one is read
        under its own guard and a vanished child simply contributes nothing.

        **The `Process` objects are kept from one read to the next
        (2026-09-30).** They were not, and so this never subtracted anything.
        `psutil`'s `children()` builds a *new* `Process` for every child on
        every call, and `cpu_percent(interval=None)` answers 0.0 the first
        time it is asked of an object - it has nothing to compare against
        yet. Asked once and thrown away, every child read 0.0 for ever.
        Tested for real on the owner's laptop (12 logical processors): with a
        child burning one core, this probe reported Leasha's own use as
        0.07-0.21% of the machine while the child was truly using 7.2-7.7% of
        it. So a converter, a reader process or a transcription counted as
        "other programs", and the governor paused Leasha for being busy with
        its own work - the very thing the 2026-09-08 change was for. Now each
        child is kept under its pid **and creation time** (Windows reuses
        pids), asked again on the next read, and dropped when it has gone. A
        child's first reading is still 0.0; it is right from the second.

        **What it costs, and why the list is not fetched every time.** The
        cost is the walk of the process table that lists the children, not
        the children. Measured on the owner's laptop on 2026-09-30, with 550
        to 570 processes on the machine: the walk alone 39 ms (28 to 52 over
        30 walks), a whole `read()` that walks 44 ms (31 to 61), and a
        `read()` that does not walk 0.8 to 0.9 ms - everything in `read()`
        but the walk is under a millisecond. (2026-09-20, 597 processes:
        25-35 ms a read, which agrees.) Those figures were taken with other
        work running; with every core busy the same calls were seen waiting
        hundreds of milliseconds for a turn, which is the machine and not
        the probe, so no figure from those runs is quoted here.

        That cost was argued here to be "about 1.5% of a 2s poll", and it
        was, for the governor's own two-second poll. But since 2026-09-30 a
        run asks the governor every `pipeline.SCAN_GOVERNOR_S` = 0.25 s while
        it scans and while it reads (`Pipeline._governor_allows`): four walks
        a second, 120 to 180 ms of every second on the thread that asks -
        about 12 to 18% of it. So the list is fetched at most once every
        `CHILD_LIST_S`, and between fetches the kept children are asked
        directly: one walk and three sub-millisecond reads a second, about
        35 to 50 ms, 3.5 to 5%.

        What that changes in the answer: a child is first noticed up to
        `CHILD_LIST_S` after it starts, where it used to be listed on the next
        read. Its first reading is 0.0 either way, and the governor pauses for
        CPU only after `busy_seconds` (5 s) of it, so one more second before a
        new converter is counted cannot cause or prevent a pause by itself.

        `test_a_real_busy_child_is_counted_as_our_own_load` holds the
        subtraction against a real child; the tests around it hold the
        keeping, the dropping and the once-a-second walk with a psutil-shaped
        fake.
        """
        kept = self._children
        now = time.monotonic()
        if now >= self._children_listed_until:
            self._children_listed_until = now + CHILD_LIST_S
            try:
                listed = self._process.children(recursive=True)
            except Exception:                   # noqa: BLE001 - keep what we have
                listed = None
            if listed is not None:
                fresh: dict = {}
                for child in listed:
                    try:
                        created = getattr(child, "create_time", None)
                        key = (child.pid, created() if callable(created) else None)
                    except Exception:           # noqa: BLE001 - gone already
                        continue
                    # The object that has been read before, where there is one.
                    fresh[key] = kept.get(key, child)
                kept = self._children = fresh

        total = 0.0
        for key, child in list(kept.items()):
            try:
                total += child.cpu_percent(interval=None)
            except Exception:                   # noqa: BLE001 - NoSuchProcess, AccessDenied
                kept.pop(key, None)
        return total

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
            # The per-system part (Windows' priority class and low disk
            # priority, or the Unix "nice" number) now lives with the other
            # operating-system code in `app.core.osbridge` (work order 0x §1b).
            # Same calls in the same order; a failure still lands here.
            lower_process_priority(psutil)
            return True
        except Exception as exc:                # noqa: BLE001
            log.debug("could not lower process priority: {}", exc)
            return False

    def lower_io_priority(self) -> bool:
        """Background I/O priority for the process, and nothing else.

        The half of `lower_priority` a window that shares its process with the
        run can still use. CPU priority is set per thread there - see
        `app.core.priority` - because lowering the process lowers the interface
        with it; disk priority has no per-thread form in `psutil`, and the
        courtesy to the person's other programs is the same as it always was.
        """
        psutil = self._psutil()
        if psutil is None or not hasattr(psutil, "IOPRIO_LOW"):
            return False
        try:
            psutil.Process().ionice(psutil.IOPRIO_LOW)
            return True
        except Exception as exc:                # noqa: BLE001 - not on every Windows build
            log.debug("could not lower I/O priority: {}", exc)
            return False


#: Seconds between two "busiest right now" samples. A governor flapping every
#: few seconds must not spend half a second sampling on every flap.
BUSIEST_MIN_INTERVAL = 30.0

#: How long the sample waits between priming and reading, in seconds.
BUSIEST_SAMPLE_SECONDS = 0.5


def busiest_processes(
    *,
    count: int = 3,
    psutil_module=None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[tuple[str, float]]:
    """The `count` processes using the most CPU right now, as `(name, percent)`.

    2026-09-08. **The "plant logging" move.** A run paused ~25 times in 22
    minutes on "the machine is busy (81-95% CPU used by other programs)" and
    nothing in the log said *which* programs - so every diagnosis was a guess
    between antivirus, Ollama, Windows Search and OneDrive. This puts the
    answer next to the pause.

    Percentages are divided by the core count so they are in the same units as
    the pause message. Our own process and its children are left out - they
    are already counted as our load - and if one still appears (a child that
    started between the two listings) it is labelled `(Leasha)`.

    Costs about half a second: `cpu_percent` needs two readings to say
    anything. This runs on the consumer thread at a pause boundary - we are
    pausing anyway, so half a second is fine. Never raises; a failure returns
    an empty list and the caller logs nothing extra.
    """
    try:
        psutil = psutil_module
        if psutil is None:
            import psutil  # noqa: PLC0415
        cores = psutil.cpu_count() or 1
        me = psutil.Process()
        ours = {me.pid}
        try:
            ours.update(child.pid for child in me.children(recursive=True))
        except Exception:                       # noqa: BLE001
            pass

        primed = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.pid in ours:
                    continue
                proc.cpu_percent(interval=None)     # prime: the first reading is 0.0
                primed.append(proc)
            except Exception:                   # noqa: BLE001 - gone, or not ours to read
                continue

        sleep(BUSIEST_SAMPLE_SECONDS)

        readings: list[tuple[str, float]] = []
        for proc in primed:
            try:
                percent = proc.cpu_percent(interval=None) / cores
                name = str(proc.info.get("name") or proc.pid)
                if proc.pid in ours:
                    name = f"{name} (Leasha)"
                readings.append((name, percent))
            except Exception:                   # noqa: BLE001
                continue
        readings.sort(key=lambda item: item[1], reverse=True)
        return readings[:count]
    except Exception:                           # noqa: BLE001 - diagnostics never break a run
        return []


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
        busiest: Optional[Callable[[], list[tuple[str, float]]]] = None,
        manual_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.limits = limits or ResourceLimits()
        self._probe = probe or SystemProbe().read
        self._sleep = sleep
        self._on_state_change = on_state_change
        #: Names the programs behind a CPU pause. Injectable for the same
        #: reason `probe` is: the real one reads the process table.
        self._busiest = busiest or (lambda: busiest_processes(sleep=self._sleep))
        self._last_busiest_at: Optional[float] = None
        self._busy_since: Optional[float] = None
        self._last_action = "run"
        #: The most recent RSS reading, so a pause can tell whether waiting is
        #: achieving anything. See `wait_while_throttled`.
        self._last_rss: Optional[float] = None
        self._settles = 0
        self.paused_seconds = 0.0
        self.pauses = 0
        #: Whether the run is waiting **right now**, and why.
        #:
        #: `paused_seconds` is a total and answers a different question. Only
        #: this one can tell a window "we are stopped, here is the reason" -
        #: without it a pause is a frozen progress bar with no explanation.
        self.paused = False
        self.pause_reason = ""
        #: The person's own pause - see `MANUAL_PAUSE_REASON`. An `Event`
        #: because it is set from the interface thread and read by the walker,
        #: every extraction worker and the consumer.
        self._manual = threading.Event()
        #: The same answer from outside this process - the command line's
        #: pause file. Polled here rather than by each waiter, so one wait
        #: path notices it. Never raises; an unreadable source means "not
        #: paused", because a pause nobody asked for is worse than none.
        self._manual_check = manual_check
        #: Of `paused_seconds`, how much the person asked for. Kept apart so
        #: "waited 40 minutes to stay out of the way" stays true.
        self.manual_paused_seconds = 0.0

    # -- the person's pause -------------------------------------------------

    def pause_manually(self) -> None:
        """Hold the run where it is. Not a stop: nothing is settled or ended."""
        self._manual.set()

    def resume(self) -> None:
        """Let go of the person's pause. The machine's own may still hold."""
        self._manual.clear()

    def note_manual_pause(self, seconds: float) -> None:
        """Record one pause the person held, and how long it lasted.

        **Measured by the caller, not by this loop.** A manual pause is held
        at several places at once - the walker, every extraction worker, the
        consumer - and each of them adding its own wait would report a
        two-minute pause as eight. So the run's one spine (the consumer, which
        is also the only thread that commits anything) times it, and says so
        here once.
        """
        if seconds <= 0:
            return
        self.paused_seconds += seconds
        self.manual_paused_seconds += seconds
        self.pauses += 1

    @property
    def manually_paused(self) -> bool:
        if self._manual.is_set():
            return True
        if self._manual_check is None:
            return False
        try:
            return bool(self._manual_check())
        except Exception:                       # noqa: BLE001 - see `_manual_check`
            return False

    def check(self, now: Optional[float] = None) -> Verdict:
        """One decision, with the busy-timer maintained across calls."""
        # **First, and without reading the machine.** The person's pause is not
        # a measurement and does not need one; probing while held would cost
        # 25-35ms of process-table walk every poll for an answer nothing uses.
        # It also means the *reason* shown while they hold it is theirs, and
        # the machine's own reason reappears by itself on the next check after
        # Resume - which is the true one at that moment.
        if self.manually_paused:
            found = Verdict("pause", MANUAL_PAUSE_REASON, cause="manual")
            if found.action != self._last_action:
                self._last_action = found.action
                # **Not counted here.** However many threads ask, one pause
                # happened; `note_manual_pause` counts it once, from the one
                # thread that waited the whole of it out.
                log.info("resource governor: paused at the person's request")
                if self._on_state_change is not None:
                    self._on_state_change(found)
            return found

        snapshot = self._probe()
        self._last_rss = snapshot.rss_mb
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
            if found.action != "run" and found.cause == "cpu":
                self._log_busiest(moment)
            if self._on_state_change is not None:
                self._on_state_change(found)
        return found

    def _log_busiest(self, moment: float) -> None:
        """Say who the machine is busy with, on the way into a CPU pause.

        2026-09-08. Once per pause transition (this is only reached from the
        state-change branch) and never twice within `BUSIEST_MIN_INTERVAL`, so
        a flapping governor cannot spend half a second per flap. Never raises.
        """
        if (
            self._last_busiest_at is not None
            and (moment - self._last_busiest_at) < BUSIEST_MIN_INTERVAL
        ):
            return
        self._last_busiest_at = moment
        try:
            top = self._busiest()
        except Exception:                       # noqa: BLE001 - diagnostics only
            return
        if not top:
            return
        log.info("resource governor: busiest right now - {}",
                 ", ".join(f"{name} {percent:.0f}%" for name, percent in top))

    def wait_while_throttled(self, should_stop: Callable[[], bool] = lambda: False) -> Verdict:
        """Block until it is reasonable to continue, or until told to stop.

        Returns the verdict that ended the wait: `run` to carry on, `stop` to
        end the run. `should_stop` is checked every poll so a user pressing
        Stop is never left waiting out a battery pause.

        **A memory pause that frees nothing is waiting for something that will
        never happen.** The ceiling is growth above a baseline taken on the
        first probe - seconds into the run, before the embedding model, the
        reranker and the OCR engine have loaded. Those are ~1GB that is never
        released, so growth sits permanently over the ceiling and the run
        oscillates: pause, resume, pause, for as long as it is left alone.
        Observed on a real run doing this for ten minutes.

        So a pause that does not move RSS is treated as evidence that the
        memory is *resident*, not transient, and the floor is raised to accept
        it. That is not the ceiling being ignored: indexing may still only add
        `memory_mb` above the new floor, and a genuine runaway keeps growing
        and trips again. `MAX_SETTLES` bounds how far this can go.
        """
        settle_from: Optional[float] = None
        ineffective = 0

        while True:
            found = self.check()
            if found.action == "run" or found.action == "stop":
                # **Cleared on every exit, including the stop path.** A flag
                # left set makes the window say "paused" for ever after one
                # pause, which is worse than never saying it.
                self.paused = False
                self.pause_reason = ""
                return found
            if should_stop():
                self.paused = False
                self.pause_reason = ""
                return Verdict("stop", "Stopped at your request.")

            # **Live, not cumulative.** `paused_seconds` says how long the run
            # has spent waiting in total; nothing said whether it is waiting
            # *now*. So a governor pause froze the progress bar and its text
            # with no explanation - six minutes of a real run looking
            # indistinguishable from a hang, which is what "the progress bar was
            # not working" meant.
            self.paused = True
            self.pause_reason = found.reason or "Waiting for resources."

            if found.cause == "memory":
                rss = self._last_rss
                if rss is None:
                    pass
                elif settle_from is None or abs(rss - settle_from) > SETTLE_DROP_MB:
                    # First look, or memory *moved*. Falling means the pause is
                    # working. **Rising means this is a leak, not residency** -
                    # and settling on it would hand a runaway another ceiling's
                    # worth of headroom every few seconds. Either way, reset and
                    # keep waiting: only memory that sits still is resident.
                    settle_from, ineffective = rss, 0
                else:
                    ineffective += 1
                    if ineffective >= SETTLE_POLLS and self._accept_resident(rss):
                        settle_from, ineffective = None, 0
                        continue
            else:
                settle_from, ineffective = None, 0

            self.pauses = self.pauses            # state kept for reporting
            if found.cause != "manual":
                # The person's pause is timed once, by whoever waits the whole
                # of it out - see `note_manual_pause`. Counting it here too
                # would add this thread's share of the same wait a second time.
                self.paused_seconds += self.limits.poll_seconds
            self._sleep(self.limits.poll_seconds)

    def _accept_resident(self, rss: float) -> bool:
        """Raise the floor to `rss`. False once the run has done this enough.

        Logged at WARNING, once per adjustment, because a ceiling quietly
        moving itself is exactly the kind of thing that should never happen
        silently - and because the message names the real fix, which is to set
        the ceiling above what the models cost on this machine.
        """
        if self._settles >= MAX_SETTLES:
            return False
        # The baseline lives on the probe, which is what reads RSS. Reached the
        # same way `apply_priority` reaches it rather than by threading a
        # second reference through every caller.
        probe = getattr(self._probe, "__self__", None)
        if not isinstance(probe, SystemProbe):
            return False                # an injected fake: nothing to adjust
        self._settles += 1
        previous = probe.baseline_mb or 0.0
        probe.baseline_mb = rss
        log.warning(
            "Pausing freed nothing, so {:,.0f}MB is resident rather than "
            "indexing - most likely the embedding, rerank and OCR models, "
            "which load after the run starts and are never released. Carrying "
            "on with the ceiling measured from {:,.0f}MB instead of {:,.0f}MB "
            "({} of {}). Set a higher memory ceiling to stop this being needed.",
            rss - previous, rss, previous, self._settles, MAX_SETTLES,
        )
        return True

    def apply_priority(self) -> bool:
        if not self.limits.low_priority:
            return False
        probe = getattr(self._probe, "__self__", None)
        if isinstance(probe, SystemProbe):
            return probe.lower_priority()
        return SystemProbe().lower_priority()

    def apply_io_priority(self) -> bool:
        """`apply_priority` for a run that lowers its own threads instead."""
        if not self.limits.low_priority:
            return False
        probe = getattr(self._probe, "__self__", None)
        if isinstance(probe, SystemProbe):
            return probe.lower_io_priority()
        return SystemProbe().lower_io_priority()

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
            "manual_paused_seconds": round(self.manual_paused_seconds, 1),
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
