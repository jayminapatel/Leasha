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
from app.core.logging import logger

_log = logger.bind(component="storage.migrations")

__all__ = ["CURRENT_VERSION", "SCHEMA_BASELINE_VERSION",
           "apply_migrations", "read_version", "MIGRATIONS",
           "trigram_available"]

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

#: The schema version this build creates and understands.
SCHEMA_BASELINE_VERSION = 4
"""The version `schema.sql` creates, before any migration runs.

Not `CURRENT_VERSION`: see the note beside the seed in `schema.sql`.
"""

CURRENT_VERSION = 34

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


def _v6_repositories(conn: sqlite3.Connection) -> None:
    """Record which repository a file belongs to.

    **Additive, and no re-index.** A new table and one nullable column: an
    existing 100GB index gains these in seconds and every row keeps everything
    it had. `repo_id` stays NULL until the next indexing run attributes it,
    which is a correct state rather than a broken one - nothing reads it
    expecting a value.

    `ON DELETE SET NULL`, deliberately not `CASCADE`. A repository that is
    moved, deleted or unmounted must not take the indexed content of its files
    with it. The files are still on disk in every case that matters, and
    re-reading 40,000 of them because a folder was renamed is not a behaviour
    anybody would ask for.

    `kind` is stored because the three are found differently - a `.git`
    directory, or a `.git` *file* pointing at `/modules/` or elsewhere - and
    because a submodule's files sit inside its parent's tree, which anything
    drawing a list has to know before it can draw one.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS repos (
            id         INTEGER PRIMARY KEY,
            root_path  TEXT    NOT NULL UNIQUE,
            name       TEXT    NOT NULL,
            kind       TEXT    NOT NULL,
            last_seen  INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_repos_name ON repos(name)")

    # SQLite has no `ADD COLUMN IF NOT EXISTS`, and re-running a migration is a
    # thing that happens - a half-finished run, or a version bumped by hand.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "repo_id" not in columns:
        conn.execute(
            "ALTER TABLE files ADD COLUMN repo_id INTEGER "
            "REFERENCES repos(id) ON DELETE SET NULL"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo_id)")
    conn.execute("ANALYZE")


#: version -> callable applying the step that produces it.
#: Version 1 is the baseline created by schema.sql, so it has no step here.
def _v7_identifier_tokens(conn: sqlite3.Connection) -> None:
    r"""Index `ResetPasswordHandler` so that searching `password` finds it.

    **Measured before it was built.** FTS5's `unicode61` tokenizer already
    splits on every non-alphanumeric character, so `get_user_name`,
    `reset-password` and `Order.Service` are already three tokens each. Only
    **camelCase and PascalCase** carry their boundaries in capitalisation, and
    those are stored as a single token - so no search for `password` could ever
    reach `ResetPasswordHandler`.

    `chunks` gains a `symbols` column holding the split parts, and `chunks_fts`
    gains a column over it. `MATCH` against the table searches every column, so
    no query has to know this exists.

    **The FTS table is rebuilt, not altered.** FTS5 has no `ALTER TABLE ... ADD
    COLUMN`, and an external-content table's column list must match the columns
    the triggers feed it. Dropping and recreating is the only route, and the
    `rebuild` command repopulates it from `chunks` without re-reading a single
    file from disk - which matters when the alternative is re-indexing 600GB.

    Column weights are set at query time, not here: the split forms are a weaker
    signal than the text as written, and `bm25()` takes per-column weights.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    if "symbols" not in columns:
        conn.execute("ALTER TABLE chunks ADD COLUMN symbols TEXT NOT NULL DEFAULT ''")

    # Backfill before the FTS rebuild, so the rebuild sees the finished column.
    # Done in Python rather than SQL because the splitting rules are a hundred
    # lines of regex with acronym handling - see app/core/identifiers.py - and
    # a second implementation in SQL would drift from the first within a month.
    # `app.core`, not `app.search` - the comment two lines above already says
    # so. As written this raised ModuleNotFoundError inside the v7 migration,
    # which runs on `connect()`, so every store in the application failed to
    # open and every test that touches one failed at setup.
    from app.core.identifiers import symbol_tokens

    _backfill_symbols(conn, symbol_tokens)

    conn.execute("DROP TRIGGER IF EXISTS chunks_ai")
    conn.execute("DROP TRIGGER IF EXISTS chunks_ad")
    conn.execute("DROP TRIGGER IF EXISTS chunks_au")
    conn.execute("DROP TABLE IF EXISTS chunks_fts")
    conn.execute("""
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            text,
            symbols,
            content='chunks',
            content_rowid='id',
            tokenize='porter unicode61'
        )
    """)
    conn.execute("""
        CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, text, symbols)
            VALUES (new.id, new.text, new.symbols);
        END
    """)
    conn.execute("""
        CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text, symbols)
            VALUES ('delete', old.id, old.text, old.symbols);
        END
    """)
    conn.execute("""
        CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text, symbols)
            VALUES ('delete', old.id, old.text, old.symbols);
            INSERT INTO chunks_fts(rowid, text, symbols)
            VALUES (new.id, new.text, new.symbols);
        END
    """)
    # Repopulate from the content table. Cheap next to a re-index and the only
    # way to get the existing rows into the new column list.
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")


#: Every trigger that mirrors a row into an external-content FTS table, as
#: idempotent statements. Two consumers: `SqliteStore.check_and_rebuild_fts_if_dirty`
#: puts them back after a bulk run that dropped them was interrupted, and
#: `test_fts_bulk_recovery.py` pins that they match what a fresh database creates.
#: The migrations above and below remain the definition of a *new* database;
#: this is the copy that lets a *damaged* one be repaired without a migration.
CONTENT_TRIGGERS: tuple[str, ...] = (
    """CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid, text, symbols)
        VALUES (new.id, new.text, new.symbols);
    END""",
    """CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, text, symbols)
        VALUES ('delete', old.id, old.text, old.symbols);
    END""",
    # Schema v28 (work order 0x item 5d): only an UPDATE that names a column
    # the keyword index holds. See `_v28_chunk_index_follows_its_columns`.
    """CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF text, symbols ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, text, symbols)
        VALUES ('delete', old.id, old.text, old.symbols);
        INSERT INTO chunks_fts(rowid, text, symbols)
        VALUES (new.id, new.text, new.symbols);
    END""",
    """CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
      INSERT INTO messages_fts(rowid, subject, sender, recipients)
      VALUES (new.file_id, new.subject, new.sender, new.recipients);
    END""",
    """CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
      INSERT INTO messages_fts(messages_fts, rowid, subject, sender, recipients)
      VALUES('delete', old.file_id, old.subject, old.sender, old.recipients);
    END""",
    """CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
      INSERT INTO messages_fts(messages_fts, rowid, subject, sender, recipients)
      VALUES('delete', old.file_id, old.subject, old.sender, old.recipients);
      INSERT INTO messages_fts(rowid, subject, sender, recipients)
      VALUES (new.file_id, new.subject, new.sender, new.recipients);
    END""",
)

#: The mail-header index, and the one place it is defined.
#:
#: **Created from Python rather than from `schema.sql`, because it may not be
#: creatable.** `tokenize='trigram'` needs SQLite 3.34, and while the bundled
#: Python is far newer, the version a user's Python happens to ship is not
#: something this should depend on - `browse_messages` already says so about a
#: different feature. A `CREATE` inside `schema.sql` that fails takes the whole
#: schema with it, so the guard has to be here, where a failure can mean
#: "carry on without it".
_MESSAGES_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, sender, recipients,
    content='messages', content_rowid='file_id', tokenize='trigram');

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, subject, sender, recipients)
  VALUES (new.file_id, new.subject, new.sender, new.recipients);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject, sender, recipients)
  VALUES('delete', old.file_id, old.subject, old.sender, old.recipients);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject, sender, recipients)
  VALUES('delete', old.file_id, old.subject, old.sender, old.recipients);
  INSERT INTO messages_fts(rowid, subject, sender, recipients)
  VALUES (new.file_id, new.subject, new.sender, new.recipients);
