r"""Which disk is this, on a Mac - by the volume's own identity, never by where
it happens to be mounted.

Layer: L0 (part of `app.core.osbridge`)

Work order `offline-drives-on-a-mac` (2026-10-05). On Windows a scanned drive
is remembered by its volume GUID and found again by it, whatever letter it
comes back under (`app/core/volumes_win.py`, order 202626270513). This is the
same thing for macOS: a drive is remembered by its **volume UUID** and found
again by it, whether it comes back as `/Volumes/Photos` or `/Volumes/Photos 1`.
`volumes_win.py` hands the question here when it is asked on a Mac, so nothing
that calls it had to change.

**Two ways of asking, the quick one first.**

  * `getattrlist(2)` with `ATTR_VOL_UUID` is one system call, no new process.
    It matters because "is this drive plugged in" is asked every time a search
    result from a drive is opened or previewed.
  * `diskutil info -plist` is asked only when the system call gives nothing.
    It starts a process, so it is slow, and it is the fall-back, not the rule.

**An identity made here says so.** It is stored as `macos-volume:<UUID>`. A
drive scanned on Windows keeps its Windows GUID, the two never compare equal,
and so one drive scanned on both systems is two sources - the owner's decision
(2026-10-05), not an accident.

**Only a mount point has an identity.** `/Volumes/Photos` does;
`/Volumes/Photos/2019` does not, exactly as `D:\Projects` does not on Windows.
The system call would happily answer for a folder with the UUID of the disk it
is on, which is why the mount-point test comes first.

**Run on GitHub's Mac, not yet on a real one.** `tests/unit/test_mac_volumes.py`
makes real disk images there (APFS, Mac OS Extended, exFAT, FAT32), mounts,
unmounts and remounts them, and checks the identity holds. A USB stick in a
person's hand has not been tried: **UNVERIFIED on a real Mac** until
`docs/MAC_VERIFICATION.md` 5.3 is done.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    "MACOS_IDENTITY_PREFIX",
    "MacVolume",
    "identify_macos_root",
    "mounted_macos_roots",
    "volume_uuid_by_system_call",
    "volume_uuid_by_diskutil",
]

#: What every identity made on a Mac starts with. See the module docstring.
MACOS_IDENTITY_PREFIX = "macos-volume:"

#: Where macOS mounts every disk that is not the start-up disk.
VOLUMES_FOLDER = "/Volumes"

#: How long `diskutil` may take. It is the fall-back and advisory in the same
#: way the Windows serial probe is: a hung tool must never hold up a Scan.
_DISKUTIL_TIMEOUT_S = 8.0

# <sys/attr.h>
_ATTR_BIT_MAP_COUNT = 5
_ATTR_VOL_INFO = 0x80000000
_ATTR_VOL_UUID = 0x00040000


@dataclass(frozen=True)
class MacVolume:
    """What a mounted Mac volume says about itself, right now."""

    identity: str                     # "macos-volume:<UUID>"
    label: Optional[str]              # the name it is mounted under
    fs_name: Optional[str]            # "apfs", "exfat", "msdos", "hfs" ...


def _is_uuid(text: Any) -> bool:
    import uuid

    try:
        return uuid.UUID(str(text)).int != 0
    except (ValueError, AttributeError, TypeError):
        return False


def volume_uuid_by_system_call(root: Any) -> Optional[str]:
    """The volume UUID of the disk `root` is on, through `getattrlist`.

    None when the file system has no UUID to give, when the call fails, or
    anywhere that is not macOS. Never raises.
    """
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util
        import uuid

        class _AttrList(ctypes.Structure):
            _fields_ = [
                ("bitmapcount", ctypes.c_uint16),
                ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32),
                ("volattr", ctypes.c_uint32),
                ("dirattr", ctypes.c_uint32),
                ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32),
            ]

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libSystem.B.dylib",
                           use_errno=True)
        libc.getattrlist.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_size_t, ctypes.c_ulong]
        libc.getattrlist.restype = ctypes.c_int
        wanted = _AttrList(_ATTR_BIT_MAP_COUNT, 0, 0, _ATTR_VOL_INFO | _ATTR_VOL_UUID, 0, 0, 0)
        # The answer: a 4-byte length, then the 16 bytes of the UUID.
        answer = ctypes.create_string_buffer(4 + 16)
        if libc.getattrlist(os.fsencode(str(root)), ctypes.byref(wanted), answer,
                            ctypes.sizeof(answer), 0) != 0:
            return None
        if int.from_bytes(answer.raw[:4], sys.byteorder) < 20:
            return None
        raw = answer.raw[4:20]
        if not any(raw):
            return None
        return str(uuid.UUID(bytes=raw)).upper()
    except Exception:                              # noqa: BLE001 - "cannot tell" is the answer
        return None


def _diskutil_info(root: Any) -> dict:
    """`diskutil info -plist <root>` as a dictionary; `{}` if it cannot be had."""
    if sys.platform != "darwin":
        return {}
    try:
        import plistlib
        import subprocess

        done = subprocess.run(["diskutil", "info", "-plist", str(root)],
                              capture_output=True, timeout=_DISKUTIL_TIMEOUT_S)
        if done.returncode != 0 or not done.stdout:
            return {}
        info = plistlib.loads(done.stdout)
        return info if isinstance(info, dict) else {}
    except Exception:                              # noqa: BLE001
        return {}


def volume_uuid_by_diskutil(root: Any, *, info: Optional[dict] = None) -> Optional[str]:
    """The volume UUID `diskutil` reports for `root`. Slow; the fall-back."""
    info = _diskutil_info(root) if info is None else info
    found = info.get("VolumeUUID")
    return str(found).upper() if _is_uuid(found) else None


def _fs_name(root: str) -> Optional[str]:
    """The file system mounted at `root`, if psutil can say. Advisory only."""
    try:
        import psutil

        for part in psutil.disk_partitions(all=True):
            if part.mountpoint == root:
                return part.fstype or None
    except Exception:                              # noqa: BLE001
        pass
    return None


def identify_macos_root(
    root: Any,
    *,
    is_mount: Optional[Callable[[str], bool]] = None,
    quick: Optional[Callable[[str], Optional[str]]] = None,
    slow: Optional[Callable[[str], Optional[str]]] = None,
    fs_name: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[MacVolume]:
    """The identity of the volume mounted at `root`, or None.

    None for a folder that is not a mount point, for a disk that is not
    mounted, and for a volume that has no UUID by either route - "cannot
    identify it right now", never an error. The keyword arguments are the four
    questions asked of the system, replaceable so this can be tested anywhere.
    """
    try:
        text = str(root)
        if len(text) > 1:
            text = text.rstrip("/")
        if not text.startswith("/"):
            return None                            # `E:\`, a share, a relative path
        if not (is_mount or os.path.ismount)(text):
            return None
        found = (quick or volume_uuid_by_system_call)(text)
        if not _is_uuid(found):
            found = (slow or volume_uuid_by_diskutil)(text)
        if not _is_uuid(found):
            return None
        label = os.path.basename(text) or None
        return MacVolume(identity=MACOS_IDENTITY_PREFIX + str(found).upper(),
                         label=label, fs_name=(fs_name or _fs_name)(text))
    except Exception:                              # noqa: BLE001 - never raises
        return None


def mounted_macos_roots(
    volumes_folder: Any = VOLUMES_FOLDER,
    *,
    is_mount: Optional[Callable[[str], bool]] = None,
) -> list[Path]:
    """Every mount point a drive can be at: the start-up disk, then each
    entry of `/Volumes` that is a mount point.

    One directory listing. The start-up disk also appears in `/Volumes` under
    its name, as a link to `/`; a link is not a mount point, so it is listed
    once. Never raises; an unreadable `/Volumes` is an empty list.
    """
    mounted = is_mount or os.path.ismount
    roots: list[Path] = []
    try:
        if mounted("/"):
            roots.append(Path("/"))
    except OSError:
        pass
    try:
        names = sorted(os.listdir(str(volumes_folder)))
    except OSError:
        return roots
    for name in names:
        candidate = os.path.join(str(volumes_folder), name)
        try:
            if mounted(candidate):
                roots.append(Path(candidate))
        except OSError:
            continue
    return roots
