r"""One writer at a time, and a window that may stay open while it works.

Layer: L0

**The report was "if leasha is open i cant get another cli interface to start
indexing".** The cause was one lock doing two jobs: `app/main.py` held
`SingleInstance` for the entire lifetime of the window and `app.cli index` took
the same mutex, so having Leasha open made indexing from a terminal impossible.

The refusal was correct in form and wrong in scope. What cannot overlap is two
*writers* - two pipelines against one LanceDB table corrupt it - and that hazard
lasts as long as a run, not as long as a window. A window that is merely open is
a reader.

So the property under test is a pair, and both halves matter equally:

* two index runs exclude each other, whichever process starts them;
* a window being open excludes neither.

The stale-record case is the one worth reading twice. A process that dies leaves
its description behind in `index_state` but has its mutex released by the
operating system. If the record were the authority, a crash during indexing
would lock the feature until somebody found the right table to edit - a paper
lock, breakable only by hand and only at the worst moment.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

from app.core.errors import AppErrorException
from app.core.run_lock import (
    COMMAND_LINE,
    GUI,
    GUI_MUTEX_NAME,
    INDEX_MUTEX_NAME,
    RUN_STATE_KEY,
    IndexRunLock,
    active_run,
    clear_stop,
    describe_holder,
    is_indexing,
    front_window,
    open_window,
    publish,
    publish_window,
    request_front,
    request_stop,
    stop_requested,
    take_front_request,
)
from app.core.single_instance import SingleInstance
from app.storage.sqlite_store import SqliteStore


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


@pytest.fixture()
def locks(tmp_path):
    """POSIX lock files land in a temp directory, so runs cannot collide."""
    directory = tmp_path / "locks"
    directory.mkdir()
    return directory


@pytest.fixture()
def window_name():
    """The window's mutex under a name of this test's own.

    The real `GUI_MUTEX_NAME` is machine-wide and Leasha holds it for as long
    as its window is open, so these tests failed whenever the owner had Leasha
    running (2026-09-30: opened mid-suite, two failures). What they prove - the
    window's lock is not the run lock, and a second window is refused - does not
    depend on the real name; `test_the_two_locks_are_not_the_same_name` pins that.
    """
    return f"{GUI_MUTEX_NAME}.test-{uuid.uuid4().hex}"


@pytest.fixture()
def index_name():
    """The run lock under a name of this test's own, as `window_name` is.

    The real `INDEX_MUTEX_NAME` is machine-wide and is held for as long as an
    index run lasts - hours, on the owner's machine. Taken by name here, every
    test below failed while a real run was going, and a real run that started
    while one of them held it was refused. `lock_dir` does not help: it is
    where the lock *file* goes on Linux and macOS, and a Windows mutex has no
    folder. What the tests prove - two runs exclude each other, a dead
    process's record locks nothing - is true of any name;
    `test_the_two_locks_are_not_the_same_name` pins the real ones.

    `tests/private_locks.py` already keeps the whole suite off the real names.
    A name per test is on top of that: it also keeps these tests apart from a
    window some earlier test left open, whose four-second probe of the run
    lock is what `CONTENTION_WAIT_S` was added to wait out.
    """
    return f"{INDEX_MUTEX_NAME}.test-{uuid.uuid4().hex}"


@pytest.fixture()
def held_for_real(locks):
    r"""Hold one of the machine's real locks, from a thread, for a few lines.

        with held_for_real(INDEX_MUTEX_NAME):
            ...                    # a real index run is going, as far as
                                   # anything on this machine can tell

    **If somebody else already has it, that is just as good** - Leasha being
    open is exactly that for the window's lock - so a refusal is not a
    failure here; either way the name is held while the body runs.

    From a thread because a Windows mutex belongs to the thread that took it:
    the same thread asking again is given it again, which would prove nothing.
    Kept to a few lines because the owner's own Leasha can see this one.
    """
    import contextlib
    import threading

    from tests import private_locks

    @contextlib.contextmanager
    def hold(name: str):
        ready, done = threading.Event(), threading.Event()

        def holder() -> None:
            lock = private_locks.real(name, lock_dir=locks)
            try:
                lock.acquire()
            except AppErrorException:
                pass                     # held already, by the real thing
            ready.set()
            done.wait(30)
            lock.release()

        thread = threading.Thread(target=holder, name=f"holds {name}")
        thread.start()
        try:
            assert ready.wait(10), "the holder thread never started"
            yield
        finally:
            done.set()
            thread.join(10)

    return hold


# ---------------------------------------------------------------------------
# The two halves of the property
# ---------------------------------------------------------------------------

def test_two_index_runs_exclude_each_other(store, locks, index_name):
    with IndexRunLock(store, owner=COMMAND_LINE, name=index_name, lock_dir=locks):
        with pytest.raises(AppErrorException) as raised:
            IndexRunLock(store, owner=GUI, name=index_name, lock_dir=locks).acquire()

    assert raised.value.error.code == "ERR_INDEX_RUNNING"


def test_an_open_window_does_not_block_an_index_run(store, locks, window_name, index_name):
    r"""**The reported bug, stated as the thing that must now be possible.**

    The window's lock and the run lock are different mutexes. Holding the first
    - which is what having Leasha open means - must leave the second free.
    """
    window = SingleInstance(window_name, lock_dir=locks)
    window.acquire()
    try:
        with IndexRunLock(store, owner=COMMAND_LINE, name=index_name, lock_dir=locks) as held:
            assert held.acquired, "an open window still blocks indexing"
    finally:
        window.release()


def test_a_second_window_is_still_refused(locks, window_name):
    """Splitting the locks must not quietly permit two windows."""
    first = SingleInstance(window_name, lock_dir=locks)
    first.acquire()
    try:
        with pytest.raises(AppErrorException):
            SingleInstance(window_name, lock_dir=locks).acquire()
    finally:
        first.release()


def test_the_two_locks_are_not_the_same_name():
    """The whole fix in one line. Named so a merge cannot quietly undo it."""
    assert INDEX_MUTEX_NAME != GUI_MUTEX_NAME


def test_a_real_index_run_elsewhere_does_not_reach_the_tests(store, locks, held_for_real):
    r"""**The owner indexing must not turn the suite red, and the reverse.**

    With the machine's real run lock held - which is what a real `app.cli
    index` or the window's own run looks like from outside - a run lock taken
    *the way the application takes it*, with no name given, is still free
    here, and nothing reads as "a run is going on". That is
    `tests/private_locks.py`: in a test process the real name is not the name
    asked for. No `name=` below, on purpose - this is every test that reaches
    the lock through `cmd_index`, `IndexWorker` or a window's four-second
    probe, none of which can pass one.
    """
    from tests import private_locks

    with held_for_real(INDEX_MUTEX_NAME):
        # The bait is real: asked for by its real name, it is refused.
        with pytest.raises(AppErrorException):
            private_locks.real(INDEX_MUTEX_NAME, lock_dir=locks).acquire()

        assert not is_indexing(store, lock_dir=locks), (
            "a test saw the machine's real index run as its own")
        with IndexRunLock(store, owner=COMMAND_LINE, lock_dir=locks) as run:
            assert run.acquired, "a real index run elsewhere blocked a test"
            # And this test's run is a run, to anything else in this process.
            assert is_indexing(store, lock_dir=locks)


def test_an_open_leasha_does_not_reach_the_tests(locks, held_for_real):
    """The same for the window's lock, which Leasha holds all day."""
    from tests import private_locks

    with held_for_real(GUI_MUTEX_NAME):
        with pytest.raises(AppErrorException):
            private_locks.real(GUI_MUTEX_NAME, lock_dir=locks).acquire()

        with SingleInstance(GUI_MUTEX_NAME, lock_dir=locks) as window:
            assert window.acquired, "Leasha being open blocked a test's window"
            with pytest.raises(AppErrorException):
                SingleInstance(lock_dir=locks).acquire()      # still one window


def test_a_test_process_cannot_ask_for_a_real_name_by_accident():
    """What the two tests above rest on, said directly."""
    from app.core.single_instance import DEFAULT_MUTEX_NAME

    for real in (INDEX_MUTEX_NAME, GUI_MUTEX_NAME, DEFAULT_MUTEX_NAME):
        asked = SingleInstance(real).name
        assert asked != real and asked.startswith(real)
    assert SingleInstance().name == SingleInstance(GUI_MUTEX_NAME).name
    # Any other name is left exactly as given.
    assert SingleInstance("handover-test").name == "handover-test"


def test_the_lock_is_released_even_when_the_run_raises(store, locks, index_name):
    with pytest.raises(ValueError):
        with IndexRunLock(store, owner=COMMAND_LINE, name=index_name, lock_dir=locks):
            raise ValueError("the run failed")

    with IndexRunLock(store, owner=GUI, name=index_name, lock_dir=locks) as second:
        assert second.acquired, "a failed run kept the lock"


def test_a_probe_in_flight_is_waited_out_rather_than_named_as_a_run(store, locks, index_name):
    r"""**A probe is not a holder.** `is_indexing` answers by taking the lock and
    letting it straight go, and an open window asks it every four seconds on a
    worker. A run starting inside that instant was refused as "already in
    progress (another process)" with nothing indexing anywhere - seen on the
    Windows CI (2026-09-29), where the named mutex is machine-wide and a test
    window left open by an earlier test was doing the asking.

    The probe here is held far longer than a real one (a quarter of a second
    against microseconds), from a thread of its own, because a Windows mutex
    can only be let go by the thread that took it."""
    import threading

    from app.core.run_lock import CONTENTION_WAIT_S

    taken = threading.Event()

    def probe() -> None:
        held = SingleInstance(index_name, lock_dir=locks).acquire()
        taken.set()
        threading.Event().wait(0.25)
        held.release()

    prober = threading.Thread(target=probe, name="probe")
    prober.start()
    try:
        assert taken.wait(5), "the probe never took the lock"
        assert 0.25 < CONTENTION_WAIT_S, "the wait would not outlast this probe"
        with IndexRunLock(store, owner=COMMAND_LINE, name=index_name, lock_dir=locks) as run:
            assert run.acquired
    finally:
        prober.join(5)


# ---------------------------------------------------------------------------
# The record describes; the mutex decides
# ---------------------------------------------------------------------------

def test_a_record_left_by_a_dead_process_does_not_lock_anything(store, locks, index_name):
    r"""**A paper lock is worse than no lock.**

    A process killed mid-run leaves its row in `index_state` and has its mutex
    released by the operating system. Trusting the row would mean indexing stays
    refused until somebody edits a database by hand, which is a thing nobody
    discovers at a good moment.
    """
    publish(store, owner=COMMAND_LINE, started_at=1.0, stats=None)
    assert active_run(store) is not None, "the fixture did not write a record"

    assert not is_indexing(store, lock_dir=locks, name=index_name)
    with IndexRunLock(store, owner=GUI, name=index_name, lock_dir=locks) as held:
        assert held.acquired


def test_the_refusal_names_who_is_holding_it(store, locks, index_name):
    with IndexRunLock(store, owner=COMMAND_LINE, name=index_name, lock_dir=locks):
        with pytest.raises(AppErrorException) as raised:
            IndexRunLock(store, owner=GUI, name=index_name, lock_dir=locks).acquire()

    rendered = raised.value.error.render()
    assert COMMAND_LINE in rendered, (
        "'something else is indexing' is not an answer somebody can act on")


def test_the_holder_description_survives_a_missing_or_broken_record(store):
    assert describe_holder(None) == "another process"
    assert describe_holder(store) == "another process"      # nothing published

    store.set_state(RUN_STATE_KEY, "{not json")
    assert describe_holder(store) == "another process"


def test_releasing_takes_the_description_down_with_it(store, locks, index_name):
    """Otherwise the next reader sees a run the mutex says is over."""
    with IndexRunLock(store, owner=COMMAND_LINE, name=index_name, lock_dir=locks):
        assert active_run(store) is not None

    assert active_run(store) is None


# ---------------------------------------------------------------------------
# Progress, published for another process to read
# ---------------------------------------------------------------------------

class _Stats:
    seen = 120
    indexed = 90
    unchanged = 10
    skipped = 2
    chunks = 400
    vectors = 400
    walk_complete = False
    current = "report.pdf"


def test_progress_is_published_as_one_blob_rather_than_a_spread_of_keys(store):
    r"""A reader in another process must not be able to catch a half-written set.

    Two keys written separately can be read between the two writes, which is a
    bar drawn from one instant's numerator and another's denominator - the exact
    tearing this is meant to remove.
    """
    publish(store, owner=COMMAND_LINE, started_at=1.0, stats=_Stats())

    raw = store.get_state(RUN_STATE_KEY, "")
    payload = json.loads(raw)

    assert payload["owner"] == COMMAND_LINE
    assert payload["stats"]["indexed"] == 90
    assert payload["stats"]["seen"] == 120
    assert payload["stats"]["walk_complete"] is False


def test_the_snapshot_is_a_copy_not_a_reference(store):
    r"""**`IndexStats` is mutated by three thread groups while this reads it.**

    The in-process progress signal hands the live object across a thread
    boundary, which is how a bar draws two halves of two different instants.
    Serialising forces a copy; this is the assertion that keeps it one.
    """
    stats = _Stats()
    publish(store, owner=COMMAND_LINE, started_at=1.0, stats=stats)

    stats.indexed = 99999

    assert active_run(store)["stats"]["indexed"] == 90


def test_publishing_never_raises_on_a_store_that_will_not_write(store):
    """This is on the path of a run that may last a week. A progress row is
    never worth the run."""
    class Hostile:
        def set_state(self, *_args, **_kwargs):
            raise RuntimeError("the database is locked")

    publish(Hostile(), owner=COMMAND_LINE, started_at=1.0, stats=_Stats())
    publish(None, owner=COMMAND_LINE, started_at=1.0, stats=_Stats())


# ---------------------------------------------------------------------------
# Stopping a run from another process
# ---------------------------------------------------------------------------

def test_a_stop_can_be_asked_for_and_seen(store):
    assert not stop_requested(store)

    request_stop(store)
    assert stop_requested(store)

    clear_stop(store)
    assert not stop_requested(store)


def test_taking_the_lock_clears_a_stale_stop(store, locks, index_name):
    r"""**Otherwise yesterday's Stop halts tomorrow's run before it starts.**

    Which looks exactly like indexing being broken, and leaves no trace saying
    why - the run would end immediately, having done nothing, reporting success.
    """
    request_stop(store)

    with IndexRunLock(store, owner=COMMAND_LINE, name=index_name, lock_dir=locks):
        assert not stop_requested(store)


def test_a_read_failure_is_not_read_as_a_stop(store):
    """A locked database must not silently end a five-day run."""
    class Hostile:
        def get_state(self, *_args, **_kwargs):
            raise RuntimeError("the database is locked")

    assert not stop_requested(Hostile())


# ---------------------------------------------------------------------------
# A second launch, asking the first to come to the front
# ---------------------------------------------------------------------------

def test_a_front_request_can_be_asked_for_and_taken(store):
    r"""**The bug, reported live: "the box is hard to get to."**

    A second launch that found the window already open writes this; the
    window's own poll takes it. `take_front_request` must both report it
    happened and clear it - a "take", the same shape as `deeplink.
    take_pending`, so the window does not front itself again next poll.
    """
    assert not take_front_request(store), "nothing was asked for yet"

    request_front(store)
    assert take_front_request(store), "the request was written but not seen"
    assert not take_front_request(store), (
        "a request must be cleared once taken, or the window keeps "
        "stealing focus back on every later poll")


def test_a_front_request_never_raises_on_a_store_that_will_not_write():
    """Same discipline as `publish`/`request_stop`: this runs in a process
    that is exiting either way, or on a timer beside a live window - neither
    is a place to raise over a flag that failed to write."""
    class Hostile:
        def set_state(self, *_args, **_kwargs):
            raise RuntimeError("the database is locked")

        def get_state(self, *_args, **_kwargs):
            raise RuntimeError("the database is locked")

    request_front(Hostile())
    request_front(None)
    assert not take_front_request(Hostile())
    assert not take_front_request(None)


def test_an_open_window_is_recorded_and_a_closing_one_is_not(store):
    """2026-10-05, "it stops the running copy and starts a new one": every
    second launch waited out the closing handover because nothing told an
    open window from a closing one. The record is that difference."""
    assert open_window(store) is None, "nothing published yet"

    publish_window(store, 4321, 98765)
    assert open_window(store) == (4321, 98765)

    store.set_state("gui:window", "")            # what closeEvent writes
    assert open_window(store) is None, "a closing copy must not be fronted"

    store.set_state("gui:window", "rubbish")
    assert open_window(store) is None
    assert open_window(None) is None


def test_a_window_record_left_by_a_crash_is_not_believed():
    """A record is a claim: a handle that is gone, or that belongs to another
    process, is refused - the launch then waits as it always did."""
    assert not front_window(os.getpid(), 0), "no such window"
    if sys.platform == "win32":
        import ctypes

        desktop = ctypes.windll.user32.GetDesktopWindow()
        assert desktop, "the desktop window always exists"
        assert not front_window(os.getpid(), desktop), (
            "a live handle owned by another process must not be fronted")


@pytest.mark.skipif(sys.platform != "win32", reason="the foreground is a Windows thing")
def test_a_live_window_of_the_recorded_process_is_fronted():
    """The other half: a handle that is a live window of the recorded process
    is accepted, so a second launch fronts it instead of waiting twelve
    seconds. A plain native window, because the suite runs Qt offscreen and
    a widget there has no real handle."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    hwnd = user32.CreateWindowExW(0, "STATIC", "leasha-test", 0,
                                  0, 0, 10, 10, None, None, None, None)
    assert hwnd, "could not create a test window"
    try:
        assert front_window(os.getpid(), hwnd)
        assert not front_window(os.getpid() + 1, hwnd), "wrong process"
    finally:
        user32.DestroyWindow(hwnd)
    assert not front_window(os.getpid(), hwnd), "a destroyed window is gone"


def test_the_pipeline_polls_the_stop_flag_at_its_checkpoint():
    """The link between the flag and the run, which no fixture here can drive.

    `_checkpoint` is called between files, which is where an in-process stop is
    honoured too - so a cross-process stop keeps everything already written, the
    same as pressing Stop in the window.
    """
    import inspect

    from app.index.pipeline import Pipeline

    source = inspect.getsource(Pipeline._checkpoint)

    assert "stop_requested" in source
    assert "request_stop" in source
    assert "publish(" in source