END;
"""


def trigram_available(conn: sqlite3.Connection) -> bool:
    """Can this SQLite build a trigram FTS5 index? Never raises.

    Asked rather than inferred from `sqlite_version`, because a build can be
    new enough and still be compiled without FTS5.
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp.__trigram_probe USING fts5(a, tokenize='trigram')")
        conn.execute("DROP TABLE temp.__trigram_probe")
        return True
    except sqlite3.Error:
        return False


def _v8_mail_header_index(conn: sqlite3.Connection) -> None:
    r"""Index the mail headers, so `from:` and `subject:` stop scanning.

    **Every keystroke on the Mail tab read every message.** `browse_messages`
    filters with `sender LIKE '%dave%'`, and a leading wildcard cannot use an
    index however many are declared - `idx_messages_sender` has never once been
    used by that query. On a 200,000-message archive that is a full scan per
    keystroke, and the Mail tab is a live filter.

    **Trigram, so the results do not change.** The obvious FTS choice tokenises
    on word boundaries, which would quietly narrow the meaning: `from:ave` would
    stop finding `dave.smith@acme.com`, and `browse_messages` documents
    substring matching as deliberate - *"exact `IN` matching was a real bug here
    once, and it made the filter look broken to anybody who did not know the
    full address by heart"*. A trigram index answers `LIKE '%x%'` exactly, mid-
    token and punctuation included, so this is the same filter with an index
    under it rather than a different filter that is faster.

    Its one limit is arithmetic: a trigram index cannot answer a term shorter
    than three characters, because there is no trigram to look up. Those fall
    back to the scan, which is what they did before and which is cheap to
    describe - see `SqliteStore.browse_messages`.

    Skipped, not failed, where trigram is unavailable. The application then
    behaves exactly as it did, which is the correct outcome for an index that
    is an optimisation.
    """
    if not trigram_available(conn):
        return
    conn.executescript(_MESSAGES_FTS)
    # Existing rows predate the triggers. `rebuild` reads them straight from
    # `messages` - no file is re-read and no mail is re-parsed, which matters
    # when the alternative is re-indexing a 30GB archive for a lookup table.
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")


def _v9_forget_dragged_column_widths(conn: sqlite3.Connection) -> None:
    r"""Clear every saved column width, once.

    Asked for directly: *"can you reset all column width as with the new cap
    that should not happen"* - after a column on the Files tab was dragged so
    wide it was unusable, and stayed that way across restarts.

    **A width saved before the cap existed outlives the cap.** `_apply_widths`
    caps what it restores, so the table on screen is right, but the stored
    preference stays as wide as it ever was - and it is what "Reset widths"
    reports and what any later change restores from. The cap stops new ones
    being created; this clears the ones that already were.

    **A migration rather than a startup step in the window**, for two reasons.
    It runs before a single view exists, so no list can restore a width between
    the clear and the redraw. And `test_no_store_call_outside_a_worker` is right
    to refuse a store write on the interface thread - that guard caught this
    when it was in `MainWindow.__init__`, which is exactly the kind of small
    "it is only one write at startup" that freezes a window on a network share.

    Once, by construction: a migration runs at its version and never again, so a
    width set deliberately after this survives every later start. That is the
    property a state-key guard would have had to imitate.
    """
    conn.execute(
        "DELETE FROM index_state WHERE key LIKE 'ui:%:widths'")


def _v10_name_only_status(conn: sqlite3.Connection) -> None:
    r"""Let `files.status` hold `NAME_ONLY`, so every file can have a row.

    Asked for: *"the files search should include all files, not just the ones
    we have read the content of"*. A `.zip`, `.mp4` or `.exe` got no row at
    all - not indexed by name, not in the skip ledger, nothing anywhere saying
    it had been passed over. Invisible is the worst of the three possible
    answers, because somebody who knows the file is there concludes the index
    is broken, and they are not wrong.

    `NAME_ONLY` is a status rather than a skip: nothing went wrong, there is
    simply no reader for a `.mp4`. And it is emphatically not `INDEXED` - a row
    claiming that while holding no chunks is the bug that made `--force`
    necessary, and doing it deliberately for millions of rows would be worse.

    **This is a table rebuild, and the dangerous part is not the rebuild.**
    `status` carries a CHECK constraint, which SQLite cannot alter in place, so
    the twelve-step dance is the only route. The hazard is that `chunks`,
    `messages` and `entity_mentions` all reference `files(id)` with
    `ON DELETE CASCADE` **and this store runs with `PRAGMA foreign_keys = ON`**:
    a `DROP TABLE files` with them enabled deletes every chunk in the index.

    So foreign keys are turned off for the rebuild and back on afterwards,
    which is only possible because the connection uses `isolation_level=None`
    and this runs outside a transaction. `id` is copied rather than
    regenerated, so every existing reference stays valid.
    `test_the_rebuild_keeps_every_chunk` proves both halves.

    Cost is proportional to the row count: seconds on a normal index, a couple
    of minutes on a corpus of millions. Once.
    """
    if _status_allows(conn, "NAME_ONLY"):
        return                                # already rebuilt, or a fresh schema

    # **Executed statement by statement, never `executescript`.** That helper
    # implicitly commits any open transaction before it runs, so the `BEGIN`
    # below would be discarded and a failure half way through would leave the
    # schema in pieces. Found by testing the rebuild rather than by reading it.
    steps = [
        """CREATE TABLE files_rebuilt (
                id            INTEGER PRIMARY KEY,
                path          TEXT    NOT NULL UNIQUE,
                parent_dir    TEXT    NOT NULL,
                ext           TEXT    NOT NULL,
                size_bytes    INTEGER NOT NULL,
                mtime_ns      INTEGER NOT NULL,
                content_hash  TEXT,
                status        TEXT    NOT NULL,
                skip_code     TEXT,
                skip_detail   TEXT,
                indexed_at    INTEGER,
                source_kind   TEXT    NOT NULL,
                repo_id       INTEGER REFERENCES repos(id) ON DELETE SET NULL,
                CHECK (status IN ('PENDING', 'INDEXED', 'SKIPPED', 'FAILED',
                                  'NAME_ONLY'))
           )""",
        """INSERT INTO files_rebuilt
                SELECT id, path, parent_dir, ext, size_bytes, mtime_ns,
                       content_hash, status, skip_code, skip_detail,
                       indexed_at, source_kind, repo_id
                FROM files""",
        "DROP TABLE files",
        "ALTER TABLE files_rebuilt RENAME TO files",
        "CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)",
        "CREATE INDEX IF NOT EXISTS idx_files_dir    ON files(parent_dir)",
        "CREATE INDEX IF NOT EXISTS idx_files_ext    ON files(ext)",
        "CREATE INDEX IF NOT EXISTS idx_files_skip   ON files(skip_code) "
        "WHERE skip_code IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_files_kind_ext ON files(source_kind, ext)",
        "CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo_id) "
        "WHERE repo_id IS NOT NULL",
        # **These two were missing, and dropping them was permanent.**
        #
        # `DROP TABLE files` takes every index on it. This list recreated six of
        # the eight, so any database that passed through v10 lost
        # `idx_files_mtime` and `idx_files_source_kind` for good - and v5, which
        # created them, had already run, so nothing would ever put them back.
        #
        # `idx_files_mtime` is the one v5 documents as the **6.30ms to 0.04ms**
        # fix for the filter-only scan: "newest first", `after:` and `before:`
        # all full-scan `files` without it. `idx_files_source_kind` is what
        # `filters.py:133` names for the same reason.
        #
        # It survived because a *fresh* database never runs this rebuild - it is
        # created at the current schema with every index present - so every test
        # that starts from an empty file sees eight indexes and passes. Only a
        # real database, carried forward, loses them. See `_v13_repair_indexes`,
        # which puts them back on the databases that already have.
        "CREATE INDEX IF NOT EXISTS idx_files_source_kind ON files(source_kind)",
        "CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime_ns)",
    ]

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in steps:
            conn.execute(statement)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        # **Back on whatever happened.** Leaving them off would silently
        # disable every cascade for the life of the connection, which is a far
        # worse state than a failed migration.
        conn.execute("PRAGMA foreign_keys = ON")


