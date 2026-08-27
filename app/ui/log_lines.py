r"""Reading a log line: what level it is, and what file it is about.

Layer: L5 presenter — Qt-free, so both halves of §1 can be tested without a
window.

**The colour and the click are the same question asked twice.** Both need the
line taken apart: one wants the level, the other wants the path. Doing it in
the widget would have put a regular expression inside a paint path and made
neither half testable.

**The level word stays in the line.** §1a is explicit and it is this
codebase's standing rule: colour is never the only signal. Somebody who cannot
tell the warn colour from the normal one — a common enough thing, and true of
every screenshot pasted into a message — still reads `WARNING`.

The format is `core.logging._CONSOLE_FORMAT`: `HH:mm:ss LEVEL component
message`. Nothing here assumes more than that, and everything degrades to
"normal line, no file" rather than raising, because this runs once per line
per second for as long as somebody is watching a run.
"""

from __future__ import annotations

import re
from typing import Any, Optional

__all__ = [
    "NORMAL", "WARN", "ERROR", "LEVELS", "TOKENS",
    "level_of", "path_in", "is_actionable", "colour_token",
]

#: The three states a line can be in, as far as a person reading one cares.
NORMAL = "normal"
WARN = "warn"
ERROR = "error"

#: loguru's level word -> what it means here. **Only three buckets**, because
#: a log that renders five severities in five colours is a log that is harder
#: to scan than one rendered in none: the eye is looking for the two lines
#: that are not fine.
LEVELS: dict[str, str] = {
    "TRACE": NORMAL,
    "DEBUG": NORMAL,
    "INFO": NORMAL,
    "SUCCESS": NORMAL,
    "WARNING": WARN,
    "ERROR": ERROR,
    "CRITICAL": ERROR,
}

#: Which **theme token** paints each state. Names, never hex.
#:
#: §1a's own words: *theme tokens only, never hardcoded hex - a hardcoded red
#: disappears on the dark theme*. Both palettes define these, with different
#: values, which is the entire reason the palette is two dictionaries rather
#: than one with a flag.
TOKENS: dict[str, str] = {
    NORMAL: "text",
    WARN: "warning",
    ERROR: "danger",
}

#: `15:09:34 WARNING  component the message`. The level is the second field.
_LINE = re.compile(
    r"^\s*(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<level>[A-Z]+)\s+(?P<rest>.*)$")

#: A path in a log line. Windows drive letters, UNC shares and POSIX paths, in
#: quotes or bare, with an extension - the shape a *file* has rather than the
#: shape a sentence has.
#:
#: **An extension is required**, and that is what stops this matching prose.
#: "could not read C:/work" would give a folder, and a click that opened the
#: wrong thing is worse than a line that is not clickable.
#:
#: **The bare form allows spaces, and it has to.** `C:\Users\jay\My Docs\a.docx`
#: is not an edge case, it is where most people keep most things - and the
#: first version of this stopped at the first space and found nothing there.
#: Non-greedy up to the first extension that is followed by a boundary, so the
#: match ends at the file rather than running on into the sentence.
_PATH = re.compile(
    r"""(?P<quote>['"])(?P<quoted>[^'"]+\.[A-Za-z0-9]{1,8})(?P=quote)"""
    r"""|(?P<bare>(?:[A-Za-z]:[\\/]|\\\\|/)[^'"<>|\r\n]*?"""
    r"""\.[A-Za-z0-9]{1,8})(?=[\s'":,;)\]]|$)""")


def level_of(line: Any) -> str:
    """`normal`, `warn` or `error` for one line. Never raises.

    An unparseable line is `normal`: the log holds continuation lines,
    tracebacks and anything a subprocess printed, and colouring those by
    accident would be worse than leaving them plain.
    """
    match = _LINE.match(str(line or ""))
    if match is None:
        return NORMAL
    return LEVELS.get(match.group("level").upper(), NORMAL)


def colour_token(line: Any) -> str:
    """The theme token that paints this line. Never a colour value."""
    return TOKENS.get(level_of(line), TOKENS[NORMAL])


def path_in(line: Any) -> Optional[str]:
    r"""The file a line is about, or None. Never raises.

    Quoted first, because that is how this codebase's own errors write a path
    (`could not read 'C:/work/report.pdf'`) and a quoted run cannot be cut
    short by a space in a folder name - which is most folders anybody has.

    >>> path_in("15:09:34 ERROR  x could not read 'C:/work/a b.pdf': locked")
    'C:/work/a b.pdf'
    """
    match = _PATH.search(str(line or ""))
    if match is None:
        return None
    found = match.group("quoted") or match.group("bare") or ""
    # Trailing punctuation belongs to the sentence, not to the file.
    return found.rstrip(".,;:)") or None


def is_actionable(line: Any) -> bool:
    r"""Is this a line worth being able to click?

    **A warning or an error that names a file**, and nothing else. §1b asks
    for *"a log you can act on"*, and the way to fail at that is to make every
    line look clickable: an underlined `INFO` that goes nowhere teaches people
    that none of them go anywhere.
    """
    return level_of(line) in (WARN, ERROR) and path_in(line) is not None
