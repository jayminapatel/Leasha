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

__all__ = ["CURRENT_VERSION", "apply_migrations", "read_version", "MIGRATIONS",
           "trigram_available"]

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

#: The schema version this build creates and understands.
CURRENT_VERSION = 13

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


def _status_allows(conn: sqlite3.Connection, value: str) -> bool:
    """Whether `files.status` already permits `value`. Never raises."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and value in str(row[0] or "")


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
