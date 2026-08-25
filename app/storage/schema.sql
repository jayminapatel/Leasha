-- Leasha — SQLite schema
-- Layer: L1
-- SQLite is the authority on files, chunks and indexing state.
-- LanceDB is a DERIVED store: if the two disagree, this file wins and the
-- vectors are rebuilt from `chunks`. Never the reverse.

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Repositories — before `files`, which references this
-- ---------------------------------------------------------------------------
--
-- Source code was already indexed and already searchable. What was missing was
-- any record that a file *belongs* to something, so results could not be
-- grouped, filtered or listed by repository. The walker stood next to the
-- evidence on every pass - `.git` is in `DEFAULT_EXCLUDE_DIRS` - and threw it
-- away.
--
-- `kind` is stored because the three are found differently: a `.git`
-- directory, or a `.git` *file* whose `gitdir:` points into `/modules/` (a
-- submodule) or elsewhere (a linked worktree). A submodule's files sit inside
-- its parent's tree, which anything drawing a list has to know first.
CREATE TABLE IF NOT EXISTS repos (
    id         INTEGER PRIMARY KEY,
    root_path  TEXT    NOT NULL UNIQUE,      -- absolute, as walked
    name       TEXT    NOT NULL,             -- basename of root_path
    kind       TEXT    NOT NULL,             -- work | submodule | worktree
    last_seen  INTEGER NOT NULL              -- unix seconds, from the last walk
);

CREATE INDEX IF NOT EXISTS idx_repos_name ON repos(name);

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
    status        TEXT    NOT NULL,          -- PENDING | INDEXED | SKIPPED | FAILED | NAME_ONLY
    skip_code     TEXT,                      -- AppError.code when SKIPPED/FAILED
    skip_detail   TEXT,
    indexed_at    INTEGER,
    source_kind   TEXT    NOT NULL,          -- file | pst_message | eml
    repo_id       INTEGER REFERENCES repos(id) ON DELETE SET NULL,
    -- NAME_ONLY: the file exists and is findable by name; its contents
    -- were never read. A status, not a failure - see FileStatus.
    CHECK (status IN ('PENDING', 'INDEXED', 'SKIPPED', 'FAILED', 'NAME_ONLY'))
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_dir    ON files(parent_dir);
CREATE INDEX IF NOT EXISTS idx_files_ext    ON files(ext);
CREATE INDEX IF NOT EXISTS idx_files_skip   ON files(skip_code) WHERE skip_code IS NOT NULL;
-- **Three indexes that were missing, and one comment that claimed otherwise.**
--
-- `keyword.py` asserted that `source_kind` was "already indexed, so this costs
-- nothing" while no such index existed. Clicking the Mail chip full-scanned
-- `files` - twenty million rows at the target scale, on every keystroke of
-- every scoped search.
CREATE INDEX IF NOT EXISTS idx_files_source_kind ON files(source_kind);

-- `scope="code"` is `repo_id IS NOT NULL`, and `repo:` joins on it.
--
-- **`ON DELETE SET NULL`, deliberately not `CASCADE`.** A repository that is
-- moved, deleted or unmounted must not take the indexed content of its files
-- with it - they are still on disk in every case that matters.
CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo_id);

-- `after:` and `before:` filter on this, and `_filter_only` sorts by it. A
-- scan plus a sort, per query.
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime_ns);

-- **`idx_chunks_file_ord` is `(file_id, ordinal)`, so `ordinal` is not
-- leading** and `WHERE c.ordinal = 0` could never use it. A filter-only query
-- like `type:pdf after:2024` scanned every chunk in the index to find the
-- first of each file.
--
-- Partial, so it holds one row per file rather than one per chunk - a twentieth
-- of the size at the observed chunks-per-file ratio, and it exactly matches the
-- predicate it exists for.


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
    embedded    INTEGER NOT NULL DEFAULT 0,
    -- Split forms of camelCase identifiers found in `text`, indexed beside it.
    -- Empty for prose, which is most documents. See app/core/identifiers.py.
    symbols     TEXT    NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_file_ord ON chunks(file_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_chunks_pending ON chunks(embedded) WHERE embedded = 0;

-- **There is deliberately no `chunks(file_id) WHERE ordinal = 0` index here.**
--
-- It looks like there should be. `idx_chunks_file_ord` is `(file_id, ordinal)`,
-- so `ordinal` is not leading, and a filter-only query like `type:pdf
-- after:2024` scans every chunk to find the first of each file. One was written
-- for schema v5 and the planner never chose it - measured with it and without,
-- the plan and the timing were identical.
--
-- The fix is `idx_files_mtime` above. With an ordered way into `files`, SQLite
-- walks newest-first and looks up each file's first chunk through
-- `idx_chunks_file_ord`, so `LIMIT 20` stops after twenty files instead of
-- sorting the whole index. See `_v5_missing_indexes` for the measurement.

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

-- `symbols` holds the split forms of camelCase and PascalCase identifiers -
-- "ResetPasswordHandler" indexes "Reset Password Handler" beside it, so a
-- search for "password" reaches it. snake_case, kebab-case and dotted names
-- need nothing: unicode61 already splits on every non-alphanumeric character.
-- MATCH against the table searches both columns, so no query knows this exists.
-- See app/core/identifiers.py.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    symbols,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- External-content FTS5 tables are not maintained automatically.
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, symbols) VALUES (new.id, new.text, new.symbols);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, symbols)
    VALUES ('delete', old.id, old.text, old.symbols);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, symbols)
    VALUES ('delete', old.id, old.text, old.symbols);
    INSERT INTO chunks_fts(rowid, text, symbols) VALUES (new.id, new.text, new.symbols);
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
--
-- ###########################################################################
-- DEPRECATED as of the scope change recorded in HANDOFF.md and CHANGELOG.md.
--
-- The knowledge graph was removed: it was never in the search path, nothing
-- depended on it, and the only user did not want it. `app/graph/` and the Graph
-- tab are gone; git preserves them at tag v0.3.2.
--
-- **These three tables stay, empty, and the schema version does NOT move.**
-- Dropping them would need a migration to v5, and migrations only step forward
-- - so reviving the feature would then need a v6 to undo the v5, and the
-- database would carry a permanent record of a decision that was reversed. An
-- empty table costs nothing; a migration pair costs clarity forever.
--
-- Drop them at Layer 9 if it still seems worthwhile by then.
-- ###########################################################################

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
