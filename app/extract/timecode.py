r"""Timestamps: `754.3` seconds as `12:34`, and back. Pure, Qt-free.

Layer: L2

**A transcript hit has to say when.** "Found in `holiday.mp4`" is a two-hour
search; "found in `holiday.mp4` at 12:41" is a scrub of the slider. The place a
chunk starts is stored in the same `chunks.label` column a spreadsheet uses for
`Q3!A14`, and this module is the one place that knows what a *time* looks like
in it - so the writer (`app/extract/media.py`) and the reader
(`app/ui/presenter/results.py`) cannot disagree about the shape.

The shape is what a player's own time display shows: `m:ss` under an hour,
`h:mm:ss` from an hour up. It is deliberately not `mm:ss` with a leading zero
on the minutes - `0:07` is how every player writes it - and it is unambiguous
next to a cell address, because that always contains `!`.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = ["format_timecode", "parse_timecode", "is_timecode", "describe_timecode"]

_PATTERN = re.compile(r"^(?:(\d{1,3}):)?(\d{1,2}):(\d{2})$")


def format_timecode(seconds: float) -> str:
    """`754.3` -> `12:34`; `3725` -> `1:02:05`. Negative and junk become `0:00`.

    Truncates rather than rounds, so a hit reported at `12:34` is never *after*
    the words - a person who scrubs to it hears them start, not finish.
    """
    try:
        whole = int(float(seconds))
    except (TypeError, ValueError, OverflowError):
        whole = 0
    if whole < 0:
        whole = 0
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_timecode(text: object) -> Optional[int]:
    """Whole seconds from `12:34` or `1:02:05`, or None if it is not one."""
    if not isinstance(text, str):
        return None
    found = _PATTERN.match(text.strip())
    if found is None:
        return None
    hours = int(found.group(1) or 0)
    minutes = int(found.group(2))
    secs = int(found.group(3))
    if secs > 59 or (found.group(1) is not None and minutes > 59):
        return None
    return hours * 3600 + minutes * 60 + secs


def is_timecode(text: object) -> bool:
    """Whether `text` is an `m:ss` or `h:mm:ss` locator rather than, say, `Q3!A14`."""
    return parse_timecode(text) is not None


def describe_timecode(locator: object) -> str:
    """`12:41` as the sentence a result shows: *at 12:41*. `""` for anything else.

    Same contract as `presenter.results.cell_location`: the store keeps the
    code, the words are written here, and anything that is not a timecode -
    which is nearly every document - answers the empty string.
    """
    if not is_timecode(locator):
        return ""
    return f"at {str(locator).strip()}"
