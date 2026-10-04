r"""The facts a row shows that the layers below the window need too. Qt-free.

Layer: L0

2026-10-04, the owner: *"where ever possible the same code should run for
functions so they are all consistent and standard"*. Each function here is the
one answer to one question, read by the presenter (every list, the preview
pane) and by the store and the reports, which cannot import `app.ui`:

* `format_size` - bytes as words, B to TB. There were five copies, one of
  which stopped at GB ("2,048.0 GB") and two of which said "bytes".
* `has_own_size` / `archived_message_sql` - **a message read out of a mail
  archive has no size of its own.** Its row carries the archive's
  (`pipeline._row_type_and_size` keeps it, on purpose), so every list, the
  `/size` filter and the Space report have to agree to ignore it. The rule is
  about the row - a message (`source_kind` mail) whose path is not a message
  file on disk (`.eml`, `.msg`, `.mht`, `.emlx`) - and not about a path
  prefix: `pst://` was caught and an `.mbox` or `.olm` message
  (`D:\x.mbox/123`) was not.
* `message_name` - what a message is called: its subject, or "(no subject)".

2026-10-04, code review: the rest of "what kind of row is this" moved here too,
each from three to six copies that had drifted apart -

* **the file-type families** (`MESSAGE_FILE_EXTS`, `MAIL_ARCHIVE_EXTS`,
  `OUTLOOK_ARCHIVE_EXTS`, `ZIP_FAMILY_EXTS`). The extractors, the walker, the
  activity log, the scan report and the window read these; one copy lacked
  `.olm`, one had `mbx` (which nothing reads), and the message-file list lacked
  `.mht`/`.emlx`, so those files lost their size on every list;
* **the attachment key** (`ATTACHMENT_MARKER`, `attachment_of`,
  `is_mail_attachment`, `listed_files_sql`) - one marker, one parser, and the
  SQL built from them. Python's `in` was case-sensitive and SQL's `LIKE` was
  not, so a zip member under `.../Attachments/...` was a listed attachment to
  the store and a plain file to the window. Both now read the marker exactly
  (`instr`, case and all) and only after the first character, as the parsers
  always did;
* **the mail key** (`is_message_key`, `container_of`, `is_synthetic_path`) -
  decided five ways (`startswith("pst://")`, `"://" in`, an extension list);
* **the date words** (`day_words`, `moment_words`) - two lists said
  "17 September 2023" and the rest "17 Sep 2023".
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

from app.extract.base import SourceKind

__all__ = [
    "MAIL_SOURCE_KINDS", "MESSAGE_FILE_EXTS", "OUTLOOK_ARCHIVE_EXTS", "MAIL_ARCHIVE_EXTS",
    "ZIP_FAMILY_EXTS", "NO_SUBJECT", "ATTACHMENT_MARKER", "MAIL_KEY_SCHEME", "DAY_FORMAT",
    "suffixes", "ext_alternation",
    "format_size", "is_message_row", "has_own_size", "own_size",
    "archived_message_sql", "message_name",
    "attachment_of", "is_mail_attachment", "attachment_sql", "listed_files_sql",
    "is_message_key", "container_of", "is_synthetic_path",
    "day_words", "moment_words",
]

#: `files.source_kind` for a message, or for a file attached to one - the two
#: mail members of `SourceKind`, which mirrors the column.
MAIL_SOURCE_KINDS: tuple[str, ...] = (SourceKind.PST_MESSAGE, SourceKind.EML)
#: A message that is a file of its own on disk: what `EmlExtractor` (`.eml`,
#: `.mht`, `.mhtml`), `MsgExtractor` and `EmlxExtractor` read.
#: `test_row_facts_homes.py` holds this to those readers' own lists.
MESSAGE_FILE_EXTS: tuple[str, ...] = ("eml", "msg", "mht", "mhtml", "emlx")
#: Mail archives Outlook itself opens (`email_pst.OUTLOOK_EXTENSIONS`).
OUTLOOK_ARCHIVE_EXTS: tuple[str, ...] = ("pst", "ost")
#: Every mail archive read one message at a time: Outlook's, an mbox, an
#: Outlook for Mac export. Its messages are keys, never files on disk.
MAIL_ARCHIVE_EXTS: tuple[str, ...] = OUTLOOK_ARCHIVE_EXTS + ("mbox", "olm")
#: Zip-format archives `archive.ArchiveExtractor` reads. **Not `.docx` and
#: friends**, which are zips too but documents with readers of their own.
ZIP_FAMILY_EXTS: tuple[str, ...] = ("zip", "jar", "nupkg", "whl")
#: What a message with a blank subject is called, on every list.
NO_SUBJECT = "(no subject)"
#: How an attachment's key says which message it belongs to:
#: `<message key>/attachments/<name>`, written by `archive.attachment_key`.
ATTACHMENT_MARKER = "/attachments/"
#: The scheme of a message read out of an Outlook archive: `pst://<store>/<id>`.
MAIL_KEY_SCHEME = "pst://"
#: A day as every list writes it: "17 May 2023".
DAY_FORMAT = "%d %b %Y"

_UNITS = ("B", "KB", "MB", "GB", "TB")


def suffixes(exts: Iterable[str]) -> frozenset[str]:
    """`{".zip", ".jar"}` from `("zip", "jar")` - the shape `Path.suffix` has."""
    return frozenset(f".{ext}" for ext in exts)


def ext_alternation(exts: Iterable[str]) -> str:
    """`zip|jar|nupkg|whl`, for a regular expression built from a family."""
    return "|".join(re.escape(ext) for ext in exts)


def format_size(size_bytes: Any) -> str:
    """Bytes as something a person reads: "812 B", "4.2 MB", "2.0 TB".

    Never "1234567 bytes", and no longer "2,048.0 GB": the unit climbs to TB.
    """
    try:
        value = float(max(0, int(size_bytes or 0)))
    except (TypeError, ValueError):
        value = 0.0
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} TB"                    # not reached


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def attachment_of(path: Any) -> tuple[str, str]:
    """`(the parent message's key, the attachment's name)`, or `("", "")` for
    a path that is not an indexed attachment. A rule about a string - no I/O.

    The marker counts only after the first character (a key that *starts*
    with it has no message) and only exactly as written - the same two rules
    `attachment_sql` gives SQLite.
    """
    text = str(path or "")
    index = text.find(ATTACHMENT_MARKER)
    if index <= 0:
        return "", ""
    return text[:index], text[index + len(ATTACHMENT_MARKER):]


def is_mail_attachment(path: Any, source_kind: Any) -> bool:
    """Whether a row is a file that arrived attached to a message."""
    return str(source_kind or "") in MAIL_SOURCE_KINDS and bool(attachment_of(path)[0])


def attachment_sql(alias: str = "f") -> str:
    """`attachment_of(path)[0] != ""` as SQL. `instr` is exact about letter
    case, as Python is; `LIKE` was not. Constants only."""
    a = f"{alias}." if alias else ""
    return f"instr({a}path, '{ATTACHMENT_MARKER}') > 1"


def listed_files_sql(alias: str = "f") -> str:
    """What the Files tab lists, as SQL: files, and the attachments inside mail.

    Owner, 1 October 2026: *"files in emails should come up on the files list
    tab"*. The message itself stays on the Mail tab.
    """
    a = f"{alias}." if alias else ""
    kinds = ", ".join(f"'{kind}'" for kind in MAIL_SOURCE_KINDS)
    return (f"({a}source_kind = '{SourceKind.FILE}' OR ({a}source_kind IN ({kinds})"
            f" AND {attachment_sql(alias)}))")


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

#: A message (or its attachment) inside a mail archive on disk:
#: `D:\x.mbox/123`, `D:\x.olm/Accounts/.../message_00001.xml`. Never a
#: `scheme://` key - a catalogued drive's `leasha-volume://` is not mail.
_IN_A_MAILBOX = re.compile(
    r"^(?P<box>(?![\w.+-]+://).+?\.(?:" + ext_alternation(MAIL_ARCHIVE_EXTS)
    + r")(?:\.bak)?)/(?P<inner>.+)$", re.IGNORECASE)


def container_of(path: Any) -> str:
    r"""The mail archive on disk a key was read out of, or `""`.

    `D:\x.mbox/123` -> `D:\x.mbox`. `""` for anything that is not inside one,
    **and for a `pst://` key**: that names the store by its display name, not
    the file - `messages.store_path` holds where the `.pst` is. An mbox with no
    extension (Apple Mail's `Inbox.mbox\mbox`) is not recognisable from its key
    alone; `is_message_row`, which has the row's `source_kind`, still knows.
    """
    match = _IN_A_MAILBOX.match(str(path or ""))
    return match.group("box") if match else ""


def is_message_key(path: Any) -> bool:
    """A key of something read out of a mail archive - a message or a file
    attached to one - rather than a file on disk. No I/O."""
    text = str(path or "")
    return text.startswith(MAIL_KEY_SCHEME) or bool(container_of(text))


def is_synthetic_path(path: Any) -> bool:
    """Not a path the disk can answer for: any `scheme://` key (a message, a
    catalogued drive's file) or a message inside a mailbox on disk."""
    text = str(path or "")
    return "://" in text or is_message_key(text)


def is_message_row(path: Any, source_kind: Any) -> bool:
    """A message itself - mail, and not a file attached to it."""
    return str(source_kind or "") in MAIL_SOURCE_KINDS and not attachment_of(path)[0]


def has_own_size(path: Any, source_kind: Any) -> bool:
    """False for a message read out of a mail archive (`.pst`, `.mbox`, `.olm`).

    2026-10-04, the owner: such a row is stored with the archive's size, which
    is not the message's. A message file on disk (`.eml`, `.msg`, `.mht`,
    `.emlx`) is its own file and keeps its size; an attachment has its own
    (`attachment_size`).
    """
    if not path or not is_message_row(path, source_kind):
        return True                              # no path: nothing says it is inside one
    return str(path).lower().endswith(tuple(f".{ext}" for ext in MESSAGE_FILE_EXTS))


def own_size(size_bytes: Any, path: Any, source_kind: Any) -> int:
    """The row's own size in bytes, or 0 when it has none (see `has_own_size`)."""
    if not has_own_size(path, source_kind):
        return 0
    try:
        return max(0, int(size_bytes or 0))
    except (TypeError, ValueError):
        return 0


def archived_message_sql(alias: str = "f") -> str:
    """`has_own_size` as SQL: true for a row whose `size_bytes` is its archive's.

    For a `WHERE ... AND NOT <this>`. Constants only, nothing typed by anyone.
    """
    a = f"{alias}." if alias else ""
    kinds = ", ".join(f"'{kind}'" for kind in MAIL_SOURCE_KINDS)
    files = " ".join(f"AND {a}path NOT LIKE '%.{ext}'" for ext in MESSAGE_FILE_EXTS)
    return (f"({a}source_kind IN ({kinds}) "
            f"AND NOT ({attachment_sql(alias)}) {files})")


def message_name(subject: Any) -> str:
    """What a message is called on every list: its subject, or "(no subject)".

    Never its key - `pst://...` or an EntryID means nothing to anybody.
    """
    return str(subject or "").strip() or NO_SUBJECT


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def _local(stamp_ns: Any) -> Any:
    try:
        value = int(stamp_ns or 0)
    except (TypeError, ValueError):
        return None
    return time.localtime(value / 1_000_000_000) if value > 0 else None


def day_words(stamp_ns: Any) -> str:
    """A day from nanoseconds since 1970, as every list writes it: "17 May 2023".
    `""` when there is no date."""
    stamp = _local(stamp_ns)
    return time.strftime(DAY_FORMAT, stamp) if stamp is not None else ""


def moment_words(stamp_ns: Any) -> str:
    """The day and the minute: "17 May 2023, 09:30". `""` when there is none."""
    stamp = _local(stamp_ns)
    return time.strftime(f"{DAY_FORMAT}, %H:%M", stamp) if stamp is not None else ""
