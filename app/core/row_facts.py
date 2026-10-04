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
  file on disk (`.eml`, `.msg`) - and not about a path prefix: `pst://` was
  caught and an `.mbox` or `.olm` message (`D:\x.mbox/123`) was not.
* `message_name` - what a message is called: its subject, or "(no subject)".
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MAIL_SOURCE_KINDS", "MESSAGE_FILE_EXTS", "NO_SUBJECT", "ATTACHMENT_MARKER",
    "format_size", "is_message_row", "has_own_size", "own_size",
    "archived_message_sql", "message_name",
]

#: `files.source_kind` for a message, or for a file attached to one.
MAIL_SOURCE_KINDS: tuple[str, ...] = ("pst_message", "eml")
#: A message that is a file of its own on disk.
MESSAGE_FILE_EXTS: tuple[str, ...] = ("eml", "msg")
#: What a message with a blank subject is called, on every list.
NO_SUBJECT = "(no subject)"
#: How an attachment's key says which message it belongs to - the same marker
#: `presenter.mail.ATTACHMENT_MARKER` and `sqlite_store.LISTED_FILES` read.
ATTACHMENT_MARKER = "/attachments/"

_UNITS = ("B", "KB", "MB", "GB", "TB")


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


def is_message_row(path: Any, source_kind: Any) -> bool:
    """A message itself - mail, and not a file attached to it."""
    return (str(source_kind or "") in MAIL_SOURCE_KINDS
            and ATTACHMENT_MARKER not in str(path or ""))


def has_own_size(path: Any, source_kind: Any) -> bool:
    """False for a message read out of a mail archive (`.pst`, `.mbox`, `.olm`).

    2026-10-04, the owner: such a row is stored with the archive's size, which
    is not the message's. A `.eml` or `.msg` on disk is its own file and keeps
    its size; an attachment has its own (`attachment_size`).
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
            f"AND {a}path NOT LIKE '%{ATTACHMENT_MARKER}%' {files})")


def message_name(subject: Any) -> str:
    """What a message is called on every list: its subject, or "(no subject)".

    Never its key - `pst://...` or an EntryID means nothing to anybody.
    """
    return str(subject or "").strip() or NO_SUBJECT
