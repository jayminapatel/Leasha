# Local Knowledge Graph V2 — Layer-by-Layer Build Spec

**Doc version:** 2.17 · **Updated:** 2026-10-07 · **Applies to:** app v0.3.5

Companion to `LOCAL_KNOWLEDGE_GRAPH_V2.md`. That document defines the architecture and
the environment; this one defines **what gets built, in what order, and how each layer
proves it works**.

The V1 layers assumed FastAPI, PostgreSQL and Qdrant and are void. These replace them.

**Prerequisite:** `doctor.py` prints `READY`.

---

## Ground rules for every layer

1. **A layer is not done until its acceptance tests pass.** No moving on with a red test.
2. **Nothing in the search hot path may require a service.** Ollama is never called by search.
3. **Every failure returns an `AppError`** (see the error contract in the main spec) — never a bare
   traceback, never a silent `except: pass`.
4. **One bad file never halts a batch.** Log, mark, count, continue.
5. **Everything long-running is resumable.** Persist the cursor before you need it.
6. **The UI thread never does I/O.** All work happens in a `QThreadPool` worker; results
   return via Qt signals.
7. **Every layer ships a CLI entry point** before it ships UI, so it can be tested headless.
8. **Every `.ps1` is ASCII-only or saved UTF-8 with a BOM.** Windows PowerShell 5.1 decodes a
   BOM-less file using the ANSI codepage, so a stray em dash becomes a smart quote and the
   script dies at parse time with no output at all. `scripts\parse-check.ps1` enforces this.

---

## Target module layout

```
D:\SearchProject\
├── install.ps1
├── requirements.txt
├── doctor.py
├── .env                          # written by install.ps1, never committed
├── logs\
└── app\
    ├── __init__.py
    ├── main.py                   # PySide6 entry point
    ├── cli.py                    # headless entry point (index / search / stats)
    ├── core\
    │   ├── config.py             # .env -> typed pydantic Settings
    │   ├── errors.py             # AppError, ActionType, error registry
    │   ├── logging.py            # loguru sinks: console + rotating file
    │   └── single_instance.py    # named mutex; refuses a second copy
    ├── storage\
    │   ├── sqlite_store.py       # metadata + FTS5 + skip ledger + cursor
    │   ├── vector_store.py       # LanceDB table lifecycle + ANN index
    │   ├── schema.sql            # DDL, versioned
    │   └── migrations.py
    ├── extract\
    │   ├── base.py               # Extractor protocol -> list[Document]
    │   ├── pdf.py                # PyMuPDF
    │   ├── office.py             # python-docx / openpyxl / python-pptx
    │   ├── plaintext.py          # txt, md, csv, code, with encoding detection
    │   ├── email_pst.py          # win32com Outlook MAPI
    │   ├── email_files.py        # .eml / .msg fallback
    │   └── chunker.py            # token-aware splitting with overlap
    ├── index\
    │   ├── walker.py             # roots, filters, exclusions, mtime+hash
    │   ├── pipeline.py           # queue, workers, resumability, backpressure
    │   └── embedder.py           # FastEmbed batch wrapper
    ├── search\
    │   ├── keyword.py            # FTS5 BM25
    │   ├── vector.py             # LanceDB ANN
    │   ├── fusion.py             # reciprocal rank fusion
    │   ├── rerank.py             # cross-encoder, optional
    │   └── engine.py             # orchestration + diskcache
    ├── graph\
    │   ├── cooccurrence.py       # baseline, no LLM
    │   ├── entities_llm.py       # optional Ollama upgrade
    │   └── render.py             # pyvis / networkx
    ├── office\
    │   └── builder.py            # DOCX / XLSX / PPTX generation from results
    ├── llm\
    │   └── ollama.py             # optional; every call guarded and degradable
    └── ui\
        ├── shell.py              # QMainWindow, layout, shortcuts, theme
        ├── search_view.py
        ├── results_view.py
        ├── indexing_view.py      # progress, skipped-files panel
        ├── graph_view.py
        ├── settings_view.py
        └── widgets\
```

---

## Layer 0 — Foundation

**Goal:** a process that starts, configures itself, logs properly, refuses to run twice,
and can express an error correctly. No search, no UI beyond a bare window.

**Build**

- `core/config.py` — pydantic `Settings` loaded from `.env`. Every path validated at
  startup, not at first use. Fail with `ERR_CONFIG_INVALID` naming the offending key.
- `core/errors.py` — `AppError`, `ActionType`, plus a registry mapping every code in the
  main spec's recovery table to its message, suggestion and action. Exceptions raised
  inside workers are converted to `AppError` at the worker boundary, never above it.
- `core/logging.py` — loguru with two sinks: console (INFO) and
  `logs/app_{time}.log` (DEBUG, 10MB rotation, 14-day retention). Log the `AppError`
  code as a structured field so the skipped-files panel can group by it.
- `core/single_instance.py` — Windows named mutex. Second launch shows `ERR_DB_LOCKED`
  and exits, rather than corrupting the index.
- `app/cli.py` — argparse skeleton with `doctor`, `index`, `search`, `stats` subcommands.

**Acceptance**

All four are met, and each is a named test in `tests/integration/test_layer0_acceptance.py`
rather than a remembered fact - the file numbers its tests after the boxes so the two cannot
drift apart.

