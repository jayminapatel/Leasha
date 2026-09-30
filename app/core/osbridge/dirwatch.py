r"""Asking the operating system to say when something changes in a folder.

Layer: L0 (part of `app.core.osbridge`; used by `app/index/folder_watch.py`)

Work order 0z, item F1. The folder watch wants to know the moment a file is
saved, renamed or deleted under an indexed folder, without walking the folder
to find out. Each operating system has its own way to be told, and only
Windows' is built:

**Windows: `ReadDirectoryChangesW`**, through pywin32 (`win32file`), which is
already a dependency. One open handle on the folder covers the whole tree below
it (`bWatchSubtree`), so watching a folder of a hundred thousand sub-folders
costs the same one handle as watching an empty one. Windows fills a buffer with
`(action, name)` records and hands it over when asked.

* **The buffer can overflow.** A large copy writes thousands of records faster
  than they are read; Windows then throws the whole buffer away and returns
  nothing (zero bytes). That is reported here as one `(OVERFLOW, "")` record -
  never as "nothing happened" - and the caller looks at the whole folder again.
* **The buffer is 64KB and no bigger**, because a larger one fails outright on
  a network share (Microsoft documents the limit as a property of the network
  protocol).
* **The read is "overlapped"** (asynchronous): it is started once and then
  waited on with a time limit, so `close()` from another thread ends the wait
  at once instead of leaving a thread stuck inside Windows for ever.
* **Nothing is opened for writing.** The handle asks only to list the folder,
  and shares reading, writing and deleting with every other program.

  **One cost, measured 2026-09-30 on this machine:** while the handle is open,
  the watched folder itself can still be renamed or deleted, but a folder
  *above* it cannot be renamed or moved - Windows answers "Access is denied"
  to whoever tries, until the watch is closed.

**macOS and Linux: not built.** macOS has FSEvents and Linux has inotify; both
would need a package this project does not carry, so `native_available()` is
False there and the folder watch compares the folder with what it saw last
time instead (`folder_watch.PollingSource`). **(UNCONFIRMED on macOS.)**

Cheap to import: the Windows modules are imported inside the functions that
need them, so importing this can never fail on a Mac.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.osbridge._platform import is_windows

__all__ = [
    "ADDED", "REMOVED", "MODIFIED", "RENAMED_FROM", "RENAMED_TO", "OVERFLOW",
    "BUFFER_BYTES", "DirectoryWatch", "native_available",
]

#: What happened to a name. The words `DirectoryWatch.read` returns, whatever
#: the operating system calls them.
ADDED = "added"
REMOVED = "removed"
MODIFIED = "modified"
RENAMED_FROM = "renamed_from"
RENAMED_TO = "renamed_to"
#: The operating system lost track: look at the whole folder again.
OVERFLOW = "overflow"

#: Windows' own numbers for the five actions (`FILE_ACTION_*` in `winnt.h`).
_ACTIONS = {1: ADDED, 2: REMOVED, 3: MODIFIED, 4: RENAMED_FROM, 5: RENAMED_TO}

#: See the module notes: larger fails on a network share. Fixed, not a setting
#: (non-negotiable 11): an overflow is handled, not avoided, so nobody gains
#: anything by changing this.
BUFFER_BYTES = 64 * 1024

#: `FILE_LIST_DIRECTORY`: the right to list a folder, which is all a watch needs.
_FILE_LIST_DIRECTORY = 0x0001


def native_available() -> bool:
    """True when this system can tell us about changes as they happen.

    Windows with pywin32 importable. False everywhere else, and on a Windows
    install where pywin32 is broken - the caller then compares instead.
    """
    if not is_windows():
        return False
    try:
        import win32event  # noqa: F401
        import win32file  # noqa: F401
    except Exception:                        # noqa: BLE001 - any failure means "no"
        return False
    return True


class DirectoryWatch:
    r"""One folder, watched with everything below it.

        watch = DirectoryWatch(r"D:\Docs")
        while True:
            records = watch.read(1.0)        # None: nothing in that second
            for action, name in records or ():
                ...                          # name is relative to the folder
        watch.close()

    Raises `OSError` from the constructor when the folder cannot be opened,
    and from `read` when the folder has gone (deleted, or its drive removed).
    `close()` may be called from another thread and ends a waiting `read`.
    """

    def __init__(self, root: Any) -> None:
        if not native_available():
            raise OSError("this system has no change notification built")
        import pywintypes
        import win32con
        import win32event
        import win32file

        self.root = str(root)
        self._closed = False
        self._pending = False
        self._flags = (
            win32con.FILE_NOTIFY_CHANGE_FILE_NAME
            | win32con.FILE_NOTIFY_CHANGE_DIR_NAME
            | win32con.FILE_NOTIFY_CHANGE_LAST_WRITE
            | win32con.FILE_NOTIFY_CHANGE_SIZE
            # A cloud file that is downloaded, or sent back to the cloud,
            # changes its attributes and nothing else.
            | win32con.FILE_NOTIFY_CHANGE_ATTRIBUTES
        )
        try:
            self._handle = win32file.CreateFile(
                self.root, _FILE_LIST_DIRECTORY,
                (win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE
                 | win32con.FILE_SHARE_DELETE),
                None, win32con.OPEN_EXISTING,
                (win32con.FILE_FLAG_BACKUP_SEMANTICS
                 | win32con.FILE_FLAG_OVERLAPPED),
                None)
        except pywintypes.error as exc:
            raise OSError(exc.winerror, exc.strerror, self.root) from exc
        self._overlapped = pywintypes.OVERLAPPED()
        self._overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        #: Set by `close()`, so a `read` waiting in another thread returns.
        self._stop = win32event.CreateEvent(None, True, False, None)
        self._buffer = win32file.AllocateReadBuffer(BUFFER_BYTES)
        # Started now rather than at the first `read`: Windows only records
        # changes while a read is outstanding or has been made once, and a
        # file saved between opening and the first read would otherwise be
        # missed (measured).
        self._begin()

    def _begin(self) -> None:
        import pywintypes
        import win32event
        import win32file

        win32event.ResetEvent(self._overlapped.hEvent)
        try:
            win32file.ReadDirectoryChangesW(
                self._handle, self._buffer, True, self._flags, self._overlapped)
        except pywintypes.error as exc:
            raise OSError(exc.winerror, exc.strerror, self.root) from exc
        self._pending = True

    def read(self, timeout_s: float) -> Optional[list[tuple[str, str]]]:
        """Changes since the last call, or None if there were none in time.

        Each record is `(action, name)`, `name` relative to the folder with the
        system's own separator. An overflow is the single record
        `(OVERFLOW, "")`.
        """
        import pywintypes
        import win32event
        import win32file

        if self._closed:
            raise OSError("the watch is closed")
        if not self._pending:
            self._begin()
        found = win32event.WaitForMultipleObjects(
            [self._overlapped.hEvent, self._stop], False,
            max(0, int(timeout_s * 1000)))
        if found != win32event.WAIT_OBJECT_0:
            return None                      # time ran out, or `close()` was called
        self._pending = False
        try:
            count = win32file.GetOverlappedResult(
                self._handle, self._overlapped, True)
        except pywintypes.error as exc:
            raise OSError(exc.winerror, exc.strerror, self.root) from exc
        if count == 0:
            records = [(OVERFLOW, "")]
        else:
            records = [
                (_ACTIONS.get(int(action), MODIFIED), str(name))
                for action, name in win32file.FILE_NOTIFY_INFORMATION(
                    self._buffer, count)
            ]
        # Asked again straight away, so nothing saved while the caller works
        # through these records is lost.
        self._begin()
        return records

    def close(self) -> None:
        """Stop watching and let go of the folder. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        import win32event
        import win32file

        try:
            win32event.SetEvent(self._stop)
        except Exception:                    # noqa: BLE001 - closing never raises
            pass
        try:
            if self._pending:
                win32file.CancelIo(self._handle)
        except Exception:                    # noqa: BLE001
            pass
        try:
            win32file.CloseHandle(self._handle)
        except Exception:                    # noqa: BLE001
            pass

    def __enter__(self) -> "DirectoryWatch":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
