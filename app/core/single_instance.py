"""Single-instance enforcement.

Layer: L0

Two copies of the app cannot share one index safely: SQLite would contend on the
write lock and LanceDB would be corrupted outright. The second copy must refuse
to start, with ERR_DB_LOCKED, rather than discover this later.

Windows uses a named mutex through ctypes rather than pywin32, because pywin32
is an OPTIONAL dependency (it is only needed for PST ingestion) and refusing to
start must never depend on an optional package. A POSIX fallback keeps the test
suite runnable off Windows.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

from app.core.errors import AppErrorException, make_error

__all__ = ["SingleInstance", "DEFAULT_MUTEX_NAME"]

DEFAULT_MUTEX_NAME = "Local.KnowledgeGraph.V2.SingleInstance"

_ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """Hold a system-wide lock for the lifetime of this process.

    Use as a context manager:

        with SingleInstance():
            run_the_app()

    Raises AppErrorException(ERR_DB_LOCKED) if another copy already holds it.
    """

    def __init__(self, name: str = DEFAULT_MUTEX_NAME, lock_dir: Optional[Path] = None):
        self.name = name
        self.lock_dir = lock_dir
        self._handle: Optional[int] = None
        self._fd: Optional[int] = None
        self._lock_file: Optional[Path] = None
        self.acquired = False

    # -- acquisition ---------------------------------------------------------

    def acquire(self) -> "SingleInstance":
        if self.acquired:
            return self
        if sys.platform == "win32":
            self._acquire_windows()
        else:
            self._acquire_posix()
        self.acquired = True
        return self

    def _acquire_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        # "Global\\" would span terminal-server sessions. This app is single
        # user and per session, so the local namespace is correct.
        handle = kernel32.CreateMutexW(None, True, self.name)
        last_error = ctypes.get_last_error()

        if not handle:
            raise AppErrorException(make_error(
                "ERR_DB_LOCKED", "core.single_instance",
                details=f"CreateMutexW failed with error {last_error}",
            ))

        if last_error == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise AppErrorException(make_error(
                "ERR_DB_LOCKED", "core.single_instance",
                details=f"Named mutex '{self.name}' is already held by another process.",
            ))

        self._handle = handle

    def _acquire_posix(self) -> None:
        directory = self.lock_dir or Path(os.environ.get("TMPDIR", "/tmp"))
        directory.mkdir(parents=True, exist_ok=True)
        self._lock_file = directory / f"{self.name}.lock"

        try:
            fd = os.open(self._lock_file, os.O_CREAT | os.O_RDWR)
        except OSError as exc:
            raise AppErrorException(make_error(
                "ERR_DB_LOCKED", "core.single_instance",
                details=f"Could not open lock file {self._lock_file}: {exc}",
            )) from exc

        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError) as exc:
            os.close(fd)
            raise AppErrorException(make_error(
                "ERR_DB_LOCKED", "core.single_instance",
                details=f"Lock file {self._lock_file} is held by another process ({exc}).",
            )) from exc

        os.truncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._fd = fd

    # -- release -------------------------------------------------------------

    def release(self) -> None:
        """Release the lock. Safe to call more than once."""
        if self._handle is not None:
            try:
                import ctypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.ReleaseMutex(self._handle)
                kernel32.CloseHandle(self._handle)
            except Exception:  # noqa: BLE001 - shutdown must never raise
                pass
            self._handle = None

        if self._fd is not None:
            try:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

        if self._lock_file is not None:
            try:
                self._lock_file.unlink()
            except OSError:
                pass
            self._lock_file = None

        self.acquired = False

    # -- context manager -----------------------------------------------------

    def __enter__(self) -> "SingleInstance":
        return self.acquire()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()