- [x] `python -m app.cli stats` runs, prints config, exits 0. — `test_acceptance_1_*`
- [x] Corrupting a path in `.env` produces a readable `AppError`, not a traceback. — `test_acceptance_2_*`
- [x] Launching the app twice: the second instance exits with the mutex message. — `test_acceptance_3_*`
- [x] A deliberately raised exception in a worker arrives as an `AppError` with a fix. — `test_acceptance_4_*`

**One later amendment to the third.** A second *window* is still refused, but the mutex was
split in two: `GUI_MUTEX_NAME` for the window and `INDEX_MUTEX_NAME` for a run. One lock doing
both jobs meant an open window made `app.cli index` impossible, which protected nothing — a
window that is merely open is a reader. And a start now waits `HANDOVER_WAIT_S` before
refusing, because closing holds the lock for as long as the stores take to shut and the window
has already left the screen by then. See `core/run_lock.py`.

---

## Layer 1 — Storage

**Goal:** both stores exist, are versioned, survive restart, and round-trip data.

**SQLite (`knowledge.db`, WAL)** — the authority on files, chunks and state.

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

CREATE TABLE files (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    parent_dir    TEXT NOT NULL,
    ext           TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    content_hash  TEXT,                 -- blake2b of bytes; NULL until read
    status        TEXT NOT NULL,        -- PENDING|INDEXED|SKIPPED|FAILED
    skip_code     TEXT,                 -- AppError.code when SKIPPED/FAILED
    skip_detail   TEXT,
    indexed_at    INTEGER,
    source_kind   TEXT NOT NULL         -- file|pst_message|eml
);
CREATE INDEX idx_files_status ON files(status);
CREATE INDEX idx_files_dir    ON files(parent_dir);