def _v12_quoted_removed(conn: sqlite3.Connection) -> None:
    r"""How much of each message was a quoted reply or a signature.

    **Measured since quoting was built, and only ever logged.** `StripResult`
    has carried `removed_chars` from the start - the overnight run reported
    *"stripped 38,609 chars of quoted"* on a single message - and the number
    went to a file nobody reads while looking at that message.

    It is needed on screen. A mail preview shows the *indexed* text, so a reply
    appears with the thread it is replying to gone; without a line saying so,
    the preview looks like a message that was sent without context. Storing the
    amount is what lets the pane say what happened rather than imply that
    nothing did.

    One nullable integer on an existing table, so an index built before this
    keeps working and simply says nothing about older messages - which is
    honest, because for those the answer genuinely is not known.
    """
    # `row[1]`, not `row["name"]` - a migration runs against whatever connection
    # it is handed, and that one may have no `row_factory`. The two migrations
    # above already index positionally for the same reason.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "quoted_removed" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN quoted_removed INTEGER")


def _v11_wildcard_vocabulary(conn: sqlite3.Connection) -> None:
    r"""A view over the terms FTS5 already stores, for wildcard expansion.

    From `docs/WORKORDER-202626081106-wildcards.md` §3.1. `fts5vocab` is not an
    index and holds no rows of its own: it reads `chunks_fts`'s existing term
    dictionary. **So an index built before this migration gains it instantly** -
    no rebuild, nothing rewritten, no extra disk. Anything requiring a re-index
    at 600GB is the wrong answer to a rare query.

    The `'row'` form gives `term`, `doc` and `cnt`. `doc` is what orders the
    expansion, so when the 200-term cap bites it is the commonest real words
    that survive rather than an arbitrary alphabetical slice.

    Guarded rather than assumed: `chunks_fts` is created by `schema.sql`, but a
    database recovered from a partial write may not have it, and a migration
    that raises leaves an index nobody can open. A missing vocabulary costs
    wildcards and nothing else - `vocabulary_terms` reports it and the ordinary
    search path never touches this table.
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vocab "
            "USING fts5vocab('chunks_fts', 'row')"
        )
    except sqlite3.Error as exc:
        _log.warning("wildcard vocabulary unavailable: {}", exc)


#: Chunks read, split and written per batch during the v7 backfill. Large
#: enough that the per-batch overhead is noise, small enough that the working
#: set is a few megabytes rather than the corpus.
_BACKFILL_BATCH = 5_000


def _backfill_symbols(conn: sqlite3.Connection, split: Callable[[str], str]) -> None:
    r"""Fill `chunks.symbols`, in batches, without loading the corpus into RAM.

    **This was one `fetchall()` over `chunks`, and it ran inside `connect()`.**
    At twenty to thirty million chunks that is tens of gigabytes of text
    materialised as a Python list before a single row is written - so upgrading
    a real index did not run slowly, it exhausted memory and died. Before the
    window opened, with no backup taken and no progress reported, on a database
    now stuck between two schema versions.

    Three changes, and each is load-bearing.

    **Keyset pagination, not `LIMIT/OFFSET`.** `OFFSET n` makes SQLite walk and
    discard n rows every batch, so the backfill gets quadratically slower the
    further it gets - the classic way a paginated migration appears to hang at
    80%. Carrying the last id forward is an index seek each time.

    **Committed per batch.** An interrupted upgrade then keeps the work it has
    done: `symbols = ''` is the predicate for "not yet filled", so re-running
    resumes rather than restarts. It also lets SQLite release the pages instead
    of growing one enormous transaction.

    **Progress is logged.** An upgrade that will take twenty minutes must say
    so; silence is indistinguishable from the hang this used to cause.
    """
    total = 0
    last_id = 0
    while True:
        rows = conn.execute(
            "SELECT id, text FROM chunks "
            "WHERE id > ? AND symbols = '' AND text IS NOT NULL "
            "ORDER BY id LIMIT ?",
            (last_id, _BACKFILL_BATCH),
        ).fetchall()
        if not rows:
            break

        # **`last_id` advances over every row read, not every row written.**
        # A chunk whose text yields no tokens is skipped for the update, and if
        # the cursor only followed updates it would be read again in the next
        # batch - for ever, on a corpus where nothing splits.
        last_id = rows[-1][0]
        updates = [(split(text or ""), chunk_id) for chunk_id, text in rows]
        updates = [pair for pair in updates if pair[0]]
        if updates:
            # **One explicit transaction per batch, and it is not decoration.**
            # The store opens its connections with `isolation_level=None`, so
            # sqlite3 is in autocommit: without a `BEGIN` around it, this
            # `executemany` is five thousand separate transactions and five
            # thousand fsyncs. Measured while writing the test for this - ten
            # thousand rows did not finish inside a minute.
            #
            # The old `fetchall()` version had the same fault and it was hidden
            # behind the far larger one of loading the corpus into memory.
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany("UPDATE chunks SET symbols = ? WHERE id = ?", updates)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        total += len(rows)
        if total % (_BACKFILL_BATCH * 10) == 0:
            _log.info("schema v7: split identifiers in {:,} chunks so far", total)

    if total:
        _log.info("schema v7: identifier backfill complete, {:,} chunks", total)


#: The mail columns people search by name, and their folded twins.
_FOLDED_COLUMNS = (("sender", "sender_lc"), ("recipients", "recipients_lc"),
                   ("subject", "subject_lc"))


def _v14_folded_mail_columns(conn: sqlite3.Connection) -> None:
    r"""Case-folded copies of the mail fields, so `from:josé` finds `JOSÉ@…`.

    **SQLite cannot do this, and neither can `LOWER()`.** Measured rather than
    assumed:

        SELECT 'Dave@x' LIKE '%dave%'   -> 1
        SELECT 'José@x' LIKE '%josé%'   -> 1
        SELECT 'JOSÉ@x' LIKE '%josé%'   -> 0
        SELECT lower('JOSÉ@x')          -> 'josÉ@x'

    `LIKE` is case-insensitive for ASCII only, and the built-in `lower()` is
    too - so `É` never folds, and there is no expression over the existing
    column that fixes it. Worse, the *other half of the same search* disagrees:
    FTS5's `unicode61` tokeniser folds properly, so a query naming a colleague
    with an accent matched the message body and missed the sender field.

    Python's `str.lower()` does fold Unicode, so the fold is done once at write
    time and stored. Additive and nullable, which is what makes it safe: the
    filters read `COALESCE(sender_lc, sender)`, so an interrupted backfill
    degrades to exactly today's behaviour rather than losing rows.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    for _source, folded in _FOLDED_COLUMNS:
        if folded not in existing:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {folded} TEXT")

    # Batched, for the reason `_backfill_symbols` documents at length: a mail
    # archive is tens of millions of rows and this runs inside `connect()`.
    last_id = 0
    filled = 0
    while True:
        rows = conn.execute(
            "SELECT file_id, sender, recipients, subject FROM messages "
            "WHERE file_id > ? AND sender_lc IS NULL "
            "ORDER BY file_id LIMIT ?",
            (last_id, _BACKFILL_BATCH),
        ).fetchall()
        if not rows:
            break
        last_id = rows[-1][0]
        updates = [
            (str(sender or "").lower(), str(recipients or "").lower(),
             str(subject or "").lower(), file_id)
            for file_id, sender, recipients, subject in rows
        ]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                "UPDATE messages SET sender_lc = ?, recipients_lc = ?, "
                "subject_lc = ? WHERE file_id = ?", updates)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        filled += len(rows)

    if filled:
        _log.info("schema v14: folded the mail fields of {:,} messages", filled)


