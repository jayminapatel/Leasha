r"""The command palette's registry and its matching. Qt-free.

Layer: L5 (presenter half)

IA-1 / MS-11 of the UX review (2026-10-14): one box, Ctrl+K, that reaches every
verb and every place the window has - the core of Raycast and Linear. **Nothing
here is declared twice.** The window hands over what its menu bar already
declares (text, key, icon, what it runs), every page on the rail in the rail's
order, every Settings category and the few doors that live only on a page's
toolbar (the Sync window). This module turns that into `Entry` rows and decides
which rows a line of typing shows:

* **Words, fuzzily.** Each word typed must start a word of the entry ("pre pa"
  finds "Preview pane"), or failing that appear inside its text, or failing that
  appear in order letter by letter ("prvw"). Every word must find something.
* **`>` first means actions only** - the menu bar's verbs, no pages, no settings.
* **Plain text always ends with "Search for '...'"**, which runs it on the Search
  page - so a palette that matches nothing is never a dead end, and one that
  matches a page ("mail from dave") still offers the search somebody meant.

An `Entry` carries its `run` as a plain callable; this module never calls it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

__all__ = [
    "Entry", "PAGE", "SETTING", "ACTION", "SEARCH", "ACTIONS_ONLY", "PLACEHOLDER", "TITLE",
    "page_entry", "setting_entry", "action_entry", "search_entry", "clean_text", "match",
    "build",
]

PAGE, SETTING, ACTION, SEARCH = "page", "setting", "action", "search"

#: A line that starts with this shows the menu bar's actions only.
ACTIONS_ONLY = ">"

#: The palette's own words (new, 2026-10-14).
TITLE = "Command palette…"
PLACEHOLDER = "Go to a page, a setting or an action - or type > for actions only"

#: Order of kinds when two entries score the same: places first, then verbs.
_KIND_ORDER = {PAGE: 0, SETTING: 1, ACTION: 2, SEARCH: 3}

_WORD = re.compile(r"[\w']+", re.UNICODE)


@dataclass(frozen=True)
class Entry:
    """One line of the palette."""

    key: str                       #: unique, stable: "page:Mail", "action:Preview pane"
    text: str                      #: what the line says
    kind: str                      #: PAGE, SETTING, ACTION or SEARCH
    icon: str = ""                 #: a Lucide name from `icons.ICON_NAMES`
    keys: str = ""                 #: the shortcut shown on the right, if any
    tip: str = ""                  #: the line's tooltip
    words: tuple = ()              #: extra words it answers to ("preferences")
    run: Optional[Callable[[], Any]] = field(default=None, compare=False, repr=False)
    query: str = ""                #: SEARCH only: what is searched for


def clean_text(text: str) -> str:
    """A menu item's text without its `&` accelerator marker (`&&` is a literal `&`)."""
    return str(text or "").replace("&&", "\0").replace("&", "").replace("\0", "&").strip()


def page_entry(title: str, run: Callable[[], Any], *, icon: str = "", keys: str = "",
               tip: str = "") -> Entry:
    """'Go to <title>' - one rail page."""
    return Entry(f"page:{title}", f"Go to {title}", PAGE, icon=icon, keys=keys, tip=tip,
                 words=(title,), run=run)


def setting_entry(category: str, run: Callable[[], Any], *, icon: str = "settings") -> Entry:
    """'Settings: <category>' - one Settings category."""
    return Entry(f"setting:{category}", f"Settings: {category}", SETTING, icon=icon,
                 words=("settings", "preferences", "options"), run=run)


def action_entry(text: str, run: Callable[[], Any], *, icon: str = "", keys: str = "",
                 tip: str = "", words: Sequence[str] = ()) -> Entry:
    """One verb - a menu bar item, or a toolbar's door the menus do not have."""
    text = clean_text(text)
    return Entry(f"action:{text}", text, ACTION, icon=icon, keys=keys, tip=tip,
                 words=tuple(words), run=run)


def search_entry(query: str, run: Optional[Callable[[], Any]] = None) -> Entry:
    """"Search for '<query>'" - the fall-through, run on the Search page."""
    return Entry(f"search:{query}", f"Search for '{query}'", SEARCH, icon="search",
                 tip="Run this search on the Search page", run=run, query=query)


def build(*groups: Iterable[Entry]) -> list[Entry]:
    """Every group in order, the first entry of any key kept and later ones dropped,
    so a page reached from the rail and from the Go menu is listed once."""
    seen: set = set()
    out: list[Entry] = []
    for group in groups:
        for entry in group:
            if entry.key in seen or not entry.text:
                continue
            seen.add(entry.key)
            out.append(entry)
    return out


def _subsequence(needle: str, hay: str) -> bool:
    """`needle`'s letters appear in `hay` in order ("prvw" in "preview")."""
    letters = iter(hay)
    return all(ch in letters for ch in needle)


def _score(entry: Entry, words: Sequence[str], whole: str) -> float:
    """How well `entry` answers the typed `words`; 0 when any word finds nothing."""
    text = entry.text.lower()
    hay_words = _WORD.findall(" ".join((text, *(w.lower() for w in entry.words))))
    hay = " ".join(hay_words)
    total = 0.0
    for word in words:
        if any(h.startswith(word) for h in hay_words):
            total += 3.0
        elif word in hay:
            total += 1.5
        elif _subsequence(word, hay.replace(" ", "")):
            total += 0.5
        else:
            return 0.0
    stripped = text.removeprefix("go to ").removeprefix("settings: ")
    if stripped.startswith(whole) or text.startswith(whole):
        total += 2.0
    if stripped == whole:
        total += 2.0
    return total


def match(entries: Sequence[Entry], typed: str, *, limit: int = 50,
          search: Optional[Callable[[str], Callable[[], Any]]] = None) -> list[Entry]:
    """The lines to show for `typed`, best first.

    `search(query)` gives the run for the "Search for" line; without it the line
    still appears (its `run` is None and the caller runs it by `query`).
    """
    text = str(typed or "")
    actions_only = text.lstrip().startswith(ACTIONS_ONLY)
    if actions_only:
        text = text.lstrip()[len(ACTIONS_ONLY):]
    query = " ".join(text.split())
    pool = [e for e in entries if e.kind == ACTION] if actions_only else list(entries)
    if not query:
        return pool[:limit]
    words = _WORD.findall(query.lower()) or [query.lower()]
    whole = query.lower()
    scored = []
    for position, entry in enumerate(pool):
        score = _score(entry, words, whole)
        if score > 0:
            scored.append((-score, _KIND_ORDER.get(entry.kind, 9), position, entry))
    scored.sort(key=lambda item: item[:3])
    found = [item[3] for item in scored]
    if not actions_only:
        found = found[:max(0, limit - 1)]
        found.append(search_entry(query, search(query) if search is not None else None))
        return found
    return found[:limit]
