# Local Knowledge Graph V2 — Layer-by-Layer Build Spec

**Doc version:** 2.2 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2

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
    ├── main.py                   # PyQt6 entry point
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

- [ ] `python -m app.cli stats` runs, prints config, exits 0.
- [ ] Corrupting a path in `.env` produces a readable `AppError`, not a traceback.
- [ ] Launching the app twice: the second instance exits with the mutex message.
- [ ] A deliberately raised exception in a worker arrives as an `AppError` with a fix.

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

- [ ] Fresh DB is created, `schema_version` set, WAL confirmed on disk (`-wal` file present).
- [ ] Insert 10k synthetic chunks; `chunks_fts` MATCH returns the expected rows.
- [ ] Deleting a `files` row cascades to `chunks`, `chunks_fts` and the LanceDB rows.
- [ ] Kill the process mid-write; on restart the DB opens clean and reports the last cursor.
- [ ] LanceDB round-trip: 384-dim insert, ANN query, correct `chunk_id` returned.

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
- `email_pst.py` — `win32com.client` Outlook MAPI. Walk stores → folders → items.
  Capture subject, sender, recipients, sent time, conversation ID, body, and recurse into
  attachments through the normal extractor registry. **Never `pypff`. Never `extract-msg`
  for `.pst`** — it reads `.msg` only.
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

**Acceptance**

Tests in `tests/integration/test_layer2_acceptance.py`, numbered to match.

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
- [ ] PST extraction over a small test archive preserves conversation grouping.
- [ ] The live Outlook mailbox is enumerated alongside `.pst` stores, and closing Outlook
      mid-run yields `ERR_OUTLOOK_BUSY` rather than failing the run.
- [~] A OneDrive folder with Files On-Demand indexes pinned files and skips placeholders,
      and no placeholder is hydrated. **Detection** is proved off Windows, because
      `winfs.is_cloud_placeholder` accepts injected attribute bits and never opens the file.
      **Non-hydration** still needs confirming by watching the sync client on a real machine,
      exactly as written — it cannot be asserted in a test.
- [x] Chunk overlap verified: no text lost at boundaries. Asserted as a property over every
      healthy fixture at a chunk size small enough to force many boundaries: every word of the
      source must appear in at least one chunk.

**Outstanding: `email_pst.py`.** PST needs `win32com` against a live Outlook and cannot be
verified anywhere else, so it is a separate pass rather than untested code that looks finished.
Criteria 5 and 6 are marked `xfail(run=False)` in the acceptance file — visibly outstanding
rather than quietly missing. Layer 2 is **not complete** until they run and pass.

---

## Layer 3 — Indexing pipeline

**Goal:** point it at 100GB, walk away, come back to a resumable, incremental index.

**Build**

- `walker.py` — configurable roots, extension allowlist, exclusion globs
  (`node_modules`, `AppData`, `$Recycle.Bin`, `.git`, …). Emits candidates with
  `mtime_ns` + `size_bytes`; hashes contents only when mtime or size changed.
- `pipeline.py` — bounded producer/consumer:
  - walker → work queue (bounded, for backpressure)
  - N extraction workers (CPU count − 1)
  - one embedding worker, batching 64 chunks per `embed()` call
  - one writer (SQLite writes are serialised; LanceDB appends in batches of 1000)
  - cursor committed to `index_state` every N files, so a crash costs seconds, not hours
- **Prioritisation:** the user nominates folders to index first. The queue is a priority
  queue, not FIFO.
- **Disk guard:** a background check pauses the pipeline at `MIN_FREE_GB` with
  `ERR_DISK_SPACE`, preserving the cursor for interactive resume.
- **Incremental pass:** re-walk, compare `mtime_ns`+hash, re-index only changes, delete
  rows for files that disappeared, and re-queue anything marked `ERR_FILE_LOCKED`.

**Acceptance**

