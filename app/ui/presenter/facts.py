r"""One function per fact a row shows, read by every list. Qt-free.

Layer: L5. Part of the presenter package; imports no Qt and no store.

2026-10-04, the owner: *"where ever possible the same code should run for
functions so they are all consistent and standard"*, and his decision on how:
**Files, Code and Search show the type badge and the date the Search tab's
way**, the date following the plain/technical setting the Search tab follows;
Mail keeps its exact dates (`format_sent`). Before this, the same row read
differently on each list - "DOCX" here and "DOC" there, "3 weeks ago" on Files
whatever the setting, an attachment dated by its archive, a message named by
its EntryID in the pinned panel.

Each function is the one answer to its question:

| Fact | Function |
|---|---|
| The type badge | `kind_tag` (`presenter.results`) |
| The date to show | `shown_date_ns`, then `date_words` |
| The size | `size_words` (`row_facts.own_size` + `format_size`) |
| A message's name | `message_name` (`app.core.row_facts`) |
| What a row is called | `display_name` |
| An attachment's folder | `attachment_context` |
| A catalogued drive's folder | `volume_folder` |
| A path's folder, anywhere | `folder_words` |
| How many attachments | `attachment_words` |
| Why the contents cannot be searched | `status_note` |

The facts a store-reading worker has to find first - an attachment's message,
a drive's name - are found once per page (`tasks.file_row_context`), never per
row and never on the thread that paints.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.file_state import (
    DEFERRED, FAILED, NAME_ONLY, QUEUED, SKIPPED, TIMED_OUT, derive, explain,
)
from app.core.row_facts import (
    ATTACHMENT_MARKER, NO_SUBJECT, format_size, has_own_size, message_name, own_size,
)
from app.ui.presenter.formatting import _exact_date, breadcrumb, format_address, format_when

__all__ = [
    "NO_SUBJECT", "message_name", "display_name", "shown_date_ns", "date_words",
    "set_date_register", "date_register", "size_words", "attachment_context",
    "volume_folder", "folder_words", "attachment_words", "status_note",
    "UNSEARCHABLE_WORDS", "has_own_size", "own_size",
]

# ---------------------------------------------------------------------------
# The date
# ---------------------------------------------------------------------------

#: The register every list's dates are read in. The Search tab's own setting
#: ("Explain in plain words"), pushed here when it changes
#: (`settings_controller._apply_search_preferences`), so Files and Code follow
#: it without each view being handed it. `"plain"` until told.
_REGISTER = {"value": "plain"}


def set_date_register(register: Any) -> None:
    """`"plain"` or `"technical"` - what `date_words` reads by default."""
    value = str(register or "plain").lower()
    _REGISTER["value"] = "technical" if value == "technical" else "plain"


def date_register() -> str:
    return _REGISTER["value"]


def shown_date_ns(*, mtime_ns: Any = 0, taken_at_ns: Any = 0, sent_at: Any = 0) -> int:
    """The date a row shows, in nanoseconds; 0 when none is known.

    In order: **the message's sent date** (`sent_at`, seconds - for a message,
    or for an attachment, whose own file time is its archive's); **a photo's
    own date** (`taken_at_ns`, the date the Files list is ordered by); then
    the file's modification time.
    """
    for value, scale in ((sent_at, 1_000_000_000), (taken_at_ns, 1), (mtime_ns, 1)):
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        if number > 0:
            return number * scale
    return 0


def date_words(when_ns: Any, *, register: Optional[str] = None,
               now: Optional[float] = None) -> str:
    """A date the Search tab's way: "3 weeks ago" in the plain register, the
    exact date and time in the technical one. `""` for no date."""
    try:
        when = int(when_ns or 0)
    except (TypeError, ValueError):
        when = 0
    if not when:
        return ""
    if str(register or date_register()).lower() == "technical":
        return _exact_date(when)
    return format_when(when, now=now)


# ---------------------------------------------------------------------------
# The size and the name
# ---------------------------------------------------------------------------

def size_words(size_bytes: Any, path: Any = "", source_kind: Any = "file") -> str:
    """The size column: blank for a row with no size of its own - a message
    inside an archive, or an attachment not yet sized - never "0 B"."""
    size = own_size(size_bytes, path, source_kind)
    if not size and str(source_kind or "file") != "file":
        return ""
    return format_size(size)


#: The extensions of a mail archive, whose messages have keys, not names.
_ARCHIVE_EXTS = frozenset({"pst", "ost", "mbox", "mbx", "olm"})


def display_name(name: Any, path: Any, ext: Any = "") -> str:
    """What to call a row: its name, or its file name - **never a message's
    key** (`pst://.../<EntryID>`, `D:\\x.mbox/123`), which means nothing to
    anybody. A message whose name is not known yet is called "(no subject)"
    only once its subject is known to be blank; until then, "Message".
    """
    named = str(name or "").strip()
    if named:
        return named
    text = str(path or "")
    is_key = ATTACHMENT_MARKER not in text and (
        text.startswith("pst://") or str(ext or "").lower() in _ARCHIVE_EXTS)
    if is_key:
        return "Message"
    return text.replace("\\", "/").rstrip("/").rpartition("/")[2] or text


# ---------------------------------------------------------------------------
# Where it is
# ---------------------------------------------------------------------------

def attachment_context(sender: Any, subject: Any) -> str:
    """An attachment's folder: who sent it and what about.

    "from Dave Smith · School trip". Never the `pst://.../attachments` key.
    """
    who = format_address(sender)
    about = str(subject or "").strip()
    context = f"from {who}" if who else ""
    if about:
        context = f"{context} · {about}" if context else about
    return context or "from a message"


def volume_folder(label: Any, relative_path: Any, *, volume_id: Any = None) -> str:
    """A file on a catalogued drive: "<the drive's name> > Photos > 2019".

    The drive's name when it is known, else "drive <n>"; then the folder on it
    the Search tab's breadcrumb way. Never the `leasha-volume://` key.
    """
    name = str(label or "").strip()
    if not name and volume_id is not None:
        name = f"drive {volume_id}"
    relative = str(relative_path or "").replace("\\", "/").strip("/")
    folder = relative.rpartition("/")[0]
    crumb = breadcrumb(folder) if folder else ""
    return " > ".join(part for part in (name, crumb) if part)


def folder_words(path: Any, *, relative_path: Any = "", volume_id: Any = None,
                 label: Any = "") -> str:
    """A row's folder when the row carries none of its own: a drive's folder,
    nothing for an address that is not a path, else the breadcrumb."""
    if volume_id is not None:
        return volume_folder(label, relative_path, volume_id=volume_id)
    text = str(path or "")
    if "://" in text:
        return ""
    name = text.replace("\\", "/").rstrip("/").rpartition("/")[2]
    return breadcrumb(text[: len(text) - len(name)])


# ---------------------------------------------------------------------------
# Counts and notes
# ---------------------------------------------------------------------------

def attachment_words(count: Optional[int] = None, *, has_attach: bool = False) -> str:
    """"3 attachments" when the number is known; "attachments" when only the
    fact is. It used to say "1 attachment" for every message with any."""
    if count:
        return f"{int(count):,} attachment{'s' if int(count) != 1 else ''}"
    return "attachments" if has_attach else ""


#: The Status words that mean "the contents cannot be searched (yet)" - the
#: Files list's note on the name, and the command line's line under a file.
UNSEARCHABLE_WORDS = frozenset({QUEUED, DEFERRED, TIMED_OUT, SKIPPED, FAILED, NAME_ONLY})


#: The Files list's notes as they were before 2026-10-04 - kept (see `status_note`).
_KEPT_NOTES = {
    "SKIPPED": "indexed by name only - contents could not be read",
    "FAILED": "could not be read",
    "PENDING": "not indexed yet",
}


def status_note(status: Any, skip_code: Any = None) -> str:
    """Why a file's contents cannot be searched, in the Status column's words.

    2026-10-04: these notes were their own vocabulary ("not indexed yet")
    beside the Status column's ("Queued"), and disagreed with it for a
    deferred or timed-out file. Now `file_state.explain` of the same word,
    with the reader's code when there is one.
    """
    word = derive(status, skip_code)
    if word not in UNSEARCHABLE_WORDS:
        return ""
    # *Corrected 4 October 2026, the same night:* the existing notes are kept,
    # word for word, wherever they were true - the standing rule says an
    # existing tooltip is never reworded. The Status column's explanation is
    # used only where the old note was wrong: a file whose word differs from
    # its plain status (a picture held for the images pass was "contents
    # could not be read"; it is waiting, not failed).
    if word == derive(status, None) and str(status or "").upper() in _KEPT_NOTES:
        note = _KEPT_NOTES[str(status or "").upper()]
    else:
        note = explain(word)
    code = str(skip_code or "")
    if code and str(status or "").upper() in ("SKIPPED", "FAILED"):
        note = f"{note} ({code})"
    return note