def _v13_repair_indexes(conn: sqlite3.Connection) -> None:
    r"""Put back the two indexes v10 dropped, and re-plan against them.

    **A repair, not a feature.** `_v10_name_only_status` rebuilds `files` to
    widen a CHECK constraint, and `DROP TABLE files` takes every index with it.
    Its recreate list held six of the eight, so `idx_files_mtime` and
    `idx_files_source_kind` were gone from that moment on - permanently, since
    `_v5_missing_indexes` had already run and migrations never run twice.

    The cost is not subtle. v5's own docstring records `idx_files_mtime` as the
    **6.30ms to 0.04ms** fix for the filter-only scan; without it every "newest
    first", every `after:` and every `before:` full-scans `files`. On a corpus
    of a few thousand rows nobody notices. At twenty million it is the query.

    v10 is fixed too, so a database migrating from v9 today never loses them.
    This exists for the ones that already did, and there is no way to tell those
    apart from a fresh database after the fact - so it simply asserts the end
    state. `IF NOT EXISTS` makes that free where nothing is missing.

    **`ANALYZE` is half the point.** SQLite's planner chooses from
    `sqlite_stat1`, and those statistics were gathered while the indexes did not
    exist. Recreating an index that the planner has been told is useless is a
    disk-space change and nothing else.

    Never raises. A database that cannot be re-analysed is still a correct
    database, and refusing to open one over a query-plan optimisation would turn
    a slow search into no search at all.
    """
    for name, definition in (
        ("idx_files_source_kind", "files(source_kind)"),
        ("idx_files_mtime", "files(mtime_ns)"),
    ):
        try:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {definition}")
        except sqlite3.Error as exc:            # a table mid-recovery
            _log.warning(
                "could not restore {}: {}. Searches ordered or filtered by date "
                "will scan the whole file table until it exists - re-run the "
                "application once nothing else has the database open.", name, exc)
            return

    try:
        conn.execute("ANALYZE")
    except sqlite3.Error as exc:
        _log.warning(
            "could not re-analyse the index after restoring the date indexes: "
            "{}. They exist but the query planner may keep ignoring them; "
            "running `app.cli stats` later will re-analyse.", exc)


def _v15_saved_searches(conn: sqlite3.Connection) -> None:
    r"""One small table for named searches. Adoptions §3.

    **A query, not a result set.** The row holds the text somebody typed and
    the scope they typed it in, and running it re-executes the search - so a
    saved search is a smart folder rather than a snapshot, and a file indexed
    tomorrow appears in it without anybody re-saving anything. Storing the
    result ids instead would have been less code and a feature that silently
    goes stale, which is the worse of the two by a distance.

    **`name_lc` is folded in Python, for the reason v14 records at length.**
    SQLite's `LOWER()` is ASCII-only and `UNIQUE` on the raw name would let
    `Leeds` and `leeds` both exist - two rows nobody can tell apart in a menu.
    The folded copy carries the uniqueness; the typed spelling is what is
    shown, because showing somebody their own name back in a different case
    reads as a correction.

    Additive, and nothing else in the schema refers to it: a database that
    fails to gain this table loses saved searches and keeps every document.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS saved_searches (
            id          INTEGER PRIMARY KEY,
            name        TEXT    NOT NULL,
            name_lc     TEXT    NOT NULL UNIQUE,
            query       TEXT    NOT NULL,
            scope       TEXT    NOT NULL DEFAULT 'all',
            created_at  INTEGER NOT NULL,
            run_count   INTEGER NOT NULL DEFAULT 0,
            last_run_at INTEGER
        );

        -- The menu orders by how often a search is actually run, so that is
        -- the column the menu's ORDER BY has to be able to walk. Forty rows
        -- would not need it; it costs nothing and removes the question.
        CREATE INDEX IF NOT EXISTS idx_saved_run_count
            ON saved_searches(run_count DESC, name);
    """)