- [ ] 10k-file corpus indexes end-to-end with zero unhandled exceptions.
- [ ] Kill the process at 50%; restart resumes within one batch of where it stopped.
- [ ] Re-running over an unchanged corpus does near-zero work (verify by timing and write count).
- [ ] Touch one file → only that file is re-indexed.
- [ ] Delete one file → its rows disappear from SQLite, FTS and LanceDB.
- [ ] Inject a corrupt file mid-run → run completes, file appears in the skipped panel.
- [ ] Fill the disk artificially → pipeline pauses, does not corrupt, resumes after space is freed.
- [ ] Throughput measured and recorded (files/min, MB/min) as the baseline for the UI's ETA.

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
- `keyword.py` — FTS5 `MATCH` with BM25 ranking, taking its expression from `query.py`
  rather than from raw user input; support phrase, prefix and `NEAR`.
  `search_bm25()`'s `OperationalError` catch stays as a backstop, but with `query.py` in
  front of it that path should now be unreachable — if it ever fires, that is a bug in the
  sanitiser and must be logged, not swallowed.
- `vector.py` — embed the query once, ANN search with pushed-down filters (ext, date, folder).
- `fusion.py` — **[BUILT]** RRF: `score = Σ 1/(k + rank)`, `k=60`. Pure function, unit-tested.
  Fuses on rank, not score, so BM25 and cosine never need normalising against each other.
  Ties break by best rank then list order, so the output is deterministic and recall@10 is a
  meaningful regression guard. `fuse_hits()` tags each row with `sources` — which retrievers
  found it — because agreement between both is the strongest signal the pipeline produces and
  the UI should show it.
- `rerank.py` — cross-encoder over the top 30, loaded lazily, behind the Settings toggle.
  If the model is missing, log once and return the fused order — never fail the search.
- `engine.py` — parallel dispatch, diskcache keyed on
  `(query, filters, rerank_on, index_generation)`. Bump `index_generation` on any write so
  stale results cannot be served.
- **Model warm-up:** load the embedding model on app start in a background thread so the
  first user search is not the one paying the 1–2s ONNX load.
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

- [ ] A golden set of 30 query→expected-document pairs; track recall@10 as a regression guard.
- [ ] Warm search p95 <300ms on the real index — measured, not assumed.
- [ ] First search after launch <3s.
- [ ] Rerank on/off both work; toggling does not require a restart.
- [ ] Deleting the rerank model mid-session degrades gracefully with one log line.
- [x] Malformed queries (`"unclosed`, `AND AND`, emoji, 10k characters) return results or a
      clean `AppError` — never a crash. *Proved in `tests/unit/test_query.py` by running every
      generated expression against a real FTS5 table, rather than by catching the error.*
- [ ] Cache invalidates correctly after an incremental index run.
- [ ] Typing a 40-character query never blocks the UI and never embeds more than once.
- [ ] `type:pdf after:2024 "site survey" -draft` returns the same set as the equivalent
      filter chips; the two are interchangeable and keyboard-only operation is possible.
- [ ] An unparseable operator value (`after:nextthursday`) is surfaced as a hint in the UI,
      not silently ignored — `ParsedQuery.unknown_operators` carries it.

---

## Layer 5 — UI shell

**Goal:** the app a person actually uses. Everything before this was plumbing.

**Build**

- `shell.py` — `QMainWindow`; search bar always focused on launch; dark mode; shortcuts
  (`Ctrl+K` focus search, `Ctrl+,` settings, `Enter` open, `Ctrl+Enter` open containing
  folder, `Esc` clear).
- `search_view.py` — as-you-type with a 150ms debounce, running the **two-tier dispatch** from
  Layer 4 (BM25-only while typing, full hybrid on idle or `Enter`); filter chips for type, date
  range and folder, kept in sync both ways with the typed operator syntax from `query.py` —
  setting a chip writes `type:pdf` into the bar, and typing `type:pdf` lights the chip, so
  there is one state, not two; every search dispatched to the worker pool, never the UI thread.
- `results_view.py` — result rows showing path, snippet with query-term highlighting,
  score, modified date; click to open, right-click for "Open containing folder", "Copy path",
  "Add to document". Missing-file detection: if the path no longer exists, mark the row and
  offer to re-index.
