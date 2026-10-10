r"""A lock on an open file that other processes on this computer respect.

Layer: L0 (part of `app.core.osbridge`)

**What it is for.** `app/core/gpu_serialize.py` keeps two graphics-card sessions
from running at once. A `threading.Lock` only does that inside one process, and
since 2026-10-09 Leasha's card work runs in three (the indexer, the OCR helper and
the vision host). A lock on a shared file is the one primitive every system has
that the operating system itself releases when the holder dies - a process killed
mid-hold cannot leave the others waiting for ever.

**One byte, non-blocking.** `msvcrt.locking` locks a byte range from the file's
current position, so the handle is put back at offset 0 first and one byte is the
whole lock; `LK_NBLCK` answers at once rather than retrying for ten seconds. The
other systems use `fcntl.flock`, which locks the whole file. Callers poll.

Never raises for "somebody else holds it" - that is `False`. An `OSError` that is
not contention (a closed handle, a file system that cannot lock) is raised, so the
caller can decide to go on without the lock.
"""

from __future__ import annotations

import errno
from typing import IO, Any

from app.core.osbridge._platform import is_windows

__all__ = ["try_lock", "unlock"]

#: The errors that mean "another holder", not "this cannot work".
_BUSY = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}


def try_lock(handle: IO[Any]) -> bool:
    """Take the lock on `handle` if it is free. `True` when taken, `False` when held."""
    try:
        if is_windows():
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _BUSY:
            return False
        raise
    return True


def unlock(handle: IO[Any]) -> None:
    """Give the lock on `handle` back. Never raises."""
    try:
        if is_windows():
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
