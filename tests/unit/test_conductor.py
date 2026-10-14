"""The conductor: one processor budget and one graphics-card slot for a run's models.

Layer: L3

Work order `indexing-cockpit-and-conductor` 5a. Held here:

* the budget is the envelope's own numbers, added - never a new formula;
* a free slot goes to the most important waiter (priority), and a slot is
  never left idle while something that fits is waiting (work-conserving);
* the graphics card is one slot, and who holds it is reported;
* a stop wakes every waiter and admits it; a pause admits nothing new;
* no deadlock with the store's write lock, whichever thread holds it.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app.core import envelope
from app.index.conductor import (
    FACE_POOL_MAX,
    Conductor,
    NULL_CONDUCTOR,
    budget_for,
    reader_ceiling,
)

OWNER = SimpleNamespace(physical_cores=10, logical_processors=12,
                        performance_cores=2, efficiency_cores=8)


def _wait(predicate, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _hold(conductor, lane, *, weight=1, gpu=False, release=None, order=None):
    """A thread that takes a slot, notes it, and holds it until `release`."""
    got = threading.Event()

    def body():
        ctx = conductor.gpu(lane) if gpu else conductor.cpu(lane, weight)
        with ctx:
            if order is not None:
                order.append(lane)
            got.set()
            if release is not None:
                release.wait(5)

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, got


# --- the budget ---------------------------------------------------------------

def test_the_budget_is_the_envelopes_own_numbers_added():
    budget = budget_for(OWNER, readers=4)
    meaning = envelope.onnx_threads(OWNER, 4).auto
    pictures = envelope.picture_model_threads(OWNER, workers=4, meaning_threads=meaning)
    assert (budget.meaning_threads, budget.picture_threads) == (meaning, pictures)
    assert budget.cpu_slots == meaning + pictures
    # On the owner's laptop: 4 + 4 of 12, the rest left to readers and window.
    assert (meaning, pictures, budget.cpu_slots) == (4, 4, 8)
    assert budget.cpu_slots <= OWNER.logical_processors - 4


def test_the_face_pool_is_one_on_the_card_and_the_measured_most_on_the_processor():
    budget = budget_for(OWNER, readers=4)
    assert budget.face_pool(faces_on_gpu=True) == 1
    assert budget.face_pool(faces_on_gpu=False) == min(FACE_POOL_MAX, 4) == 4
    # N callers x threads stays inside the picture share (5c).
    pool = budget.face_pool(faces_on_gpu=False)
    assert budget.threads_for(pool) == 1
    assert budget.weight_for(pool) * pool <= budget.picture_threads


def test_a_machine_nobody_could_count_still_gets_a_budget():
    budget = budget_for(SimpleNamespace(), readers=4)
    assert budget.cpu_slots >= 2 and budget.picture_threads >= 1


def test_the_reader_ceiling_lets_growth_happen_and_leaves_the_meaning_model_its_threads():
    ceiling = reader_ceiling(OWNER, readers=4, meaning_threads=4)
    assert ceiling == min(envelope.index_workers(OWNER).ceiling, 12 - 4) == 8
    assert reader_ceiling(SimpleNamespace(), readers=4, meaning_threads=4) >= 4


# --- priority and work-conservation ------------------------------------------

def test_a_freed_slot_goes_to_the_most_important_waiter():
    conductor = Conductor(1)
    release = threading.Event()
    order: list[str] = []
    holder, got = _hold(conductor, "cards", release=release, order=order)
    assert got.wait(2)
    # Asked in the reverse of their importance.
    waiters = []
    for lane in ("cards", "picture_text", "describe", "faces", "meaning"):
        thread, _ = _hold(conductor, lane, order=order)
        waiters.append(thread)
        assert _wait(lambda n=len(waiters): len(conductor.state()["waiting"]) == n)
    release.set()
    for thread in [holder, *waiters]:
        thread.join(3)
    assert order == ["cards", "meaning", "faces", "describe", "picture_text", "cards"]


def test_a_slot_is_never_idle_while_something_that_fits_waits():
    conductor = Conductor(4)
    release = threading.Event()
    heavy, got = _hold(conductor, "describe", weight=3, release=release)
    assert got.wait(2)
    # The meaning model wants 4 and cannot fit; a face scan wanting 1 can.
    meaning, meaning_got = _hold(conductor, "meaning", weight=4, release=release)
    faces, faces_got = _hold(conductor, "faces", weight=1, release=release)
    assert faces_got.wait(1), "the free slot went to the work that fits"
    assert not meaning_got.is_set()
    release.set()
    assert meaning_got.wait(2)
    for thread in (heavy, meaning, faces):
        thread.join(3)


def test_a_big_waiter_reserves_the_slots_after_waiting_a_while():
    clock = [0.0]
    conductor = Conductor(2, clock=lambda: clock[0], reserve_after_s=1.0)
    release = threading.Event()
    first, got = _hold(conductor, "faces", weight=1, release=release)
    assert got.wait(2)
    meaning, meaning_got = _hold(conductor, "meaning", weight=2, release=release)
    assert _wait(lambda: conductor.state()["waiting"] == ["meaning"])
    clock[0] = 5.0                                  # the meaning model has waited
    late, late_got = _hold(conductor, "faces", weight=1, release=release)
    assert _wait(lambda: "faces" in conductor.state()["waiting"])
    time.sleep(0.15)
    assert not late_got.is_set(), "nothing less important passes a reserved waiter"
    release.set()
    for thread in (first, meaning, late):
        thread.join(3)
    assert meaning_got.is_set() and late_got.is_set()


def test_too_big_a_weight_still_runs_alone():
    conductor = Conductor(2)
    with conductor.cpu("describe", weight=16):
        assert conductor.state()["cpu_free"] == 0
    assert conductor.state()["cpu_free"] == 2


# --- the graphics card ---------------------------------------------------------

def test_the_graphics_card_is_one_slot_and_its_holder_is_reported():
    conductor = Conductor(8)
    release = threading.Event()
    first, got = _hold(conductor, "describe", gpu=True, release=release)
    assert got.wait(2)
    assert conductor.gpu_holder == "describe"
    second, second_got = _hold(conductor, "picture_text", gpu=True, release=release)
    time.sleep(0.15)
    assert not second_got.is_set(), "a second model waits for the card"
    assert conductor.state()["cpu_free"] == 8, "the card is not processor slots"
    release.set()
    for thread in (first, second):
        thread.join(3)
    assert second_got.is_set()
    assert conductor.gpu_holder == ""


def test_model_picks_the_card_or_the_processor():
    conductor = Conductor(4)
    with conductor.model("faces", on_gpu=True):
        assert conductor.gpu_holder == "faces"
        assert conductor.state()["cpu_free"] == 4
    with conductor.model("faces", on_gpu=False, weight=3):
        assert conductor.state()["cpu_free"] == 1
        assert conductor.gpu_holder == ""


# --- stop and pause ------------------------------------------------------------

def test_a_stop_wakes_every_waiter_and_lets_it_through():
    stopped = threading.Event()
    conductor = Conductor(1, should_stop=stopped.is_set)
    release = threading.Event()
    holder, got = _hold(conductor, "cards", release=release)
    assert got.wait(2)
    waiters = [_hold(conductor, lane) for lane in ("meaning", "faces", "describe")]
    assert _wait(lambda: len(conductor.state()["waiting"]) == 3)
    stopped.set()
    conductor.wake()
    for thread, waiter_got in waiters:
        assert waiter_got.wait(2), "a stopping run never waits on the conductor"
        thread.join(2)
    release.set()
    holder.join(2)
    assert conductor.bypassed.get("stop") == 3


def test_a_pause_admits_nothing_new_and_resume_lets_it_go():
    paused = threading.Event()
    paused.set()
    conductor = Conductor(4, paused=paused.is_set)
    thread, got = _hold(conductor, "faces")
    time.sleep(0.2)
    assert not got.is_set(), "the governor's brake holds new work"
    paused.clear()
    assert got.wait(2)
    thread.join(2)


# --- the write lock --------------------------------------------------------------

def test_no_deadlock_with_the_write_lock_whichever_thread_holds_it():
    """Rule 2: a thread holding the write lock is never made to wait for a slot.
    Here a picture thread holds every slot and then wants the write lock, held
    by a writer thread that wants a slot - the classic cycle. Without the rule
    both wait for ever; with it the writer goes through and lets go."""
    write_lock = threading.RLock()
    holders = threading.local()

    def write_lock_held() -> bool:
        return bool(getattr(holders, "depth", 0))

    conductor = Conductor(1, write_lock_held=write_lock_held)
    writer_has_lock = threading.Event()
    done = []

    def writer():
        with write_lock:
            holders.depth = 1
            writer_has_lock.set()
            time.sleep(0.1)                       # the picture thread takes the slot
            with conductor.cpu("meaning", weight=1):
                done.append("writer")
            holders.depth = 0

    def pictures():
        writer_has_lock.wait(2)
        with conductor.cpu("faces", weight=1):
            with write_lock:                       # the model's result, written
                done.append("pictures")

    threads = [threading.Thread(target=writer, daemon=True),
               threading.Thread(target=pictures, daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sorted(done) == ["pictures", "writer"], "nobody waited for ever"
    assert conductor.bypassed.get("write_lock") == 1


def test_a_thread_already_holding_a_slot_is_never_made_to_wait_for_another():
    conductor = Conductor(1)
    with conductor.cpu("describe"):
        with conductor.gpu("describe"):
            with conductor.cpu("meaning", weight=1):
                pass
    assert conductor.bypassed.get("nested") == 2
    assert conductor.state()["cpu_free"] == 1


# --- accounting ---------------------------------------------------------------------

def test_busy_seconds_per_lane_are_counted():
    clock = [0.0]
    conductor = Conductor(2, clock=lambda: clock[0])
    with conductor.cpu("faces"):
        clock[0] += 2.0
    with conductor.gpu("describe"):
        clock[0] += 3.0
    assert conductor.busy_seconds() == {"faces": 2.0, "describe": 3.0}


def test_the_null_conductor_takes_every_call_and_holds_nothing():
    with NULL_CONDUCTOR.model("faces", on_gpu=True):
        with NULL_CONDUCTOR.cpu("meaning", 4):
            pass
    assert NULL_CONDUCTOR.gpu_holder == ""


@pytest.mark.parametrize("slots", [1, 3, 8])
def test_many_threads_never_exceed_the_budget(slots):
    conductor = Conductor(slots)
    in_use = [0]
    peak = [0]
    lock = threading.Lock()

    def body(lane, weight):
        for _ in range(30):
            with conductor.cpu(lane, weight):
                with lock:
                    in_use[0] += min(weight, slots)
                    peak[0] = max(peak[0], in_use[0])
                time.sleep(0.0005)
                with lock:
                    in_use[0] -= min(weight, slots)

    lanes = [("meaning", 2), ("faces", 1), ("describe", 3), ("picture_text", 1),
             ("cards", 1), ("faces", 1)]
    threads = [threading.Thread(target=body, args=lane, daemon=True) for lane in lanes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert not any(thread.is_alive() for thread in threads)
    assert peak[0] <= slots
    assert conductor.state()["cpu_free"] == slots
