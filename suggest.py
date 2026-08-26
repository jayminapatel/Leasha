r"""`leasha suggest <source> [prefix]` — values for a shell completer.

**Standalone on purpose, and this is the whole design.** A Tab press expects an
answer in tens of milliseconds. Measured on this machine:

    bare interpreter            14ms
    sqlite3 + json              20ms
    import app                  62ms
    import app.cli             243ms

`app.cli` costs 243ms before it has opened anything - most of a 300ms budget
spent on imports the answer does not need - so this file lives outside the
`app` package and imports nothing from it. That is `doctor.py`'s discipline,
adopted here for the same reason it was adopted there: a tool that has to work
when the rest is slow or half-built cannot depend on the rest.

It is the **fallback**, not the usual path. The PowerShell completer reads
`completions.json` first and never starts Python at all; this answers the cases
the sidecar has no entry for - a source that was empty at index time, or a
prefix beyond the fifty values it holds.

Prints one value per line. Prints nothing and exits 0 when it cannot answer: a
completer that receives an error message offers it as a completion, which is
worse than offering nothing.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

#: How the value tables are shaped, mirroring `SqliteStore._VALUE_SHAPES`.
#:
#: **A second copy, and it is a deliberate one.** Importing the store costs
#: 228ms and this file exists to avoid exactly that. The drift risk is real, so
#: `test_the_standalone_suggester_matches_the_store` runs both against one
#: database and compares - which is the honest way to keep two copies honest.
SHAPES = {
    "ext": ("files f", "f.ext", "f.ext <> ''"),
    "folder": ("files f", "f.parent_dir", "f.parent_dir <> ''"),
    "repo": ("repos r", "r.name", "r.name <> ''"),
    "sender": ("messages m", "m.sender", "m.sender IS NOT NULL AND m.sender <> ''"),
    "recipient": ("messages m", "m.recipients",
                  "m.recipients IS NOT NULL AND m.recipients <> ''"),
}

LIMIT = 40


def env_value(key: str) -> str:
    """One key from `.env`, without importing the config module.

    The environment wins, as it does everywhere else in this application.
    """
    if os.environ.get(key):
        return os.environ[key]
    env = Path(__file__).resolve().parent / ".env"
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def database_path() -> Path:
    """Where the index is, derived exactly as `config.py` derives it."""
    explicit = env_value("FTS_DB")
    if explicit:
        return Path(explicit)
    data = env_value("DATA_PATH")
    return Path(data) / "fts" / "knowledge.db" if data else Path()


def like_escape(value: str) -> str:
    r"""`%` and `_` are ordinary characters in a path and an address."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def values(source: str, prefix: str = "", limit: int = LIMIT) -> list[str]:
    """The commonest values of `source` matching `prefix`. **Never raises.**"""
    shape = SHAPES.get(source)
    if shape is None:
        return []
    table, column, guard = shape
    path = database_path()
    if not path.is_file():
        return []

    try:
        # Read-only, so a completer can never be the thing that locks an index
        # run out of its own database.
        connection = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            f"SELECT {column} AS v, COUNT(*) AS n FROM {table} "
            f"WHERE {guard} AND {column} LIKE ? ESCAPE '\\' "
            f"GROUP BY {column} ORDER BY n DESC, v LIMIT ?",
            (f"%{like_escape(prefix)}%", max(1, int(limit))),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


def main(argv: list[str]) -> int:
    if not argv:
        return 0                                 # nothing to say, said quietly
    source = argv[0]
    prefix = argv[1] if len(argv) > 1 else ""
    for value in values(source, prefix):
        print(value)
    return 0


if __name__ == "__main__":                       # pragma: no cover
    sys.exit(main(sys.argv[1:]))
