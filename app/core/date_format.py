r"""How the Files and Mail tabs write a date: one format, chosen in Settings.

Layer: L0

2026-10-11, the owner: "all the dates in the files and mail tab should be of
the format yyyy-mm-dd hh:nn and make this configurable have drop down of most
common formats and have a option of a validated custom format too."

A format is written in the letters people already use for one, not in
`strftime`'s: `yyyy-mm-dd hh:nn`. `mm` is the month and `nn` the minutes, as
in Excel and Delphi, so the two never collide. The tokens:

| Token | Gives | Token | Gives |
|---|---|---|---|
| `yyyy` | 2026 | `dd` | 07 |
| `yy` | 26 | `ddd` | Wed |
| `mmmm` | October | `dddd` | Wednesday |
| `mmm` | Oct | `hh` | 14 (02 with `am/pm`) |
| `mm` | 10 | `nn` | 05 |
| `ss` | 09 | `am/pm` | PM |

Between them: spaces and `- / . , :`. Anything else is refused with a
sentence saying which character, because a format that silently prints a
stray letter is a date nobody can trust. A format must name the day, the
month and the year: a list date without one of them is ambiguous down a
column of fifteen years of mail.

Window state (`ui:date_format`), like the text size beside it, not an `.env`
key. Imports nothing from the application.
"""

from __future__ import annotations

import time
from typing import Any, Optional

__all__ = [
    "DEFAULT", "PRESETS", "SEPARATORS", "validate", "to_strftime",
    "set_date_format", "date_format", "format_seconds", "format_ns", "example",
]

DEFAULT = "yyyy-mm-dd hh:nn"

#: The drop-down, most common first. The default is the owner's.
PRESETS: tuple[str, ...] = (
    DEFAULT,
    "dd/mm/yyyy hh:nn",
    "mm/dd/yyyy hh:nn",
    "dd-mm-yyyy hh:nn",
    "dd.mm.yyyy hh:nn",
    "dd mmm yyyy hh:nn",
    "mmm dd, yyyy hh:nn am/pm",
    "yyyy-mm-dd",
)

SEPARATORS = " -/.,:"

#: Longest first, so `yyyy` is never read as two `yy`.
_TOKENS: tuple[tuple[str, str], ...] = (
    ("am/pm", "%p"), ("yyyy", "%Y"), ("mmmm", "%B"), ("dddd", "%A"),
    ("mmm", "%b"), ("ddd", "%a"), ("yy", "%y"), ("mm", "%m"), ("dd", "%d"),
    ("hh", "%H"), ("nn", "%M"), ("ss", "%S"),
)

_CURRENT = {"value": DEFAULT}


def _tokens(pattern: str) -> tuple[list[str], Optional[str]]:
    """The pattern as tokens and separators, or the first character that is neither."""
    out: list[str] = []
    text = pattern.lower()
    index = 0
    while index < len(text):
        for token, _code in _TOKENS:
            if text.startswith(token, index):
                out.append(token)
                index += len(token)
                break
        else:
            char = pattern[index]
            if char not in SEPARATORS:
                return out, char
            out.append(char)
            index += 1
    return out, None


def validate(pattern: Any) -> Optional[str]:
    """`None` for a format that can be used, or a sentence saying what is wrong."""
    text = str(pattern or "").strip()
    if not text:
        return "Type a format, for example yyyy-mm-dd hh:nn."
    if len(text) > 40:
        return "That format is too long - 40 characters at most."
    parts, bad = _tokens(text)
    if bad is not None:
        return (f"\"{bad}\" is not part of a date. Use yyyy, yy, mmmm, mmm, mm, dddd, ddd, "
                f"dd, hh, nn, ss and am/pm, with spaces or - / . , : between them.")
    used = {part for part in parts if part not in SEPARATORS}
    if parts.count("mm") > 1:
        return "Use nn for minutes - mm is the month (yyyy-mm-dd hh:nn)."
    if len([part for part in parts if part not in SEPARATORS]) != len(used):
        return "Each part of the date can appear once."
    if "dd" not in used:                      # ddd and dddd are the weekday, not the day
        return "The format needs the day of the month (dd)."
    if not used & {"mm", "mmm", "mmmm"}:
        return "The format needs the month (mm, mmm or mmmm)."
    if not used & {"yy", "yyyy"}:
        return "The format needs the year (yyyy or yy)."
    if used & {"nn", "ss"} and "hh" not in used:
        return "Minutes and seconds need the hour (hh) too."
    if "ss" in used and "nn" not in used:
        return "Seconds need the minutes (nn) too."
    if "am/pm" in used and "hh" not in used:
        return "am/pm needs the hour (hh)."
    return None


def to_strftime(pattern: str) -> str:
    """The `time.strftime` form of a valid pattern. Call `validate` first."""
    parts, _bad = _tokens(str(pattern).strip())
    twelve = "am/pm" in parts
    codes = dict(_TOKENS)
    if twelve:
        codes["hh"] = "%I"
    return "".join(codes.get(part, part) for part in parts)


def set_date_format(pattern: Any) -> str:
    """Use this format from now on; an invalid or empty one is the default. Returns it."""
    text = str(pattern or "").strip()
    _CURRENT["value"] = text if text and validate(text) is None else DEFAULT
    return _CURRENT["value"]


def date_format() -> str:
    return _CURRENT["value"]


def format_seconds(seconds: Any, pattern: Optional[str] = None) -> str:
    """A moment, in seconds since 1970, in local time. `""` when there is none:
    a blank is honest, and 1 January 1970 is a claim."""
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    try:
        stamp = time.localtime(value)
    except (OverflowError, OSError, ValueError):
        return ""
    chosen = pattern if pattern is not None else _CURRENT["value"]
    if validate(chosen) is not None:
        chosen = DEFAULT
    return time.strftime(to_strftime(chosen), stamp)


def format_ns(stamp_ns: Any, pattern: Optional[str] = None) -> str:
    """`format_seconds` for nanoseconds since 1970, as file times are stored."""
    try:
        value = int(stamp_ns or 0)
    except (TypeError, ValueError):
        return ""
    return format_seconds(value // 1_000_000_000, pattern) if value > 0 else ""


def example(pattern: str) -> str:
    """The pattern applied to one fixed afternoon, for the drop-down and the preview."""
    sample = time.mktime((2026, 3, 7, 14, 5, 9, 0, 0, -1))
    return format_seconds(int(sample), pattern)
