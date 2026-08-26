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

__all__ = ["ESCAPE", "like_escape", "contains"]

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


def contains(value: str) -> str:
    """`value` as a substring pattern: escaped, lowered, wrapped in `%`.

    The lowering is not cosmetic. `LIKE` is case-insensitive for ASCII only, so
    the callers that store a folded column compare against the folded form; see
    the note in `filters.py` about `LOWER()` costing 2.49x for no rows.
    """
    return f"%{like_escape(value.lower())}%"
