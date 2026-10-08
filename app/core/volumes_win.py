r"""Windows removable-volume identity: the GUID, label and disk serial behind
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
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # `_probe_pool`/`_pool`'s annotations only - the real import stays
    # inside `_pool()`, imported once and reused rather than paying for it
    # at module load on every process that imports this file.
    from concurrent.futures import ThreadPoolExecutor

__all__ = [
    "VolumeIdentity",
    "identify_root",
    "mounted_drive_roots",
    "find_drive_by_guid",
    "hardware_serial_for_root",
    "resolve_unc",
    "probe_unc_reachable",
    "is_bitlocker_locked",
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
        # 2026-10-05, order `offline-drives-on-a-mac`: on a Mac the volume's
        # UUID stands where the GUID does (`osbridge/volumes.py`). Elsewhere
        # None, as before.
        return _identify_macos_root(root)
    text = str(root)
    if not text.endswith("\\"):
        text += "\\"

    try:
        k32 = ctypes.windll.kernel32
        # 261 = MAX_PATH + 1: the buffer size the Windows volume APIs document
        # for a volume GUID path, a label and a file-system name alike.
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


def _identify_macos_root(root: Path) -> Optional[VolumeIdentity]:
    """A Mac volume's identity in this module's own shape; None off macOS.

    `volume_guid` holds `macos-volume:<UUID>`. The field keeps its name: it is
    "the identity a rescan matches against", and every caller reads it as that.
    """
    if sys.platform != "darwin":
        return None
    from app.core.osbridge.volumes import identify_macos_root

    found = identify_macos_root(root)
    if found is None:
        return None
    return VolumeIdentity(volume_guid=found.identity, fs_label=found.label,
                          fs_name=found.fs_name, volume_serial=None)


def _mounted_macos_roots() -> list[Path]:
    if sys.platform != "darwin":
        return []
    from app.core.osbridge.volumes import mounted_macos_roots

    return mounted_macos_roots()


def mounted_drive_roots() -> list[Path]:
    """Every drive letter currently mounted, as `Path("E:\\")`.

    `GetLogicalDrives()` is one call returning a 26-bit mask - cheap enough to
    call on every panel refresh (2a: "checked passively... no device
    watcher"), unlike probing 26 paths with `os.path.exists`, which is 26
    stats including the ones almost certainly absent.
    """
    if sys.platform != "win32":
        # 2026-10-05: on a Mac, the start-up disk and what is in `/Volumes`.
        return _mounted_macos_roots()
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

# ---------------------------------------------------------------------------
# Network shares (order 202626270514 kind=network) - the same "never trust
# the letter" rule, for a mapped drive rather than a removable one.
# ---------------------------------------------------------------------------

#: Windows' own error for "this letter is not a network mapping at all" -
#: the ordinary answer for C:, D:, and every local drive.
_ERROR_NOT_CONNECTED = 2250

#: How long a reachability probe may run before it is treated as "cannot
#: confirm right now" - 1b/1c: unreachable must read as offline, not hang
#: the caller, and an SMB timeout to a genuinely dead server is tens of
#: seconds, not the sub-second cost every other check here pays.
_UNC_PROBE_TIMEOUT_S = 3.0

#: One small, reused pool for reachability probes. Not a `with` block per
#: call: `ThreadPoolExecutor.__exit__` waits for every submitted task to
#: finish, which would make a "hard timeout" call block for the full SMB
#: timeout anyway - exactly the hang 1c exists to prevent. A probe that
#: times out simply abandons its thread to finish on its own; the pool is
#: sized to absorb a realistic number of these without growing unbounded.
_probe_pool: Optional["ThreadPoolExecutor"] = None


def _pool() -> "ThreadPoolExecutor":
    global _probe_pool
    if _probe_pool is None:
        from concurrent.futures import ThreadPoolExecutor

        _probe_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="unc-probe")
    return _probe_pool


def resolve_unc(mapped_letter: str) -> Optional[str]:
    r"""The UNC path behind a mapped drive letter, or None if it is not one.

    **1a: "a mapped Z: is resolved to UNC at add-time and never stored."**
    Called once, when a network source is first catalogued - never kept
    around and re-read later, because the mapping is exactly the kind of
    per-machine, per-session fact the letter itself is.

    `mapped_letter` is a bare letter (`"Z"`), not `"Z:"` or `"Z:\\"` - this
    normalises so a caller can pass whichever form `Path(...).drive` handed
    it.

    Verified on this machine only for the negative case - `ERROR_NOT_CONNECTED`
    (2250) for an ordinary local drive - there being no mapped network drive
    here to confirm the success path against. **(UNCONFIRMED: the success
    path.)** Say so rather than guessing past it.
    """
    if sys.platform != "win32":
        return None
    letter = str(mapped_letter).rstrip("\\/").rstrip(":")
    if not letter or len(letter) != 1:
        return None
    try:
        mpr = ctypes.windll.mpr
        buf = ctypes.create_unicode_buffer(261)
        length = ctypes.c_uint(261)
        rc = mpr.WNetGetConnectionW(
            ctypes.c_wchar_p(f"{letter}:"), buf, ctypes.byref(length)
        )
    except OSError:
        return None
    if rc != 0 or not buf.value:
        return None
    return buf.value


def normalise_unc(path: Path) -> Optional[str]:
    r"""A UNC path as typed (`\\server\share\sub`), normalised to its share
    root (`\\server\share`) - that root is the identity (1a); a sub-path is
    where inside the share the user pointed, not a different source."""
    text = str(path).replace("/", "\\")
    if not text.startswith("\\\\"):
        return None
    parts = [p for p in text.split("\\") if p]
    if len(parts) < 2:
        return None
    return "\\\\" + parts[0] + "\\" + parts[1]


def probe_unc_reachable(unc_root: str,
                        timeout: float = _UNC_PROBE_TIMEOUT_S) -> Optional[bool]:
    r"""Is this share reachable **right now**? None if the answer could not
    be confirmed within budget - which 1b/1c both read the same way as
    False: "offline - not signed in or not reachable", never an error and
    never a retry loop.

    **Never touches credentials.** This is exactly the check Explorer itself
    does when a mapped drive shows a red X - "is the thing there", nothing
    about who is allowed to see it. No prompt, no sign-in dialog: 1b's
    "credentials NEVER" is upheld by this function doing nothing more than
    `os.path.exists` ever could.
    """
    if not unc_root:
        return None
    import os

    future = _pool().submit(os.path.exists, unc_root)
    try:
        return bool(future.result(timeout=timeout))
    except Exception:                              # noqa: BLE001 - includes TimeoutError
        return None


#: How long a BitLocker lock-status probe may run - advisory only, and a
#: hung or missing BitLocker module must never hold up a panel refresh.
_BITLOCKER_TIMEOUT_S = 5.0


def is_bitlocker_locked(letter: str, timeout: float = _BITLOCKER_TIMEOUT_S) -> Optional[bool]:
    r"""Is the volume at this drive letter BitLocker-locked right now?

    **2d: "BitLocker-locked volume = offline-with-reason ('locked')."**
    Called only when a mounted letter's ordinary identity read has already
    failed - `identify_root` returns None for a locked volume, because
    Windows will not hand out a GUID or label for one, and that failure
    looks identical to "nothing is there" without this. `None` means the
    question could not be answered (no BitLocker module, no permission, or
    the probe timed out) - read the same way every other advisory probe
    here is: as "cannot confirm", never as a definite answer either way.

    **Never touches credentials or a recovery key.** This only reads the
    lock state Explorer itself already shows as a padlock icon - the same
    boundary `probe_unc_reachable`'s docstring draws for a network share.
    """
    if sys.platform != "win32":
        return None
    text = str(letter).rstrip("\\/").rstrip(":")
    if not text or len(text) != 1:
        return None
    script = f"(Get-BitLockerVolume -MountPoint '{text}:' -ErrorAction Stop).LockStatus"
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:                              # noqa: BLE001
        return None
    status = (done.stdout or "").strip()
    if not status:
        return None
    return status.lower() == "locked"
