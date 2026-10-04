r"""What Open and Show in folder do with a row - one rule for every page.

Layer: L5 presenter - decisions only. No I/O, no Qt.

2026-10-04, the owner: "where ever possible the same code should run for
functions so they are all consistent and standard". Open had grown five
routes - the window's, the Files page's, a pinned window's, the lightbox's and
the Mail page's - and each knew a different part of the truth: only one found
a recording's moment, only one resolved a catalogued drive, none opened a
message in Outlook. One route now (`workers.open_row_async`, its body
`tasks.open_target`), and the decision it acts on is here, where a test can
read it without opening anything.

The route takes the ROW wherever there is one, because the row is what knows
its drive (`volume_id`), its moment (`label` on a recording), its line
(`line_no` on a Code row, `char_start` on a code hit) and its passage
(`chunk_id`, for "opened" in the usage log). A caller with only a path wraps
it in a `Place`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "OpenPlan", "Place", "SearchInside", "VolumeKey", "is_offline", "is_web", "key_of",
    "moment_of", "on_a_volume", "plan_for", "usable",
]

#: Addresses the person's browser opens - a web source in Chat, or its chip.
WEB_PREFIXES = ("http://", "https://")


@dataclass(frozen=True, slots=True)
class Place:
    """A path - and a line, for code - from a caller with no result row."""

    path: str
    line_no: int = 0


@dataclass(frozen=True, slots=True)
class SearchInside:
    """The route's answer for a message nothing can open: search inside it.

    A message read out of an mbox, or one with no Outlook identifier, has no
    program to show it; reading it in Leasha is what opening it means then -
    what the Mail page's Open always did.
    """

    path: str


@dataclass(frozen=True, slots=True)
class OpenPlan:
    """One open, decided. `how` is what the worker does with `path`:

    `web` (the browser), `file` (its own program), `reveal` (its folder, the
    file selected), `archive` (the mail archive a message or attachment is in),
    `copy` (a read-only copy of an attachment or zip member, opened),
    `message` (Outlook; else search inside), `media` (a player at `seconds`),
    `code` (an editor at `line`, or at the line `char_start` falls on).
    `volume`: resolve the catalogued drive's current mount point first.
    """

    how: str
    path: str
    volume: bool = False
    seconds: Optional[int] = None
    line: int = 0
    char_start: Optional[int] = None


#: `sqlite_store.VOLUME_PATH_SCHEME`: a file on a catalogued drive, by drive id.
VOLUME_PREFIX = "leasha-volume://"


@dataclass(frozen=True, slots=True)
class VolumeKey:
    """A catalogued drive's file known only by its key - a pin, a chip."""

    path: str
    volume_id: int
    relative_path: str


def on_a_volume(row: Any) -> Any:
    """`row`, or - for a bare `leasha-volume://<id>/<path>` key with no row - the
    drive and path read off the key, so it resolves as a row would. No I/O."""
    path = key_of(row)
    if getattr(row, "volume_id", None) is not None or not path.startswith(VOLUME_PREFIX):
        return row
    number, _, relative = path[len(VOLUME_PREFIX):].partition("/")
    return VolumeKey(path, int(number), relative) if number.isdigit() and relative else row


def key_of(row: Any) -> str:
    """The row's own path, whole. A Code row keeps it in `full_path` - its
    `path` is shortened for the column (`code.repo_file_rows`), which is what a
    pinned window from the Code tab used to open. A plain string is a path."""
    if isinstance(row, str):
        return row
    full = getattr(row, "full_path", None)
    return str((full if full is not None else getattr(row, "path", "")) or "")


def is_web(path: Any) -> bool:
    return str(path or "").lower().startswith(WEB_PREFIXES)


def is_offline(row: Any) -> bool:
    """The row says its drive is not plugged in: a Files row's Status word, or
    a timeline entry that is not reachable. Read off the row - never a stat."""
    from app.core.file_state import OFFLINE

    return (str(getattr(row, "status", "") or "") == OFFLINE
            or getattr(row, "reachable", True) is False)


def usable(row: Any, *, missing: bool = False, offline: bool = False) -> bool:
    """Whether Open and Show in folder are offered for this row. **No I/O.**

    2026-10-04: the menu used to stat the path on the interface thread, which
    greyed both out for an email attachment, a file inside a zip, a message
    and a file on a catalogued drive - every one of which the route opens. Now
    only what the list already knows takes them away (`missing`, decided on a
    worker; `offline`); anything else is the open's to report.
    """
    return bool(key_of(row)) and not missing and not offline and not is_offline(row)


def moment_of(row: Any) -> Optional[int]:
    """Seconds into a recording that a result row is about, else None."""
    from app.core.media_open import seconds_for_result
    from app.extract.media import media_extensions

    ext = str(getattr(row, "ext", "") or "").lower().lstrip(".")
    if f".{ext}" not in media_extensions():
        return None
    return seconds_for_result(getattr(row, "label", ""))


def plan_for(row: Any, *, reveal: bool = False) -> OpenPlan:
    """What opening (or revealing) `row` means. Pure; the worker carries it out."""
    from app.ui.attachment_open import opens_from_a_copy, zip_member_of
    from app.ui.presenter.results import is_code_kind

    path = key_of(row)
    volume = getattr(on_a_volume(row), "volume_id", None) is not None
    if is_web(path):
        return OpenPlan("web", path)
    if reveal:
        zip_path = zip_member_of(path)[0]
        if zip_path:
            return OpenPlan("reveal", zip_path)          # the zip that holds it
        if path.startswith("pst://"):
            return OpenPlan("archive", path)             # a message, or its attachment
        return OpenPlan("reveal", path, volume)
    if opens_from_a_copy(path):
        return OpenPlan("copy", path)
    if "://" in path and not volume:
        return OpenPlan("message", path)
    seconds = moment_of(row)
    if seconds is not None:
        return OpenPlan("media", path, volume, seconds=seconds)
    line = int(getattr(row, "line_no", 0) or 0)
    if line > 0:
        return OpenPlan("code", path, volume, line=line)
    start = getattr(row, "char_start", None)
    if start is not None and is_code_kind(str(getattr(row, "ext", "") or "").lstrip(".")):
        return OpenPlan("code", path, volume, char_start=int(start))
    return OpenPlan("file", path, volume)
