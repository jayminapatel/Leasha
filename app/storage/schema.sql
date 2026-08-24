-- Leasha — SQLite schema
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
-- Usage logging (schema v2) — the evidence Layer 10's tuning is derived from.
--
-- Built seven layers before anything reads it, because it is the one part of
-- adaptive tuning that CANNOT be added later: in six months there is no record
-- of what was searched or what turned out to be useful, and the only route left
-- is hand-writing a golden set.
--
-- Entirely local. Never transmitted. Clearable from Settings — it is a record of
-- what someone searched on their own machine, so they must be able to see it and
-- delete it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY,
    query       TEXT    NOT NULL,      -- the raw string, before parsing
    filters     TEXT,                  -- JSON: the ParsedQuery operators
    hits        INTEGER NOT NULL,
    elapsed_ms  INTEGER NOT NULL,
    rerank_on   INTEGER NOT NULL,
    searched_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_searches_at ON searches(searched_at);

CREATE TABLE IF NOT EXISTS search_hits (
    search_id   INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    chunk_id    INTEGER NOT NULL,
    rank        INTEGER NOT NULL,      -- 1-based, after fusion and rerank
    sources     TEXT    NOT NULL,      -- which retrievers found it
    opened      INTEGER NOT NULL DEFAULT 0,
    opened_at   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_hits_search ON search_hits(search_id);
CREATE INDEX IF NOT EXISTS idx_hits_opened ON search_hits(opened) WHERE opened = 1;

-- ---------------------------------------------------------------------------
-- Knowledge graph (v3) — entirely derived from `chunks`, so always rebuildable
--
-- `key` is the identity (casefolded, whitespace-collapsed) and `display` is
-- what gets shown, so "Acme Ltd" and "ACME LTD" are one node rather than two.
-- Mentions are kept, not just counts: clicking a node has to be able to show
-- the passages behind it, and PMI has to be recomputable without re-extracting.
-- Edges are stored once, with a_id < b_id enforced by CHECK — co-occurrence is
-- symmetric, and two stored halves can only ever disagree.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY,
    key         TEXT    NOT NULL UNIQUE,     -- casefolded identity
    display     TEXT    NOT NULL,            -- most common surface form
    kind        TEXT    NOT NULL,            -- person/org/email/file/term/...
    source      TEXT    NOT NULL DEFAULT 'cooccurrence',   -- or 'llm'
    mentions    INTEGER NOT NULL DEFAULT 0,  -- total occurrences
    chunk_count INTEGER NOT NULL DEFAULT 0,  -- distinct chunks; PMI is computed
                                             -- over chunks, so this is the
                                             -- denominator, not doc_count
    doc_count   INTEGER NOT NULL DEFAULT 0   -- distinct files, for ranking
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
    weight      INTEGER NOT NULL DEFAULT 0,  -- chunks where both appear
    pmi         REAL,                        -- NULL until the scoring pass runs
    PRIMARY KEY (a_id, b_id),
    CHECK (a_id < b_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_edges_b   ON entity_edges(b_id);
CREATE INDEX IF NOT EXISTS idx_edges_pmi ON entity_edges(pmi DESC);


-- ---------------------------------------------------------------------------
-- Filename index (v4) — for finding a file you know the NAME of
--
-- `chunks_fts` indexes what documents *say*. Nothing indexed what they are
-- *called*, so a file named "Invoice 2024.pdf" whose contents never use those
-- words was unfindable - which is how most people look for most files.
--
-- **Trigram, not unicode61.** Word tokenisation cannot match "voice" inside
-- "Invoice", and filename search is substring search: people type the middle of
-- a name, or half of it, and expect it to hit. Trigram costs roughly three
-- times the index size of a word tokeniser, on a table of filenames - which is
-- nothing next to the chunk index.
--
-- **Only real files are listed here**, never PST messages. A message's "name"
-- is a synthetic key nobody typed and nobody would recognise, and mail would
-- outnumber documents ten to one in the results.
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    name,                       -- "Invoice 2024.pdf"
    folder,                     -- the containing directory
    tokenize='trigram'
);

-- ---------------------------------------------------------------------------
-- Schema version — independent of the app version. See docs/VERSIONING.md
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 4);
