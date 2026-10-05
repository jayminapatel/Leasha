r"""Folder-year era hints: a date guess for photos with no EXIF at all.

Layer: L2

Work order 0i (`202626270511`) section 4b. A scanned print has no EXIF -
there was no camera writing metadata, only a scanner years later stamping
today's date onto the file. "Diwali 2004" or "Summer_1999" as a folder or
file name is the one clue that survives the scan, and this reads it.

**A hint, never a fact.** Ranked below EXIF (`app.extract.exif.
read_datetime`) and below a Takeout JSON sidecar's own timestamp *where one
exists* - this item's own text places it there, though no Takeout-sidecar
date reader exists anywhere in this codebase to rank against (checked
before writing this, not assumed; see the dated note on this item in the
work order). Ranked above the file's bare mtime, which after twenty years
of drive-to-drive copies is the date of the last copy and nothing else -
exactly the problem `app.extract.exif`'s own module docstring names for
EXIF, one rung further down the ladder of "how much can this date be
trusted".
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Optional

from app.core.logging import logger

log = logger.bind(component="extract.era_hints")

__all__ = ["guess_year", "year_to_epoch_ns"]

#: Photography's own floor - the earliest a "year" in a folder name could
#: plausibly mean a photograph rather than an invoice number, a road name,
#: a filename counter, or a movie sequel. Loose on purpose: this is a hint,
#: not a filter, and a scanned tintype from the 1850s is a real photograph
#: this range must not silently exclude.
_EARLIEST_YEAR = 1826

#: A 4-digit run at a word boundary, so "2004" inside "img20045.jpg" - a
#: camera's own numbering, not a year - does not match: the boundary after
#: the run demands a non-digit, so "20045" never offers "2004" as a
#: candidate at all.
_YEAR = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)")


def _candidate_years(text: str) -> list[int]:
    """Every plausible year in one string, in the order they appear."""
    found = []
    for match in _YEAR.finditer(text):
        year = int(match.group(1))
        if _EARLIEST_YEAR <= year <= datetime.date.today().year:
            found.append(year)
    return found


def guess_year(path: Path) -> Optional[int]:
    r"""A plausible year from a photo's own name or its immediate folder.

    **The folder wins over the filename.** "Diwali 2004\IMG_0007.jpg" is a
    deliberate album name; "scan_2004_0007.jpg" alone, with no such folder,
    is far more likely to be a scanner's own batch counter than a year
    anyone typed on purpose - so the filename is only consulted when the
    folder offers nothing. Never raises.
    """
    try:
        parent = path.parent.name
        years = _candidate_years(parent)
        if years:
            return years[0]

        years = _candidate_years(path.stem)
        if years:
            return years[0]
    except Exception as exc:                        # noqa: BLE001 - a hint, not the job
        log.debug("could not guess a year for {}: {}: {}",
                  path, type(exc).__name__, exc)
    return None


#: A whole date in a file name - `2022-08-11_16-22-38`, `IMG_20190302_141500`,
#: `IMG-20130607-WA0010`, `PXL_20230101_123456789`, `Screenshot_2023-01-05-12-30-45`
#: - with the time when there is one. Not touching a letter or digit on either
#: side, so a hash like `2aed2e02ff6a` or a counter like `120190302` is not read
#: as a date.
_NAME_MOMENT = re.compile(
    r"(?<![0-9A-Za-z])(?P<y>19[7-9]\d|20[0-4]\d)[-_.]?(?P<m>0[1-9]|1[0-2])[-_.]?"
    r"(?P<d>0[1-9]|[12]\d|3[01])"
    r"(?:[ T_.-]{1,2}(?P<H>[01]\d|2[0-3])[-_.:h]?(?P<M>[0-5]\d)[-_.:m]?(?P<S>[0-5]\d)\d{0,3})?"
    r"(?![0-9])")


def guess_moment(path: Path) -> Optional[datetime.datetime]:
    r"""The date - and time, when there is one - a photo's own file name gives.

    2026-10-05, the owner: "dates are in the meta data of the file". Checked on
    their library: of 6,132 photos holding only a folder-year guess, 4,682 carry
    the whole date in their name (phones and WhatsApp write it there; WhatsApp
    strips it from the metadata), and Windows shows no Date taken for them
    either. A day, often to the second, beats "about 2022".

    Local time, naive, as `exif.read_datetime` returns it. Still a hint - a
    name can be changed - but a precise one. None when the name has no valid
    date, or one in the future. Never raises.
    """
    try:
        found = _NAME_MOMENT.search(Path(path).stem)
        if found is None:
            return None
        parts = {k: int(v) for k, v in found.groupdict().items() if v is not None}
        moment = datetime.datetime(parts["y"], parts["m"], parts["d"],
                                   parts.get("H", 0), parts.get("M", 0), parts.get("S", 0))
        if moment > datetime.datetime.now() + datetime.timedelta(days=1):
            return None
        return moment
    except Exception as exc:                        # noqa: BLE001 - a hint, not the job
        log.debug("no date in the name of {}: {}: {}", path, type(exc).__name__, exc)
        return None


def year_to_epoch_ns(year: int) -> int:
    """1 January of `year`, 00:00:00 UTC, in epoch nanoseconds.

    A hint carries no month or day, so the least-committal point in the
    year is the honest answer - the same reason `after:2024` in the search
    grammar resolves to the year's first moment rather than guessing a mid-
    year date that would look more precise than it is.
    """
    moment = datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc)
    return int(moment.timestamp() * 1_000_000_000)