def _v16_chunk_label(conn: sqlite3.Connection) -> None:
    r"""`chunks.label` — where inside a document a chunk starts. Adoptions §6a.

    **`page` was never enough for a spreadsheet.** It holds the sheet index, so
    a hit in a forty-thousand-row workbook says "sheet 3" and stops. The row is
    what turns that into an answer, and `Q3!A14` is an address the person can
    paste into the Name Box.

    One nullable column rather than three (sheet, row, column). The value is
    only ever read whole, three columns would be three places to forget, and
    `!` is the spreadsheet's own separator - so what is stored is also what
    somebody could type. `extract/cells.py` builds it and parses it back.

    Additive and nullable, so an index that has not been rebuilt keeps every
    row and simply has no locators until the next run touches those files -
    which is the correct degradation for a label.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    if "label" not in existing:
        conn.execute("ALTER TABLE chunks ADD COLUMN label TEXT")


def _v17_image_phash(conn: sqlite3.Connection) -> None:
    r"""`files.phash` — a perceptual hash for photos. Work order 0h §2a.

    **Lives beside `content_hash`, not in the image-vector LanceDB table -
    a genuine design decision, made here rather than guessed at.** The work
    order left the storage location open ("a new SQLite column ... or a new
    LanceDB column"). `files` already carries every other per-file scalar a
    photo has - `content_hash`, `ext`, `mtime_ns` - and `app/search/
    folding.py`'s existing copy-fold already reads `content_hash` straight
    off a hydrated `SearchResult` for exactly the same purpose this column
    exists for: telling two rows apart as "the same picture". A perceptual
    hash is a natural extension of that same fact, not a new one - it
    belongs where the fact it is closest to already lives.

    It is also **not** derived from a CLIP forward pass and has nothing to
    do with the embedding model - `imagehash.phash` is a DCT over the
    pixels, independent of `ClipImageEmbedder` entirely (see
    `app/index/phash.py`) - so there is no argument from "it is written in
    the same pass as the vector" that it belongs in that vector's own table.
    `vector_store.py`'s own opening comment calls that table "DERIVED...
    regenerated from SQLite" - conceptually the wrong shelf for a fact that
    is not derived from anything else stored here, SQLite included.

    Additive and nullable, so an index built before this migration keeps
    every row and simply has no pHash until the next images pass touches
    each file - the same degradation `_v16_chunk_label` already accepts for
    `label`. Written by `Pipeline._maybe_compute_phash`
    (`app/index/pipeline.py`), batched through `SqliteStore.set_phashes` the
    same way the CLIP vector flush already batches - see that method's
    docstring for the H7-shaped reasoning.

    The partial index mirrors `idx_files_skip`'s shape (`WHERE skip_code IS
    NOT NULL`): most rows will have no pHash for a long time after this
    migration runs on an existing index, and an index entry for a NULL that
    can never be searched for is pure write cost with no reader.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "phash" not in existing:
        conn.execute("ALTER TABLE files ADD COLUMN phash TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_files_phash ON files(phash) "
        "WHERE phash IS NOT NULL"
    )


def _v18_photo_taken_at(conn: sqlite3.Connection) -> None:
    r"""`files.taken_at_ns` — a photograph's EXIF shot date. Work order 0f §3a.

    **A column of its own, because `mtime_ns` cannot be both things.** The
    obvious fix for "a 2006 photo copied in 2019 sorts as 2019" is to write the
    EXIF date into `mtime_ns` and change nothing else. That breaks incremental
    indexing outright: `app/index/walker.py` compares the stored `mtime_ns`
    against the file's live mtime to decide whether a file changed, so every
    photo would differ from its own row on every pass and be re-read, re-OCRed
    and re-embedded forever. `mtime_ns` stays the file's real mtime and answers
    only "did this change"; `taken_at_ns` answers "when is this from". Two
    questions, two columns - commit `c58dca9` diagnosed exactly this and
    stopped rather than guess, which is why the column exists.

    Nanoseconds since the epoch, matching `mtime_ns` so the two are directly
    comparable and `file_filter_sql` can substitute one for the other without
    converting units at query time.

    Additive and nullable, the same degradation `_v16_chunk_label` and
    `_v17_image_phash` already accept: an index built before this keeps every
    row and simply has no shot date until a later run re-touches each photo.
    NULL is also the permanent, correct value for every file that is not a
    photograph - which is nearly all of them - and the filter reads it as
    "fall back to `mtime_ns`".

    **The partial index earns its place at query time, not just on write.**
    `after:`/`before:` cannot use `idx_files_mtime` alone once the shot date
    can override it, and the obvious spelling of that comparison -
    `COALESCE(taken_at_ns, mtime_ns) <= ?` - is not sargable and scans.
    Measured on a 200,000-row table with a selective cutoff: the COALESCE form
    scanned in 5.79ms, while the two-branch OR `file_filter_sql` now emits ran
    in 0.86ms against these two indexes (the old single-column comparison was
    0.37ms). `files` is aimed at twenty million rows, so that difference is the
    reason this index is here and the reason the clause is shaped as it is.
    `WHERE taken_at_ns IS NOT NULL` mirrors `idx_files_phash`: most rows will
    never have one.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "taken_at_ns" not in existing:
        conn.execute("ALTER TABLE files ADD COLUMN taken_at_ns INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_files_taken_at ON files(taken_at_ns) "
        "WHERE taken_at_ns IS NOT NULL"
    )


def _v19_offline_media_volumes(conn: sqlite3.Connection) -> None:
    r"""`volumes` and `files.volume_id`/`relative_path`. Orders 202626270513/14.

    **Renumbered from v17 to v19 at merge time** — this branch was written
    against a base before `_v17_image_phash` (0h §2a) and `_v18_photo_taken_at`
    (0f §3a) had landed on the main line, and both claimed v17 independently.
    Nothing about this migration's own logic changed; only its version number
    and its position in `MIGRATIONS` did.

    **Additive, and no re-index.** A new table and two nullable columns: an
    existing 100GB index gains these in seconds and every row keeps whatever
    it had. `volume_id` stays NULL - "an ordinary, always-connected file" -
    until a Scan on the Offline Media tab attributes rows to a catalogued
    source, which is a correct state rather than a broken one.

    One table for every kind (drive, network, cloud, phone, archived) rather
    than one per order, because building 202626270513's table first and
    202626270514's second would have forced a second, incompatible design onto
    rows the first order had already written. `kind` and the two orders'
    identity shapes (`volume_guid`+`hardware_serial` for a drive,
    `identity_key` alone for a UNC path or a cloud account) were read from
    both work orders before this ran once.

    `ON DELETE SET NULL`, deliberately not `CASCADE` - the same reasoning as
    `repo_id` (`_v6_repositories`). Deleting a volume's catalogue entry must
    not silently be how someone deletes 40,000 indexed rows; §2c's Delete
    verb does that explicitly, with a stated count, never as a side effect of
    this foreign key.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS volumes (
            id                 INTEGER PRIMARY KEY,
            kind               TEXT    NOT NULL,
            identity_key       TEXT    NOT NULL UNIQUE,
            volume_guid        TEXT,
            hardware_serial    TEXT,
            fs_label           TEXT,
            name               TEXT    NOT NULL,
            description        TEXT,
            location_note      TEXT,
            status             TEXT    NOT NULL DEFAULT 'OFFLINE',
            sequential_medium  INTEGER NOT NULL DEFAULT 0,
            first_seen         INTEGER NOT NULL,
            last_seen          INTEGER NOT NULL,
            last_scanned_at    INTEGER,
            size_bytes         INTEGER,
            file_count         INTEGER,
            CHECK (kind IN ('drive', 'network', 'cloud', 'phone', 'archived')),
            CHECK (status IN ('ONLINE', 'OFFLINE', 'LOCKED', 'ARCHIVED'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_volumes_kind ON volumes(kind)")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "volume_id" not in columns:
        conn.execute(
            "ALTER TABLE files ADD COLUMN volume_id INTEGER "
            "REFERENCES volumes(id) ON DELETE SET NULL"
        )
    if "relative_path" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN relative_path TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_files_volume ON files(volume_id) "
        "WHERE volume_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_files_volume_relpath "
        "ON files(volume_id, relative_path) WHERE volume_id IS NOT NULL"
    )
    conn.execute("ANALYZE")


def _v20_file_tags(conn: sqlite3.Connection) -> None:
    r"""`file_tags` - Florence-2 tag vocabulary, for the `/shows` operator.

    **Renumbered from v19 to v20 at merge time** — written against a base
    before `_v19_offline_media_volumes` (orders 202626270513/14) had landed
    on the main line, and both claimed v19 independently. Nothing about this
    migration's own logic changed; only its version number and its position
    in `MIGRATIONS` did.

    Work order 0i sections 1a-1c. The tag words themselves already flow into
    search through the ordinary document/chunk/FTS path - 1b's "zero new
    storage concepts" is about that text. This table is the different thing
    1c asks for: a **browsable vocabulary with counts**, the same job `repos`
    (schema v6) does for `/repo` - `distinct_values` needs a real table to
    `GROUP BY`, and free text inside a chunk cannot be grouped or counted
    cheaply behind a keystroke.

    One row per (file, tag) - a file commonly has several tags, unlike a
    repository, which owns exactly one file each. `ON DELETE CASCADE`
    because a tag row has no meaning once its file is gone - the same
    reasoning `chunks` already uses, and different from `repos`' `SET NULL`
    on `files.repo_id`, where the *file* survives a repository disappearing.

    Additive: an index built before this migration keeps every row and
    simply has no tags until the next Florence-2 pass touches each photo -
    the same degradation `_v17_image_phash` and `_v18_photo_taken_at` already
    accept for their own columns.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_tags (
            file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            tag        TEXT    NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_tags_file ON file_tags(file_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags(tag)")


def _v21_taken_at_is_hint(conn: sqlite3.Connection) -> None:
    r"""`files.taken_at_is_hint` - is `taken_at_ns` a guess or a fact?

    **Renumbered from v20 to v21 at merge time**, for the same reason
    `_v20_file_tags` was — see that function's note.

    Work order 0i section 4b. `taken_at_ns` (schema v18) already answers
    "when is this from"; this answers "how much should that be trusted".
    EXIF's `DateTimeOriginal` is a fact the camera wrote once - `taken_at_
    is_hint` is 0 for it. A folder-year era hint ("Diwali 2004") is a
    guess from a human-chosen album name, not a camera - 1 for it. The
    distinction matters because work order 0512's future batch-era control
    ("these are roughly 1998-2002") must be able to find and override only
    the guesses, never a camera's own timestamp - a single `taken_at_ns`
    column cannot answer "which kind is this one" on its own.

    Additive and defaulted to 0 (a fact, not a hint), the same degradation
    `_v17_image_phash` and `_v18_photo_taken_at` already accept: an index
    built before this migration keeps every row, and every existing
    `taken_at_ns` it already holds is EXIF-sourced (era hints did not exist
    before this order), so 0 is not merely a safe default here - it is the
    correct historical fact for every row that predates this column.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "taken_at_is_hint" not in existing:
        conn.execute(
            "ALTER TABLE files ADD COLUMN taken_at_is_hint INTEGER NOT NULL DEFAULT 0")


def _v22_places(conn: sqlite3.Connection) -> None:
    r"""`files.place` - the nearest town to a photo's EXIF GPS, offline.

    **Renumbered from v21 to v22 at merge time**, for the same reason
    `_v20_file_tags` was — see that function's note.

    Work order 0i section 4a. A single nullable column, not a join table
    like `file_tags` (schema v20): a photo has exactly one GPS reading and
    therefore at most one place, where a photo commonly carries several
    Florence-2 tags - the cardinality is the whole reason one is a table and
    the other is a column, the same reasoning `taken_at_ns` already used
    against `file_tags` when 4b was built.

    Additive and nullable - an index built before this migration keeps
    every row and simply has no place until the next images pass re-touches
    each photo, the same degradation every column this order has added
    already accepts. The partial index mirrors `idx_files_taken_at`: most
    rows have no GPS and never will.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "place" not in existing:
        conn.execute("ALTER TABLE files ADD COLUMN place TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_files_place ON files(place) "
        "WHERE place IS NOT NULL")


def _v23_people(conn: sqlite3.Connection) -> None:
    r"""`piles` and `faces` - the Photo Tagger's own storage. Work order 0j
    (`202626270512`), the whole order.

    **One table for both an unnamed pile and a named person**, deliberately -
    the guardrails' own principled line is "detection and grouping are
    automatic; IDENTITY ONLY EVER COMES FROM THE USER", and a pile that
    becomes a person the moment somebody names it is the same row before and
    after, not a promotion between two tables. `piles.name IS NULL` is an
    unnamed pile ("Person 1 - 47 photos", the count and label computed, not
    stored); `piles.name IS NOT NULL` is what `/who` and the `People:`
    segment (section 3a) read.

    **`faces` is the automatic half, `piles.name` is the only place identity
    lives.** A face row records what detection and clustering found -
    `embedding` for the incremental cosine clustering (section 1b), `bbox_*`
    for the sample crop the grid (section 2a) draws - and never a name of
    its own. `pile_id` is nullable and `ON DELETE SET NULL` rather than
    `CASCADE`: deleting a pile (a merge's losing side, or the second half of
    "Forget this person" - section 2e) must return its faces to the unnamed
    pool, never delete photos' worth of detections silently.

    **`suggested_pile_id` is the learning loop's own queue** (section 2c) -
    "close to a named pile, not confident enough to auto-assign" is a
    different state from "assigned" and from "unclustered", and conflating
    any two of the three loses either the suggestion chip or the auto-assign
    guarantee.

    Additive: an index built before this migration has no `piles`/`faces`
    rows at all, which is the correct state for the Photo Tagger's own
    switch (`PEOPLE_RECOGNITION_ENABLED`) defaulting OFF - see the order's
    guardrails. `ON DELETE CASCADE` from `files` (not `SET NULL`, unlike
    `pile_id`): a face detection has no meaning once its photo is gone, the
    same reasoning `file_tags` (schema v20) already uses.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS piles (
            id         INTEGER PRIMARY KEY,
            name       TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_piles_name ON piles(name) "
        "WHERE name IS NOT NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS faces (
            id                 INTEGER PRIMARY KEY,
            file_id            INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            bbox_x             REAL    NOT NULL,
            bbox_y             REAL    NOT NULL,
            bbox_w             REAL    NOT NULL,
            bbox_h             REAL    NOT NULL,
            embedding          BLOB    NOT NULL,
            pile_id            INTEGER REFERENCES piles(id) ON DELETE SET NULL,
            confidence         REAL,
            suggested_pile_id  INTEGER REFERENCES piles(id) ON DELETE SET NULL,
            created_at         INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faces_file ON faces(file_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faces_pile ON faces(pile_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_faces_suggested ON faces(suggested_pile_id) "
        "WHERE suggested_pile_id IS NOT NULL")
    # Clustering (section 1b) asks "which faces have no pile yet" every run -
    # a plain `pile_id IS NULL` scan with nothing to seek to on a corpus
    # where most faces are already sorted. The partial index is exactly this
    # query's shape, the same reasoning `idx_files_place` already uses.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_faces_unassigned ON faces(id) "
        "WHERE pile_id IS NULL AND suggested_pile_id IS NULL")
    # **"Scanned, found nothing" needs its own marker.** A photo that
    # genuinely has no face in it never gets a `faces` row, so a backfill
    # query that asks "does this file have any face rows" would re-scan it
    # on every single drain, forever - precisely the H1 bug class 0i's own
    # review found for extraction ("every skipped file is re-extracted on
    # every incremental run"). This table answers "have we looked" as its
    # own fact, independent of "did we find one".
    conn.execute("""
        CREATE TABLE IF NOT EXISTS face_scans (
            file_id     INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
            scanned_at  INTEGER NOT NULL
        )
    """)


def _v24_content_hash_index(conn: sqlite3.Connection) -> None:
    r"""Work order 202626270602 (0n) §3c: the Space Report's own performance
    box - "hash and pHash columns get the indexes these GROUP BYs need."

    `content_hash` has carried duplicate-detection data since it was added
    to `files`, and nothing ever indexed it. The Space Report's whole first
    half (§3a: total duplicate bytes, the largest duplicate groups) is a
    `GROUP BY content_hash HAVING COUNT(*) > 1` over the full table - the
    exact shape H2's own lesson (`docs/REVIEW-2026-08-26.md`) warns against
    running unindexed at scale. `idx_files_phash` already exists (schema
    v17) for the photo half of the same report; this is its missing
    sibling for the general-file half.

    Partial, same reasoning `idx_files_phash`/`idx_files_skip` already use:
    most rows outside the exact-duplicate set share their hash with
    nothing, and an index entry for a hash nothing will ever `GROUP BY`
    into a group of one is write cost with no reader.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_files_content_hash ON files(content_hash) "
        "WHERE content_hash IS NOT NULL")


def _v25_partial_status(conn: sqlite3.Connection) -> None:
    r"""Let `files.status` hold `PARTIAL`. Work order 202626270114 (0b)
    section 6d - see `SqliteStore.FileStatus.PARTIAL`'s own docstring for
    what it means and why `PENDING`/`INDEXED` were not enough.

    **The same twelve-step dance as `_v10_name_only_status`, for the same
    reason: `status` carries a `CHECK` constraint, which SQLite cannot
    alter in place.** That migration's own docstring has the full hazard
    account (`DROP TABLE files` cascades to every table with a foreign key
    onto it while `PRAGMA foreign_keys = ON`) - it applies unchanged here,
    against a wider table: `files` gained `repo_id`, `volume_id`,
    `relative_path`, `phash`, `taken_at_ns`, `taken_at_is_hint` and `place`
    since v10, and six tables now cascade from it (`chunks`, `messages`,
    `entity_mentions`, `file_tags`, `faces`, `face_scans`), not three.

    The column and index lists below are copied from a fully-migrated
    database's own `PRAGMA table_info(files)` and `sqlite_master`, not
    retyped from memory - the risk in a rebuild is exactly a column or
    index quietly left out, and `test_the_rebuild_preserves_every_column_
    and_index` in `test_partial_status_migration.py` checks both lists
    against a fresh v24 database before this migration runs, so a future
    column added to `files` without updating this migration fails loudly
    here rather than silently losing data at whoever's real database
    happens to run it first.
    """
    if _status_allows(conn, "PARTIAL"):
        return                                # already rebuilt, or a fresh schema

    steps = [
        """CREATE TABLE files_rebuilt (
                id                INTEGER PRIMARY KEY,
                path              TEXT    NOT NULL UNIQUE,
                parent_dir        TEXT    NOT NULL,
                ext               TEXT    NOT NULL,
                size_bytes        INTEGER NOT NULL,
                mtime_ns          INTEGER NOT NULL,
                content_hash      TEXT,
                status            TEXT    NOT NULL,
                skip_code         TEXT,
                skip_detail       TEXT,
                indexed_at        INTEGER,
                source_kind       TEXT    NOT NULL,
                repo_id           INTEGER REFERENCES repos(id) ON DELETE SET NULL,
                volume_id         INTEGER,
                relative_path     TEXT,
                phash             TEXT,
                taken_at_ns       INTEGER,
                taken_at_is_hint  INTEGER NOT NULL DEFAULT 0,
                place             TEXT,
                CHECK (status IN ('PENDING', 'INDEXED', 'SKIPPED', 'FAILED',
                                  'NAME_ONLY', 'PARTIAL'))
           )""",
        """INSERT INTO files_rebuilt
                SELECT id, path, parent_dir, ext, size_bytes, mtime_ns,
                       content_hash, status, skip_code, skip_detail,
                       indexed_at, source_kind, repo_id, volume_id,
                       relative_path, phash, taken_at_ns, taken_at_is_hint,
                       place
                FROM files""",
        "DROP TABLE files",
        "ALTER TABLE files_rebuilt RENAME TO files",
        # Every index `PRAGMA table_info`/`sqlite_master` reported on a
        # fresh v24 database, recreated verbatim - `DROP TABLE` takes every
        # one of them with it.
        "CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)",
        "CREATE INDEX IF NOT EXISTS idx_files_dir ON files(parent_dir)",
        "CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext)",
        "CREATE INDEX IF NOT EXISTS idx_files_skip ON files(skip_code) "
        "WHERE skip_code IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_files_volume ON files(volume_id) "
        "WHERE volume_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_files_volume_relpath "
        "ON files(volume_id, relative_path) WHERE volume_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_files_source_kind ON files(source_kind)",
        "CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo_id) "
        "WHERE repo_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime_ns)",
        "CREATE INDEX IF NOT EXISTS idx_files_phash ON files(phash) "
        "WHERE phash IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_files_taken_at ON files(taken_at_ns) "
        "WHERE taken_at_ns IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_files_place ON files(place) "
        "WHERE place IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_files_content_hash ON files(content_hash) "
        "WHERE content_hash IS NOT NULL",
    ]

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in steps:
            conn.execute(statement)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        # **Back on whatever happened.** Leaving them off would silently
        # disable every cascade for the life of the connection, which is a
        # far worse state than a failed migration - see `_v10_name_only_
        # status`'s own identical comment.
        conn.execute("PRAGMA foreign_keys = ON")


def _status_allows(conn: sqlite3.Connection, value: str) -> bool:
    """Whether `files.status` already permits `value`. Never raises."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and value in str(row[0] or "")


def _v26_chat_sessions(conn: sqlite3.Connection) -> None:
    r"""One table for saved Chat conversations. Work order 202626270611, 3d.

    **A conversation, not a result set.** A row holds the turns as the tab
    displayed them (JSON: text, receipts, the kind of answer) and the context
    shelf as it stood, so reopening a session shows what was shown and follows
    up from the same documents. The receipts carry `file_id`s and the quoted
    words themselves, so a session whose documents have since been re-indexed
    still reads correctly - the quote is the evidence at the time.

    Additive, and nothing else in the schema refers to it: a database that
    fails to gain this table loses saved conversations and keeps every
    document. **The index-sensitivity sentence extends to this table**: the
    quotes in a session are the contents of indexed files.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id          INTEGER PRIMARY KEY,
            title       TEXT    NOT NULL,
            model       TEXT    NOT NULL DEFAULT '',
            turns_json  TEXT    NOT NULL DEFAULT '[]',
            shelf_json  TEXT    NOT NULL DEFAULT '[]',
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL
        );

        -- The sidebar lists the most recently used first.
        CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated
            ON chat_sessions(updated_at DESC);
    """)


def _v27_mail_sent_date(conn: sqlite3.Connection) -> None:
    r"""Every message's sent date, backfilled into `files.taken_at_ns`.

    **The owner's "mail from 2017" found no 2017 mail, and this is why.**
    `after:`/`before:` compare `taken_at_ns` when a row has one and
    `mtime_ns` otherwise (`app/storage/filters.py::_date_clause`). A message
    read out of a `.pst` has no file of its own, so the pipeline wrote it with
    the *archive's* `mtime_ns` - and an archive the mail client touched last
    week made every letter in it "last week". Loose `.eml`/`.msg` files had
    the milder form of the same fault: a saved copy's mtime is the day it was
    saved. `messages.sent_at` always held the right answer; only the Mail tab
    read it.

    **Why this column, rather than joining `messages` in the filter.**
    `taken_at_ns` already means "the date this is from" - it is what the
    filter, the result date, recency, version folding and a filter-only
    browse all read before `mtime_ns` - so writing the sent date there fixes
    every one of them at once, and the filter keeps the two-index shape it
    was measured with. A join would have fixed the filter alone and added a
    third branch to it; measured, it was no faster (see `_range_clause`).
    The readers that must *not* see a message as a photograph already exclude
    mail: the timeline's camera branch and the Files tab both require
    `source_kind = 'file'`, and mail has its own timeline branch on
    `messages.sent_at`. `mtime_ns` is left alone - change detection compares
    it against the archive, exactly as `_v18_photo_taken_at` explains.

    **A sent date is a fact, so it replaces a folder-year guess** (`taken_at_
    is_hint = 1`, which `apply_batch_era` can write onto any row under a
    folder) and is written with the flag cleared, so a later era hint can
    never overwrite it. A real camera date on a mail row cannot exist - the
    pipeline only reads EXIF for images - so nothing a fact wrote is replaced.

    Messages with no sent date, or one past what nanoseconds can hold
    (`SENT_AT_LIMIT_S`), keep NULL and go on falling back to `mtime_ns` -
    no date is better than a wrong one. New messages get the same value at
    index time (`pipeline._sent_at_ns`); this is only for the ones already
    indexed, so nobody has to rebuild a mailbox index to benefit.

    Idempotent: a second run finds every row already equal to its message.
    One `UPDATE`. Measured 2026-09-27 on a Linux sandbox against a
    200,000-row `files` table holding 60,000 messages: 167 ms, once.
    """
    from app.storage.filters import MAIL_KINDS, SENT_AT_LIMIT_S

    marks = ", ".join("?" for _ in MAIL_KINDS)
    conn.execute(
        f"""UPDATE files
               SET taken_at_ns = (SELECT m.sent_at * 1000000000 FROM messages m
                                  WHERE m.file_id = files.id),
                   taken_at_is_hint = 0
             WHERE source_kind IN ({marks})
               AND (taken_at_ns IS NULL OR taken_at_is_hint = 1)
               AND id IN (SELECT file_id FROM messages
                          WHERE sent_at > 0 AND sent_at <= ?)""",
        (*MAIL_KINDS, SENT_AT_LIMIT_S),
    )


def _v28_chunk_index_follows_its_columns(conn: sqlite3.Connection) -> None:
    r"""The keyword index is rewritten only when a passage's words change.

    Work order 0x item 5d. **Marking a passage embedded used to re-index its
    words.** `chunks_au` fired on *any* UPDATE of a `chunks` row, and the
    commonest UPDATE by far is the embedding thread's
    `UPDATE chunks SET embedded = 1`, once for every passage the indexer
    writes. Each one told the keyword index to delete the passage and add it
    again - the same words, twice the index work, and a delete marker left
    behind in the index until a merge cleared it. `mark_all_unembedded`
    (re-embed everything) did the same to the whole index at once.

    Now the trigger fires only for `UPDATE OF text, symbols` - the two columns
    `chunks_fts` holds. An UPDATE that changes neither cannot change what the
    index should contain, so nothing any search can see is different. An
    UPDATE that names either column (the v10 backfill of `symbols` did) is
    mirrored exactly as before.

    Measured 2026-09-27, Linux sandbox: writing the medium benchmark corpus's
    19,077 passages into a fresh store on one thread, with the embedding
    thread's updates replayed every 256 passages - 1.45 ms a document with the
    old trigger, 1.11 ms with this one (the same writes with no updates at all
    cost 1.16 ms). End to end, `app.cli bench-pipeline --full-speed`, fake
    embedder, same sandbox and day, interleaved with the version before:
    medium corpus 85.8 s -> 60.8 s median over 3 runs each (-29.1%); small
    13.07 s -> 12.36 s over 5 each (-5.4%); the same documents, passages and
    vectors.

    Only a trigger is replaced: no row is read or written, so this is instant
    on any size of index. Idempotent.
    """
    conn.execute("DROP TRIGGER IF EXISTS chunks_au")
    for statement in CONTENT_TRIGGERS:
        if "chunks_au" in statement:
            conn.execute(statement)


def _v29_image_hashes(conn: sqlite3.Connection) -> None:
    r"""The junk-image filter's book: one row per picture's bytes. Order 0z lane D.

    A signature logo repeated in four thousand messages across a dozen
    archives was read by OCR in every archive, on every run that re-read one.
    This remembers, per blake2b content hash (the same 128-bit digest
    `pst_libpff._hash_bytes` already takes), how often those bytes were met and
    how many words reading them gave, so bytes met five times that gave fewer
    than three words are never read again (`app/extract/junk_images.py`).
    `phash`, `width` and `height` let a re-encoded copy of the same logo be
    recognised too.

    **Derived, like everything in the index**: lose it and the cost is one
    reading of each picture again, nothing else. `clear_index` empties it.
    `phash` is the 64-bit perceptual hash as 16 hex characters, the form
    `files.phash` uses. `WITHOUT ROWID` because the hash is the key and the
    row is small. Idempotent.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS image_hashes ("
        " hash TEXT PRIMARY KEY,"
        " seen INTEGER NOT NULL DEFAULT 0,"
        " words INTEGER,"
        " phash TEXT,"
        " width INTEGER NOT NULL DEFAULT 0,"
        " height INTEGER NOT NULL DEFAULT 0,"
        " updated_at INTEGER NOT NULL DEFAULT 0"
        ") WITHOUT ROWID")



