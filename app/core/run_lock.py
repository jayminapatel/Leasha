r"""Who is allowed to write to the index, and who is merely reading it.

Layer: L0

**One lock was doing two jobs, and the smaller of them was winning.**
`SingleInstance` is held by `app/main.py` for the entire lifetime of the window,
and `app.cli index` takes the same mutex - so a person with Leasha open could not
start an index from a terminal at all. The refusal was correct in form and wrong
in scope: the window spends almost all of its life *reading*, and reading is not
what makes two processes dangerous to each other.

What actually cannot overlap is two **writers**. Two pipelines against one
LanceDB table corrupt it, and against one SQLite file they serialise into
timeouts. That hazard lasts exactly as long as an index run, not as long as a
window is open.

So there are two locks now, with two different lifetimes:

* `GUI_MUTEX_NAME` - one window at a time, held by `app/main.py`. A second
  window is still refused, because two windows sharing preferences and a cache
  is its own quiet mess.
* `INDEX_MUTEX_NAME` - one index run at a time, held by `app.cli index`, by
  `app.cli reembed`, and by the window's own run alike. Whoever asks second is
  told who has it and since when.

**The mutex is the authority; the record beside it is only a description.** A
process that dies has its mutex released by the operating system but leaves its
row in `index_state` behind, so a record without a mutex is a stale record and
never a reason to refuse. Reading it the other way round - trusting the row -
would mean a crash during indexing locked the feature until somebody found the
right table to edit, which is precisely the kind of paper lock that has to be
broken by hand at the worst moment.

The record earns its place twice over: it turns *"something else is indexing"*
into *"the command line has been indexing since 14:02"*, and it is what lets the
window draw a run it did not start. See `publish` and `active_run`.
"""

from __future__ import annotations

import json
import os
import time
from types import TracebackType
from typing import Any, Optional, Type

from app.core.errors import AppErrorException, make_error
from app.core.single_instance import SingleInstance

__all__ = [
    "IndexRunLock", "GUI_MUTEX_NAME", "INDEX_MUTEX_NAME",
    "RUN_STATE_KEY", "STOP_STATE_KEY", "FRONT_STATE_KEY",
    "publish", "active_run", "request_stop", "stop_requested", "clear_stop",
    "request_front", "take_front_request",
    "describe_holder", "GUI", "COMMAND_LINE",
]

#: One window at a time. This is the name `SingleInstance` used to default to,
#: kept so an older copy still recognises a newer one during an upgrade.
GUI_MUTEX_NAME = "Local.KnowledgeGraph.V2.SingleInstance"

#: One index run at a time, across every process on this machine.
INDEX_MUTEX_NAME = "Local.KnowledgeGraph.V2.IndexRun"

#: Where the current run describes itself, for a refusal message and for the
#: window's progress bar. One key holding JSON rather than a dozen scalar keys:
#: it is written whole on every checkpoint, and a half-updated set of keys read
#: mid-write is exactly the torn snapshot this is meant to avoid.
RUN_STATE_KEY = "run:active"

#: Set by whoever wants the current run to stop. The runner polls it and clears
#: it. A flag rather than a signal because the two processes share nothing else,
#: and because "please stop" must survive the runner being mid-file.
STOP_STATE_KEY = "run:stop_requested"

#: Set by a second launch that found `GUI_MUTEX_NAME` already held by a copy
#: that is not closing - the ordinary "the app is already open" case, not the
#: brief handover `HANDOVER_WAIT_S` exists for. The running window's own poll
#: picks this up and fronts itself. Same shape as `STOP_STATE_KEY` and
#: `deeplink.PENDING_KEY`: a flag rather than a signal, because a starting
#: process that could not get the lock shares nothing else with the one that
#: did.
FRONT_STATE_KEY = "gui:front_requested"

#: What kind of process is holding the lock. Only for the sentence a person
#: reads, so these are words rather than an enum.
GUI = "the window"
COMMAND_LINE = "the command line"


def _now() -> float:
    return time.time()


