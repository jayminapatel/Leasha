r"""One conductor for every model an index run uses: who may run, and when.

Layer: L3

Work order `indexing-cockpit-and-conductor` section 5 (released 2026-10-14).
The owner saw one thread busy, three idle, and thousands of photos with no
faces. A run's models - the meaning model, picture search (CLIP), faces,
Florence-2's descriptions, text in pictures (OCR) and the document cards -
each asked the processor for threads on their own, sized in isolation, and the
end of a run ran its passes one after another on one thread. This module is
the one place that decides how much of the processor a run's models may have
at once, hands it out by priority, and keeps the graphics card to one model at
a time.

## The budget (`budget_for`)

**Slots are processor threads**, the unit `onnxruntime` is asked for, so a
model that holds a slot of weight 4 is a session running four intra-op
threads. The budget is not a new formula; it is the sum of what
`app/core/envelope.py` already allows a run's models:

    cpu_slots = meaning + pictures
    meaning   = envelope.onnx_threads(profile, readers).auto   (or the run's own)
    pictures  = envelope.picture_model_threads(profile, workers=readers,
                                               meaning_threads=meaning)
              = max(1, min(meaning, logical - readers - meaning))

which is `min(2 x meaning, logical - readers)`: the logical processors less
one per reader - the share `envelope.index_workers` already keeps back so "the
machine stays usable while it indexes" (a reader is busy 6.4% of a long run,
so in practice that share is the person's) - and never more than two
inference sessions' worth. On the owner's 2P+8E laptop, 12 logical, four
readers: meaning 4, pictures 4, **8 slots**, 4 left to the readers and the
window. Nothing here can make the run ask for more than the envelope already
allowed; it decides who gets it when.

## Priority (`PRIORITY`)

meaning > reading > faces and pictures > describing > text in pictures >
cards - the order's own list. A free slot goes to the most important waiter
that fits (**work-conserving**: a slot is never left idle while anything that
fits is waiting). A waiter too big for what is free lets smaller, less
important ones past it - but only for `RESERVE_AFTER_S`; after that nothing
of lower priority is admitted on that resource until it fits, so a heavy
important model is never starved by a stream of light ones.

## The graphics card

**One slot.** `gpu_serialize.gpu_exclusive` already makes the process's
DirectML calls take turns, per call; the conductor's slot is the coarser,
priority-ordered turn above it, so a photo description is not cut into by OCR
between its four graphs. **Lock order, everywhere: the conductor's slot
first, then `gpu_exclusive`** (which a model takes inside its own call). The
conductor is never asked for while `gpu_exclusive` is held, because nothing
holds that lock outside a model call.

## Never a deadlock (the rules)

1. **No slot is held while waiting for the store's write lock.** A slot is
   taken around a model call and nothing else; a pass writes its result after
   the `with` block, never inside it. So every holder gives its slot back in
   bounded time.
2. **No thread waits for a slot while it holds the write lock.** A caller
   that holds it (`write_lock_held()` says so - in the pipeline, the store's
   per-thread transaction depth) is admitted at once, over the budget, and
   counted. Together with rule 1 the two locks can never wait on each other.
3. **A thread already holding a slot is never made to wait for another**
   (nested model calls on one thread): admitted at once. No thread ever holds
   one conductor resource while waiting for another, so the conductor cannot
   deadlock with itself.

## Stop and pause

The resource governor stays the brake: while it holds the run - the person's
pause, or the machine's (CPU, battery) - nothing new is admitted, and waiters
wait. A memory pause does not hold the conductor: the memory is the models'
own, and holding them would keep it from ever falling (`Pipeline._disk_ok`
says the same of the consumer). A stop wakes every waiter and admits it at
once, so nothing ever waits on a run that is ending; each caller then meets
its own stop check between photos, exactly as before the conductor existed.

Thread-safe; never raises into a caller for its own reasons.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from app.core.logging import logger

__all__ = [
    "Budget", "Conductor", "NULL_CONDUCTOR", "PRIORITY", "budget_for",
    "reader_ceiling", "FACE_POOL_MAX", "RESERVE_AFTER_S",
    "LANE_MEANING", "LANE_READING", "LANE_PICTURES", "LANE_FACES",
    "LANE_DESCRIBE", "LANE_PICTURE_TEXT", "LANE_CARDS",
]

_log = logger.bind(component="index.conductor")

#: Lane ids the conductor is asked for by. The same ids as the cockpit's lanes
#: (`live_progress`), so "who holds the graphics card" names a row on the page.
LANE_MEANING = "meaning"
LANE_READING = "reading"
LANE_PICTURES = "pictures"
LANE_FACES = "faces"
LANE_DESCRIBE = "describe"
LANE_PICTURE_TEXT = "picture_text"
LANE_CARDS = "cards"

#: Lower is more important. The order's list (section 5a), verbatim in effect:
#: meaning > reading > faces and pictures > describing > text in pictures >
#: cards. A lane not named here is least important of all.
PRIORITY: dict[str, int] = {
    LANE_MEANING: 0,
    LANE_READING: 1,
    LANE_FACES: 2,
    LANE_PICTURES: 2,
    LANE_DESCRIBE: 3,
    LANE_PICTURE_TEXT: 4,
    LANE_CARDS: 5,
}

#: How long a waiter too big for what is free lets less important ones past
#: it before it reserves the resource. One second: a face scan is 1.6-1.9 s of
#: model time (measured 2026-10-14), so a meaning batch waits at most about one
#: photo's worth of faces, and light work fills the gaps meanwhile. A
#: measurement of the meaning model waiting longer than that would change it.
RESERVE_AFTER_S = 1.0

#: The most photos looked at for faces at once on the processor. **Measured**,
#: 2026-10-14, the owner's laptop (2P+8E, 12 logical, below-normal priority,
#: the owner's own run active): 1 thread 0.16 photos/s, 2 -> 0.37, 4 -> 0.66.
#: Nothing past four was measured, so nothing past four is used.
FACE_POOL_MAX = 4

#: How often a waiter looks again at the pause and the stop, when nothing has
#: woken it. A fixed constant: a release or a stop wakes waiters at once, so
#: this only bounds how soon the end of a governor pause is noticed.
POLL_S = 0.05


@dataclass(frozen=True)
class Budget:
    """What a run's models may ask of the processor, and why.

    `cpu_slots` is the conductor's capacity in threads; `meaning_threads` the
    meaning model's session; `picture_threads` one picture model's session
    when it is called by one thread at a time. `face_pool` and
    `threads_for(callers)` derive from these - see the module note.
    """

    cpu_slots: int
    meaning_threads: int
    picture_threads: int
    logical: int = 0
    readers: int = 0
    why: str = ""

    def face_pool(self, faces_on_gpu: bool) -> int:
        """Photos looked at for faces side by side: one on the graphics card
        (the card is one slot, and `gpu_exclusive` would queue the rest),
        else the picture models' share, at most `FACE_POOL_MAX`."""
        if faces_on_gpu:
            return 1
        return max(1, min(FACE_POOL_MAX, self.picture_threads))

    def threads_for(self, callers: int) -> int:
        """Intra-op threads for one picture session called by `callers` threads
        at once - `envelope.picture_model_threads`' own `callers` rule: each
        extra caller is a thread already busy inside its own call."""
        callers = max(1, int(callers or 1))
        return max(1, self.picture_threads - (callers - 1))

    def weight_for(self, callers: int) -> int:
        """Slots one of `callers` concurrent callers of a picture session is
        charged, so all of them together are charged the picture share."""
        return max(1, self.picture_threads // max(1, int(callers or 1)))

    def as_dict(self) -> dict[str, Any]:
        return {"cpu_slots": self.cpu_slots, "meaning_threads": self.meaning_threads,
                "picture_threads": self.picture_threads, "logical": self.logical,
                "readers": self.readers, "why": self.why}


def budget_for(profile: Any, *, readers: int,
               meaning_threads: Optional[int] = None) -> Budget:
    """The run's processor budget from `envelope` - see the module note.

    A machine the envelope cannot describe (no cores counted) gets the
    envelope's own floor for each term, so the conductor still orders the
    models and the run still runs. Never raises.
    """
    from app.core import envelope

    readers = max(0, int(readers or 0))
    logical = int(getattr(profile, "logical_processors", 0) or 0)
    physical = int(getattr(profile, "physical_cores", 0) or 0)
    try:
        meaning = int(meaning_threads or 0) or envelope.onnx_threads(profile, readers).auto
    except Exception:                                # noqa: BLE001 - a profile stand-in
        meaning = int(meaning_threads or 0) or 1
    meaning = max(1, meaning)
    try:
        pictures = envelope.picture_model_threads(
            profile, workers=readers, meaning_threads=meaning)
    except Exception:                                # noqa: BLE001 - as above
        pictures = None
    if not pictures:
        # Cores unknown: one session's worth, the floor the envelope gives a
        # machine it can describe least well.
        pictures = 1
    slots = meaning + int(pictures)
    why = (f"{logical or physical or '?'} logical processor(s), {readers} reader(s): "
           f"the meaning model's {meaning} thread(s) and the picture models' "
           f"{pictures} (envelope.onnx_threads, envelope.picture_model_threads)")
    return Budget(cpu_slots=slots, meaning_threads=meaning, picture_threads=int(pictures),
                  logical=logical or physical, readers=readers, why=why)


def reader_ceiling(profile: Any, *, readers: int, meaning_threads: int) -> int:
    r"""How far `_maybe_grow_workers` may grow the readers (`worker_ceiling`).

    `envelope.index_workers(profile).ceiling` - one per core - capped so the
    readers and the meaning model's threads together never ask for more than
    the logical processors: `logical - meaning`. Growth only happens when the
    run is waiting on reading and the meaning model is not mid-batch on the
    processor (`_maybe_grow_workers`), so while it grows it is taking the
    picture models' share at a moment the run is waiting on reading, which is
    what the conductor's priority (reading above pictures) says too. Never
    below `readers`: the static count is the floor of the range.
    """
    from app.core import envelope

    readers = max(1, int(readers or 1))
    try:
        ceiling = envelope.index_workers(profile).ceiling
    except Exception:                                # noqa: BLE001
        return readers
    logical = int(getattr(profile, "logical_processors", 0) or 0)
    if logical:
        ceiling = min(ceiling, logical - max(1, int(meaning_threads or 1)))
    return max(readers, ceiling)


class _Waiter:
    __slots__ = ("lane", "priority", "seq", "kind", "weight", "granted", "since")

    def __init__(self, lane: str, kind: str, weight: int, seq: int, since: float) -> None:
        self.lane = lane
        self.priority = PRIORITY.get(lane, len(PRIORITY) + 1)
        self.seq = seq
        self.kind = kind
        self.weight = weight
        self.granted = False
        self.since = since


class Conductor:
    """The run's processor slots and its one graphics-card slot.

    `should_stop()` - the person's Stop: every waiter is admitted at once.
    `paused()` - the governor's brake: nothing new is admitted while true.
    `write_lock_held()` - does the calling thread hold the store's write lock?
    Such a caller is admitted at once (rule 2 in the module note).
    """

    CPU = "cpu"
    GPU = "gpu"

    def __init__(self, cpu_slots: int, *,
                 should_stop: Optional[Callable[[], bool]] = None,
                 paused: Optional[Callable[[], bool]] = None,
                 write_lock_held: Optional[Callable[[], bool]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 reserve_after_s: float = RESERVE_AFTER_S) -> None:
        self.cpu_slots = max(1, int(cpu_slots or 1))
        self._should_stop = should_stop or (lambda: False)
        self._paused = paused or (lambda: False)
        self._write_lock_held = write_lock_held or (lambda: False)
        self._clock = clock
        self._reserve_after = float(reserve_after_s)
        self._cond = threading.Condition(threading.Lock())
        self._cpu_free = self.cpu_slots
        self._gpu_holder: Optional[str] = None
        self._waiters: list[_Waiter] = []
        self._seq = 0
        self._local = threading.local()
        #: lane -> seconds a slot was held, summed over holders (5e's measure).
        self._busy: dict[str, float] = {}
        #: lane -> holders right now.
        self._holding: dict[str, int] = {}
        #: Admissions past the budget, by why: "stop", "write_lock", "nested".
        self.bypassed: dict[str, int] = {}

    # -- asking -------------------------------------------------------------

    @contextmanager
    def cpu(self, lane: str, weight: int = 1) -> Iterator[None]:
        """Hold `weight` processor slots for the block. Blocks until they are
        free, in priority order; see the module note for stop and pause."""
        weight = max(1, min(int(weight or 1), self.cpu_slots))
        token = self._acquire(lane, self.CPU, weight)
        try:
            yield
        finally:
            self._release(token)

    @contextmanager
    def gpu(self, lane: str) -> Iterator[None]:
        """Hold the graphics card for the block. Acquired before the model's
        own `gpu_exclusive`, never after it."""
        token = self._acquire(lane, self.GPU, 1)
        try:
            yield
        finally:
            self._release(token)

    @contextmanager
    def model(self, lane: str, *, on_gpu: bool, weight: int = 1) -> Iterator[None]:
        """`gpu(lane)` for a model on the graphics card, else `cpu(lane, weight)`."""
        if on_gpu:
            with self.gpu(lane):
                yield
        else:
            with self.cpu(lane, weight):
                yield

    def wake(self) -> None:
        """Make every waiter look again now - after a stop, pause or resume."""
        with self._cond:
            self._dispatch()
            self._cond.notify_all()

    # -- what the cockpit reads ---------------------------------------------

    @property
    def gpu_holder(self) -> str:
        """The lane holding the graphics card, or ""."""
        return self._gpu_holder or ""

    def busy_seconds(self) -> dict[str, float]:
        """Seconds each lane held a slot this run (concurrent holders summed)."""
        with self._cond:
            return dict(self._busy)

    def state(self) -> dict[str, Any]:
        """Plain values: slots, what is free, who holds and who waits."""
        with self._cond:
            return {
                "cpu_slots": self.cpu_slots, "cpu_free": self._cpu_free,
                "gpu_lane": self._gpu_holder or "",
                "holding": {k: v for k, v in self._holding.items() if v},
                "waiting": [w.lane for w in sorted(self._waiters, key=_order)],
            }

    # -- the mechanism -------------------------------------------------------

    def _held_here(self) -> int:
        return int(getattr(self._local, "held", 0) or 0)

    def _acquire(self, lane: str, kind: str, weight: int) -> tuple:
        bypass = ""
        if self._held_here():
            bypass = "nested"
        else:
            try:
                if self._write_lock_held():
                    bypass = "write_lock"
            except Exception:                        # noqa: BLE001 - asked, never trusted to raise
                bypass = ""
        started = self._clock()
        with self._cond:
            if not bypass and self._stopping():
                bypass = "stop"
            if bypass:
                self.bypassed[bypass] = self.bypassed.get(bypass, 0) + 1
                metered = False
            else:
                self._seq += 1
                waiter = _Waiter(lane, kind, weight, self._seq, started)
                self._waiters.append(waiter)
                self._dispatch()
                while not waiter.granted:
                    if self._stopping():
                        self._waiters.remove(waiter)
                        self.bypassed["stop"] = self.bypassed.get("stop", 0) + 1
                        break
                    self._cond.wait(POLL_S)
                    self._dispatch()
                metered = waiter.granted
            if kind == self.GPU and self._gpu_holder is None:
                self._gpu_holder = lane
                gpu_named = True
            else:
                gpu_named = kind == self.GPU and metered
            self._holding[lane] = self._holding.get(lane, 0) + 1
        self._local.held = self._held_here() + 1
        return (lane, kind, weight, metered, gpu_named, self._clock())

    def _release(self, token: tuple) -> None:
        lane, kind, weight, metered, gpu_named, began = token
        self._local.held = max(0, self._held_here() - 1)
        held_for = max(0.0, self._clock() - began)
        with self._cond:
            self._busy[lane] = self._busy.get(lane, 0.0) + held_for
            self._holding[lane] = max(0, self._holding.get(lane, 0) - 1)
            if metered:
                if kind == self.CPU:
                    self._cpu_free = min(self.cpu_slots, self._cpu_free + weight)
            if kind == self.GPU and gpu_named and self._gpu_holder == lane:
                self._gpu_holder = None
            self._dispatch()
            self._cond.notify_all()

    def _stopping(self) -> bool:
        try:
            return bool(self._should_stop())
        except Exception:                            # noqa: BLE001 - "not stopping" keeps order
            return False

    def _held_by_governor(self) -> bool:
        try:
            return bool(self._paused())
        except Exception:                            # noqa: BLE001 - a brake that cannot be read is off
            return False

    def _dispatch(self) -> None:
        """Grant whatever fits, most important first. Called under the lock."""
        if not self._waiters or self._held_by_governor():
            return
        now = self._clock()
        reserved: set[str] = set()
        granted_any = False
        for waiter in sorted(self._waiters, key=_order):
            if waiter.kind in reserved:
                continue
            if waiter.kind == self.CPU:
                fits = waiter.weight <= self._cpu_free
                if fits:
                    self._cpu_free -= waiter.weight
            else:
                fits = self._gpu_holder is None
                if fits:
                    self._gpu_holder = waiter.lane
            if fits:
                waiter.granted = True
                granted_any = True
            elif now - waiter.since >= self._reserve_after:
                # Waited long enough: nothing less important passes it now.
                reserved.add(waiter.kind)
        if granted_any:
            self._waiters = [w for w in self._waiters if not w.granted]
            self._cond.notify_all()


def _order(waiter: _Waiter) -> tuple[int, int]:
    return (waiter.priority, waiter.seq)


class _NullConductor:
    """No budget at all: every slot granted at once. For a pipeline a test
    built without `run()`, and for the sequential mode 5e compares against."""

    cpu_slots = 0
    gpu_holder = ""
    bypassed: dict[str, int] = {}

    @contextmanager
    def cpu(self, lane: str, weight: int = 1) -> Iterator[None]:
        yield

    @contextmanager
    def gpu(self, lane: str) -> Iterator[None]:
        yield

    @contextmanager
    def model(self, lane: str, *, on_gpu: bool, weight: int = 1) -> Iterator[None]:
        yield

    def wake(self) -> None:
        return None

    def busy_seconds(self) -> dict[str, float]:
        return {}

    def state(self) -> dict[str, Any]:
        return {}


NULL_CONDUCTOR: Any = _NullConductor()