def _v30_attachment_names(conn: sqlite3.Connection) -> None:
    r"""Mail attachments join the filename index, so the Files tab finds them.

    **Owner, 1 October 2026:** *"files in emails should come up on the files
    list tab"*. An attachment has always been its own `files` row - keyed
    `<message>/attachments/<name>` - but `files_fts` held only files and
    archives, so typing an attachment's name on the Files tab found nothing.
    `upsert_file` now writes the row for new attachments; this backfills the
    ones already indexed. Idempotent: a row already present is replaced.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'files_fts'").fetchone()
    if exists is None:
        return
    rows = conn.execute(
        "SELECT id, path, parent_dir FROM files "
        "WHERE source_kind IN ('pst_message', 'eml') AND path LIKE '%/attachments/%'"
    ).fetchall()
    for file_id, path, parent_dir in rows:
        name = str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
        conn.execute("INSERT INTO files_fts(rowid, name, folder) VALUES (?, ?, ?)",
                     (file_id, name, parent_dir or ""))

def _v31_attachment_type_and_size(conn: sqlite3.Connection) -> None:
    r"""An attachment's row carries its own type, not the archive's.

    **Owner, 4 October 2026**, from the Files page: every attachment read out
    of a `.pst` was listed as *PST, 4.9 GB* - the archive's `ext` and
    `size_bytes` were written to every document the archive produced
    (`pipeline._row_type_and_size` is the fix for new rows). The type is in
    the row's own key (`.../attachments/<name>`), so it is set here. **The
    size is not recoverable**: it was never stored, so it is set to 0 - shown
    as blank, never as the archive's - until the archive is read again
    (Index now on its line, or a run that rechecks archives). Idempotent.
    """
    from app.extract.source_types import indexed_ext

    rows = conn.execute(
        "SELECT id, path FROM files "
        "WHERE source_kind IN ('pst_message', 'eml') AND path LIKE '%/attachments/%'"
    ).fetchall()
    for file_id, path in rows:
        name = str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        ext = indexed_ext(Path(name))
        conn.execute(
            "UPDATE files SET ext = ?, "
            "size_bytes = CASE WHEN ext = ? THEN 0 ELSE size_bytes END WHERE id = ?",
            (ext, "pst", file_id))


def _v32_message_position(conn: sqlite3.Connection) -> None:
    r"""Where each message sits in its archive: folder path and position.

    **2026-10-04, "Open" on an attachment.** pypff cannot look a message up by
    its number, and walking a 4.9 GB archive for one took 38 s (the owner's
    `2024.pst`). The direct reader now records the folder and the position;
    `pst_attachment.read_attachment` goes straight there and checks the
    number. Rows already indexed keep NULL and are found by searching.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "folder_path" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN folder_path TEXT")
    if "folder_index" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN folder_index INTEGER")


