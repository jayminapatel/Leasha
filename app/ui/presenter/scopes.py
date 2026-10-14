r"""One table of search scopes, for the Search page and the quick search alike. Qt-free.

Layer: L5 (presenter half)

Review 2026-10-14, IA-4: the two boxes had two vocabularies - Search offered
Everything / Mail only / Documents only / Code only (`search_bar.SCOPES`), the
quick search All / Files / Mail / Photos / Code (`quick_search.SCOPE_CHIPS`) -
and only one of them could narrow to photos. Both are read from `SCOPES` here
now. **Neither box's words changed**: each scope carries the label each box has
always shown, the Search page gains "Photos only", and Shift+Enter from the
quick search hands its chosen scope to the Search page by `key`.

`key` is the Search page's value (what `ParsedQuery.scope` has always been
given, and what a saved search stores); `bucket` is the quick search's chip
(`None` for All). `engine` is the scope the engine itself knows - `all`,
`mail`, `documents`, `code`; a picture is a document with an image extension,
so Photos searches documents with `type:image` added (`engine_request`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = ["Scope", "SCOPES", "QUICK_ORDER", "search_scopes", "quick_chips", "quick_tips",
           "by_key", "by_bucket", "engine_request", "key_for_bucket"]


@dataclass(frozen=True, slots=True)
class Scope:
    """One scope, as both boxes name it."""

    key: str
    bucket: Optional[str]
    search_label: str
    quick_label: str
    icon: str
    quick_tip: str
    engine: str
    #: Added to the query unless the line already names a type.
    adds: str = ""


#: The Search page's order. "Photos only" sits with the other kinds of thing,
#: before Code, which is about where a thing lives rather than what it is.
SCOPES: tuple[Scope, ...] = (
    Scope("all", None, "Everything", "All", "layout-grid",
          "Show every kind of result", "all"),
    Scope("mail", "mail", "Mail only", "Mail", "mail", "Show only emails", "mail"),
    Scope("documents", "files", "Documents only", "Files", "file-text",
          "Show only documents and other files", "documents"),
    Scope("photos", "photos", "Photos only", "Photos", "image",
          "Show only photos and pictures", "documents", adds="type:image"),
    Scope("code", "code", "Code only", "Code", "code", "Show only code", "code"),
)

#: The quick search's chip order - unchanged, because its number keys (1-5)
#: pick a chip by position and a person's fingers already know them.
QUICK_ORDER: tuple[str, ...] = ("all", "documents", "mail", "photos", "code")


def by_key(key: str) -> Scope:
    """The scope with this Search-page value; Everything for anything unknown."""
    for scope in SCOPES:
        if scope.key == key:
            return scope
    return SCOPES[0]


def by_bucket(bucket: Optional[str]) -> Scope:
    """The scope behind a quick-search chip; All for anything unknown."""
    for scope in SCOPES:
        if scope.bucket == bucket:
            return scope
    return SCOPES[0]


def key_for_bucket(bucket: Optional[str]) -> str:
    """The Search page's value for a quick-search chip - what Shift+Enter carries."""
    return by_bucket(bucket).key


def search_scopes() -> tuple[tuple[str, str], ...]:
    """`(label, value)` pairs for the Search page's scope picker, in its order."""
    return tuple((scope.search_label, scope.key) for scope in SCOPES)


def quick_chips() -> tuple[tuple[Optional[str], str, str], ...]:
    """`(bucket, label, icon)` for the quick search's chips, in its order."""
    return tuple((by_key(key).bucket, by_key(key).quick_label, by_key(key).icon)
                 for key in QUICK_ORDER)


def quick_tips() -> dict:
    """What pressing each quick-search chip does, by bucket."""
    return {scope.bucket: scope.quick_tip for scope in SCOPES}


def engine_request(scope: Scope, query: str) -> tuple[str, str]:
    """`(engine scope, query)` for a search narrowed to `scope`.

    A scope that adds a filter (Photos: `type:image`) leaves a line that
    already names a type alone - the person's own words win.
    """
    text = str(query or "")
    if not scope.adds:
        return scope.engine, text
    lowered = text.lower()
    if "type:" in lowered or "/type" in lowered:
        return scope.engine, text
    return scope.engine, f"{text} {scope.adds}".strip()
