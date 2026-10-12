r"""Send one file to the Recycle Bin, find it there, and put it back.

Layer: L0 (part of `app.core.osbridge`)

Order 1q D1 (2026-10-11): the Tidy page never deletes a file outright. It sends
it to the Windows Recycle Bin, where Explorer can restore it too, and records
where it went so Leasha can put it back itself (D6).

**Windows.** `SHFileOperationW` with `FOF_ALLOWUNDO` is the call Explorer's own
Delete makes. `FOF_WANTNUKEWARNING` is set because Windows deletes outright,
without the Recycle Bin, a file too big for the bin or on a drive that has
none: with the flag it asks first, and a "no" leaves the file where it is. Only
a fixed local drive is accepted (`on_fixed_drive`), so a USB stick or a network
share - neither of which has a Recycle Bin - is never offered.

Where a file went is read back from the bin itself: each deleted file is a pair
in `<drive>\$Recycle.Bin\<user SID>\` - `$R<id>.<ext>`, the file, and
`$I<id>.<ext>`, a record of its original path and when it was deleted. The `$I`
layout is version 2 on Windows 10 and 11 (8-byte version, 8-byte size, 8-byte
FILETIME, 4-byte name length, then the path in UTF-16) and version 1 before
(the path in a fixed 520 bytes). Putting back is moving `$R` to the original
path and removing its `$I`, which is what Explorer's Restore does.

**macOS (UNCONFIRMED on macOS).** The file is moved into `~/.Trash`, renamed if
the name is taken, and the Trash path is the record.

Anything that fails raises `OSError` with a sentence; the caller counts the file
as left where it was and carries on (non-negotiable 3).
"""

from __future__ import annotations

import os
import secrets
import shutil
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from app.core.osbridge._platform import is_macos, is_windows

__all__ = ["BinRecord", "RecycleBin", "read_bin_record", "LAZY"]

_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400
_FOF_WANTNUKEWARNING = 0x4000
_DRIVE_FIXED = 3
#: FILETIME counts 100 ns steps from 1601; Unix time counts seconds from 1970.
_FILETIME_UNIX_OFFSET = 116444736000000000
#: How long `send` looks for the `$I` record of a file it just moved. Found within 0.11 s
#: in four probes on the owner's D: drive (2026-10-11); one pytest run under load found
#: nothing at once - the record was there 24 s later, most likely held open for a moment.
_FIND_FOR_S = 2.0
#: A record for a file that went to the bin but whose `$I` was not found in time: Put
#: back looks it up again by its original path (`_find`, any time since `deleted at`).
LAZY = "lazy:"


@dataclass(frozen=True)
class BinRecord:
    """One file in the Recycle Bin: where it came from, where it is, when it went."""

    original_path: str
    bin_path: str
    deleted_at: float
    size_bytes: int


def read_bin_record(info_file: Path) -> Optional[BinRecord]:
    """A `$I` file read as a `BinRecord`, or None when it is not one Leasha can read."""
    try:
        data = info_file.read_bytes()
        version, size, filetime = struct.unpack_from("<qqq", data, 0)
        if version == 2:
            (length,) = struct.unpack_from("<i", data, 24)
            raw = data[28:28 + 2 * length]
        elif version == 1:
            raw = data[24:24 + 520]
        else:
            return None
        original = raw.decode("utf-16-le").split("\x00", 1)[0]
    except (OSError, struct.error, UnicodeDecodeError):
        return None
    bin_path = info_file.with_name("$R" + info_file.name[2:])
    when = (filetime - _FILETIME_UNIX_OFFSET) / 10_000_000
    return BinRecord(original, str(bin_path), when, int(size))