def _v33_outlook_attachment_sizes_and_skip_index(conn: sqlite3.Connection) -> None:
    r"""Two things, both 2026-10-04, code review:

    * **an attachment read out of an `.ost` keeps the archive's size no
      longer.** v31 set the size of every attachment whose row carried the
      `.pst`'s type to 0, and left `.ost` attachments - same reader, same
      fault - at the archive's size. By now v31 has given each its own type,
      so the archive's size is recognised another way: it is the size its own
      message's row carries (a message row keeps its archive's type and size
      on purpose), from either Outlook archive. Set to 0, shown blank, until
      the archive is read again. A correct size is never equal to it.
    * **`idx_files_status_skip`**, so "the skipped rows with these codes"
      (`iter_files(status=..., skip_codes=...)`) is a search, not a scan.

    Idempotent: a second run finds nothing left to change.
    """
    from app.core.row_facts import (
        ATTACHMENT_MARKER, MAIL_SOURCE_KINDS, OUTLOOK_ARCHIVE_EXTS, attachment_sql,
    )

    kinds = ", ".join("?" for _ in MAIL_SOURCE_KINDS)
    exts = ", ".join("?" for _ in OUTLOOK_ARCHIVE_EXTS)
    conn.execute(
        f"""
        UPDATE files SET size_bytes = 0
        WHERE source_kind IN ({kinds}) AND {attachment_sql("files")} AND size_bytes > 0
          AND EXISTS (
                SELECT 1 FROM files AS message
                WHERE message.path = substr(files.path, 1,
                                            instr(files.path, '{ATTACHMENT_MARKER}') - 1)
                  AND message.ext IN ({exts})
                  AND message.size_bytes = files.size_bytes)
        """,
        (*MAIL_SOURCE_KINDS, *OUTLOOK_ARCHIVE_EXTS),
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_files_status_skip ON files(status, skip_code) "
        "WHERE skip_code IS NOT NULL")


