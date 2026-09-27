r"""Is this file really here, or only a stand-in for one kept in the cloud?

Layer: L0 (part of `app.core.osbridge`; used by the Layer 3 walker)

**The problem, in one paragraph.** OneDrive (on Windows) and iCloud Drive (on a
Mac) can both keep a file's *name, size and date* on the computer while the
contents stay online. That stand-in is called a **placeholder**. It looks like
an ordinary file, but the moment a program reads it, the whole file is
downloaded. Point an indexer at a 500GB cloud library full of placeholders and
it would quietly download all 500GB - filling the disk, using up the internet
connection and taking days. So Leasha notices placeholders and skips them
unless the person has said otherwise. `app/core/winfs.py` has always explained
this; its code moved here unchanged (work order 0x §1b) and `winfs` still
offers every name it did.

**Windows** marks a placeholder with *attributes* - bits in a number the file
system keeps for every file:

    FILE_ATTRIBUTE_OFFLINE                  0x00001000
    FILE_ATTRIBUTE_RECALL_ON_OPEN           0x00040000
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS    0x00400000

A file "pinned" with 'Always keep on this device' has none of them and is read
normally. A dehydrated placeholder has RECALL_ON_DATA_ACCESS.

**macOS** marks one with a *flag* instead: `SF_DATALESS` (0x40000000) in the
file's `st_flags`, which Python's `os.stat` reports on a Mac. Apple's
documentation describes it as "file is a dataless object" - the contents live
elsewhere and are fetched on first read. That this is the flag iCloud Drive's
"Optimise Mac Storage" sets, and that `os.stat` sees it without triggering the
download, is **(UNCONFIRMED on macOS)** - it is Apple's documented meaning,
not something run on a real Mac from this code.

**Linux and anything else** has no such marker that we know of, so every file
there is treated as really present. **Not knowing never causes a skip**: a
false skip loses a document silently, which is worse than an unnecessary read.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from app.core.osbridge._platform import is_macos, is_windows

__all__ = [
    "FILE_ATTRIBUTE_OFFLINE",
    "FILE_ATTRIBUTE_RECALL_ON_OPEN",
    "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS",
    "CLOUD_PLACEHOLDER_MASK",
    "SF_DATALESS",
    "file_attributes",
    "file_flags",
    "is_dataless",
    "is_cloud_placeholder",
    "describe_placeholder",
]

FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

#: All three Windows placeholder bits in one number, so "is any of them set?"
#: is a single `&` (bitwise AND) rather than three comparisons.
CLOUD_PLACEHOLDER_MASK = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

#: macOS's "the contents are not on this disk" flag, from Apple's
#: `<sys/stat.h>`. Written out as a number because Python's `stat` module does
#: not name it on every version. (UNCONFIRMED on macOS - see the module notes.)
SF_DATALESS = 0x40000000


def file_attributes(path: Path) -> Optional[int]:
    """Raw Windows attribute bits, or None if unavailable.

    `os.stat` exposes `st_file_attributes` on Windows only. Returning None off
    Windows keeps every caller free of platform branches.
    """
    if not is_windows():
        return None
    try:
        return int(os.stat(path).st_file_attributes)  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return None


def file_flags(path: Path) -> Optional[int]:
    """Raw macOS file flags (`st_flags`), or None if unavailable.

    The Mac counterpart of `file_attributes`: None anywhere else, or when the
    file cannot be read, so the caller never has to ask which system it is on.
    `st_flags` also exists on the BSDs, but only macOS is asked here because
    only its meaning of `SF_DATALESS` is the one described above.
    """
    if not is_macos():
        return None
    try:
        return int(os.stat(path).st_flags)            # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return None


def is_dataless(flags: Optional[int]) -> bool:
    """True if these macOS flags say the contents live in the cloud.

    Takes the number rather than a path, like `is_cloud_placeholder` takes
    `attributes`, so it can be tested on any machine and reuse a stat the
    caller already made. (UNCONFIRMED on macOS.)
    """
    return bool(flags) and bool(flags & SF_DATALESS)


def is_cloud_placeholder(path: Path, attributes: Optional[int] = None) -> bool:
    """True if reading this file would trigger a download from the cloud.

    `attributes` can be supplied directly, which is what makes this testable off
    Windows and lets the walker reuse a stat it already performed rather than
    paying for a second one per file.
    """
    # `attributes`, when given, are always *Windows* attribute bits - that is
    # what every caller has passed since this was written, so it keeps that
    # meaning on every system.
    bits = attributes if attributes is not None else file_attributes(path)
    if bits is None:
        # No Windows attributes: either not Windows, or the stat failed. On a
        # Mac, ask the Mac's own question instead. Everywhere else this is
        # `file_flags` returning None, so the answer stays False as before.
        if attributes is None and is_macos():
            return is_dataless(file_flags(path))
        return False
    return bool(bits & CLOUD_PLACEHOLDER_MASK)


def describe_placeholder(attributes: Optional[int]) -> str:
    """Which attributes are set, for the skip detail. Empty if none are."""
    if not attributes:
        return ""
    names = []
    if attributes & FILE_ATTRIBUTE_OFFLINE:
        names.append("OFFLINE")
    if attributes & FILE_ATTRIBUTE_RECALL_ON_OPEN:
        names.append("RECALL_ON_OPEN")
    if attributes & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
        names.append("RECALL_ON_DATA_ACCESS")
    return "+".join(names)
