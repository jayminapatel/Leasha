"""Versioned schema migrations.

Layer: L1

The index schema carries its own integer version in `schema_version`,
independent of the application version (see docs/VERSIONING.md).

Two rules:

  * Migrations step forward one at a time and are never edited once released.
    Add a new entry instead.
  * The app refuses to open an index whose schema version is HIGHER than it
    understands, with an AppError, rather than corrupting it. An older build
    opening a newer index is a real scenario once there are tags to roll back to.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Optional

from app.core.errors import AppErrorException, make_error

__all__ = ["CURRENT_VERSION", "apply_migrations", "read_version", "MIGRATIONS"]

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

#: The schema version this build creates and understands.
CURRENT_VERSION = 2

def _v2_usage_logging(conn: sqlite3.Connection) -> None:
    """Add `searches` and `search_hits` (see schema.sql for why they exist).

    Additive only - no existing table is touched - so an index built by v1 gains
    the tables and keeps every row it had. A migration that required a re-index
    would cost hours on a 100GB corpus for two empty tables, which would be an
    absurd trade and would tempt anyone to skip the upgrade.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS searches (
            id          INTEGER PRIMARY KEY,
            query       TEXT    NOT NULL,
            filters     TEXT,
            hits        INTEGER NOT NULL,
            elapsed_ms  INTEGER NOT NULL,
            rerank_on   INTEGER NOT NULL,
            searched_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_searches_at ON searches(searched_at);

        CREATE TABLE IF NOT EXISTS search_hits (
            search_id   INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
            chunk_id    INTEGER NOT NULL,
            rank        INTEGER NOT NULL,
            sources     TEXT    NOT NULL,
            opened      INTEGER NOT NULL DEFAULT 0,
            opened_at   INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_hits_search ON search_hits(search_id);
        CREATE INDEX IF NOT EXISTS idx_hits_opened ON search_hits(opened) WHERE opened = 1;
    """)


#: version -> callable applying the step that produces it.
#: Version 1 is the baseline created by schema.sql, so it has no step here.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _v2_usage_logging,
}


def read_version(conn: sqlite3.Connection) -> int:
    """Current schema version, or 0 for a database with no schema yet."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    result = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    return int(result[0]) if result else 0


def _write_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_version (id, version) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
        (version,),
    )


def apply_migrations(conn: sqlite3.Connection, *, schema_file: Optional[Path] = None) -> int:
    """Bring `conn` up to CURRENT_VERSION. Returns the resulting version.

    Raises AppErrorException(ERR_CONFIG_INVALID) if the database was written by
    a newer build.
    """
    found = read_version(conn)

    if found > CURRENT_VERSION:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "storage.migrations",
            key="schema_version",
            reason=(
                f"this index was created by a newer build (schema v{found}); "
                f"this build understands v{CURRENT_VERSION}"
            ),
            suggestion=(
                "Update the application, or point DATA_PATH at a different index. "
                "Opening a newer index with an older build would corrupt it, so it is refused."
            ),
        ))

    if found == 0:
        source = schema_file or SCHEMA_FILE
        try:
            conn.executescript(source.read_text(encoding="utf-8"))
        except OSError as exc:
            raise AppErrorException(make_error(
                "ERR_CONFIG_INVALID", "storage.migrations",
                key="schema.sql", reason=f"could not be read from {source}",
                details=str(exc),
            )) from exc
        except sqlite3.Error as exc:
            raise AppErrorException(make_error(
                "ERR_CONFIG_INVALID", "storage.migrations",
                key="schema.sql", reason=f"failed to apply: {exc}",
                details=str(exc),
            )) from exc
        found = read_version(conn)

    while found < CURRENT_VERSION:
        step = found + 1
        migration = MIGRATIONS.get(step)
        if migration is None:
            raise AppErrorException(make_error(
                "ERR_CONFIG_INVALID", "storage.migrations",
                key="schema_version",
                reason=f"no migration registered to reach v{step} from v{found}",
            ))
        migration(conn)
        _write_version(conn, step)
        conn.commit()
        found = step

    return found
