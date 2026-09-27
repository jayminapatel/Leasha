r"""Which parts of the search box are filters, and how to take one out. Qt-free.

Layer: L5 (presenter half)

UI Redesign (202626160950 §3d). Every operator the `/` grammar already
extracts from the box is shown as a removable chip under it. **The box stays
the single source of truth**: a chip is a view of a span of the box's text,
and removing one edits that span out and re-dispatches through the same
debounce typing uses. Nothing here is a second parser - the operator names
are asked of `app.search.commands`, the same catalogue the `/` menu and
`expand_slashes` use, so a filter the grammar accepts is a chip and one it
does not is left as words.

Two spellings are recognised, because both reach the parser: the slash form
somebody types (`/type pdf`) and the colon form it becomes (`type:pdf`),
including the negated colon form (`-type:pdf`) `parse_query` accepts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from app.search.commands import VALUELESS, command_for

__all__ = ["Chip", "chips_for", "without", "chip_label"]

_SLASH = re.compile(r'(?<!\S)/(?P<name>[A-Za-z]+)(?:[ \t]+(?P<value>"[^"]*"|[^\s/]+))?')
_COLON = re.compile(r'(?<!\S)(?P<neg>[-!])?(?P<name>[A-Za-z]+):(?P<value>"[^"]*"|\S+)')

#: The second half of `/between A and B`, straight after `A`. Order 0x §6a.
#:
#: **One chip for the whole range, not a chip and two stray words.** The
#: parser joins `between:A and B` into `A..B` (`query.join_between_words`),
#: so the filter really is all four words; a chip covering only `between A`
#: would, when removed, leave `and B` behind as a search for the word "and".
#: The same rules as the parser's: `and` or `to` in any case, then one value
#: that is not itself an operator (`between:2024 to:priya` is two filters).
_BETWEEN_TAIL = re.compile(
    r'[ \t]+(?:and|to)[ \t]+(?![A-Za-z][\w-]*:)(?P<last>"[^"]*"|[^\s"/]+)',
    re.IGNORECASE)


def _with_between_tail(text: str, name: str, value: str, end: int) -> tuple[str, int]:
    """`(value, end)` stretched over `and B` when `name` is `between`.

    Anything else, or a value that is already a range, comes back unchanged.
    """
    if name != "between" or not value or ".." in value:
        return value, end
    tail = _BETWEEN_TAIL.match(text, end)
    if tail is None or ".." in tail.group("last"):
        return value, end
    return f"{value}{tail.group(0)}".strip(), tail.end()


@dataclass(frozen=True)
class Chip:
    """One filter in the box: its operator, value, and where it sits."""

    op: str
    value: str
    start: int
    end: int
    negated: bool = False

    @property
    def label(self) -> str:
        return chip_label(self.op, self.value, negated=self.negated)


def chip_label(op: str, value: str, *, negated: bool = False) -> str:
    """The words on the chip. `newest` alone; `type: pdf` otherwise."""
    name = op.strip().lower()
    prefix = "not " if negated else ""
    if value.strip().lower() in VALUELESS:
        # `/newest` reaches the parser as `sort:newest`; the word typed is
        # the word shown.
        return f"{prefix}{value.strip().lower()}"
    if name in VALUELESS or not value or value.lower() == name:
        return f"{prefix}{name}"
    shown = value.strip('"')
    return f"{prefix}{name}: {shown}"


def chips_for(text: str) -> list[Chip]:
    """Every recognised operator in `text`, in the order typed.

    Spans are over `text` exactly as given, so `without()` can cut them.
    """
    found: list[Chip] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    for match in _SLASH.finditer(text):
        command = command_for(match.group("name"))
        if command is None:
            continue
        name = match.group("name").lower()
        if name in VALUELESS:
            start, end = match.start(), match.start("name") + len(match.group("name"))
            value = name
        else:
            value = match.group("value") or ""
            start, end = match.start(), match.end()
            value, end = _with_between_tail(text, command.name, value, end)
        if free(start, end):
            found.append(Chip(command.name, value, start, end))
            taken.append((start, end))
    for match in _COLON.finditer(text):
        command = command_for(match.group("name"))
        if command is None or not free(match.start(), match.end()):
            continue
        value, end = _with_between_tail(text, command.name, match.group("value"), match.end())
        found.append(Chip(command.name, value, match.start(),
                          end, negated=bool(match.group("neg"))))
        taken.append((match.start(), end))
    found.sort(key=lambda chip: chip.start)
    return found


def without(text: str, chip: Chip) -> str:
    """`text` with `chip`'s span cut out and the whitespace around it tidied.

    Only the span is touched - everything the person typed around it stays
    exactly as typed, including other filters.
    """
    if not (0 <= chip.start <= chip.end <= len(text)):
        return text
    before, after = text[:chip.start], text[chip.end:]
    joined = f"{before.rstrip()} {after.lstrip()}" if before.strip() and after.strip() \
        else (before.rstrip() or after.lstrip())
    return joined.strip()


def labels(text: str) -> Sequence[str]:
    """Just the words, for a test or a summary line."""
    return tuple(chip.label for chip in chips_for(text))