class IndexRunLock:
    """Exclusive right to write to the index, for the length of one run.

    Use as a context manager:

        with IndexRunLock(store, owner=COMMAND_LINE):
            pipeline.run()

    Raises `AppErrorException(ERR_INDEX_RUNNING)` when somebody else has it,
    with the holder named in the message.

    `store` is optional. The lock works without it - the mutex is what excludes -
    and a store only adds the description and the progress feed. Passing `None`
    is how a test, or a run before the store is open, takes the lock without
    needing a database.
    """

    def __init__(self, store: Any = None, *, owner: str = COMMAND_LINE,
                 name: str = INDEX_MUTEX_NAME, lock_dir: Any = None) -> None:
        self.store = store
        self.owner = owner
        self._guard = SingleInstance(name, lock_dir=lock_dir)
        self.acquired = False

    def acquire(self) -> "IndexRunLock":
        try:
            self._guard.acquire()
        except AppErrorException as exc:
            # **The holder is described, never trusted.** By the time this is
            # read the other process may have finished; the mutex already said
            # it had not, so this only supplies the words.
            raise AppErrorException(make_error(
                "ERR_INDEX_RUNNING", "core.run_lock",
                holder=describe_holder(self.store),
                details=str(getattr(exc.error, "details", "") or ""),
            )) from exc

        self.acquired = True
        # A fresh run inherits nobody's stop request. Otherwise a run stopped
        # yesterday leaves a flag that halts the next one before it starts,
        # which looks exactly like indexing being broken.
        clear_stop(self.store)
        publish(self.store, owner=self.owner, started_at=_now(), stats=None)
        return self

    def release(self) -> None:
        """Release, and take the description down with it.

        Both halves matter. Leaving the record would tell the next reader that a
        run is in progress when the mutex says otherwise - and since the mutex
        is the authority, that reader would show progress for a run that ended.
        """
        if self.acquired:
            _clear(self.store)
            clear_stop(self.store)
        self._guard.release()
        self.acquired = False

    def __enter__(self) -> "IndexRunLock":
        return self.acquire()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()


# ---------------------------------------------------------------------------
# The description, and the progress feed
# ---------------------------------------------------------------------------

def publish(store: Any, *, owner: str, started_at: float,
            stats: Any = None) -> None:
    """Record what this run is and how far it has got. **Never raises.**

    Called on every checkpoint, so it is on the path of a run that may last a
    week: a failure to write a progress row must cost the row and never the run.

    The payload is one JSON blob rather than a set of keys, because a reader in
    another process would otherwise be able to catch a half-written set and draw
    a bar from one run's numerator and another's denominator.
    """
    if store is None:
        return
    payload: dict[str, Any] = {
        "pid": os.getpid(),
        "owner": owner,
        "started_at": float(started_at),
        "updated_at": _now(),
    }
    if stats is not None:
        payload["stats"] = _snapshot(stats)
    try:
        store.set_state(RUN_STATE_KEY, json.dumps(payload))
    except Exception:                    # a progress row is never worth a run
        return


def _snapshot(stats: Any) -> dict[str, Any]:
    """The numbers a watching window needs, copied out of a live object.

    **Copied, deliberately.** `IndexStats` is mutated by the walker, by every
    extraction worker and by the consumer while this reads it; handing the
    object itself to another thread - which is what the in-process progress
    signal does - is how a bar draws a numerator and a denominator from two
    different instants. Serialising forces a copy, and this is the one place
    that would otherwise be tempted to skip it.
    """
    def number(name: str, default: int = 0) -> Any:
        try:
            return getattr(stats, name, default)
        except Exception:                # noqa: BLE001 - a torn read, not a fault
            return default

    wanted = (
        "seen", "indexed", "unchanged", "unchanged_documents", "skipped",
        "deleted", "chunks", "vectors", "embed_failures", "name_only",
        "bytes_read", "elapsed_s", "files_per_minute", "mb_per_minute",
        "pauses", "paused_seconds", "paused", "walk_complete", "current",
        "ocr_mode",
    )
    out: dict[str, Any] = {}
    for key in wanted:
        value = number(key)
        if isinstance(value, (int, float, str, bool)) or value is None:
            out[key] = value
    return out


