"""Small shared formatters: sizes, dates, counts, addresses and breadcrumbs.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

import time as _time
from typing import Any, Optional


def shorten_path(path: str, *, limit: int = 70) -> str:
    """Elide the middle of a long path, keeping the drive and the filename.

    The two ends carry the information: which drive it is on, and what it is
    called. The middle is usually a folder hierarchy the person already knows.
    """
    if len(path) <= limit:
        return path
    separator = "\\" if "\\" in path else "/"
    parts = path.split(separator)
    if len(parts) <= 2:
        return path[: limit - 1] + "…"

    head, tail = parts[0], parts[-1]
    if len(head) + len(tail) + 5 >= limit:
        return f"{head}{separator}…{separator}{tail[-(limit - len(head) - 3):]}"

    middle: list[str] = []
    budget = limit - len(head) - len(tail) - 5
    for part in reversed(parts[1:-1]):
        if len(part) + 1 > budget:
            break
        middle.insert(0, part)
        budget -= len(part) + 1
    return separator.join([head, "…", *middle, tail])


def format_count(value: int) -> str:
    return f"{value:,}"


def format_eta(remaining: int, *, files_per_minute: float) -> str:
    """A human ETA from a measured rate.

    Deliberately vague past an hour. A progress bar claiming "2 hours 14 minutes"
    on a rate measured over the last thirty seconds is precision the number does
    not have, and being visibly wrong about it costs more trust than saying
    "about 2 hours" and being right.
    """
    if remaining <= 0:
        return "done"
    if files_per_minute <= 0:
        return "estimating…"

    minutes = remaining / files_per_minute
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return f"about {round(minutes)} minute{'s' if round(minutes) != 1 else ''}"

    hours = minutes / 60
    if hours < 24:
        return f"about {round(hours)} hour{'s' if round(hours) != 1 else ''}"
    return f"about {round(hours / 24)} day{'s' if round(hours / 24) != 1 else ''}"


#: How many folders of a path to show in the breadcrumb. The last few are the
#: ones that distinguish; `D:\Archive` is the same for everything.
BREADCRUMB_PARTS = 3


def breadcrumb(path: str, *, parts: int = BREADCRUMB_PARTS) -> str:
    r"""A path as `Archive > 2019 > Leeds`, keeping the end rather than the start.

    **`shorten_path` elides the middle, which is where the distinguishing part
    of a long archive path lives.** A person recognises the last two or three
    folders; nobody scans `D:\Archive\2019\Projects\...`. Keeping the tail is
    the same instinct as showing a filename before its directory.
    """
    cleaned = (path or "").replace("\\", "/").strip("/")
    if not cleaned:
        return ""
    pieces = [piece for piece in cleaned.split("/") if piece]
    # A drive letter on its own is not a folder anybody thinks in.
    if pieces and pieces[0].endswith(":"):
        pieces = pieces[1:]
    if not pieces:
        return ""
    tail = pieces[-parts:]
    prefix = "… > " if len(pieces) > parts else ""
    return prefix + " > ".join(tail)


def format_size(size_bytes: int) -> str:
    """Bytes as something a person reads. Never "1234567 bytes"."""
    value = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} GB"


def format_when(mtime_ns: int, *, now: Optional[float] = None) -> str:
    """A modification time as an age, because that is what gets compared.

    "Yesterday" and "3 weeks ago" answer "is this the version I was working on"
    instantly; "2026-08-03 14:22:07" requires arithmetic. Beyond a year the
    date is more useful than the age, so it switches over.

    **Zero means "not known", and produces nothing.** It used to produce
    "01 Jan 1970", which is not a fallback but a claim - and a false one that
    looks entirely plausible in a fifteen-year archive. A row with no date is
    honest; a row dated 1970 sends somebody looking for a file that does not
    exist.
    """
    if not mtime_ns:
        return ""
    seconds = (now if now is not None else _time.time()) - (mtime_ns / 1_000_000_000)
    if seconds < 0:
        return "just now"          # a clock skew, or a file from the future
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86_400:
        hours = int(seconds // 3600)
        return f"{hours} hour ago" if hours == 1 else f"{hours} hours ago"
    days = int(seconds // 86_400)
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        weeks = days // 7
        return f"{weeks} week ago" if weeks == 1 else f"{weeks} weeks ago"
    return _time.strftime("%d %b %Y", _time.localtime(mtime_ns / 1_000_000_000))


def _exact_date(mtime_ns: int) -> str:
    """The precise moment `format_when` blurs into an age - item 4b.

    Always available regardless of register, because the tooltip promises it
    whichever way the visible row is reading.
    """
    if not mtime_ns:
        return ""
    return _time.strftime("%d %b %Y, %H:%M", _time.localtime(mtime_ns / 1_000_000_000))


def _exact_date_from_epoch(epoch_seconds: Any) -> str:
    """The same exact format as `_exact_date`, from a mail `sent_at` (seconds,
    not nanoseconds)."""
    try:
        seconds = int(epoch_seconds)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return _time.strftime("%d %b %Y, %H:%M", _time.localtime(seconds))


#: How many recipients to name before summarising. Long enough to recognise a
#: two-person thread at a glance, short enough that a message to a distribution
#: list does not push every other column off the screen.
RECIPIENTS_SHOWN = 2


def format_address(value: Any) -> str:
    """One address, as short as it can be without becoming ambiguous.

    `Dave Smith <dave@acme.com>` becomes `Dave Smith`, because in a column of
    thirty rows the name is what distinguishes them and the domain is usually
    the same for all thirty. A bare address is left alone - there is nothing to
    shorten to.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "<" in text and text.endswith(">"):
        name = text.split("<", 1)[0].strip().strip('"').strip()
        if name:
            return name
    return text


def format_recipients(value: Any, *, shown: int = RECIPIENTS_SHOWN) -> str:
    """The recipients column, from the JSON array the store holds.

    **Never raises on bad input.** This runs over every row of a mailbox that
    may hold two hundred thousand messages written by a decade of different
    clients; one malformed field must cost that row its column, not the table.
    """
    import json

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                return text          # not JSON after all; show what is there
        else:
            return text

    if not isinstance(value, (list, tuple)):
        return str(value or "")

    names = [format_address(item) for item in value]
    names = [name for name in names if name]
    if not names:
        return ""
    if len(names) <= shown:
        return ", ".join(names)
    return f"{', '.join(names[:shown])} +{len(names) - shown}"


def format_sent(sent_at: Any, *, now: Optional[float] = None) -> str:
    """A date a person can scan a column of.

    Absolute, not "3 days ago". Relative time reads well for a single file but
    badly down a sorted column, where the eye is looking for a boundary between
    March and April and finds "5 weeks ago" instead.
    """
    try:
        seconds = int(sent_at)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""

    reference = now if now is not None else _time.time()
    stamp = _time.localtime(seconds)
    # Within the last year, the year is noise - the month and day carry it, and
    # the time of day is what separates messages sent the same afternoon.
    if 0 <= reference - seconds < 365 * 86_400:
        return _time.strftime("%d %b %H:%M", stamp)
    return _time.strftime("%d %b %Y", stamp)
