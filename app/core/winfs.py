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

**2026-09-27, work order 0x §1b: the code moved.** It now lives in
`app/core/osbridge/cloudfs.py`, the one package allowed to hold
platform-specific code, which also answers the same question on a Mac (iCloud's
"dataless" flag). Nothing changed on Windows. This module stays, re-offering
every name it always had, so the walker, the command line and the window import
it exactly as before.
"""

from __future__ import annotations

# Everything is re-exported under its old name. `__all__` lists exactly what it
# always listed, so `from app.core.winfs import *` gives the same names too.
from app.core.osbridge.cloudfs import (
    CLOUD_PLACEHOLDER_MASK,
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    describe_placeholder,
    file_attributes,
    is_cloud_placeholder,
)

__all__ = [
    "FILE_ATTRIBUTE_OFFLINE",
    "FILE_ATTRIBUTE_RECALL_ON_OPEN",
    "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS",
    "CLOUD_PLACEHOLDER_MASK",
    "file_attributes",
    "is_cloud_placeholder",
    "describe_placeholder",
]
