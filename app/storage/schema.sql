-- Local Knowledge Graph V2 — SQLite schema
-- Layer: L1
-- SQLite is the authority on files, chunks and indexing state.
-- LanceDB is a DERIVED store: if the two disagree, this file wins and the
-- vectors are rebuilt from `chunks`. Never the reverse.

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Files
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    parent_dir    TEXT    NOT NULL,
    ext           TEXT    NOT NULL,
    size_bytes    INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    content_hash  TEXT,                      -- blake2b of bytes; NULL until read
    status        TEXT    NOT NULL,          -- PENDING | INDEXED | SKIPPED | FAILED
    skip_code     TEXT,                      -- AppError.code when SKIPPED/FAILED
    skip_detail   TEXT,
    indexed_at    INTEGER,
    source_kind   TEXT    NOT NULL,          -- file | pst_message | eml
    CHECK (status IN ('PENDING', 'INDEXED', 'SKIPPED', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_dir    ON files(parent_dir);
CREATE INDEX IF NOT EXISTS idx_files_ext    ON files(ext);
CREATE INDEX IF NOT EXISTS idx_files_skip   ON files(skip_code) WHERE skip_code IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Chunks
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    char_start  INTEGER,                     -- offsets into the extracted text,
    char_end    INTEGER,                     -- so results can highlight in place
    page        INTEGER,                     -- PDF page / slide number / sheet ref
    embedded    INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_file_ord ON chunks(file_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_chunks_pending ON chunks(embedded) WHERE embedded = 0;

-- ---------------------------------------------------------------------------
-- Email metadata — kept separate to avoid a wide sparse `files` table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS messages (
    file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    store_path   TEXT,                       -- originating .pst
    entry_id     TEXT,                       -- MAPI EntryID
    conversation TEXT,
    subject      TEXT,
    sender       TEXT,
    recipients   TEXT,                       -- JSON array
    sent_at      INTEGER,
    has_attach   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_conv   ON messages(conversation);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender);
CREATE INDEX IF NOT EXISTS idx_messages_sent   ON messages(sent_at);

-- ---------------------------------------------------------------------------
-- Full-text index — external content table over `chunks`
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- External-content FTS5 tables are not maintained automatically.
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

-- ---------------------------------------------------------------------------
-- Indexing state — the cursor that makes a 100GB run resumable
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS index_state (
    key        TEXT    PRIMARY KEY,          -- e.g. 'cursor:D:\Docs'
    value      TEXT    NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Bumped on every write so the search cache cannot serve stale results.
CREATE TABLE IF NOT EXISTS index_generation (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO index_generation (id, generation) VALUES (1, 0);

-- ---------------------------------------------------------------------------
-- Schema version — independent of the app version. See docs/VERSIONING.md
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 1);