def active_run(store: Any) -> Optional[dict[str, Any]]:
    """What the current run says about itself, or None.

    **Only meaningful alongside the mutex.** A record here with the mutex free
    is a process that died, and `is_indexing` is the function that asks both.
    """
    if store is None:
        return None
    try:
        raw = store.get_state(RUN_STATE_KEY, "") or ""
    except Exception:                    # noqa: BLE001 - a locked store reads as idle
        return None
    if not raw:
        return None
    try:
        found = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def is_indexing(store: Any, *, lock_dir: Any = None) -> bool:
    """Is a run genuinely under way anywhere on this machine?

    Asks the **mutex**, because that is the thing an operating system releases
    when a process dies. The record is only consulted for the words.
    """
    probe = SingleInstance(INDEX_MUTEX_NAME, lock_dir=lock_dir)
    try:
        probe.acquire()
    except AppErrorException:
        return True                      # somebody holds it
    probe.release()
    return False


def describe_holder(store: Any) -> str:
    """A sentence naming who is indexing and since when.

    Falls back to something true but vague rather than inventing detail: the
    record may be missing, from an older version, or from a process that has
    just this moment finished.
    """
    found = active_run(store)
    if not found:
        return "another process"
    owner = str(found.get("owner") or "another process")
    started = found.get("started_at")
    try:
        when = time.strftime("%H:%M", time.localtime(float(started)))
    except (TypeError, ValueError):
        return owner
    return f"{owner}, since {when}"


def _clear(store: Any) -> None:
    if store is None:
        return
    try:
        store.set_state(RUN_STATE_KEY, "")
    except Exception:                    # noqa: BLE001 - shutdown never raises
        return


# ---------------------------------------------------------------------------
# Stopping a run from another process
# ---------------------------------------------------------------------------

def request_stop(store: Any) -> None:
    """Ask whoever is indexing to stop. **A request, not a kill.**

    The run finishes the file it is on and shuts down the way the Stop button
    already does, so everything read so far is kept. Terminating the process
    instead would leave the vector store mid-write, which is the one thing all
    of this exists to prevent.
    """
    if store is None:
        return
    try:
        store.set_state(STOP_STATE_KEY, str(_now()))
    except Exception:                    # noqa: BLE001
        return


def stop_requested(store: Any) -> bool:
    """Polled by the running pipeline, between files."""
    if store is None:
        return False
    try:
        return bool(str(store.get_state(STOP_STATE_KEY, "") or "").strip())
    except Exception:                    # noqa: BLE001 - a read failure is not a stop
        return False


def clear_stop(store: Any) -> None:
    if store is None:
        return
    try:
        store.set_state(STOP_STATE_KEY, "")
    except Exception:                    # noqa: BLE001
        return


# ---------------------------------------------------------------------------
# A second launch, asking the first to come to the front
# ---------------------------------------------------------------------------

def request_front(store: Any) -> None:
    """Ask the running window to come to the front.

    Written by a second launch of the app that found `GUI_MUTEX_NAME`
    already held after waiting out `HANDOVER_WAIT_S` - by then, the
    overwhelmingly likely explanation has stopped being "the copy that was
    just closed has not finished" (that finishes in seconds, well inside
    the wait) and started being "the app is already open, and somebody
    just double-clicked its icon again." Reported live: that used to show
    a splash for the whole wait and then a fatal error, with the already-
    running window never brought forward - exactly the "second copy is
    hard to get to" complaint this exists to fix.

    Never raises: this runs in a process that is exiting either way, and a
    request that failed to write is not a reason to show an error for what
    is, from the person's point of view, an ordinary double-click.
    """
    if store is None:
        return
    try:
        store.set_state(FRONT_STATE_KEY, str(_now()))
    except Exception:                    # noqa: BLE001
        return


def take_front_request(store: Any) -> bool:
    """Was fronting asked for? **And clear it.**

    Taken rather than read, the same shape as `deeplink.take_pending` and
    for the same reason: left in place, the window would front itself again
    on every later poll, stealing focus back from whatever the person moved
    on to after the first time it worked.

    Never raises: this runs on a timer beside a live window.
    """
    if store is None:
        return False
    try:
        raw = store.get_state(FRONT_STATE_KEY, "")
        if not raw:
            return False
        store.set_state(FRONT_STATE_KEY, "")
        return True
    except Exception:                    # noqa: BLE001
        return False
