r"""The working set: results gathered across searches. Workspace §3c.

Layer: L5 presenter — Qt-free, so the rules about what is in the set and what
order it is in are testable without a panel.

**Five queries, one action.** The thing this answers is the shape of real
work: you do not find everything you need in one search. Gather from five,
then open them all, copy every path, or drag the lot into an email — the step
that used to be a scribbled list of filenames.

**Cleared explicitly, never by a new search.** §3c is emphatic and it is the
whole difference between a working set and a results list. Something that
emptied itself when you searched again would be useless for the one job it
exists to do.

**It survives the session.** One `index_state` row, the same channel every
other preference uses. Somebody who spent ten minutes gathering nine documents
and then closed the window by accident has not lost the ten minutes.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, NamedTuple, Optional

__all__ = ["PINNED_KEY", "Pin", "LIMIT", "add", "remove", "encode", "decode",
           "paths", "summary"]

#: Where the set lives between sessions.
PINNED_KEY = "ui:pinned_results"

#: How many may be gathered.
#:
#: **Fifty, and it is a guard rather than a design.** The panel is meant to
#: hold a handful; the ceiling exists so that a stuck key or a misread click
#: cannot grow an `index_state` row without bound. Nobody working normally
#: will ever meet it, and somebody who does is told rather than silently
#: losing the fifty-first.
LIMIT = 50


class Pin(NamedTuple):
    """One gathered result: enough to show it, open it and drag it."""

    path: str
    name: str = ""

    @property
    def label(self) -> str:
        """What the panel shows. The name, or the path when there is none."""
        return self.name or self.path.replace("\\", "/").rstrip("/").rpartition(
            "/")[2] or self.path


def _as_pin(item: Any) -> Optional[Pin]:
    """A row, a mapping or a `Pin` as a `Pin`. None if there is no path.

    **Never raises**: this reads rows from four different views and a JSON
    blob from a database, and one malformed entry must cost that entry rather
    than the panel.
    """
    if isinstance(item, Pin):
        return item if item.path else None
    try:
        if isinstance(item, dict):
            path = str(item.get("path", "") or "").strip()
            name = str(item.get("name", "") or "").strip()
        else:
            path = str(getattr(item, "path", "") or "").strip()
            name = str(getattr(item, "name", "") or "").strip()
    except Exception:                            # noqa: BLE001 - see docstring
        return None
    return Pin(path, name) if path else None


def add(current: Iterable[Any], item: Any, *, limit: int = LIMIT) -> tuple:
    r"""The set with `item` in it. Never raises.

    **Newest last, and already-there is not an error.** Somebody pinning a
    result they pinned an hour ago meant "make sure this is in the set", not
    "add a second copy" - and moving it to the end would shuffle a list they
    are reading.
    """
    found = decode(current)
    pin = _as_pin(item)
    if pin is None:
        return found
    if any(existing.path == pin.path for existing in found):
        return found
    if len(found) >= max(1, int(limit)):
        return found
    return (*found, pin)


def remove(current: Iterable[Any], path: Any) -> tuple:
    """The set without that path. Never raises."""
    wanted = str(path or "").strip()
    return tuple(pin for pin in decode(current) if pin.path != wanted)


def decode(value: Any) -> tuple:
    r"""Whatever was stored or passed, as a tuple of `Pin`. **Never raises.**

    Accepts the JSON `index_state` holds, a list of rows, or a tuple of pins
    already - the panel hands its own state back in when it redraws, and a
    second pass over it has to be the identity.
    """
    if value is None:
        return ()
    items: Any = value
    if isinstance(value, str):
        try:
            items = json.loads(value) if value.strip() else []
        except (TypeError, ValueError):
            return ()
    try:
        found = [_as_pin(item) for item in items]
    except TypeError:
        return ()
    return tuple(pin for pin in found if pin is not None)


def encode(current: Iterable[Any]) -> str:
    r"""The set as one `index_state` value. **Never raises.**

    JSON rather than a delimiter, because a path may contain very nearly any
    character - including the ones anybody would reach for as a separator.
    """
    try:
        return json.dumps([{"path": pin.path, "name": pin.name}
                           for pin in decode(current)])
    except Exception:                            # noqa: BLE001 - a preference
        return "[]"


def paths(current: Iterable[Any]) -> tuple:
    """Every path in the set, in order. For open-all and copy-all."""
    return tuple(pin.path for pin in decode(current))


def summary(current: Iterable[Any], *, limit: int = LIMIT) -> str:
    r"""One line about the set, in plain words.

    `""` when it is empty, because a panel that says "0 items" is a panel
    announcing that it has nothing to say.
    """
    found = decode(current)
    if not found:
        return ""
    noun = "document" if len(found) == 1 else "documents"
    if len(found) >= max(1, int(limit)):
        return (f"{len(found)} {noun} — that is as many as this can hold. "
                f"Remove one to add another.")
    return f"{len(found)} {noun}"
