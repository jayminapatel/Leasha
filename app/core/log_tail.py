r"""Following today's application log file, for the Indexing page's System log tab.

Layer: L0

**Why a file, not the ring.** The window's log pane reads `core.logging`'s
in-memory ring, which holds only what *this* process logged. An index run in a
separate process (`app.index.child_run`, `python -m app.cli index`) logs in that
process, so the window's ring never sees its lines. Both processes call
`setup_logging(settings.log_path)`, so both write the same file,
`<log_path>/app/app_<YYYY-MM-DD>.log` - and following that file is the one
place where every line of a run, whichever process wrote it, can be read.

**Only new lines, read in pieces.** The first poll reads the last
`FIRST_READ_BYTES` of the file (not a whole day's log); each later poll reads
what was appended since. A line still being written (no newline yet) waits for
the next poll. A file that shrank was replaced, and is read from its start.

**Midnight.** The file sinks rotate at "00:00" to a new date-named file, nothing
renamed (`setup_logging`'s docstring says why). When the date changes, the rest
of yesterday's file is read, then today's from its first byte.

**No Qt, and no thread of its own.** `poll()` does file I/O, so the window calls
it on a worker (`widgets/cockpit_tabs.LogTailFeed`), never on the UI thread;
`lines()` only copies what was kept, under a lock, and is safe to call there.

Lines are rewritten from the file format to the console format the log pane
colours (`ui/log_lines`: `HH:mm:ss LEVEL component message`), so a warning from
the indexing process is coloured exactly like one from the window.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from datetime import date
from pathlib import Path
from typing import Callable, Optional

__all__ = ["FIRST_READ_BYTES", "KEEP_LINES", "LogTail", "console_form", "log_file_for"]

#: How much of an existing file the first poll reads: the end of it, not a day.
FIRST_READ_BYTES = 64 * 1024

#: How many lines are kept. The pane shows fewer; this is what it can scroll.
KEEP_LINES = 300

#: `core.logging._FILE_FORMAT`: `2026-10-14 09:30:01.123 | INFO     | ui.index | - | app.x:f:12 | text`.
_FILE_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} (?P<time>\d{2}:\d{2}:\d{2})(?:\.\d+)? \| (?P<level>[A-Z]+)\s*\| "
    r"(?P<component>[^|]*?)\s*\| [^|]*\| [^|]*\| ?(?P<message>.*)$")


def console_form(line: str) -> str:
    """A file-format line in the console's shape; any other line unchanged.

    A traceback's continuation lines carry no prefix and stay as they are.
    """
    found = _FILE_LINE.match(line)
    if found is None:
        return line
    level = found.group("level")
    return (f"{found.group('time')} {level: <8} {found.group('component')} "
            f"{found.group('message')}")


def log_file_for(folder: Path, day: date) -> Path:
    """The application log for `day` in `folder` (the logs' `app` folder)."""
    return Path(folder) / f"app_{day:%Y-%m-%d}.log"


def _default_folder() -> Optional[Path]:
    """The `app` folder under the configured logs folder. Never raises."""
    try:
        from app.core.config import log_dir_for

        return Path(log_dir_for()) / "app"
    except Exception:                            # noqa: BLE001 - nothing to follow
        return None


class LogTail:
    """New lines of today's application log, kept for a pane to show.

    `folder` is the logs' `app` folder; `None` resolves it on the first poll
    (`config.log_dir_for`, which reads `.env` - hence on the worker too).
    `today` is the clock, replaceable in tests.
    """

    def __init__(self, folder: Optional[Path] = None, *,
                 today: Callable[[], date] = date.today,
                 keep: int = KEEP_LINES,
                 first_read: int = FIRST_READ_BYTES) -> None:
        self._folder = Path(folder) if folder is not None else None
        self._resolved = folder is not None
        self._today = today
        self._first_read = int(first_read)
        self._path: Optional[Path] = None
        #: Where the next read starts; `None` before the first read of a file
        #: that existed when following began (read from its tail).
        self._offset: Optional[int] = None
        self._lines: deque = deque(maxlen=int(keep))
        self._lock = threading.Lock()

    @property
    def path(self) -> Optional[Path]:
        """The file being followed, once the first poll has chosen it."""
        return self._path

    def poll(self) -> list[str]:
        """Read what was appended since the last poll; keep and return it.

        **A worker's call** - this reads a file. Never raises: a log that cannot
        be read is a pane that shows nothing new, not a failure.
        """
        try:
            return self._poll()
        except Exception:                        # noqa: BLE001 - see docstring
            return []

    def _poll(self) -> list[str]:
        if not self._resolved:
            self._folder, self._resolved = _default_folder(), True
        if self._folder is None:
            return []
        today = log_file_for(self._folder, self._today())
        new: list[str] = []
        if self._path is None:
            self._path, self._offset = today, None
        elif today != self._path:
            # Midnight: finish yesterday's file, then all of today's is new.
            new += self._read(self._path)
            self._path, self._offset = today, 0
        new += self._read(self._path)
        if new:
            with self._lock:
                self._lines.extend(new)
        return new

    def _read(self, path: Path) -> list[str]:
        """Complete lines of `path` from `_offset`, moving `_offset` past them."""
        try:
            size = path.stat().st_size
        except OSError:
            # Not written yet today: everything that appears in it is new.
            self._offset = 0
            return []
        offset = self._offset
        partial_first = False
        if offset is None:
            offset = max(0, size - self._first_read)
            partial_first = offset > 0
        elif size < offset:
            offset = 0                       # replaced or emptied: start again
        if size <= offset:
            self._offset = offset
            return []
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(size - offset)
        if partial_first:
            cut = data.find(b"\n")
            if cut < 0:
                self._offset = offset
                return []
            data, offset = data[cut + 1:], offset + cut + 1
        end = data.rfind(b"\n")
        if end < 0:
            self._offset = offset            # a line still being written
            return []
        self._offset = offset + end + 1
        text = data[:end + 1].decode("utf-8", errors="replace")
        return [console_form(line.rstrip("\r")) for line in text.split("\n")
                if line.strip()]

    def lines(self, limit: Optional[int] = None) -> list[str]:
        """The kept lines, oldest first - the last `limit` of them. UI-thread safe."""
        with self._lock:
            kept = list(self._lines)
        if limit is not None and limit > 0:
            return kept[-int(limit):]
        return kept