def _v34_face_declines(conn: sqlite3.Connection) -> None:
    r"""Who a face has been told it is not. 2026-10-05.

    The suggestion chip's No promises "Leasha will not guess this one on its
    own again", and nothing kept it: declining only cleared the suggestion,
    so the next grouping could suggest the same face for the same person
    again - or file it there outright. The owner's "manage the faces in the
    pile" needs the same memory for "Not this person". Gone with the face or
    the group (`ON DELETE CASCADE`), so a reset leaves none behind.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS face_declines (
            face_id  INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
            pile_id  INTEGER NOT NULL REFERENCES piles(id) ON DELETE CASCADE,
            PRIMARY KEY (face_id, pile_id)
        ) WITHOUT ROWID
    """)


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _v2_usage_logging,
    3: _v3_knowledge_graph,
    4: _v4_filename_index,
    5: _v5_missing_indexes,
    6: _v6_repositories,
    7: _v7_identifier_tokens,
    8: _v8_mail_header_index,
    9: _v9_forget_dragged_column_widths,
    10: _v10_name_only_status,
    11: _v11_wildcard_vocabulary,
    12: _v12_quoted_removed,
    13: _v13_repair_indexes,
    14: _v14_folded_mail_columns,
    15: _v15_saved_searches,
    16: _v16_chunk_label,
    17: _v17_image_phash,
    18: _v18_photo_taken_at,
    19: _v19_offline_media_volumes,
    20: _v20_file_tags,
    21: _v21_taken_at_is_hint,
    22: _v22_places,
    23: _v23_people,
    24: _v24_content_hash_index,
    25: _v25_partial_status,
    26: _v26_chat_sessions,
    27: _v27_mail_sent_date,
    28: _v28_chunk_index_follows_its_columns,
    29: _v29_image_hashes,
    30: _v30_attachment_names,
    31: _v31_attachment_type_and_size,
    32: _v32_message_position,
    33: _v33_outlook_attachment_sizes_and_skip_index,
    34: _v34_face_declines,
}

#: Released migrations that open and close transactions of their own -
#: `executescript` commits whatever is open first, and `PRAGMA foreign_keys`
#: is ignored inside a transaction - so they cannot run inside the one
#: `apply_migrations` wraps every other step in. Never edited once released;
#: `test_storage_code_review.py` holds this list to their source.
_OWN_TRANSACTION = frozenset({2, 3, 7, 8, 10, 14, 15, 25, 26})


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
        found = _apply_step(conn, step, migration)

    return found


def _apply_step(conn: sqlite3.Connection, step: int,
                migration: Callable[[sqlite3.Connection], None]) -> int:
    r"""Run one migration and record it - **both or neither.** Returns the
    version the file is at afterwards.

    2026-10-04, code review: each step ran in autocommit, so a failure part
    way left half a migration in the file with the old version beside it,
    and two processes opening the same index (the window and an index run
    started from the command line) could both run the same step. Now a step
    takes the write lock (`BEGIN IMMEDIATE`), reads the version again - the
    other process may have done it while this one waited - and commits the
    change and the new version together. A step in `_OWN_TRANSACTION` manages
    its own and runs as it always did, after the same second look.

    A failure is rolled back and reported as `ERR_MIGRATION_FAILED`, with a
    way out, rather than as a bare `sqlite3` error.
    """
    if conn.in_transaction:                       # nothing may ride along unseen
        conn.commit()
    try:
        if step in _OWN_TRANSACTION:
            if read_version(conn) >= step:
                return read_version(conn)
            migration(conn)
            if conn.in_transaction:               # a caller's implicit one, as before
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            if read_version(conn) < step:
                _write_version(conn, step)
            conn.execute("COMMIT")
            return read_version(conn)
        conn.execute("BEGIN IMMEDIATE")
        found = read_version(conn)
        if found < step:
            migration(conn)
            _write_version(conn, step)
            found = step
        conn.execute("COMMIT")
        return found
    except AppErrorException:
        _roll_back(conn)
        raise
    except Exception as exc:                      # noqa: BLE001 - reported with a fix
        _roll_back(conn)
        raise AppErrorException(make_error(
            "ERR_MIGRATION_FAILED", "storage.migrations",
            step=step, reason=f"{type(exc).__name__}: {exc}",
            details=f"{getattr(migration, '__name__', migration)}: {exc}",
        )) from exc


def _roll_back(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
