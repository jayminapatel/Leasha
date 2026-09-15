"""Windows removable-volume identity: the GUID, label and disk serial behind
a drive letter - never the letter itself.

Layer: L1 (read by storage/CLI at Scan/Rescan time; L3's walker never calls
this directly - see `app/index/offline_media.py`, which resolves a
catalogued volume's current mount point once per run and hands the walker a
plain root, the same shape `WalkConfig.roots` always took).

**Why the letter is never trusted.** Windows assigns drive letters at mount
time from whatever is free, so the same physical drive is `E:` today and `F:`
tomorrow, and a different drive can legitimately become `E:` in between. The
owner's explicit requirement - "drive letters are NEVER stored, assume the
letter is different every plug-in" - is not a preference here, it is the
thing that makes catalogue-then-unplug behave correctly at all.

Two identifiers, for two different jobs:

  * `volume_guid` (`\\?\Volume{...}\`) is the **primary identity**. NTFS/ReFS
    assigns it once, at format time, and it survives every remount, on any
    machine, at any letter. This is what a rescan matches against.
  * `hardware_serial` is **advisory only** - the physical disk's serial via
    `Get-PhysicalDisk`, used for "this looks like <name> reformatted" (1a).
    Reformatting a drive gives it a *new* `volume_guid`; the hardware serial
    is what lets Leasha still recognise it and offer, never assume, that it
    is the same physical object under a new catalogue entry.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = [
    "VolumeIdentity",
    "identify_root",
    "mounted_drive_roots",
    "find_drive_by_guid",
    "hardware_serial_for_root",
]

#: How long a `Get-Partition`/`Get-PhysicalDisk` probe may run. Advisory data
#: only - a slow or hung WMI provider must never hold up a Scan.
_SERIAL_TIMEOUT_S = 8.0


@dataclass(frozen=True)
class VolumeIdentity:
    """What a currently-mounted root can tell us about itself, right now."""

    volume_guid: Optional[str]
    fs_label: Optional[str]
    fs_name: Optional[str]
    volume_serial: Optional[str]      # the format-time DWORD serial, as hex


def identify_root(root: Path) -> Optional[VolumeIdentity]:
    """The volume GUID, label and format-time serial for a mounted root.

    `root` must already be reachable - a drive letter that is not currently
    mounted simply fails the Windows call and this returns None, which is
    exactly "cannot identify it right now" rather than an error: the caller
    (Scan) only ever calls this on a root the user just picked from a live
    file dialog, so unreachable here means it was unplugged mid-click.
    """
    if sys.platform != "win32":
        return None
    text = str(root)
    if not text.endswith("\\"):
        text += "\\"

    try:
        k32 = ctypes.windll.kernel32
        guid_buf = ctypes.create_unicode_buffer(261)
        guid_ok = k32.GetVolumeNameForVolumeMountPointW(
            ctypes.c_wchar_p(text), guid_buf, ctypes.c_uint(261)
        )
        guid = guid_buf.value if guid_ok else None

        label_buf = ctypes.create_unicode_buffer(261)
        fs_buf = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint(0)
        max_len = ctypes.c_uint(0)
        flags = ctypes.c_uint(0)
        info_ok = k32.GetVolumeInformationW(
            ctypes.c_wchar_p(text), label_buf, ctypes.c_uint(261),
            ctypes.byref(serial), ctypes.byref(max_len), ctypes.byref(flags),
            fs_buf, ctypes.c_uint(261),
        )
        if not guid_ok and not info_ok:
            return None
        return VolumeIdentity(
            volume_guid=guid,
            fs_label=(label_buf.value or None) if info_ok else None,
            fs_name=(fs_buf.value or None) if info_ok else None,
            volume_serial=f"{serial.value:08X}" if info_ok else None,
        )
    except OSError:
        return None


def mounted_drive_roots() -> list[Path]:
    """Every drive letter currently mounted, as `Path("E:\\")`.

    `GetLogicalDrives()` is one call returning a 26-bit mask - cheap enough to
    call on every panel refresh (2a: "checked passively... no device
    watcher"), unlike probing 26 paths with `os.path.exists`, which is 26
    stats including the ones almost certainly absent.
    """
    if sys.platform != "win32":
        return []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except OSError:
        return []
    roots = []
    for i in range(26):
        if mask & (1 << i):
            roots.append(Path(f"{chr(ord('A') + i)}:\\"))
    return roots


def find_drive_by_guid(volume_guid: str) -> Optional[Path]:
    """Which currently-mounted letter, if any, is this volume right now.

    This is the whole of "resolution happens at the last moment via the
    current mount point" (1b) for kind=drive: never cached beyond one call,
    so a drive unplugged and replugged under a new letter resolves correctly
    without anything having to notice the change happened.
    """
    if not volume_guid:
        return None
    for root in mounted_drive_roots():
        identity = identify_root(root)
        if identity is not None and identity.volume_guid == volume_guid:
            return root
    return None


def hardware_serial_for_root(root: Path,
                             timeout: float = _SERIAL_TIMEOUT_S) -> Optional[str]:
    r"""The physical disk's serial behind a mounted root. Best-effort, slow-safe.

    Via `Get-Partition`/`Get-PhysicalDisk`, the same admin-free route
    `compute_profile._windows_disk_kind` already uses for the same reason:
    `IOCTL_STORAGE_QUERY_PROPERTY` needs a handle to the physical drive, which
    needs elevation on some systems, and a detection that fails without
    administrator rights is a detection that fails on most machines.

    Never raises. A hung or missing WMI provider is a real Windows failure
    mode and must not turn a Scan into a permanently stuck button - this is
    advisory data (1a: "hardware serial via WMI (reformat recognition)"),
    never the identity a rescan depends on.
    """
    if sys.platform != "win32":
        return None
    letter = str(root).rstrip("\\/").rstrip(":")
    if not letter or len(letter) != 1:
        return None
    script = (
        f"$p = Get-Partition -DriveLetter {letter} -ErrorAction Stop; "
        "(Get-PhysicalDisk -ErrorAction Stop | "
        "Where-Object DeviceId -eq $p.DiskNumber).SerialNumber"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:                             # noqa: BLE001
        return None
    serial = (done.stdout or "").strip()
    return serial or None