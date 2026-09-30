r"""The machine's own two locks are never taken by a test. Not a test module.

Layer: test support.

**What was wrong (2026-09-30).** Leasha has two machine-wide locks, both named
Windows mutexes: `GUI_MUTEX_NAME` (one window) and `INDEX_MUTEX_NAME` (one
index run). The tests took them under those same names, so:

* with Leasha open, every test that needed the window's lock failed (the
  Layer 0 "a second copy refuses to start" acceptance test did, every time);
* with an index run going, every test that needed the run lock would fail -
  and, worse, **a test run could stop the owner's real run from starting**,
  because for as long as a test held the name the real run was refused.

The tests believed they were isolated. They pass `lock_dir=` or set `TMPDIR`,
which on Linux and macOS is where the lock *file* goes, so there it works. A
Windows mutex has no folder: `SingleInstance._acquire_windows` never reads
`lock_dir`. The isolation was real on the machine the tests were written on
and absent on the one the application is for.

**What this does.** Inside a test process, a lock asked for under one of the
real names is given that name plus a suffix unique to this test run. Every
test still excludes every other exactly as before - they all get the *same*
private name - but nothing the owner's Leasha holds can fail a test, and
nothing a test holds can be seen by Leasha.

**Why here and not in the application.** The obvious shortcut is an
environment variable the application reads to rename its locks. That is a way
to switch the lock off by accident: two copies started from two environments
would hold two different names, both would write, and two writers corrupt the
vector store - the one thing the lock exists to prevent. So the application
is not changed and cannot be told to do this; the test process does it to
itself, and a child process that a test starts does it by being started
through this module:

    python -m tests.private_locks app.cli index ...

which installs the same suffix (handed down in `ENV`) and then runs `app.cli`
exactly as `python -m app.cli` would.

`real()` is the one way to take a real name from a test, and exists so that
one test can hold the real lock and prove that nothing else notices.
"""

from __future__ import annotations

import os
import runpy
import sys
import uuid
from typing import Any, Optional

__all__ = ["ENV", "install", "installed_suffix", "private_name", "real",
           "through_private_locks"]

#: Carries the suffix from the test process to the children it starts. Read
#: only by this module - the application never looks at it.
ENV = "LEASHA_TEST_LOCK_SUFFIX"

#: How a child is started so that it, too, leaves the real locks alone.
MODULE = "tests.private_locks"

_suffix: Optional[str] = None
_original_init: Any = None


def _real_names() -> frozenset:
    from app.core.run_lock import GUI_MUTEX_NAME, INDEX_MUTEX_NAME
    from app.core.single_instance import DEFAULT_MUTEX_NAME

    return frozenset({GUI_MUTEX_NAME, INDEX_MUTEX_NAME, DEFAULT_MUTEX_NAME})


def installed_suffix() -> Optional[str]:
    """The suffix in force in this process, or None if `install` has not run."""
    return _suffix


def private_name(name: str) -> str:
    """`name` as this test run knows it. Only the real names are changed."""
    if _suffix and name in _real_names():
        return f"{name}{_suffix}"
    return name


def install(suffix: Optional[str] = None) -> str:
    """Give this process private names for the two real locks. Returns the suffix.

    Safe to call twice. The suffix comes from `ENV` when a parent test process
    set it, so a child shares its parent's names - a child's run lock must
    still be the one its parent's `is_indexing` asks about.
    """
    global _suffix, _original_init

    from app.core.single_instance import DEFAULT_MUTEX_NAME, SingleInstance

    if _suffix is None:
        _suffix = (suffix or os.environ.get(ENV)
                   or f".pytest-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    os.environ[ENV] = _suffix

    if _original_init is None:
        _original_init = SingleInstance.__init__

        def __init__(self, name: str = DEFAULT_MUTEX_NAME, lock_dir: Any = None) -> None:
            _original_init(self, private_name(name), lock_dir)

        SingleInstance.__init__ = __init__
    return _suffix


def real(name: str, lock_dir: Any = None) -> Any:
    """A `SingleInstance` under the machine's real `name`, not yet acquired.

    For the one test that holds the real lock to prove the rest do not care.
    Hold it for as short a time as the proof needs: while it is held, the
    owner's own Leasha sees it.
    """
    from app.core.single_instance import SingleInstance

    lock = SingleInstance.__new__(SingleInstance)
    (_original_init or SingleInstance.__init__)(lock, name, lock_dir)
    return lock


def through_private_locks(argv: list) -> list:
    """`python -m app.cli ...` as `python -m tests.private_locks app.cli ...`.

    Any other command comes back unchanged: only the application's own entry
    point takes the locks, and a fake child has nothing to isolate.
    """
    argv = list(argv)
    if argv[1:3] == ["-m", "app.cli"]:
        return [argv[0], "-m", MODULE, "app.cli", *argv[3:]]
    return argv


def _install_for_children() -> None:
    """Every index run started as a child process goes through this module.

    `ChildIndexRun` is how the window (and the benchmark) runs
    `python -m app.cli index` in a process of its own. That child is the real
    indexer and takes the real run lock, so the command is rewritten where
    every one of them is built.
    """
    from app.index.child_run import ChildIndexRun

    if getattr(ChildIndexRun.__init__, "_private_locks", False):
        return
    original = ChildIndexRun.__init__

    def __init__(self, argv: list, *args: Any, **kwargs: Any) -> None:
        original(self, through_private_locks(argv), *args, **kwargs)
        if self.env is not None and _suffix:
            self.env.setdefault(ENV, _suffix)

    __init__._private_locks = True           # type: ignore[attr-defined]
    ChildIndexRun.__init__ = __init__


def install_everywhere() -> str:
    """`install`, plus the children. What the session fixture calls."""
    suffix = install()
    _install_for_children()
    return suffix


if __name__ == "__main__":
    # python -m tests.private_locks <module> [arguments...]
    #
    # Installed through the imported module, not this `__main__` copy of it:
    # they are two module objects, and the suffix must live in the one that
    # anything else in this process would import.
    from tests import private_locks as _this_module

    _this_module.install_everywhere()
    target = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_module(target, run_name="__main__", alter_sys=True)
