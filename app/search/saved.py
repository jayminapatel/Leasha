r"""Saved searches: the rules, with no database and no window.

Layer: L4 — pure. Adoptions §3.

**A saved search is a query, not a result set.** Running one re-executes it,
so it behaves like a smart folder: a document indexed tomorrow appears in
`saved:invoices` without anybody re-saving anything. A stored list of ids
would have been less code and a feature that quietly rots.

**`saved:name` is expanded before the parser sees it**, exactly as
`/type pdf` is rewritten to `type:pdf` by `commands.expand_slashes`. The
parser never learns that saved searches exist - one grammar, one set of
tests, the rule `commands.py` opens with.

**Expanded in place, not instead of.** `saved:weekly leeds` is the saved
query *plus* the word leeds. Replacing the whole box would throw away
something the person just typed, and there is no sentence you could put on
screen that makes that feel right.

**The expansion reads a list, never a store.** It happens on the interface
thread, between a keystroke and a search; `test_no_store_call_outside_a_worker`
exists because this project has made that mistake before. The list is fetched
by the same worker that fills the `/saved` menu.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple, Optional

__all__ = [
    "NAME_MAX",
    "SavedSearch",
    "SAVED_FIELD",
    "clean_name",
    "name_key",
    "as_saved",
    "find",
    "mentions_saved",
    "names_in",
    "expand_saved",
    "suggest_name",
    "ordered",
    "summary",
]

#: The operator a saved search is referred to by, in the box and in the menu.
SAVED_FIELD = "saved"

#: Longest name kept. Long enough for "leeds site surveys 2024", short enough
#: that a menu row is still scannable - the list is read at a glance under
#: somebody's own cursor, and a name that wraps is a name nobody reads.
NAME_MAX = 60

#: Names that would collide with the grammar rather than describe a search.
#: A search called `type:pdf` would expand into a token that reads as a filter
#: and cannot be typed back out again.
_ILLEGAL = ('"', "'", ":", "/", "\\", "\n", "\r", "\t")


class SavedSearch(NamedTuple):
    """One named search, as everything above the store sees it.

    A tuple rather than a dataclass because it crosses a worker boundary and
    is only ever read.
    """

    name: str
    query: str
    scope: str = "all"
    run_count: int = 0

    def as_token(self) -> str:
        """`saved:invoices`, or `saved:"leeds surveys"` when it has a space."""
        name = self.name
        return (f'{SAVED_FIELD}:"{name}"' if any(c.isspace() for c in name)
                else f"{SAVED_FIELD}:{name}")


def clean_name(text: Any) -> str:
    r"""A name as it will be stored, or `""` if there is nothing usable in it.

    **Cleaned rather than refused.** Somebody typing ` Leeds  Surveys ` meant
    `Leeds Surveys`, and a dialog that rejects it over two spaces is a dialog
    that makes a person feel told off for nothing. The characters that are
    actually removed are the ones the grammar would misread - a colon or a
    quote in a name produces a token that cannot be typed back out.

    >>> clean_name("  Leeds   surveys ")
    'Leeds surveys'
    >>> clean_name("type:pdf")
    'typepdf'
    """
    name = str(text or "")
    for bad in _ILLEGAL:
        name = name.replace(bad, " " if bad in ("\n", "\r", "\t") else "")
    name = " ".join(name.split())
    return name[:NAME_MAX].strip()


def name_key(text: Any) -> str:
    r"""The key two names are the same under.

    `casefold`, not `lower`: `STRASSE` and `straße` are the same word, and
    `lower()` says they are not. The same fold `migrations._v14` had to add a
    whole column for, applied here before anything reaches the database.
    """
    return clean_name(text).casefold()


def as_saved(row: Any) -> Optional[SavedSearch]:
    """One store row as a `SavedSearch`, or None if it is not one.

    **Never raises.** This runs over whatever a worker handed back, and a
    malformed row must cost one entry in a menu rather than the menu.
    """
    if isinstance(row, SavedSearch):
        return row
    try:
        if isinstance(row, dict):
            name = clean_name(row.get("name"))
            query = str(row.get("query") or "")
            scope = str(row.get("scope") or "all")
            count = int(row.get("run_count") or 0)
        else:
            name = clean_name(getattr(row, "name", ""))
            query = str(getattr(row, "query", "") or "")
            scope = str(getattr(row, "scope", "") or "all")
            count = int(getattr(row, "run_count", 0) or 0)
    except Exception:                            # noqa: BLE001 - see docstring
        return None
    if not name or not query:
        return None
    return SavedSearch(name, query, scope or "all", count)


def find(saved: Any, name: Any) -> Optional[SavedSearch]:
    """The saved search called `name`, matched case-insensitively."""
    wanted = name_key(name)
    if not wanted:
        return None
    for row in saved or ():
        entry = as_saved(row)
        if entry is not None and name_key(entry.name) == wanted:
            return entry
    return None


#: `saved:name`, `saved:"two words"`. Anchored at a word boundary so a path or
#: a sentence containing the word cannot be mistaken for one.
_TOKEN = re.compile(
    r'(?:(?<=\s)|^)' + SAVED_FIELD + r':(?:"(?P<quoted>[^"]*)"|(?P<bare>\S+))')


def mentions_saved(text: Any) -> bool:
    """Whether the box holds a `saved:` token at all. Cheap; runs per search."""
    return bool(_TOKEN.search(str(text or "")))


def names_in(text: Any) -> tuple:
    """The names a query refers to, in the order they were written.

    Quoted and bare forms both, because `as_typed_value` quotes anything with
    a space in it - so `saved:"leeds surveys"` is the ordinary shape for any
    name of more than one word, not an edge case.
    """
    found = []
    for match in _TOKEN.finditer(str(text or "")):
        name = match.group("quoted")
        if name is None:
            name = match.group("bare") or ""
        if name:
            found.append(name)
    return tuple(found)


def expand_saved(text: Any, saved: Any) -> tuple[str, str]:
    r"""`(query, scope)` with every `saved:name` replaced by what it stands for.

    `scope` is `""` when nothing was expanded, which is what tells the caller
    to leave the scope control alone. A saved search that carries a scope sets
    it - saving "the pdf Dave sent" while looking at mail only and getting
    everything back on replay would make the feature untrustworthy in the one
    way that matters.

    **A name that is not there is left exactly as typed.** The same rule
    `expand_slashes` follows for an unknown `/word`: silently deleting part of
    a query is the one behaviour that makes a search box impossible to trust.
    `saved:invoices` with nothing saved under that name is then a search for
    those words, which finds nothing and looks like what it is.

    >>> expand_saved("saved:x leeds", [SavedSearch("x", "type:pdf", "mail")])
    ('type:pdf leeds', 'mail')
    """
    raw = str(text or "")
    if not raw or not _TOKEN.search(raw):
        return raw, ""

    scope = ""

    def replace(match: "re.Match[str]") -> str:
        nonlocal scope
        name = match.group("quoted")
        if name is None:
            name = match.group("bare") or ""
        entry = find(saved, name)
        if entry is None:
            return match.group(0)
        # **The first one wins.** Two saved searches in one box is a corner
        # nobody will type on purpose, and a scope decided by whichever
        # happens to be last is worse than one decided by the order they were
        # written in.
        if not scope:
            scope = entry.scope or ""
        return entry.query

    expanded = _TOKEN.sub(replace, raw)
    return " ".join(expanded.split()), scope


#: Words that make a poor name on their own, because every search has them.
_DULL = frozenset({"the", "a", "an", "and", "or", "of", "in", "on", "for"})


def suggest_name(query: Any) -> str:
    r"""A name to put in the box when somebody presses Save. Never a promise.

    **A default, not an auto-save.** §3b is explicit that nothing saves
    itself and nothing suggests saving - this fills a field in a dialog the
    person opened, which is the opposite thing: it saves them typing what
    they can plainly see, and they can replace every character of it.

    Filters are dropped, because `type:pdf leeds` named `type:pdf leeds`
    would not survive `clean_name` and reads as machinery rather than as a
    name for anything.
    """
    words = []
    for word in str(query or "").split():
        if ":" in word or word.startswith("-"):
            continue
        plain = word.strip('"').strip()
        if not plain or plain.lower() in _DULL:
            continue
        words.append(plain)
        if len(words) >= 5:
            break
    return clean_name(" ".join(words))


def ordered(saved: Any, limit: int = 0) -> tuple:
    """`SavedSearch`es, most-run first, then alphabetically. Never raises.

    The store already orders them; this exists so a caller handed a list from
    anywhere - a test, a cache, a worker - gets the same order the menu shows,
    rather than two places disagreeing about which is the top row.
    """
    entries = [entry for entry in
               (as_saved(row) for row in saved or ()) if entry is not None]
    entries.sort(key=lambda e: (-int(e.run_count), e.name.casefold()))
    return tuple(entries[:limit] if limit else entries)


def summary(entry: Any, chars: int = 48) -> str:
    """The query, short enough to sit under a name in a list.

    Shown because a name six months old says nothing about what it does, and
    a saved search nobody dares run is a saved search that may as well not
    exist.
    """
    found = as_saved(entry)
    if found is None:
        return ""
    text = " ".join(found.query.split())
    return text if len(text) <= chars else text[: max(1, chars - 1)].rstrip() + "…"
