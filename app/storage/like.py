r"""Making user text safe to put inside a SQL `LIKE` pattern.

One function, in its own module, because the alternative is what this codebase
already had: the same three-line escape written inline at some call sites, a
private copy in `sqlite_store`, and none at all in `filters`. A literal `%`
then meant one thing in a filename search and another in a `path:` filter,
which is the kind of difference nobody notices until a file will not come back.

`app.storage.filters` cannot import from `app.storage.sqlite_store` at module
scope without a cycle, which is how the copies came about. Neither imports the
other; both import this.
"""

from __future__ import annotations

__all__ = ["ESCAPE", "like_escape", "contains", "has_wildcard", "glob"]

#: The clause every `LIKE` in this package carries.
#:
#: Written once so a call site cannot add the escaping and forget the clause -
#: which is worse than doing neither, because SQLite would then read the
#: backslash as an ordinary character and search for it.
ESCAPE = r" ESCAPE '\'"


def like_escape(value: str) -> str:
    r"""Make `value` a literal inside a `LIKE ... ESCAPE '\'` pattern.

    `%` and `_` are wildcards in `LIKE`, and both are ordinary characters in a
    Windows path and an email address: `Q1_2024%_final.pst` is a filename
    somebody has. Unescaped, it matches - and in a *delete* it deletes - rows
    belonging to entirely unrelated files.

    The backslash goes first, or escaping `%` would then have its own escape
    escaped. It matters more here than it looks: every Windows path is full of
    backslashes, so `path:D:\Reports` reaches this function with several.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def has_wildcard(value: str) -> bool:
    """Did somebody type a `*` or a `?` into this value?"""
    return "*" in value or "?" in value


def _translated(value: str, *, fold: bool = True) -> str:
    r"""`value` escaped and lowered, with `*` and `?` as `LIKE`'s `%` and `_`.

    The escape runs first, so a `%` or `_` somebody typed stays a literal and
    only the star and the question mark they typed become wildcards.
    """
    return like_escape(value.lower() if fold else value).replace("*", "%").replace("?", "_")


def contains(value: str, *, fold: bool = True) -> str:
    """`value` as a substring pattern: escaped, lowered, wrapped in `%`.

    The lowering is not cosmetic. `LIKE` is case-insensitive for ASCII only, so
    the callers that store a folded column compare against the folded form; see
    the note in `filters.py` about `LOWER()` costing 2.49x for no rows.

    **A `*` or `?` in the value is a wildcard** (owner, 2026-10-05: "/ commands
    should take wild cards"). Until then `/from dav*` looked for a literal star
    and found nothing, while the same star in the words of a search worked.
    The pattern is still "contains": `/path proj*/leeds` finds the folder
    wherever it sits in the path.

    `fold=False` leaves the case as typed, for a caller comparing against a
    column that is not stored folded.
    """
    return f"%{_translated(value, fold=fold)}%"


def glob(value: str) -> str:
    """`value` as a whole-value pattern, the way `dir inv*.pdf` reads it.

    For a value that names one short thing - a file name, an extension, a
    repository - where "starts with" and "ends with" are worth being able to
    say: `/name inv*` starts with inv, `/name *2024.pdf` ends with it, and
    `/name *inv*` is anywhere. Only used when `has_wildcard(value)`.
    """
    return _translated(value)
