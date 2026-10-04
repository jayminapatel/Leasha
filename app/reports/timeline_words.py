r"""What the Life Timeline says, in plain words - one place, no Qt.

Layer: L4 (reports)

The command line (`leasha timeline`), the window and the tests all read the
same sentences, so they cannot drift apart. Nothing here decides anything about
*what* is on the timeline (`app/reports/timeline.py` does); it only words it.

**Every string here is for somebody who has never heard of Leasha's internals.**
There is a deny-list test (`test_timeline_wording.py`) that sweeps them: no
"hash", no "vector", no "embedding". A date is *taken*, *sent*, *guessed from a
folder name* or *the file's own date* - and the last two are said plainly to
be weaker than the first two, because they are.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

__all__ = [
    "BASIS_WORDS", "BASIS_TIPS", "KIND_WORDS", "KIND_TIPS",
    "basis_words", "basis_tip", "badge_words", "period_words", "day_heading",
    "summary_sentence", "thin_data_notes", "empty_period_sentence", "size_words",
    "NOTHING_INDEXED", "BAD_DATE",
]

#: How a date was arrived at (`TimelineEntry.basis`), as a short label.
BASIS_WORDS: dict[str, str] = {
    "taken": "Taken",
    "sent": "Sent",
    "guessed": "Guessed from the folder name",
    "saved": "File date",
}

#: What each label means - the tooltip on the date. The two weaker ones say so.
BASIS_TIPS: dict[str, str] = {
    "taken": "The date the camera recorded when this was taken.",
    "sent": "The date this message was sent.",
    "guessed": ("A year found in the folder's name (like \"Summer 1999\"). It is a "
                "guess, so it is placed at the start of that year."),
    "saved": ("The date the file was last saved on the drive. After a file has been "
              "copied from one drive to another this is often the day it was "
              "copied, not the day it was made."),
}

#: The choices in the "Show" box, and what each does.
KIND_WORDS: dict[str, str] = {
    "everything": "Everything except code",
    "photos": "Photos",
    "videos": "Videos",
    "documents": "Documents and other files",
    "mail": "Mail",
    "code": "Program code",
}
KIND_TIPS: dict[str, str] = {
    "everything": "Everything from the period: photos, videos, documents and mail. "
                  "Program code is left out so it does not crowd the rest.",
    "photos": "Only photographs.",
    "videos": "Only videos.",
    "documents": "Documents, spreadsheets, music and any other files - not photos, "
                 "videos or mail.",
    "mail": "Only email messages, dated by when each was sent.",
    "code": "Only files that belong to program code you have on this computer.",
}

NOTHING_INDEXED = ("There is nothing on the timeline yet. Once Leasha has read some of "
                   "your files, everything with a date will appear here.")
BAD_DATE = ("That is not a date Leasha can read. Try 2015-06-01, 2015-06, or just 2015.")

_STATUS_WORDS = {
    "offline": "not plugged in",
    "locked": "locked",
    "archived": "kept away from this computer",
}


def basis_words(basis: str) -> str:
    return BASIS_WORDS.get(str(basis), BASIS_WORDS["saved"])


def basis_tip(basis: str) -> str:
    return BASIS_TIPS.get(str(basis), BASIS_TIPS["saved"])


def size_words(size_bytes: int) -> str:
    # 2026-10-04, the owner: one size formatter everywhere (`row_facts`).
    from app.core.row_facts import format_size

    return format_size(size_bytes)


def badge_words(entry: Any) -> str:
    """Where an item lives when that is not simply "this computer".

    Empty for a file on this computer - it needs no badge. For an item on a
    catalogued drive, share or archive: its name, and *right now* whether it can
    be reached. The live answer (`real_path`) wins over the last-known status,
    because a drive plugged in an hour ago is not "not plugged in".
    """
    if not getattr(entry, "on_a_source", False):
        return ""
    name = str(getattr(entry, "source_name", "") or "a catalogued drive")
    if getattr(entry, "real_path", None):
        return name
    status = str(getattr(entry, "source_status", "") or "offline").lower()
    return f"{name} - {_STATUS_WORDS.get(status, 'not connected right now')}"


def _month_name(day: date) -> str:
    return f"{day:%B} {day.year}"


def period_words(period: Any) -> str:
    """"June 2015", "2015", "1 Jun 2015 to 30 Jun 2015", "Everything after 1 Jun 2015"."""
    after, before = getattr(period, "after", None), getattr(period, "before", None)
    if after is None and before is None:
        return "Everything, from the beginning"
    if getattr(period, "is_month", False):
        return _month_name(after)
    if (after is not None and before is not None and (after.month, after.day) == (1, 1)
            and (before.month, before.day) == (12, 31) and after.year == before.year):
        return f"{after.year}"
    # A typed time of day (order "dates" §1b) is said, or a range of two hours
    # would read as the same day twice.
    short = lambda d: f"{d.day} {d:%b} {d.year}" + (           # noqa: E731
        f" {d:%H:%M}" if isinstance(d, datetime) else "")
    if after is None:
        return f"Everything up to {short(before)}"
    if before is None:
        return f"Everything from {short(after)} on"
    return f"{short(after)} to {short(before)}"


def day_heading(moment: datetime) -> str:
    """"Saturday 13 June 2015" - the line above a day's items."""
    return f"{moment:%A} {moment.day} {moment:%B} {moment.year}"


def _count(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def summary_sentence(overview: Any) -> str:
    """One line on what the timeline holds: how many, across what years."""
    total = int(getattr(overview, "total", 0) or 0)
    if not total:
        return NOTHING_INDEXED
    first, last = overview.first, overview.last
    span = f"{first[0]}" if first[0] == last[0] else f"{first[0]} to {last[0]}"
    return f"{_count(total, 'item', 'items')} from {span}."


def thin_data_notes(overview: Any) -> list[str]:
    r"""What the timeline cannot say yet, said plainly - never an error.

    The order this belongs to promises "each report degrades gracefully when a
    dependency's data is thin (states plainly what it can't yet say)". Here the
    thin data is the *dates*: a photograph library with no camera dates is
    placed by when its files were last copied, and pretending otherwise would
    put twenty years of photographs in the month somebody plugged a drive in.
    """
    notes: list[str] = []
    camera = int(getattr(overview, "photos_with_camera_date", 0) or 0)
    filed = int(getattr(overview, "photos_by_file_date_only", 0) or 0)
    if filed and not camera:
        notes.append(
            f"None of your {_count(filed, 'photo', 'photos')} has a date from the camera "
            "yet, so they are placed by their file date - which is often the day "
            "they were copied, not the day they were taken.")
    elif filed:
        notes.append(
            f"{_count(filed, 'photo has', 'photos have')} no camera date, so "
            f"{'it is' if filed == 1 else 'they are'} placed by the file's own date "
            f"(the other {camera:,} are placed by the camera).")
    guessed = int(getattr(overview, "by_folder_guess", 0) or 0)
    if guessed:
        notes.append(
            f"{_count(guessed, 'scan is', 'scans are')} placed by a year in "
            f"{'its' if guessed == 1 else 'their'} folder name, at the start of that year. "
            "These are guesses and are marked as such.")
    undated = int(getattr(overview, "undated", 0) or 0)
    if undated:
        notes.append(
            f"{_count(undated, 'item has', 'items have')} no date at all, so "
            f"{'it is' if undated == 1 else 'they are'} not on the timeline.")
    return notes


def empty_period_sentence(period: Any) -> str:
    """When a period holds nothing: say so, and say it is a fact about the index."""
    return f"Nothing in Leasha's records is dated {period_words(period)}."
