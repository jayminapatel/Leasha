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
CURRENT_VERSION = 5

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


def _v3_knowledge_graph(conn: sqlite3.Connection) -> None:
    """Add `entities`, `entity_mentions` and `entity_edges` (Layer 6).

    Additive, like v2, and for the same reason: the graph is derived entirely
    from `chunks`, so it can be built at leisure on an existing index without
    re-reading a single file.

    Three decisions worth stating once here rather than rediscovering later.

    **`key` is the identity, `display` is what you show.** The key is casefolded
    and whitespace-collapsed, so "Acme Ltd", "ACME LTD" and "Acme  Ltd" are one
    node rather than three. Without it the graph's biggest nodes are always
    duplicates of each other, which is both wrong and the first thing anyone
    notices.

    **Mentions are stored, not just counts.** An entity nobody can trace back to
    a chunk is an assertion, not evidence - clicking a node has to be able to
    show the passages it came from, and PMI has to be recomputable without a
    re-extraction pass.

    **Edges are stored one way round only** (`a_id < b_id`), enforced by a CHECK.
    Co-occurrence is symmetric, so storing both directions doubles the table and
    creates the possibility of the two halves disagreeing.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            id          INTEGER PRIMARY KEY,
            key         TEXT    NOT NULL UNIQUE,
            display     TEXT    NOT NULL,
            kind        TEXT    NOT NULL,
            source      TEXT    NOT NULL DEFAULT 'cooccurrence',
            mentions    INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            doc_count   INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
        CREATE INDEX IF NOT EXISTS idx_entities_freq ON entities(mentions DESC);

        CREATE TABLE IF NOT EXISTS entity_mentions (
            entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            chunk_id    INTEGER NOT NULL REFERENCES chunks(id)   ON DELETE CASCADE,
            file_id     INTEGER NOT NULL REFERENCES files(id)    ON DELETE CASCADE,
            count       INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (entity_id, chunk_id)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_mentions_chunk ON entity_mentions(chunk_id);
        CREATE INDEX IF NOT EXISTS idx_mentions_file  ON entity_mentions(file_id);

        CREATE TABLE IF NOT EXISTS entity_edges (
            a_id        INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            b_id        INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            weight      INTEGER NOT NULL DEFAULT 0,
            pmi         REAL,
            PRIMARY KEY (a_id, b_id),
            CHECK (a_id < b_id)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_edges_b   ON entity_edges(b_id);
        CREATE INDEX IF NOT EXISTS idx_edges_pmi ON entity_edges(pmi DESC);
    """)


def _v4_filename_index(conn: sqlite3.Connection) -> None:
    """Add `files_fts`: an index over what files are *called*.

    `chunks_fts` indexes what a document says. Nothing indexed its name, so a
    file called "Invoice 2024.pdf" whose contents never use those words could
    not be found at all - which is how most people look for most files.

    **Trigram tokenisation**, so "voice" matches "Invoice". Word tokenisation
    cannot do that, and filename search is substring search: people type the
    middle of a name and expect a hit.

    Trigram needs SQLite 3.34+ (Python 3.12 ships far newer). If it is somehow
    missing, fall back to `unicode61` with prefix indexes rather than failing the
    migration - a filename search that only matches from the start of a word is
    much worse than trigram and enormously better than nothing.
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS files_fts "
            "USING fts5(name, folder, tokenize='trigram')"
        )
    except sqlite3.OperationalError:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS files_fts "
            "USING fts5(name, folder, tokenize='unicode61', prefix='2 3 4')"
        )

    # Backfill from what is already indexed, so upgrading does not require a
    # re-index. `rowid = files.id` is what lets a hit join straight back.
    #
    # `NOT IN` rather than a plain INSERT: an FTS5 table rejects a rowid it
    # already holds, so a migration that ran over a partly-populated index would
    # fail with `constraint failed` and leave the database stuck between
    # versions. A migration has to survive being re-run.
    conn.execute("""
        INSERT INTO files_fts(rowid, name, folder)
        SELECT f.id,
               replace(replace(f.path, rtrim(f.path, replace(replace(f.path, '\\', '/'), '/', '')), ''), '/', ''),
               f.parent_dir
        FROM files f
        WHERE f.source_kind IN ('file', 'archive')
          AND f.id NOT IN (SELECT rowid FROM files_fts)
    """)


def _v5_missing_indexes(conn: sqlite3.Connection) -> None:
    """Two indexes the queries always needed and never had.

    **Additive and re-runnable.** No table is touched and no row is rewritten,
    so an existing index gains these in seconds and keeps everything it had.

    * `files.source_kind` - `keyword.py` claimed in a comment that this was
      "already indexed, so this costs nothing". It was not. Clicking the Mail
      chip full-scanned `files`.
    * `files.mtime_ns` - `after:` and `before:` compare against it, and
      `_filter_only` sorts by it.

    **`idx_files_mtime` is what fixes the `_filter_only` scan**, which is not
    where the diagnosis pointed. The review read `WHERE c.ordinal = 0 ORDER BY
    f.mtime_ns DESC` as needing a `chunks` index, because `idx_chunks_file_ord`
    is `(file_id, ordinal)` and `ordinal` is not leading. A partial
    `chunks(file_id) WHERE ordinal = 0` was written, and the planner **never
    chose it** - measured with and without, the plan and the timing were
    identical to two decimal places.

    What the query actually needed was an ordered way in. Given `mtime_ns`,
    SQLite walks `files` newest-first and looks up each file's first chunk
    through the index that already existed, so `LIMIT 20` stops after twenty
    files. Measured on 20,000 files / 120,000 chunks: **6.30ms -> 0.04ms**, and
    `USE TEMP B-TREE FOR ORDER BY` disappears from the plan.

    The unused index was dropped rather than shipped. It cost a write on every
    document indexed and would never have been read - and it would have stood
    as evidence for a diagnosis that measurement did not support.

    `ANALYZE` afterwards because SQLite chooses a plan from statistics, and a
    brand-new index it knows nothing about may simply not be used.
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_source_kind ON files(source_kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime_ns)")
    conn.execute("ANALYZE")


#: version -> callable applying the step that produces it.
#: Version 1 is the baseline created by schema.sql, so it has no step here.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _v2_usage_logging,
    3: _v3_knowledge_graph,
    4: _v4_filename_index,
    5: _v5_missing_indexes,
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