CREATE TABLE chunks (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    char_start  INTEGER,
    char_end    INTEGER,
    page        INTEGER,                -- PDF page / slide no / sheet name ref
    embedded    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX idx_chunks_file_ord ON chunks(file_id, ordinal);

-- Email-specific metadata, kept out of `files` to avoid a wide sparse table
CREATE TABLE messages (
    file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    store_path   TEXT,                  -- originating .pst
    entry_id     TEXT,
    conversation TEXT,
    subject      TEXT,
    sender       TEXT,
    recipients   TEXT,
    sent_at      INTEGER,
    has_attach   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_messages_conv ON messages(conversation);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);
-- Triggers keep chunks_fts in sync with chunks (insert/delete/update).

CREATE TABLE index_state (
    key   TEXT PRIMARY KEY,             -- e.g. 'cursor:D:\Docs'
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE schema_version (version INTEGER NOT NULL);
```

**LanceDB (`<DATA_PATH>\vectors`)** — one table, `chunks`:

| field | type | note |
|---|---|---|
| `chunk_id` | int64 | joins back to `chunks.id` |
| `file_id` | int64 | enables filtered search without a join |
| `vector` | fixed size list\<float32\>[384] | bge-small-en-v1.5 |
| `ext` | string | pushed-down filter |
| `mtime_ns` | int64 | pushed-down date filter |

- Create the ANN index (`IVF_PQ`) only once the table exceeds ~100k rows — below that a
  flat scan is faster than a poorly trained index. Retrain when row count doubles.
- LanceDB is a **derived** store. If it is ever inconsistent with SQLite, SQLite wins and
  the vectors are rebuilt from `chunks`. Never the reverse.

**Acceptance**

All five are met, numbered against `tests/integration/test_layer1_acceptance.py`.

- [x] Fresh DB is created, `schema_version` set, WAL confirmed on disk (`-wal` file present). — `test_acceptance_1_*`
- [x] Insert 10k synthetic chunks; `chunks_fts` MATCH returns the expected rows. — `test_acceptance_2_*`
- [x] Deleting a `files` row cascades to `chunks`, `chunks_fts` and the LanceDB rows. — `test_acceptance_3_*`
- [x] Kill the process mid-write; on restart the DB opens clean and reports the last cursor. — `test_acceptance_4_*`
- [x] LanceDB round-trip: 384-dim insert, ANN query, correct `chunk_id` returned. — `test_acceptance_5_*`

**The schema is at v12**, not the version this section was written against. Migrations are
forward-only and a newer schema is refused rather than opened — see
`test_newer_schema_is_refused_not_corrupted`, which matters on a machine that has run a later
build and then gone back.

**One thing the WAL box does not cover, and it cost a bug.** WAL is confirmed present here;
nothing checked that it is ever *truncated*. Clearing a large index writes every deleted page
into `knowledge.db-wal`, so `VACUUM` alone left the file group no smaller and a reset appeared
to do nothing. `SqliteStore.reclaim_space` checkpoints, vacuums, and checkpoints again.

---

## Layer 2 — Extraction

**Goal:** any supported file in, clean `Document` + chunks out, or a precise skip reason.

**Build**

- `base.py` — **[BUILT]** `Extractor` protocol: `supports(path) -> bool`,
  `extract(path) -> Iterable[Document]`. A registry maps extension → extractor, and refuses a
  duplicate claim rather than letting whichever module imported last win.
  **The offset invariant:** a `Document` holds one flat `text`, and every `Segment` carries the
  exact `[char_start, char_end)` it occupies within it, so `text[s.char_start:s.char_end] == s.text`
  always. `DocumentBuilder` appends text and records offsets in one operation, so no extractor
  can let the two drift. Failure is a value: extractors raise `AppErrorException` for a file
  that cannot be read, and attach non-fatal problems to `Document.warnings` — a file that
  decoded with replacement characters is still worth indexing.
- `pdf.py` — **[BUILT]** PyMuPDF. Per-page text with page numbers retained. A PDF with no text
  on any page is `ERR_NO_TEXT_LAYER`, not an empty success: a scan that indexes "cleanly" with
  no text is permanently unfindable while appearing to have worked, and counting it is what
  turns an invisible failure into evidence for whether OCR is worth adding. A partly-scanned
  document indexes the pages that have text and warns about the rest. Password-protected files
  are `ERR_FILE_CORRUPT` with "password" in the detail. MuPDF's own stderr chatter is silenced —
  across 100GB it is thousands of lines nobody asked for, absent from the log file.
- `office.py` — **[BUILT]** DOCX paragraphs + tables **walked in document order** (the separate
  `paragraphs` and `tables` collections would put every table after every paragraph, moving a
  contract's obligations after its signature block); XLSX per-sheet cell text with sheet names
  written into the indexed text, opened `data_only` so a formula contributes its cached *value*
  rather than the string `=VLOOKUP(...)`; PPTX per-slide shape text plus speaker notes, which
  is usually where the sentences are. Sheets are capped at 5,000 rows with a loud warning, so
  one spreadsheet-as-database cannot dominate the index and the embedding queue.
- `plaintext.py` — **[BUILT]** encoding detection (UTF-8 → cp1252 → latin-1, BOM stripped),
  emitting `ERR_ENCODING` as an `AUTO_FIX` when it falls through to latin-1. Files with a text
  extension and NUL bytes — a `.log` that is really a database — are `ERR_NO_TEXT_LAYER` rather
  than pages of garbage in the FTS index.
- `email_pst.py` — **[BUILT]** `win32com.client` Outlook MAPI. Walk stores → folders → items.
  Capture subject, sender, recipients, sent time, conversation ID, body, and recurse into
  attachments through the normal extractor registry. **Never `pypff`. Never `extract-msg`
  for `.pst`** — it reads `.msg` only.
  **The COM calls are confined to `Win32ComSession`** and everything above works against small
  duck types, so the walk, the conversation grouping, the attachment dedup and every error path
  are tested with a fake on any machine. Only the adapter needs Windows — the smallest honest
  untested surface this can have.
  Identity is the **`EntryID`**, never a folder path: moving a message must not make it look
  like a new one, or an index over a mailbox people reorganise never settles.
  Attachments are **deduplicated by content hash**, with the set supplied by the caller so
  Layer 3 persists it in `files.content_hash` and dedups across runs — 30GB of archives holds
  the same deck mailed round the team eight times.
  `Deleted Items`, junk and sync-conflict folders are skipped by default; on a fifteen-year
  archive Deleted Items is often a third of the messages.
  A `com_error` in one folder is `ERR_OUTLOOK_BUSY` for that folder and the walk continues:
  by the time Outlook is closed, thousands of messages may already have been read.
- `email_files.py` — **[BUILT]** stdlib `email` for `.eml`; `extract-msg` is acceptable here and
  only here. `conversation` comes from `References[0]`, falling back to `In-Reply-To` then
  `Message-ID`, so every reply in a thread lands on the same key — a decision is rarely in one
  message. Headers are written into the indexed text as well as into `meta`, because "the email
  from Priya about the survey" only works if both are text the retriever sees. HTML-only bodies
  are stripped without a parser dependency.
- `chunker.py` — **[BUILT]** ~512-token chunks with ~64-token overlap, split on paragraph then
  sentence boundaries; never mid-word. Text is atomised once into words-with-spans tagged by the
  boundary preceding them, and a chunk is a contiguous range of atoms — which is what makes
  `text[c.char_start:c.char_end] == c.text` true by construction rather than by care.
  `token_cost` is the single definition of size and `estimate_tokens` is its sum, so the budget
  spent and the size reported cannot disagree. They did, once: costing a word at 1 token while
  estimating a chunk at 1.35/word let every chunk run 35% over and straight past the model's
  512-token limit, where bge-small truncates in silence.

**CLI entry point** (ground rule 7 — every layer ships one before it ships UI):

```
python -m app.cli extract "D:\Docs\report.pdf" --chunks
python -m app.cli extract "D:\Docs" --limit 200
python -m app.cli extract "D:\Docs" --out report.json
python -m app.cli extract --mailbox           # Outlook: archives + cached live mailbox
```

Read-only by construction: it opens no store and writes nothing, so it is safe to point at
anything. Files or folders; a folder is walked recursively and unsupported types are filtered
out rather than reported as skips, because listing every `.dll` would bury the failures that
matter. Cloud placeholders are checked **before** the file is opened — checking afterwards
would be pointless, since opening one is what triggers the download — and `--include-cloud`
opts in. Prints per-file timing and an overall MB/s, which is the first real input to the
throughput question Layer 3 has to answer.

**Acceptance**

Tests in `tests/integration/test_layer2_acceptance.py`, numbered to match, plus
`tests/unit/test_cli_extract.py` for the command itself.

- [x] A fixtures folder with one healthy and one deliberately corrupt file of each type.
      Fixtures are **generated by `tests/fixtures/generate.py`, not committed**: binary test
      files in git cannot be reviewed in a diff, and an editor that opens and re-saves one
      silently destroys the corruption it was testing.
- [x] Every healthy fixture yields non-empty text and plausible chunk counts.
- [x] Every corrupt fixture yields `ERR_FILE_CORRUPT` with `SKIP_CONTINUE` — and the run continues.
- [x] A password-protected PDF and a locked-open XLSX both skip cleanly. The lock is proved as
      a *mapping* off Windows: `PermissionError` → `ERR_FILE_LOCKED` ("close the program holding
      it"), never `ERR_FILE_CORRUPT` — sending someone to repair a perfectly good file is the
      failure that guards against.
- [x] PST extraction over a small test archive preserves conversation grouping.
- [x] The live Outlook mailbox is enumerated alongside `.pst` stores, and closing Outlook
      mid-run yields `ERR_OUTLOOK_BUSY` rather than failing the run.
      *Both proved through a fake MAPI session. The residue — that `Win32ComSession` drives
      real Outlook — is one `xfail(run=False)` and `app.cli extract --mailbox` on a real
      machine.*
- [~] A OneDrive folder with Files On-Demand indexes pinned files and skips placeholders,
      and no placeholder is hydrated. **Detection** is proved off Windows, because
      `winfs.is_cloud_placeholder` accepts injected attribute bits and never opens the file.
      **Non-hydration** still needs confirming by watching the sync client on a real machine,
      exactly as written — it cannot be asserted in a test.
- [x] Chunk overlap verified: no text lost at boundaries. Asserted as a property over every
      healthy fixture at a chunk size small enough to force many boundaries: every word of the
      source must appear in at least one chunk.

**Outstanding: one manual verification.** `Win32ComSession` is the only part that cannot be
tested off Windows. Run `app.cli extract --mailbox` against real Outlook; until that has been
done once, treat Layer 2 as code-complete but not signed off.

---

## Layer 3 — Indexing pipeline

**Goal:** point it at 100GB, walk away, come back to a resumable, incremental index.

**Build**

- `walker.py` — **[BUILT]** configurable roots, extension allowlist, exclusion globs
  (`node_modules`, `AppData`, `$Recycle.Bin`, `.git`, …). Emits candidates with
  `mtime_ns` + `size_bytes`; hashes contents only when mtime or size changed.
  **Exclusions prune during the walk, never filter after it** — descending into a 40,000-file
  `node_modules` and discarding it costs the whole subtree, and a test counts the directories
  actually visited rather than the output, because a filter-afterwards implementation produces
  identical output.
  **The hash decides, mtime only decides whether to hash.** robocopy, a restore from backup,
  cloud sync and archive extraction all reset mtime while leaving content identical; trusting
  mtime alone would re-index the entire corpus every time any of those happened.
  Cloud placeholders are judged from the `stat()` already performed, so the check costs nothing
  and happens before anything could open the file.
- `pipeline.py` — **[BUILT]** bounded producer/consumer:
  - walker → work queue (bounded, for backpressure)
  - N extraction workers (CPU count − 1)
  - one embedding worker, batching 64 chunks per `embed()` call — **not parallelised on
    purpose**: ONNX already uses every core inside one call, so several would contend
  - one writer (SQLite writes are serialised; LanceDB appends in batches of 1000)
  - cursor committed to `index_state` every N files, so a crash costs seconds, not hours.
    **Resumability is the `files` table, not the cursor**: a restart re-walks and skips what is
    already `INDEXED` for the cost of a `stat()`. That is more robust than a saved offset, which
    goes wrong the moment the corpus changes underneath it, and it falls out of the incremental
    logic that has to exist anyway. The cursor is progress reporting for the UI.
  - **chunks and vectors are written before the file is marked `INDEXED`.** A crash between
    them leaves a file that looks unfinished and gets redone, which is correct; the reverse
    would leave it marked done with no chunks — invisible to search and never retried.
- **Prioritisation:** the user nominates folders to index first. The queue is a priority
  queue, not FIFO.
- **Disk guard:** a background check pauses the pipeline at `MIN_FREE_GB` with
  `ERR_DISK_SPACE`, preserving the cursor for interactive resume.
- **Incremental pass:** re-walk, compare `mtime_ns`+hash, re-index only changes, delete
  rows for files that disappeared, and re-queue anything marked `ERR_FILE_LOCKED`.

**Acceptance**

Tests in `tests/integration/test_layer3_acceptance.py`, numbered to match. Embedding uses an
injected encoder throughout: the model is the slowest component and the least interesting
property here — what these prove is that the *pipeline* does not lose work, repeat work, or
fall over.

- [x] A corpus indexes end-to-end with zero unhandled exceptions. *(A bug in one extractor is
      asserted to cost that file and nothing else.)*
- [x] Kill the process at 50%; restart resumes within one batch of where it stopped.
- [x] Re-running over an unchanged corpus does near-zero work. *Asserted in the strong form:
      a second pass that opens any unchanged file fails the test.*
- [x] Touch one file → only that file is re-indexed. *Plus: the old chunks are replaced, not
      appended to — a file that still matches text it no longer contains is worse than one
      that was never re-indexed.*
- [x] Delete one file → its rows disappear from SQLite, FTS and LanceDB.
- [x] Inject a corrupt file mid-run → run completes, file appears in the skipped panel,
      grouped by cause.
- [x] Fill the disk artificially → pipeline pauses, does not corrupt, resumes after space is
      freed. *Integrity checked and both stores asserted still in agreement.*
- [x] Throughput measured and recorded (files/min, MB/min) as the baseline for the UI's ETA.

**Outstanding:** the 10k-file scale run, and the throughput number from real hardware on the
real corpus. The acceptance suite proves correctness at small scale; only a real run produces
the number the UI's ETA depends on.

**CLI entry point**

```
python -m app.cli index "D:\SearchData"
python -m app.cli index "D:\SearchData" --first "D:\SearchData\Current"
python -m app.cli index "D:\SearchData" --json
```

Unlike `extract`, this **writes**, so it takes the single-instance lock — two copies indexing
into one SQLite file is exactly the corruption the mutex exists to prevent. `--first` is
repeatable and ordered, so "my current project, then the archive" means that.

---

## Layer 4 — Search

**Goal:** the number that matters. **<300ms warm, <3s first search after launch.**

**Pipeline**

```
query
 └─ parse (operators, phrases, exclusions, FTS5 sanitise)   <0.1ms, no model
     ├─ FTS5 BM25        (thread A)  -> top 100
     └─ LanceDB ANN      (thread B)  -> top 100
            └── reciprocal rank fusion (k=60)  -> top 50
                  └── optional cross-encoder rerank of top 30
                        └── results
```

**Build**

- `query.py` — **[BUILT]** parse the raw string into a `ParsedQuery`: typed operators
  (`type:`/`ext:`, `after:`/`since:`, `before:`/`until:`, `path:`/`folder:`, `from:`),
  `"quoted phrases"`, `-exclusions`, and `prefix*`. Dates accept ISO, partial ISO
  (`2024-01`, `2024`) and relatives (`today`, `7d`, `last-month`). `to_fts_match()` quotes
  every token, so the expression handed to SQLite **cannot** be malformed — `AND`, `NOT`,
  `*` and a lone `"` are all read as text. `embed_text` strips operators, because they are
  noise to a dense model. Pure, stdlib-only, zero I/O.
- `keyword.py` — **[BUILT]** FTS5 `MATCH` with BM25 ranking, taking its expression from `query.py`
  rather than from raw user input; support phrase, prefix and `NEAR`.
  `search_bm25()`'s `OperationalError` catch stays as a backstop, but with `query.py` in
  front of it that path should now be unreachable — if it ever fires, that is a bug in the
  sanitiser and must be logged, not swallowed.
- `vector.py` — **[BUILT]** embed the query once, ANN search with pushed-down filters (ext, date, folder).
- `fusion.py` — **[BUILT]** RRF: `score = Σ 1/(k + rank)`, `k=60`. Pure function, unit-tested.
  Fuses on rank, not score, so BM25 and cosine never need normalising against each other.
  Ties break by best rank then list order, so the output is deterministic and recall@10 is a
  meaningful regression guard. `fuse_hits()` tags each row with `sources` — which retrievers
  found it — because agreement between both is the strongest signal the pipeline produces and
  the UI should show it.
- `rerank.py` — **[BUILT]** cross-encoder over the top 30, loaded lazily, behind the Settings toggle.
  If the model is missing, log once and return the fused order — never fail the search.
- `engine.py` — **[BUILT]** parallel dispatch, diskcache keyed on
  `(query, filters, rerank_on, index_generation)`. Bump `index_generation` on any write so
  stale results cannot be served.
- **Model warm-up:** load the embedding model on app start in a background thread so the
  first user search is not the one paying the 1–2s ONNX load.
- **Usage logging — build it now, use it in Layer 10.** Two tables, thirty lines, and the only
  part of adaptive tuning that **cannot be added later**:

```sql
CREATE TABLE searches (
    id          INTEGER PRIMARY KEY,
    query       TEXT    NOT NULL,      -- the raw string, before parsing
    filters     TEXT,                  -- JSON: the ParsedQuery operators
    hits        INTEGER NOT NULL,
    elapsed_ms  INTEGER NOT NULL,
    rerank_on   INTEGER NOT NULL,
    searched_at INTEGER NOT NULL
);

CREATE TABLE search_hits (
    search_id   INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    chunk_id    INTEGER NOT NULL,
    rank        INTEGER NOT NULL,      -- 1-based, after fusion and rerank
    sources     TEXT    NOT NULL,      -- which retrievers found it
    opened      INTEGER NOT NULL DEFAULT 0,   -- set when the user opens this result
    opened_at   INTEGER
);
CREATE INDEX idx_hits_opened ON search_hits(opened) WHERE opened = 1;
```

  Every tuning question in Layer 10 — are the RRF weights right for *this* corpus, is 512 the
  right chunk size, when is reranking worth 200ms — is answered from `(query, rank, opened)`
  triples and answerable from nothing else. Collecting them costs an insert per search and an
  update per click. **Not collecting them costs the entire layer**, because the data cannot be
  reconstructed after the fact: in six months there is no record of what was searched or what
  was useful, and the only route left is hand-writing a golden set.

  Purely local, never transmitted, and clearable from Settings — it is a record of what the
  user searched on their own machine, and they must be able to see and delete it.

- **Two-tier dispatch.** Layer 5 searches as you type. Running the full pipeline on every
  150ms debounce means embedding a query per keystroke burst and an ANN probe per burst —
  most of it thrown away before the user stops typing. So:

  | Trigger | Runs | Why |
  |---|---|---|
  | Typing pause (150ms) | BM25 + parse only, top 20 | Prefix-matches feel instant; no model touched |
  | Idle (400ms) or `Enter` | Full hybrid + fusion + rerank | The user has committed to the query |

  The interim tier must be visually identical to the final one — same row layout — so results
  refine in place rather than flashing. Never run the interim tier after `Enter`.

**Deliberately not built**

Both of these are standard in cloud RAG stacks and both were assessed and rejected here.
Recorded so they are not re-proposed:

- **LLM query rewriting / deconstruction.** Would put a model in the search hot path,
  violating the project's first non-negotiable, and costs 200–800ms on CPU against a 300ms
  total budget. `query.py` gets the useful 90% — dates, types, paths, phrases — deterministically,
  in under 0.1ms. If it is ever revisited it must be an optional pre-step that fails open to the
  raw query, never a dependency.
- **Graph RAG (graph as a retrieval stream).** Adds a third candidate source plus traversal to
  a budget that is already tight, and the corpus is files and email, where the co-occurrence
  graph is derived *from* the chunks the vector index already covers — so it would mostly
  re-rank documents hybrid search had already found. The graph stays a navigation surface
  (Layer 6): node click → filtered search. Revisit only if recall@10 on the golden set proves
  a class of query that hybrid genuinely misses.

**Acceptance**

Tests in `tests/integration/test_layer4_acceptance.py`, against a **real index** — files on
disk, extracted, chunked, written to both stores, then searched. Only the embedding model is
faked, with a deterministic bag-of-words encoder so semantically similar text really does land
near itself; a real model would make every test minutes long and prove nothing about the
pipeline.

- [x] A golden set of query→expected-document pairs; recall@10 tracked as a regression guard.
      *Five pairs today, seeded by hand. Layer 10 grows this automatically from real clicks —
      which is the whole reason `searches`/`search_hits` are built now.*
- [~] Warm search p95 <300ms — **measured at small scale**, and the pipeline shown to have no
      gross inefficiency. The real number needs the real corpus and the real model; record it
      in `HANDOFF.md` when the first full index run happens.
- [ ] First search after launch <3s. Needs the real ONNX load.
- [x] Rerank on/off both work; toggling does not require a restart.
- [x] Deleting the rerank model mid-session degrades gracefully with one log line — the search
      still succeeds, keeps the fused order, and does not retry for the rest of the session.
- [x] Malformed queries (`"unclosed`, `AND AND`, emoji, 10k characters) return results or a
      clean `AppError` — never a crash. *Proved twice: in `test_query.py` by running every
      generated expression against a real FTS5 table, and again through the whole engine.*
- [x] Cache invalidates correctly after an incremental index run. *The generation is in the
      key, so a write cannot leave stale results behind — the difference between a cache and
      a lie.*
- [x] Typing never blocks and never embeds — the interim tier is BM25-only, and a test injects
      an encoder that fails if it is ever called.
- [x] `type:pdf after:2024 "site survey" -draft` filters correctly, in SQL rather than after
      the fact, so the top 100 are the top 100 *that match the filter*.
- [x] An unparseable operator value (`after:nextthursday`) is surfaced rather than silently
      ignored, and the rest of the query still runs.
- [x] The interim tier is never logged — a glance at a half-typed word is not intent, and
      recording it would poison the data Layer 10 depends on.
- [x] A logging failure never fails a search.
- [x] The usage log can be cleared.

---

## Layer 5 — UI shell

**Goal:** the app a person actually uses. Everything before this was plumbing.

**Build**

- **`presenter.py`** — **[BUILT]** everything the UI decides but does not draw: which tier a
  keystroke earns, how to cut a 1,600-character chunk to a 240-character snippet centred on the
  match, where the highlight ranges fall, how to phrase an ETA, how to turn 4,000 skips into a
  handful of actionable rows. **Imports no Qt, and a test enforces that.** Qt widgets cannot be
  instantiated without a display, so logic living inside them could only ever be checked by a
  person clicking — which is not a standard this project holds anywhere else.
- `workers.py` — **[BUILT]** `QRunnable` wrappers, because non-negotiable #6 says the UI thread
  never does I/O. Every worker converts what escapes into an `AppError` and emits it: an
  exception leaving a `QRunnable` does not propagate anywhere useful, it vanishes, and the UI
  waits forever for a signal that never comes.
- `shell.py` — **[BUILT]** `QMainWindow`; search bar always focused on launch; dark mode; shortcuts
  (`Ctrl+K` focus search, `Ctrl+,` settings, `Enter` open, `Ctrl+Enter` open containing
  folder, `Esc` clear).
- `search_view.py` — **[BUILT]** as-you-type with a 150ms debounce, running the **two-tier dispatch** from
  Layer 4 (BM25-only while typing, full hybrid on idle or `Enter`); filter chips for type, date
  range and folder, kept in sync both ways with the typed operator syntax from `query.py` —
  setting a chip writes `type:pdf` into the bar, and typing `type:pdf` lights the chip, so
  there is one state, not two; every search dispatched to the worker pool, never the UI thread.
- `results_view.py` — **[BUILT]** result rows showing path, snippet with query-term highlighting,
  score, modified date; click to open, right-click for "Open containing folder", "Copy path",
  "Add to document". Missing-file detection: if the path no longer exists, mark the row and
  offer to re-index.
- `indexing_view.py` — **[BUILT]** live progress, throughput, ETA (from Layer 3's measured baseline),
  pause/resume, folder prioritisation, and the **"N files skipped — review"** panel grouped
  by `AppError.code` with the fix text and a retry button.
- `settings_view.py` — **[BUILT]** index roots, exclusions, rerank toggle, data path, Ollama URL, and a
  "Run doctor" button that renders `doctor.py --json` inline.
- Drag-and-drop onto the window indexes the dropped files immediately, at top priority.

**Acceptance**

**What is and is not verified.** Qt cannot be instantiated headlessly, so the split is the
answer: `presenter.py` holds every decision and is covered by 44 tests; the view modules draw
what it returns and are compile-checked and kept under 250 lines each (also enforced). What
remains unverified is **wiring** — which a person notices in the first minute — rather than
logic, which a person would not notice being subtly wrong.

- [~] UI stays responsive during a full index run. *Structurally guaranteed: every search and
      index run goes through `QThreadPool`. Needs one real run to confirm.*
- [x] Every error shows message + suggestion, and a retry button **only where retrying can
      help** — a scanned PDF will still have no text layer next time, so offering one there is
      worse than offering none.
- [~] Drag-drop a file → searchable within seconds. *Implemented; needs a real drop.*
- [x] Closing mid-index stops cleanly and keeps everything written — resumability is the
      `files` table, so reopening simply carries on.
- [~] Keyboard-only operation end to end. *`Ctrl+K`/`Ctrl+F` focus search, `Ctrl+,` settings,
      `Ctrl+I` indexing, `Esc` clear, `F5` index, Enter searches. Needs a real keyboard.*

**Outstanding: a human running it.** `venv\Scripts\python.exe -m app.main`. This is the one
layer where that is unavoidable.

---

## ~~Layer 6 — Knowledge graph~~ · REMOVED

Built, all four acceptance criteria passing, and removed. It was never in the search path,
nothing depended on it, and the only user did not want it. Git preserves it at tag `v0.3.2`.

The three `entities` tables remain in the schema, empty and commented as deprecated: dropping
them needs a migration to v5, and migrations only step forward, so reviving the feature would
then need a v6 to undo it.

**What would justify reviving it, and the decision record:** `HANDOFF.md` §3a.

## ~~Layer 7 — Office document builder~~ · CANCELLED

A 225-byte stub. Never requested by anybody; it was in the plan because the plan predated a
clear statement of what the tool is for. Deleted.

*What would justify building it:* a repeated need to get results **out** — into a report, a
spreadsheet, an email. Until somebody asks twice, exporting is a feature looking for a user.

## Layer 8a — Natural-language query translation

**The feature the application exists for**, and the reason Layers 6, 7 and 10 went.

> "Local search is my main objective, but I want to write in normal text what I am looking
> for, and this complexity to solve may need AI."

The model's entire job is to emit the operator syntax `parse_query` already accepts:

```
"the safety report Dave sent me about Leeds before the audit last March"
    ->  safety report Leeds from:dave before:2025-03-01
```

Three properties follow, and they are why this is safe rather than merely useful:

- **A malformed translation is caught by existing code.** Nothing new validates anything.
- **There is no injection surface.** The model cannot express anything a person could not
  have typed, because the output is checked against a fixed grammar and discarded if it does
  not fit.
- **It is testable with no model.** Sentence in, query string out, asserted against a string.

### The rule that bends, stated precisely so it does not erode

> **The retrieval path never calls the LLM.** Query *translation* may. It is optional, costs
> about a second, runs once before the search, and is always visible. Plain keyword and
> semantic search remain instant and never touch a model.

Enforced by a static test: nothing under `app/search/` imports `app.llm` except
`translate.py`, and `SearchEngine.search()` cannot reach it.

### Acceptance — all met

- [x] Fifteen-plus sentence → query pairs covering sender, type, dates, phrases, exclusions.
- [x] Junk output — empty, prose, unclosed quote, invented operator, 10,000 characters — falls
      back to the **raw text whole**, never to a partial query.
- [x] An invented operator (`colour:red`) is rejected, not passed through.
- [x] A date the parser rejects causes full fallback, not a query with the constraint dropped.
- [x] Ollama unreachable: the search still runs and returns results.
- [x] The translated query reaches the presenter, so the UI can show and edit it.
- [x] Static: `app/search/` does not import `app.llm` except the one module.
- [x] **Measured**: constrained recall at rank 1 goes 50% → 92% with translation.

### Measured, and the reason it was worth building

`app.cli evaluate` runs twenty sentences against a corpus with known answers, reporting
recall **split by whether the sentence carried a constraint** — because one number cannot
distinguish "search is bad" from "search is fine at topics and blind to constraints".

| at rank 1 | plain | translated |
|---|---|---|
| topic only | 88% | 88% |
| with a constraint | 50% | **92%** |

## Layer 8b — Prose answers · BUILT as the Chat tab

Built on the owner's instruction on 2026-09-19 as the Chat tab (`app/chat`, order
`202626270611`) and made a conversation the next day; it runs on ONNX Runtime inside Leasha by
default, with Ollama as a choice. It was built to this:

One search, then answer — not multi-step retrieval. Reuse `SearchEngine`; do not write a
second retrieval path. **Every claim carries a citation that opens the source passage**; an
answer with no citations is a bug, asserted by a test. That is the only workable mitigation
for a 7B model stating wrong things confidently over technical material: it makes a wrong
answer visibly wrong rather than plausibly wrong. Instruct the model that "the passages do
not answer this" is a correct response. When the context fills, drop history, never passages.

## Layer 9 — Hardening and packaging

**Status: the Windows installer is built** (order `202626082213`, released by the owner
2026-10-05), for the owner's own use; no copy goes to anyone else for now.
`docs/WORKORDER-202626082213-install-and-distribution.md` carries the detail and the
acceptance checks. The decisions it carries out: PyInstaller, one folder; per-user by default
with no administrator rights, per-machine as an option; the installer asks where the index
goes (default `%LOCALAPPDATA%\Leasha\Data`, checked against `REQUIRED_FREE_GB`); no update
check inside the app; Windows 11 and Windows 10 22H2 supported (`MinVersion=10.0.19045`),
Windows 11 the only one tested; unsigned. The Qt binding is PySide6 (LGPL-3.0), so a
distributed build can keep Leasha's MIT licence.

What is in `packaging/`:

- `leasha.spec` - the PyInstaller build (PyInstaller 6.22.3, pinned in
  `packaging/requirements-build.txt`). One folder with two programs: `Leasha.exe`, the window,
  and `leasha-cli.exe`, a console program that runs `-m`, `-c` and scripts as `python.exe`
  would, and any other arguments as an `app.cli` command. The folder mirrors the repository
  under `_internal`, so in a packaged build `project_root()` is `_internal`. The window starts
  its own children through `app.core.osbridge.stdio.own_python()`, which names
  `leasha-cli.exe` there.
- `leasha_entry.py` - the one entry point both programs are built from.
- `installer.iss` - the Inno Setup 6 script. It asks where the index goes and writes
  `<install folder>\_internal\.env` only if there is none. Optional: download the search
  models, install LibreOffice through winget, a desktop icon. At the end it offers the health
  check (`doctor.py --quick`) and opening Leasha. Uninstalling removes the program and never
  the index.
- `build.ps1` - PyInstaller, a quick check that `leasha-cli.exe` runs, Inno Setup, and the
  installer's SHA256. `-Release` also copies `Leasha-Setup-<version>.exe` to the Releases
  folder on Google Drive.

Some of the hardening below is also done — the crash handler writes to `logs\crash.log` via
`faulthandler`, and graceful shutdown exists and times itself — so treat the list below as the
scope and the work order as the plan.

**Build**

- Crash handler writing a redacted report to `logs\`.
- Graceful shutdown: flush the writer, commit the cursor, close both stores.
- Backup/restore of the index folder; "rebuild vectors from SQLite" recovery command.
- First-run wizard: pick index roots, estimate time from the Layer 3 baseline, start.
- PyInstaller one-folder build and an installer - built, in `packaging/`.

**Acceptance**

- [ ] 24-hour soak index run with no memory growth beyond a flat ceiling.
- [ ] Force-kill at 20 random points; the index is recoverable every time.
- [ ] Full restore from backup produces identical search results.
- [ ] Cold-start to first search under 5s.

---

## ~~Layer 10 — Adaptive tuning~~ · CANCELLED

Ranking that changes based on what you clicked is unpredictable ranking, and this
application's entire value is that you can trust what it returns. The usage log still records
searches and opens, so the option remains open if evidence ever justifies it.

## Performance budget (measure, do not assume)

| Stage | Budget | Measured | Where measured |
|---|---|---|---|
| Query parse + FTS5 sanitise | <1ms | **0.06ms** | `search/query.py` |
| Query embedding | <15ms | — | `search/vector.py` |
| FTS5 BM25 top-100 | <60ms | — | `search/keyword.py` |
| LanceDB ANN top-100 | <80ms | — | `search/vector.py` |
| RRF fusion | <5ms | **0.14ms** | `search/fusion.py` |
| Cross-encoder rerank top-30 | <200ms | — | `search/rerank.py` |
| **Total warm, rerank on** | **<300ms** | — | `search/engine.py` |
| Model load (once per session) | <2s | — | startup warm-up thread |
| *Interim tier (typing): parse + BM25 top-20* | *<40ms* | — | `search/engine.py` |

Measured figures are 1000-iteration means over 100+100 candidate rows. The two built stages
consume 0.07% of the budget between them, which is the argument for keeping the hot path
deterministic: everything spent here is spent on retrieval, not on parsing.

Log every stage timing at DEBUG. When the budget is missed, the log says which stage —
that is the entire point of the breakdown.

---

## Order of work

Layers 0 → 4 are strictly sequential; each depends on the one before. From Layer 5 the
order is negotiable — 6, 7 and 8 are independent of each other and all depend on 4.

Ship the CLI at each of 0–4. The first genuinely useful moment is the end of Layer 4:
a headless `python -m app.cli search "..."` that returns correct results in under 300ms
over the real corpus. Everything after that is interface.