- `indexing_view.py` — live progress, throughput, ETA (from Layer 3's measured baseline),
  pause/resume, folder prioritisation, and the **"N files skipped — review"** panel grouped
  by `AppError.code` with the fix text and a retry button.
- `settings_view.py` — index roots, exclusions, rerank toggle, data path, Ollama URL, and a
  "Run doctor" button that renders `doctor.py --json` inline.
- Drag-and-drop onto the window indexes the dropped files immediately, at top priority.

**Acceptance**

- [ ] UI stays responsive during a full index run (no frozen window, no beachball).
- [ ] Every error surfaced in the UI shows message + suggestion + working action button.
- [ ] Drag-drop a file → searchable within seconds.
- [ ] Closing mid-index persists the cursor; reopening offers to resume.
- [ ] Keyboard-only operation is possible end to end.

---

## Layer 6 — Knowledge graph

**Goal:** useful without Ollama; better with it.

**Build**

- `cooccurrence.py` — the **default**, no LLM. Entities from capitalised n-grams,
  email addresses, filenames and folder names; edges from co-occurrence within a chunk,
  weighted by PMI. Deterministic and fast.
- `entities_llm.py` — the **upgrade**. Batch chunks to Ollama for typed entity extraction
  (person / org / project / system / date). Strictly a background enrichment job with a
  progress indicator; it must never block search, and it must checkpoint so it can resume.
- `render.py` — networkx for layout and metrics, pyvis for the interactive view.
  Node click → filtered search on that entity.

**Acceptance**

- [ ] Graph renders from the co-occurrence baseline with Ollama stopped.
- [ ] Starting Ollama and running enrichment visibly improves entity quality.
- [ ] Killing Ollama mid-enrichment: job pauses with `ERR_OLLAMA_DOWN`, resumes later, no data loss.
- [ ] Graph stays interactive at 5k nodes (cap and cluster beyond that).

---

## Layer 7 — Office document builder

**Goal:** turn search results into a document without leaving the app.

**Build**

- `builder.py` — DOCX (python-docx), XLSX (openpyxl), PPTX (python-pptx).
- Selected results → a document with source path, date and snippet per entry, plus a
  citation list. Templates for "research summary", "evidence pack", "results table".
- Export raw results as CSV / JSON.

**Acceptance**

- [ ] Each format opens without a repair prompt in real Microsoft Office.
- [ ] Unicode, very long paths and 500-row exports all survive intact.
- [ ] Every generated document cites the source file path for every excerpt.

---

## Layer 8 — RAG answers (optional)

**Goal:** a natural-language answer over retrieved chunks — strictly additive.

**Build**

- `llm/ollama.py` — thin client. Health check before every call; on failure return
  `ERR_OLLAMA_DOWN` as `AUTO_FIX` and fall back to plain results with a one-line notice.
- Answers stream into the UI. Every claim carries a numbered citation back to a chunk,
  and clicking it opens the source at that location.
- Hard rule: **the answer panel is never on the critical path.** Results render first;
  the answer fills in when it is ready.

**Acceptance**

- [ ] With Ollama off, search is unaffected and the notice is accurate and non-alarming.
- [ ] With Ollama on, answers stream and every citation resolves to a real chunk.
- [ ] Stopping Ollama mid-answer produces a clean partial state, not a hung UI.

---

## Layer 9 — Hardening and packaging

**Build**

- Crash handler writing a redacted report to `logs\`.
- Graceful shutdown: flush the writer, commit the cursor, close both stores.
- Backup/restore of the index folder; "rebuild vectors from SQLite" recovery command.
- First-run wizard: pick index roots, estimate time from the Layer 3 baseline, start.
- Optional PyInstaller build; if it fights the ONNX runtime or Qt plugins, ship the
  venv + a shortcut instead. Do not let packaging block a working app.

**Acceptance**

- [ ] 24-hour soak index run with no memory growth beyond a flat ceiling.
- [ ] Force-kill at 20 random points; the index is recoverable every time.
- [ ] Full restore from backup produces identical search results.
- [ ] Cold-start to first search under 5s.

---

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
