# Local Knowledge Graph V2 — Layer-by-Layer Build Spec

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

- `base.py` — `Extractor` protocol: `supports(path) -> bool`, `extract(path) -> Iterable[Document]`.
  A registry maps extension → extractor.
- `pdf.py` — PyMuPDF. Per-page text with page numbers retained. Detect image-only pages
  and mark `ERR_NO_TEXT_LAYER` rather than indexing an empty string (OCR is out of scope for V2).
- `office.py` — DOCX paragraphs + tables; XLSX per-sheet cell text with sheet names;
  PPTX per-slide shape text plus speaker notes.
- `plaintext.py` — encoding detection (UTF-8 → UTF-8-sig → cp1252 → latin-1), emitting
  `ERR_ENCODING` as an `AUTO_FIX` when it falls back to replacement characters.
- `email_pst.py` — `win32com.client` Outlook MAPI. Walk stores → folders → items.
  Capture subject, sender, recipients, sent time, conversation ID, body, and recurse into
  attachments through the normal extractor registry. **Never `pypff`. Never `extract-msg`
  for `.pst`** — it reads `.msg` only.
- `email_files.py` — stdlib `email` for `.eml`; `extract-msg` is acceptable here and only here.
- `chunker.py` — ~512-token chunks with ~64-token overlap, split on paragraph then sentence
  boundaries; never mid-word. Chunk boundaries carry `char_start`/`char_end` so results can
  highlight in the original.

**Acceptance**

- [ ] A fixtures folder with one healthy and one deliberately corrupt file of each type.
- [ ] Every healthy fixture yields non-empty text and plausible chunk counts.
- [ ] Every corrupt fixture yields `ERR_FILE_CORRUPT` with `SKIP_CONTINUE` — and the run continues.
- [ ] A password-protected PDF and a locked-open XLSX both skip cleanly.
- [ ] PST extraction over a small test archive preserves conversation grouping.
- [ ] Chunk overlap verified: no text lost at boundaries.

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
 ├─ FTS5 BM25        (thread A)  -> top 100
 └─ LanceDB ANN      (thread B)  -> top 100
        └── reciprocal rank fusion (k=60)  -> top 50
              └── optional cross-encoder rerank of top 30
                    └── results
```

**Build**

- `keyword.py` — FTS5 `MATCH` with BM25 ranking; sanitise user input into valid FTS syntax
  (unbalanced quotes must not raise); support phrase, prefix and `NEAR`.
- `vector.py` — embed the query once, ANN search with pushed-down filters (ext, date, folder).
- `fusion.py` — RRF: `score = Σ 1/(k + rank)`, `k=60`. Pure function, unit-tested.
- `rerank.py` — cross-encoder over the top 30, loaded lazily, behind the Settings toggle.
  If the model is missing, log once and return the fused order — never fail the search.
- `engine.py` — parallel dispatch, diskcache keyed on
  `(query, filters, rerank_on, index_generation)`. Bump `index_generation` on any write so
  stale results cannot be served.
- **Model warm-up:** load the embedding model on app start in a background thread so the
  first user search is not the one paying the 1–2s ONNX load.

**Acceptance**

- [ ] A golden set of 30 query→expected-document pairs; track recall@10 as a regression guard.
- [ ] Warm search p95 <300ms on the real index — measured, not assumed.
- [ ] First search after launch <3s.
- [ ] Rerank on/off both work; toggling does not require a restart.
- [ ] Deleting the rerank model mid-session degrades gracefully with one log line.
- [ ] Malformed queries (`"unclosed`, `AND AND`, emoji, 10k characters) return results or a
      clean `AppError` — never a crash.
- [ ] Cache invalidates correctly after an incremental index run.

---

## Layer 5 — UI shell

**Goal:** the app a person actually uses. Everything before this was plumbing.

**Build**

- `shell.py` — `QMainWindow`; search bar always focused on launch; dark mode; shortcuts
  (`Ctrl+K` focus search, `Ctrl+,` settings, `Enter` open, `Ctrl+Enter` open containing
  folder, `Esc` clear).
- `search_view.py` — as-you-type with a 150ms debounce; filter chips for type, date range
  and folder; every search dispatched to the worker pool, never the UI thread.
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

| Stage | Budget | Where measured |
|---|---|---|
| Query embedding | <15ms | `search/vector.py` |
| FTS5 BM25 top-100 | <60ms | `search/keyword.py` |
| LanceDB ANN top-100 | <80ms | `search/vector.py` |
| RRF fusion | <5ms | `search/fusion.py` |
| Cross-encoder rerank top-30 | <200ms | `search/rerank.py` |
| **Total warm, rerank on** | **<300ms** | `search/engine.py` |
| Model load (once per session) | <2s | startup warm-up thread |

Log every stage timing at DEBUG. When the budget is missed, the log says which stage —
that is the entire point of the breakdown.

---

## Order of work

Layers 0 → 4 are strictly sequential; each depends on the one before. From Layer 5 the
order is negotiable — 6, 7 and 8 are independent of each other and all depend on 4.

Ship the CLI at each of 0–4. The first genuinely useful moment is the end of Layer 4:
a headless `python -m app.cli search "..."` that returns correct results in under 300ms
over the real corpus. Everything after that is interface.