class RecycleBin:
    """The real Recycle Bin, with a prepare/send/restore contract for durable undo."""

    def on_fixed_drive(self, path: str) -> bool:
        """True when `path` is on a drive that has a Recycle Bin Leasha will use."""
        if is_windows():
            import ctypes

            root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
            if root.startswith("\\\\"):
                return False                      # a network share has no Recycle Bin
            return ctypes.windll.kernel32.GetDriveTypeW(root) == _DRIVE_FIXED
        return is_macos() and Path(path).is_absolute()

    def prepare(self, path: str) -> str:
        """A restore reference that can be durably journalled before the OS move."""
        if is_windows():
            return f"{LAZY}{time.time() - 2:.3f}"
        if is_macos():
            return str(Path.home() / ".Trash" / (secrets.token_hex(16) + "-" + Path(path).name))
        raise OSError("Moving to the Recycle Bin works on Windows and macOS only.")

    def send(self, path: str) -> str:
        return self.send_prepared(path, self.prepare(path))

    def send_prepared(self, path: str, ref: str) -> str:
        """Move using the previously journalled restore reference."""
        started = time.time()
        if is_windows():
            try:
                self._send_windows(path)
            except OSError:
                if os.path.exists(path):
                    raise
            deadline = started + _FIND_FOR_S
            while True:
                record = self._find(path, since=float(ref[len(LAZY):]))
                if record is not None:
                    return record.bin_path
                if os.path.exists(path):
                    raise OSError(f"Windows left {path} where it was.")
                if time.time() >= deadline:
                    return ref
                time.sleep(0.05)
        if is_macos():
            if os.path.exists(ref):
                raise OSError("The reserved Trash location is occupied; try again.")
            shutil.move(path, ref)
            return ref
        raise OSError("Moving to the Recycle Bin works on Windows and macOS only.")

    def put_back(self, bin_ref: str, path: str) -> None:
        """Return the file at `bin_ref` to `path`. Never over another file."""
        if bin_ref.startswith(LAZY) and is_windows():
            record = self._find(path, since=float(bin_ref[len(LAZY):]))
            if record is None:
                raise OSError("It is no longer in the Recycle Bin - the bin may have been emptied.")
            bin_ref = record.bin_path
        if os.path.exists(path):
            raise OSError(f"Another file is at {path} now, so it was left in the Recycle Bin.")
        if not os.path.exists(bin_ref):
            raise OSError("It is no longer in the Recycle Bin - the bin may have been emptied.")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        os.replace(bin_ref, path)
        info = Path(bin_ref).with_name("$I" + Path(bin_ref).name[2:])
        if is_windows() and Path(bin_ref).name.startswith("$R") and info.exists():
            info.unlink()

    # -- Windows ---------------------------------------------------------------

    def _send_windows(self, path: str) -> None:
        import ctypes
        from ctypes import wintypes

        class _SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                        ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                        ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

        op = _SHFILEOPSTRUCTW()
        op.wFunc = _FO_DELETE
        op.pFrom = os.path.abspath(path) + "\x00"   # the API wants the list double-NUL ended
        op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
                     | _FOF_WANTNUKEWARNING)
        code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if code != 0:
            raise OSError(f"Windows could not move {path} to the Recycle Bin (code {code:#x}).")
        if op.fAnyOperationsAborted:
            raise OSError(f"{path} was left where it was: moving it was cancelled.")

    def _find(self, path: str, *, since: float) -> Optional[BinRecord]:
        """The newest `$I` record for `path` deleted after `since`, in this user's bin."""
        wanted = os.path.normcase(os.path.abspath(path))
        best: Optional[BinRecord] = None
        for record in self._records(path):
            if os.path.normcase(record.original_path) == wanted and record.deleted_at >= since:
                if best is None or record.deleted_at > best.deleted_at:
                    best = record
        return best

    def _records(self, path: str) -> Iterator[BinRecord]:
        root = Path(os.path.splitdrive(os.path.abspath(path))[0] + "\\") / "$Recycle.Bin"
        try:
            folders = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            return
        for folder in folders:                    # only this user's own folder is readable
            try:
                infos = list(folder.glob("$I*"))
            except OSError:
                continue
            for info in infos:
                record = read_bin_record(info)
                if record is not None:
                    yield record


def _free_name(target: Path) -> Path:
    stem, suffix, n = target.stem, target.suffix, 1
    while target.exists():
        n += 1
        target = target.with_name(f"{stem} {n}{suffix}")
    return target
