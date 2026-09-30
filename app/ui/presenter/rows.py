"""The Files and Mail tables' row shapes and filters.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
from typing import Any, Iterable, Mapping, Optional

from app.core.file_state import derive
from app.ui.presenter.formatting import (
    format_address,
    format_recipients,
    format_sent,
    format_size,
    format_when,
    shorten_path,
)

# ---------------------------------------------------------------------------
# Finding a file by its name
#
# A different question from "which document says this", and it deserves a
# different answer: a name, where it lives, how big it is and when it changed.
# No snippet, because there is no match inside the text to show - the match is
# the name itself.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FileRow:
    file_id: int
    name: str
    folder: str
    kind: str
    size: str
    modified: str
    #: The values those two are *formatted from*, so the columns can be sorted
    #: by what they mean rather than by how they read.
    #:
    #: **The lesson `SortableItem` was written for, applied here.** "10 KB"
    #: sorts before "3 KB" and "3 weeks ago" sorts before "yesterday"; the
    #: numbers were thrown away at formatting time, so the Files list could
    #: not have sorted correctly even if it had been allowed to.
    size_bytes: int = 0
    mtime_ns: int = 0
    #: The full path. `folder` is shortened for the column and cannot be
    #: rejoined to it, and the preview pane needs the real thing.
    path: str = ""
    #: Set when the file is in the index but its contents are not searchable -
    #: a scanned PDF, something locked, something too big. Shown rather than
    #: hidden: "I can see the file but cannot search inside it" is a real and
    #: useful thing to know, and hiding it invites the same search twice.
    note: str = ""
    #: Offline Media §3c. `None` for an ordinary file - straight through
    #: from `FileRecord`, the same pair `ResultRow` already carries (see that
    #: field's own comment). Without these, opening or previewing a file
    #: found by browsing to a catalogued volume with `/on` tried `path` -
    #: the letter-free synthetic key, never a real filesystem path - directly,
    #: which is the exact bug 1b/3a's resolution exists to prevent for search
    #: results and had never been extended to this tab.
    volume_id: Optional[int] = None
    relative_path: str = ""
    #: The one-word Status column - `app.core.file_state.derive` over the
    #: row's `status`, `skip_code` and whether its volume is connected.
    status: str = ""


#: What a status means to somebody looking at a list of files.
_STATUS_NOTES = {
    "SKIPPED": "indexed by name only - contents could not be read",
    "FAILED": "could not be read",
    "PENDING": "not indexed yet",
}


def file_rows(rows: Iterable[Mapping[str, Any]], *, now: Optional[float] = None,
              offline_volumes: Any = ()) -> list[FileRow]:
    """Store rows to display rows for the Files list.

    `offline_volumes` is the set of volume ids not connected right now,
    decided on the worker (`tasks.offline_volume_ids`) - never here.
    """
    away = set(offline_volumes or ())
    out: list[FileRow] = []
    for row in rows:
        path = str(row.get("path", ""))
        name = path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path
        folder = path[: len(path) - len(name)].rstrip("/\\") or path
        status = str(row.get("status", ""))
        note = _STATUS_NOTES.get(status, "")
        if status == "SKIPPED" and row.get("skip_code"):
            note = f"{note} ({row['skip_code']})"
        volume_id = row.get("volume_id")
        out.append(FileRow(
            file_id=int(row.get("id", 0)),
            name=name,
            folder=shorten_path(folder, limit=60),
            kind=(str(row.get("ext", "")) or "?").upper(),
            size=format_size(int(row.get("size_bytes", 0))),
            modified=format_when(int(row.get("mtime_ns", 0)), now=now),
            size_bytes=int(row.get("size_bytes", 0)),
            mtime_ns=int(row.get("mtime_ns", 0)),
            path=path,
            note=note,
            volume_id=int(volume_id) if volume_id is not None else None,
            relative_path=str(row.get("relative_path", "") or ""),
            status=derive(status, row.get("skip_code"),
                          offline=volume_id is not None and int(volume_id) in away),
        ))
    return out


#: The most the Files list's total is counted to - `tasks` reads it from here,
#: because the sentence needs it too and `tasks` imports this module. Above it
#: the summary says "more than 10,000": a Files count behind typed text is a
#: union over two full-text indexes, and a common word matches most of a
#: corpus, on a list that searches as somebody types.
LIST_TOTAL_CAP = 10_000
#: The Mail list's, larger on purpose. The owner asked for the total of a
#: mailbox, and a mailbox is tens of thousands of messages; every Mail filter is
#: a header column or the header trigram index, so the count behind a full
#: page is a walk of at most this many index entries, on the worker.
MAIL_TOTAL_CAP = 100_000


def capped_total(shown: int, total: Optional[int], noun: str, hint: str,
                 *, cap: int = LIST_TOTAL_CAP) -> str:
    """The sentence for a list showing one page of more than a page.

    "Showing 500 of 12,431 messages — narrow it with /from, /after …"

    `""` when there is nothing to add: the total is unknown, or the page
    already holds all of it. `total` above `cap` is a bounded count that
    stopped early, and says "more than" rather than a number it does not have.
    """
    if total is None or shown <= 0 or total <= shown:
        return ""
    many = f"more than {cap:,}" if total > cap else f"{total:,}"
    return f"Showing {shown:,} of {many} {noun} — narrow it with {hint}"


def file_summary(total: int, shown: int = -1, text: str = "",
                 found: Optional[int] = None) -> str:
    """The line under the Files table.

    Here rather than in the view because it is three branches choosing a
    sentence, and a branch inside a Qt widget can only be checked by somebody
    typing the right thing at the right moment.
    """
    typed = str(text or "").strip()
    capped = capped_total(shown, found, f"files matching '{typed}'" if typed else "files",
                          "/type, /after, /path …")
    if capped:
        # **The first page is not the whole answer**, and used to be shown as
        # though it were - the fault the owner reported on Mail, here too.
        return capped
    if shown >= 0:
        # **An empty box is browsing, not a search that found nothing.**
        # It used to read "7 file names contain ''", which is both wrong and
        # faintly alarming - the list is now filled on open, so this is the
        # sentence somebody sees first.
        if not str(text or "").strip():
            if not shown:
                return "No files indexed yet — run an index first."
            return (f"{shown:,} file{'s' if shown != 1 else ''}, most recently "
                    f"changed first — type to filter")
        if not shown:
            return f"No file name contains '{text}'."
        return f"{shown:,} file name{'s' if shown != 1 else ''} contain '{text}'"
    if not total:
        return "No file names indexed yet — run an index first."
    return f"{total:,} file names indexed."


# ---------------------------------------------------------------------------
# Mail, as a table
#
# A third question again. A message has no useful filename and no folder, and
# ranking a mailbox by relevance puts an eight-year-old thread above this
# morning's - so mail gets its own columns and its own order.
#
# The columns are the ones people scan for: who, to whom, when, what it was
# called, whether anything was attached, and how big. Everything below is string
# formatting, which is why it lives here and not in the view.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MailRow:
    file_id: int
    sender: str
    recipients: str
    sent: str
    subject: str
    attachment: str
    size: str
    #: The synthetic path for the message. Not shown - nobody typed it and
    #: nobody would recognise it - but the menu needs it to act on the row.
    path: str = ""
    #: What to call this row. **The field name is `name` because that is what
    #: every consumer of a row already reads** - `FileRow` and `ResultRow` both
    #: have one, and the preview pane's heading is `row.name`. Without it a
    #: message was headed with its synthetic path, which is the one string here
    #: that means nothing to anybody.
    name: str = ""
    #: How much of the body was a quoted reply or signature, or `None` for a
    #: message indexed before schema v12. **`None` is not zero** - the preview
    #: says nothing rather than claiming nothing was removed.
    quoted_removed: Optional[int] = None
    #: Sortable originals, so the table can sort by real values rather than by
    #: the formatted strings. "3 KB" and "10 KB" sort the wrong way as text, and
    #: a date column sorted alphabetically is worse than no sorting at all.
    sent_at: int = 0
    size_bytes: int = 0
    has_attachment: bool = False
    #: The one-word Status column - see `FileRow.status`.
    status: str = ""
    #: Order 0z F2. The conversation this message belongs to, as the index
    #: recorded it (`""` when it recorded none), and - only on a row standing
    #: for a folded conversation (`presenter.mail.fold_conversations`) - how
    #: many of the listed messages it stands for, as text and as a number.
    conversation: str = ""
    thread: str = ""
    thread_count: int = 0


def mail_rows(
    rows: Iterable[Mapping[str, Any]], *, now: Optional[float] = None
) -> list[MailRow]:
    """Store rows to display rows for the Mail table."""
    out: list[MailRow] = []
    for row in rows:
        sent_at = row.get("sent_at") or 0
        try:
            sent_at = int(sent_at)
        except (TypeError, ValueError):
            sent_at = 0
        size_bytes = int(row.get("size_bytes") or 0)
        attached = bool(row.get("has_attach"))
        # An empty subject is common and meaningful. Blank looks like a
        # rendering fault; saying so does not.
        subject = str(row.get("subject") or "").strip() or "(no subject)"
        out.append(MailRow(
            file_id=int(row.get("file_id") or 0),
            sender=format_address(row.get("sender")),
            recipients=format_recipients(row.get("recipients")),
            sent=format_sent(sent_at, now=now),
            subject=subject,
            # The same string, under the name every row consumer reads.
            name=subject,
            attachment="Yes" if attached else "",
            size=format_size(size_bytes),
            path=str(row.get("path") or ""),
            sent_at=sent_at,
            size_bytes=size_bytes,
            has_attachment=attached,
            quoted_removed=row.get("quoted_removed"),
            status=derive(row.get("status"), row.get("skip_code")),
            conversation=str(row.get("conversation") or "").strip(),
        ))
    return out


def mail_filters(parsed: Any) -> dict[str, Any]:
    """A `ParsedQuery` as keyword arguments for `store.browse_messages`.

    The whole reason the Mail tab can reuse the `/` commands: the parser already
    produces `senders`, `recipients`, `subjects`, `has_attachment`, `after` and
    `before`, which is precisely the set of columns `messages` has. Nothing new
    had to be invented, and a filter that works in the search box works here
    with the same spelling.

    **Only the first value of each is used.** `from:dave from:priya` is a
    contradiction on a single column - no message has two senders - and taking
    the first is more honest than silently ANDing to zero results.

    Free text is deliberately ignored. `browse_messages` reads `messages` and
    never touches chunk text, so accepting words here would produce an empty
    table for a query that looks reasonable. The view says so instead.
    """
    def first(values: Any) -> Optional[str]:
        items = tuple(values or ())
        return str(items[0]) if items else None

    filters: dict[str, Any] = {
        "sender": first(getattr(parsed, "senders", ())),
        "recipient": first(getattr(parsed, "recipients", ())),
        "subject": first(getattr(parsed, "subjects", ())),
        "has_attachment": getattr(parsed, "has_attachment", None),
        # `/newest` is what this tab already did, so only `/oldest` changes the
        # order - but it is passed either way, because the catalogue offers the
        # switch here and a switch that is offered has to be honoured.
        "sort": str(getattr(parsed, "sort", "") or "") or None,
    }

    # **The file-level switches, which this tab never had.** A message is a row
    # in `messages` joined to the file it came from, so `/type msg`, `/path`,
    # `/name` and `/size` are all answerable here and were simply never asked.
    # Built by the same `file_filter_sql` every other surface uses, so they mean
    # the same thing on this tab as on the others.
    #
    # The mail columns are cleared first, and both reasons matter: `sender` and
    # friends are already handled above through the trigram header index, which
    # is faster than the subquery this would emit; and `after`/`before` belong
    # on `m.sent_at` here - the date a message was *sent* - not on the file's
    # modification time, which for a PST is the date the whole archive last
    # changed and would filter every message in it identically.
    file_where, file_params = "", []
    try:
        from dataclasses import replace as _replace

        from app.storage.filters import file_filter_sql

        file_where, file_params = file_filter_sql(_replace(
            parsed, senders=(), recipients=(), subjects=(),
            has_attachment=None, after=None, before=None,
        ))
    except Exception:                    # noqa: BLE001 - a filter, not the tab
        file_where, file_params = "", []
    if file_where:
        filters["file_where"] = file_where
        filters["file_params"] = file_params

    # Dates arrive as `date` objects and the column holds epoch seconds.
    #
    # **`before` is the last day included, so the bound is the midnight after
    # it.** The store compares `sent_at < ?`, and this used to pass the
    # midnight *starting* that day - so `before:2024-06-01` dropped the 1st,
    # `before:2024` dropped 31 December, and `date:2017-03` lost the 31st of
    # March. Every other surface reads `before` as "on or before", which is
    # what `/before` says it does, and the Mail tab now reads it the same way.
    #
    # **A time of day is a moment, and `sent_at` is whole seconds.** The parser
    # has already resolved a `before` time to the last moment it covers
    # (`10:00` is 10:00:59.999999), so the exclusive bound is the whole second
    # after it; an `after` time is its own first second.
    for name in ("after", "before"):
        value = getattr(parsed, name, None)
        if value is None:
            continue
        if isinstance(value, _datetime):
            seconds = int(_time.mktime(value.timetuple()))
            filters[name] = seconds + 1 if name == "before" else seconds
            continue
        if name == "before":
            value = value + _timedelta(days=1)
        filters[name] = int(
            _time.mktime((value.year, value.month, value.day, 0, 0, 0, 0, 0, -1))
        )

    return {key: value for key, value in filters.items() if value is not None}
