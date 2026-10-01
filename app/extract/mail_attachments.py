r"""What is read out of a file attached to an email - one rule for every mail reader.

Layer: L2 (extraction). Pure: no I/O beyond the bytes it is handed.

**Owner, 1 October 2026:** *"for mails indexing for zips in mails only index by
name and no ocr on pictures, the zip name and the file names inside the zip
should be indexed, only for office documents contents should be indexed"* - and
PDFs are read too. So an attachment is one of three things:

* **contents** - Word, Excel, PowerPoint and PDF, and plain text, CSV and HTML:
  read through the registry as before, because that is where the text people
  search for lives;
* **names inside** - a zip: its own name and the names of the files in it,
  read from the zip's directory, nothing unpacked;
* **name only** - everything else, pictures included: never opened, never OCR'd.
  It still gets a row of its own, so it is listed on the Files tab and found by
  its name. An *inline* picture - a signature logo, a spacer - gets no row at
  all: it is not something anybody attached, and there are thousands of them.

`pst_libpff` and `email_pst` both ask `rule()`; a loose `.eml` already records
its attachments by name only, inside the message's own text.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Optional

from app.extract.archive import attachment_key
from app.extract.base import Document, SourceKind

__all__ = [
    "CONTENTS", "NAMES_INSIDE", "NAME_ONLY", "CONTENT_EXTENSIONS",
    "rule", "names_inside", "name_only_document",
]

CONTENTS = "contents"
NAMES_INSIDE = "names_inside"
NAME_ONLY = "name_only"

#: Read for their contents: the Office formats the registry reads, and PDF -
#: and, from the owner's second answer the same day, plain text, CSV and HTML,
#: which cost almost nothing to read and are often the whole point of the mail.
CONTENT_EXTENSIONS = frozenset({
    ".docx", ".docm", ".dotx", ".dotm", ".doc", ".dot",
    ".xlsx", ".xlsm", ".xltx", ".xls", ".xlt",
    ".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm", ".ppt", ".pps", ".pot",
    ".pdf",
    ".txt", ".csv", ".html", ".htm",
})

#: The zip family `archive.ArchiveExtractor` reads - the ones whose directory
#: `zipfile` can list without unpacking a byte.
_ZIP_EXTENSIONS = frozenset({".zip", ".jar", ".nupkg", ".whl"})

#: Names listed from one zip. A zip of a hundred thousand files is a build
#: artefact, not correspondence, and its first thousand names say what it is.
MAX_NAMES = 1000


def rule(name: str) -> str:
    """`CONTENTS`, `NAMES_INSIDE` or `NAME_ONLY` for an attachment called `name`."""
    suffix = Path(str(name or "")).suffix.lower()
    if suffix in CONTENT_EXTENSIONS:
        return CONTENTS
    if suffix in _ZIP_EXTENSIONS:
        return NAMES_INSIDE
    return NAME_ONLY


def names_inside(data: bytes) -> list[str]:
    """The member paths of a zip, from its directory. **Never raises.**

    Directories are left out; a damaged or encrypted zip gives what its
    directory still says, or nothing - the zip keeps its own name either way.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return [info.filename for info in archive.infolist()[:MAX_NAMES]
                    if not info.is_dir()]
    except Exception:                                  # noqa: BLE001 - a listing, not a read
        return []


def name_only_document(name: str, message_key: str, *, inside: Optional[list] = None,
                       digest: str = "", backend: str = "") -> Document:
    """A row for an attachment that is not read: its name, and a zip's member names.

    Keyed exactly as a read attachment is (`<message>/attachments/<name>`), so
    it sits beside its message and the Files tab lists it.
    """
    members = [str(member) for member in (inside or ())]
    words = [name, *(member.replace("/", " ") for member in members)]
    meta = {"attachment_of": message_key, "attachment_name": name,
            "contents_read": False}
    if members:
        meta["members"] = len(members)
    if digest:
        meta["content_hash"] = digest
    if backend:
        meta["backend"] = backend
    return Document(
        path=Path(name), text=" ".join(word for word in words if word),
        source_kind=SourceKind.PST_MESSAGE, meta=meta,
        virtual_path=attachment_key(message_key, name, None, None),
    )
