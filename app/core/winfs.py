"""Windows filesystem attributes, chiefly cloud placeholder detection.

Layer: L0 (used by the Layer 3 walker)

OneDrive and SharePoint sync folders look like ordinary directories, but with
"Files On-Demand" enabled most files are *placeholders*: the metadata is local,
the content is not. Reading one triggers a download of the whole file.

That matters enormously here. Pointing the indexer at a 500GB OneDrive library
with Files On-Demand on would silently hydrate the entire library - filling the
disk, saturating the connection, and taking days. So placeholders are detected
and skipped by default, with an explicit opt-in for people who want them.

The three attributes that matter:

    FILE_ATTRIBUTE_OFFLINE                  0x00001000
    FILE_ATTRIBUTE_RECALL_ON_OPEN           0x00040000
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS    0x00400000

A file "pinned" with 'Always keep on this device' has none of them and is read
normally. A dehydrated placeholder has RECALL_ON_DATA_ACCESS.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

__all__ = [
    "FILE_ATTRIBUTE_OFFLINE",
    "FILE_ATTRIBUTE_RECALL_ON_OPEN",
    "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS",
    "CLOUD_PLACEHOLDER_MASK",
    "file_attributes",
    "is_cloud_placeholder",
    "describe_placeholder",
]

FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

CLOUD_PLACEHOLDER_MASK = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


def file_attributes(path: Path) -> Optional[int]:
    """Raw Windows attribute bits, or None if unavailable.

    `os.stat` exposes `st_file_attributes` on Windows only. Returning None off
    Windows keeps every caller free of platform branches.
    """
    if sys.platform != "win32":
        return None
    try:
        return int(os.stat(path).st_file_attributes)  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return None


def is_cloud_placeholder(path: Path, attributes: Optional[int] = None) -> bool:
    """True if reading this file would trigger a download from the cloud.

    `attributes` can be supplied directly, which is what makes this testable off
    Windows and lets the walker reuse a stat it already performed rather than
    paying for a second one per file.
    """
    bits = attributes if attributes is not None else file_attributes(path)
    if bits is None:
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
