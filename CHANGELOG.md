# Changelog

**Doc version:** 1.7 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2

All notable changes to this project are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows the scheme in `docs/VERSIONING.md`.

## [Unreleased]

**Layers 2, 3, 4 and 5 code-complete.** All of Layer 2's eight acceptance criteria and all of
Layer 3's eight pass. The one thing no test can
reach - that `Win32ComSession` drives real Outlook - is a single `xfail(run=False)` plus a
manual `app.cli extract --mailbox`. `VERSION` stays at 0.3.2 until that has been run once.

### Added — Layer 5, the desktop app
- **`app/ui/presenter.py`** - every UI decision that is not drawing, and **it imports no Qt**.
  Widgets cannot be instantiated without a display, so logic inside them could only ever be
  checked by a person clicking; this module holds the snippet windowing, highlight offsets,
  tier selection, ETA phrasing and skip grouping, and 44 tests cover it. Two further tests
  enforce the split itself: one fails if Qt ever appears in the presenter's imports, one if a
  view module grows past 250 lines, because a long view is where untested logic hides.
- `app/ui/workers.py` - `QThreadPool` wrappers so the UI thread never does I/O. Each converts
  what escapes into an `AppError`: an exception leaving a `QRunnable` vanishes, and the UI
  would wait forever for a signal that never comes.
- `app/ui/search_view.py` - two debounce timers, and **generation-tagged dispatch** so a slow
  search landing after newer typing is dropped rather than overwriting fresher results.
- `app/ui/results_view.py` - path, location, why it matched, and a snippet centred on the hit
  with terms picked out. A result whose file has vanished is **marked, not hidden** - that is a
  genuine finding, and dropping it silently would make the count disagree with the list.
- `app/ui/indexing_view.py` - progress **by file count, never by bytes**: measured on the real
  corpus, a 40MB deck yields fewer chunks than a 30KB Word document, so a byte-based bar sits
  frozen and then races. Plus the skipped-files panel, grouped by cause, biggest first.
- `app/ui/settings_view.py` - roots, toggles, `doctor.py --json` rendered inline, and **clear
  search history**, because the usage log is a record of what someone searched on their own
  machine and must be theirs to erase.
- `app/main.py` - the entry point. Settings first, then the single-instance lock **before**
  either store is opened, then the window; models warm on a background thread.

### Added — Layer 4, search
- **Schema v2**: `searches` and `search_hits`, plus a migration so an existing index gains them
  without a rebuild. Built seven layers before anything reads them because this is the one part
  of adaptive tuning that **cannot be added later** - in six months there is no record of what
  was searched or what turned out to be useful. Local, never transmitted, and clearable.
- `app/search/keyword.py` - BM25 over the sanitised expression, with **filters applied in SQL**.
  Fetching 100 rows and discarding 90 to honour `type:pdf` leaves the 10 best of the wrong set;
  filtering in the query makes the top 100 the top 100 *that match the filter*.
- `app/search/vector.py` - one query embedding per search, ANN with eligible file ids pushed
  down as a prefilter. Above 2,000 ids the prefilter is dropped, because an `IN (...)` list
  that long costs more than the search it was meant to narrow.
- `app/search/rerank.py` - cross-encoder over the top 30, lazily loaded, **never able to fail a
  search**. The model is optional and 1.1GB; missing, half-downloaded and deleted-mid-session
  are all handled identically - log once, keep the fused order, succeed. Returning nothing
  because an optional precision step could not load would be worse than never having it.
- `app/search/engine.py` - parallel dispatch, RRF, generation-keyed cache, and the two-tier
  design: BM25-only while typing, full hybrid on Enter. `SearchResult.explain()` says *why*
  something matched, because trust comes from being able to ask.
- `app.cli search` with typed operators, `--limit` and `--no-rerank`.
- 42 Layer 4 acceptance tests against a real index, including a golden set with recall@10 as a
  regression guard.

### Added — Layer 3, the indexing pipeline
- `app/index/walker.py` - roots, allowlist, exclusions, change detection.
  **Exclusions prune during the walk, never filter after it**: descending into a 40,000-file
  `node_modules` and discarding it costs the whole subtree, so a test counts the directories
  actually visited rather than the output - a filter-afterwards implementation produces
  identical output and takes a hundred times longer.
  **The hash decides; mtime only decides whether to hash.** robocopy, a restore from backup,
  cloud sync and archive extraction all reset mtime while leaving bytes identical. Trusting
  mtime alone would re-index the whole corpus every time any of those happened, and a test
  touches a file and asserts it is *not* re-indexed.
- `app/index/embedder.py` - batched FastEmbed wrapper, lazily loaded, with three guards for
  things that otherwise fail in silence: a dimension mismatch (what changing `EMBED_MODEL`
  looks like), an un-normalised vector (raises nothing, ranks wrong forever), and a batch that
  returns the wrong number of vectors (which would pair chunks with other chunks' vectors).
  The encoder is injectable, so all of it is tested with no model on disk.
- `app/index/pipeline.py` - bounded priority queue, N extraction workers, one embed-and-write
  consumer. Embedding is deliberately not parallelised: ONNX already uses every core inside one
  call. **Both queues are bounded** - without backpressure the process dies of memory around
  hour three having written nothing.
  **Resumability is the `files` table, not a saved position**: a restart re-walks and skips
  what is already `INDEXED` for the cost of a `stat()`, which is more robust than an offset
  that goes wrong the moment the corpus changes underneath it.
  **Chunks and vectors are written before the file is marked `INDEXED`** - a crash between them
  leaves a file that looks unfinished and gets redone, where the reverse would leave it marked
  done with no chunks, invisible to search and never retried.
- `app.cli index` - Layer 3's entry point, with `--first` for folder prioritisation (repeatable
  and ordered), `--fast`, `--no-prune`, `--include-cloud`. Takes the single-instance lock,
  because unlike `extract` it writes.
- 79 Layer 3 tests: 30 walker, 19 embedder, 25 acceptance, 9 CLI.

### Added — Layer 2
- `app/extract/base.py` - the extraction contract. `Document` holds one flat `text`, and every
  `Segment` carries the exact range it occupies within it, so
  `text[s.char_start:s.char_end] == s.text` always. That invariant is what lets Layer 5
  highlight a hit inside the original instead of guessing; `DocumentBuilder` appends text and
  records offsets in one operation so no extractor can let the two drift. The registry refuses
  a duplicate extension claim rather than letting whichever module imported last win.
- `app/extract/chunker.py` - ~512-token chunks, ~64 overlap, paragraph then sentence
  boundaries, never mid-word. Text is atomised once into words-with-spans tagged by the
  boundary preceding them; a chunk is a contiguous range of atoms, which makes exact offsets
  and whole-word boundaries true by construction rather than by care.
- `app/extract/pdf.py` - PyMuPDF, page numbers retained. A PDF with no text on any page is
  skipped as `ERR_NO_TEXT_LAYER` and counted, rather than indexed as an empty success - a scan
  that "indexes cleanly" with no text is unfindable forever while appearing to have worked.
- `app/extract/office.py` - DOCX (document order, tables included), XLSX (`data_only`, sheet
  names in the text, 5,000-row cap), PPTX (slides plus speaker notes).
- `app/extract/plaintext.py` - UTF-8 → cp1252 → latin-1 with the BOM stripped, and NUL-byte
  detection so a binary file with a `.log` extension does not fill the FTS index with garbage.
- `app/extract/email_files.py` - `.eml` via the stdlib, `.msg` via extract-msg. Thread grouping
  from `References[0]`, so every reply in a conversation shares a key.
- `ERR_NO_TEXT_LAYER` and `ERR_UNSUPPORTED_TYPE` in the error registry. The spec referenced the
  first by name and Layer 0 never registered it.
- `tests/fixtures/generate.py` - the fixture corpus, **generated rather than committed**.
  Binary fixtures in git rot: nobody can review a `.docx` diff, nobody remembers which byte was
  corrupted on purpose, and an editor that opens and re-saves one silently destroys the property
  it was testing. Every corruption is now a reviewable line of code.
- 98 Layer 2 tests plus `tests/integration/test_layer2_acceptance.py`, numbered to the spec's
  checklist. The overlap criterion is asserted as a property - every word of every healthy
  fixture must survive into at least one chunk - because a word lost between two chunks is
  unfindable and nothing in the system would ever report it.
- **`app.cli extract`** - Layer 2's headless entry point, which ground rule 7 requires and the
  layer had shipped without. Files or folders, `--chunks` to see every chunk with its page,
  token estimate and offsets, `--text` for the whole document, `--json` for a machine-readable
  dump, `--limit` when pointed at something large. Read-only by construction: it opens no store
  and writes nothing, so it is safe to point at anything. Cloud placeholders are checked
  **before** the file is opened, because opening one is what triggers the download; a test
  asserts it. Prints per-file timing and an overall MB/s - the first real input to the
  throughput question Layer 3 has to answer.
- 23 tests for the command, including one asserting it never opens a store.
- **`app/extract/email_pst.py`** - Outlook archives and the live mailbox, via MAPI.
  **The COM calls are confined to `Win32ComSession`**; the walk, the conversation grouping,
  the attachment dedup and every error path work against small duck types and are tested with
  a fake on any machine. Only the adapter needs Windows, which is the smallest untested
  surface this could have had - the alternative was shipping the whole thing unverified.
  Identity is the `EntryID`, never a folder path: moving a message between folders must not
  make it look like a new one, or an index over a mailbox people reorganise never settles.
  Attachments are **deduplicated by content hash**, and the hash set is the caller's, so
  Layer 3 persists it in `files.content_hash` and dedups across runs rather than within one.
  `Deleted Items`, junk and sync-conflict folders are skipped by default. A `com_error` in one
  folder becomes `ERR_OUTLOOK_BUSY` and the walk continues - by the time Outlook gets closed,
  thousands of messages may already have been read.
- `app.cli extract --mailbox` - walk Outlook rather than a path, reporting counts per store,
  attachment totals and any folders that could not be read. Still writes nothing.
- 27 PST tests driving a fake MAPI session, plus acceptance criteria 5 and 6 implemented
  against it in `test_layer2_acceptance.py`.
- **PPTX now reads grouped shapes, tables and charts.** `slide.shapes` yields top-level shapes
  only, and a group is one opaque shape with no text frame - so reading `has_text_frame` alone
  silently lost every word inside every group, and grouping is how slides get built. On a
  deck-heavy corpus that was not an edge case, it was most of the content. Tables and charts
  had the same problem: a comparison table and a chart's category labels are exactly what
  people search for, and neither has a text frame. A test reconstructs the old logic and
  asserts it finds none of it, so the fix cannot quietly rot.
- **A picture-heavy deck is now flagged.** A PDF with no text is skipped outright, but a deck
  always has a title, so it indexed "successfully" while most of its content stayed
  unsearchable. Below 400 characters per megabyte it now carries an `ERR_NO_TEXT_LAYER`
  warning and is still indexed - the PPTX analogue of a scanned PDF.
- `app.cli extract --out PATH` writes the JSON itself, in UTF-8. Windows PowerShell 5.1's `>`
  redirection emits UTF-16LE with a BOM, which every JSON reader then rejects - the same
  encoding trap that killed `install.ps1` at parse time.
- Skipped files now carry their size, and the summary separates bytes *seen* from bytes *read*.
  A 100MB archive that vanished from the totals because it was skipped made them a lie.
- `extract-msg==0.56.1` in `requirements.txt`.

- `doctor.py` now checks **Git on PATH**. It never did, despite `install.ps1` installing Git and
  the whole release process in `docs/VERSIONING.md` depending on it. Optional, because the app
  runs fine without it - `build_info()` degrades to a version with no commit - but every
  convention silently stops working and the failure surfaces as "git is not recognized" long
  after the installer said it was done. The usual cause is not a missing install but a
  PowerShell window opened *before* Git was installed, which keeps its stale PATH until closed,
  so the fix leads with refreshing PATH in place rather than reinstalling.

### Removed
- `ERR_PST_NOT_BUILT`. It existed for one afternoon to make the gap visible; PST is built, so
  a `.pst` now either indexes or fails for a real reason. A code nothing raises is a lie in the
  registry.

### Fixed
- Three wiring bugs found by reading the UI back rather than running it: `clicked` and
  `triggered` emit a `bool`, so binding a keyword-only slot to them would have raised
  `TypeError` the first time anyone pressed the button or F5; `finished` was connected inside
  the start handler, so the tenth index run would have refreshed the status bar ten times; and
  index roots were never persisted, so every restart forgot which folders to index. Settings
  that vanish on restart are not settings - they now live in `index_state`, with the index they
  describe.
- **The search cache handed out its stored object and then mutated it.** Stamping `from_cache`
  and `elapsed_ms` on a cached `SearchResponse` changed what every earlier caller was still
  holding, so a response somebody got two searches ago would silently start claiming it came
  from a cache it had not. diskcache pickles and so returns a fresh object; an in-memory cache
  does not, and the engine must not depend on which it was handed. Found by an acceptance test.
- **`request_stop()` did not stop anything.** The flag was set but the consumer loop never
  checked it, so a run continued to completion after being asked to stop - the UI's pause
  button would have done nothing, and the disk guard only worked by accident. Found by an
  acceptance test that stopped a run halfway and got a complete one.
- **The prune step never ran.** It was guarded on the same event that `run()`'s cleanup always
  sets, so "did we stop early?" was permanently true and deleted files were never removed from
  the index. Two meanings had been folded into one flag; they are now separate, and the
  distinction matters: an interrupted walk has not seen the whole corpus, so pruning after one
  would delete perfectly good rows.
- **Chunks ran ~35% over budget.** The chunker costed each word at 1 token while
  `estimate_tokens` valued a chunk at 1.35 tokens/word, so a chunk built to a 512-token budget
  measured 690 and bge-small truncated the tail in silence - the worst kind of bug, because
  nothing fails and search just gets quietly worse. `token_cost` is now the single definition
  and `estimate_tokens` is its sum, so the two cannot disagree. A length term was added for
  words that are not words: a base64 blob costed at 1.35 tokens would have blown any budget.
- `Document.page_for_offset` and `page_lookup` disagreed for an offset landing in the separator
  written between segments. The linear scan returned the document's last page; the binary search
  returned the correct one. Both now resolve by segment *start*.
- `test_docs_versioned.py` hung: `rglob` descends into `venv/Lib/site-packages` before filtering
  it out, which on a mounted drive takes long enough to look like a crash. It now prunes during
  the walk. 0.9s.
- **`app.cli extract PATH --json` failed** with "unrecognized arguments". `--json` and `--env`
  were declared on the parent parser, which in argparse means they are only accepted *before*
  the subcommand - not the order anyone types. Both now work either side, via a shared parent
  with `default=argparse.SUPPRESS`; without SUPPRESS the subparser writes its own default over
  the already-parsed global and `--json extract` silently stops being JSON.
- **The test suite crashed after passing.** Every test went green, then pytest raised
  `PermissionError: [WinError 5]` from `pytest_sessionfinish` while cleaning its temp root: it
  keeps a `pytest-current` junction under `%LOCALAPPDATA%\Temp`, and creating or resolving a
  junction needs a privilege a normal Windows account may not have. The suite passed and the
  process still exited non-zero. `--basetemp=.pytest_tmp` in `pyproject.toml` sidesteps the
  junction entirely and puts scratch I/O on the project's own drive.
- A binary file with a text extension was skipped with `ERR_NO_TEXT_LAYER`'s default advice,
  which talks about scanned documents and OCR - nonsense for a renamed database. It now carries
  advice about the extension instead. An error giving the wrong fix is worse than one giving none.

### Known gaps
- **`.msg` happy path is untested.** A valid `.msg` is an OLE compound document and cannot be
  synthesised in a fixture generator; only the corrupt and missing-library paths are covered.
  A real Outlook-saved sample would close this.
- **`extract-msg` breaks the wheel rule** at the top of `requirements.txt`. The package itself
  is a `py3-none-any` wheel, but its dependency `red-black-tree-mod` publishes no wheel at all.
  It is two pure-Python files with no C sources, so pip builds it locally in seconds and no
  compiler is needed - but `pip download --only-binary :all:` now fails on this tree, and a
  fully offline install needs the sdist cached. `.msg` support is optional and the import is
  guarded, so removing the pin degrades `.msg` to "unsupported" rather than breaking the app.

### Docs
- `BUILD_SPEC_V2.md` → **2.3** (Layer 2 build notes, the CLI entry point, acceptance boxes
  ticked and the two PST criteria left visibly open), `CHANGELOG.md` → **1.3**,
  `HANDOFF.md` → **1.2**, `README.md` → **1.3**, `tests/fixtures/README.md` → **1.1**.

### Planned
- Attachment recursion for `.eml` files on disk. PST attachments are extracted; loose `.eml`
  files still only record attachment names.
- Layer 3 - the indexing pipeline: walker, resumable queue, embedder.

---

## [0.3.2] - 2026-08-24

Continuity documents, so the project survives being moved, paused or handed on.

### Added
- **`HANDOFF.md`** - the pick-it-up-cold document: where everything lives, what works today,
  how to resume on a new machine, the decisions already made *with their reasoning*, the traps
  that have already caused real failures, and the genuinely open questions.
- **`docs/PROJECT_INSTRUCTIONS.md`** - the standing contract: the ten non-negotiables, how a
  layer gets built, testing and commit conventions, the release checklist, what is deliberately
  out of scope, and how to brief an AI assistant on the project.
- `tests/unit/test_handoff_current.py` - fails the suite if either document's **Applies to**
  version falls behind `VERSION`, or if `HANDOFF.md` loses a section someone resuming needs.
  Enforced rather than trusted to a checklist: a stale handoff is confidently wrong.

### Changed
- Release checklist in `docs/VERSIONING.md` now requires updating `HANDOFF.md`.

### Docs
- `HANDOFF.md` 1.0, `docs/PROJECT_INSTRUCTIONS.md` 1.0, `docs/VERSIONING.md` 1.2,
  `README.md` 1.2. All documents re-pointed at app v0.3.2.

---
## [0.3.1] - 2026-08-24

Troubleshooting infrastructure, and two scope questions answered in the spec.

### Added
- **Structured log folders.** `logs/app` (daily narrative), `logs/errors`
  (warnings and errors as JSON Lines), `logs/install` (installer transcripts),
  `logs/crash`, `logs/diagnostics`. A generated `logs/README.txt` explains each.
  The JSONL sink exists so 3,000 failures in a 100GB run can be *counted and grouped*
  rather than read.
- **`app.cli diagnose`** - one command producing a zip with the environment report, both
  store summaries, config, disk space, package versions, git state and recent logs. Every
  section is collected inside its own guard: a diagnostic that fails when things are broken
  would be worse than useless, so a broken section records its own failure and the bundle is
  still produced. Large logs contribute their tail rather than being dropped or bloating the zip.
- `docs/TROUBLESHOOTING.md` - written for a hobby programmer: what each log means, how to
  read an error line, and the common failures with their fixes.
- `app/core/winfs.py` - cloud placeholder detection for OneDrive and SharePoint sync folders.
- `ERR_CLOUD_ONLY` and `ERR_OUTLOOK_BUSY` error codes, both `SKIP_CONTINUE`.
- VS Code task: **Diagnose (bundle for troubleshooting)**.

### Changed
- **Email scope corrected in the spec.** "PST indexing" was too narrow. MAPI enumerates every
  store Outlook has open, so the same code path indexes the **live Exchange/M365 mailbox** as
  well as `.pst` archives. Documented with its real constraints: Outlook must stay running,
  Cached Exchange Mode governs what is local, and the indexer is strictly read-only.
- **OneDrive and SharePoint documented, with the trap named.** Both sync to ordinary local
  folders, so indexing them needs no API. But Files On-Demand placeholders download in full
  when read, so a naive walk would hydrate an entire cloud library. Placeholders are skipped
  by default; pinned files index normally.
- Installer transcripts moved to `logs/install/`.

---
## [0.3.0] - 2026-08-24

**Layer 1 complete.** All five acceptance criteria pass; 70 tests green.

### Added
- `app/storage/sqlite_store.py` - files, chunks, messages, FTS5, skip ledger, resumability
  cursor and the write generation. One connection with a write lock: WAL gives many readers
  alongside one writer, which is exactly this app's shape, so `database is locked` is avoided
  rather than retried around. `mark_skipped()` records why a file was skipped so a bad file is
  remembered, not raised. `search_bm25()` returns [] on a malformed query instead of throwing.
- `app/storage/vector_store.py` - LanceDB table lifecycle with an explicit Arrow schema, so
  the fixed vector width is enforced by Arrow itself and an empty index is inspectable rather
  than absent. Refuses a dimension mismatch on connect, which is what a changed `EMBED_MODEL`
  looks like, instead of silently poisoning every future search. The ANN index is only built
  past 100k rows, because a flat scan beats a badly trained index below that.
- `app/storage/migrations.py` - schema versioning independent of the app version. Refuses to
  open an index written by a newer build rather than corrupting it.
- `app.cli init` - creates and migrates both stores, safe to re-run. `stats` now reports both
  stores when they exist, and stays read-only when they do not.
- 17 Layer 1 tests, including a hard `os._exit(9)` mid-transaction to prove committed data and
  the cursor survive while uncommitted data does not.

### Fixed
- `set_message()` inserted NULL into `has_attach`, which is `NOT NULL`. Caught by the tests.
- `VectorStore` used `table_names()`, deprecated in lancedb 0.37; now uses `list_tables()`
  with a fallback so a version bump cannot silently break table detection.
- `git_describe()` swallowed its failure reason, hiding why `stats` printed no git line.
  It now reports the cause, and recognises git's "dubious ownership" refusal specifically,
  appending the exact `safe.directory` command that fixes it.

---
## [0.2.0] - 2026-08-24

**Layer 0 complete.** All four acceptance criteria from `BUILD_SPEC_V2.md` pass.

### Added
- `app/core/errors.py` - `AppError`, `ActionType`, and a registry covering all eight codes
  from the spec's recovery table plus `ERR_CONFIG_INVALID`, `ERR_CONFIG_MISSING`,
  `ERR_NOT_IMPLEMENTED` and `ERR_UNEXPECTED`. `guard()` converts anything escaping a worker
  boundary into an `AppError`, and passes precise errors through unflattened. Templates fill
  safely: a missing context key degrades the message rather than raising on an error path.
- `app/core/config.py` - typed `Settings` validated at startup, not at first use. Every path
  is created and proved writable before the app runs, so a disconnected drive fails
  immediately rather than three minutes into a 100GB index. Built on plain pydantic and a
  stdlib .env parser: `pydantic-settings` is a separate distribution and is not installed.
- `app/core/logging.py` - loguru with console (INFO) and rotating file (DEBUG, 10MB, 14 days)
  sinks. `error_code` is a structured field so Layer 5 can group thousands of skips by cause.
  `diagnose=False` deliberately: variable dumps would leak indexed file contents into logs.
- `app/core/single_instance.py` - Windows named mutex via ctypes rather than pywin32, because
  refusing to start must not depend on an optional dependency. POSIX fallback for tests.
- `app/cli.py` - `stats`, `doctor`, `lock` working; `index` and `search` declared and failing
  with `ERR_NOT_IMPLEMENTED` naming the layer that delivers them.
- 53 tests: unit coverage of errors and config, plus the four Layer 0 acceptance tests.
- VS Code project: `.vscode/{settings,launch,tasks,extensions}.json`,
  `SearchProject.code-workspace`, `pyproject.toml`, `requirements-dev.txt`, `docs/VSCODE.md`.
  Settings force `utf8bom` for PowerShell files, making the parse-time encoding failure
  impossible to reintroduce from the editor.

### Fixed
- `make_error()` raised `TypeError: got multiple values for argument 'component'` whenever an
  exception was converted, because `to_app_error` put `component` into `**context` where it
  collided with the positional parameter. Found by the tests, not in production.
- The installer's transcript recorded nothing at all from `doctor.py`. Native stdout was
  being swallowed; it is now routed through `Write-Host` like every other child process.

---
## [0.1.0] — 2026-08-24

First versioned state of the project. Environment and plan only; no application code yet.

### Added
- `install.ps1` — automated Windows installer, self-locating, with per-step
  **[R]etry / [C]ontinue / [A]bort** prompting on failure
- `requirements.txt` — dependency pins re-verified against PyPI, all with Windows wheels
- `doctor.py` — environment verification with a remediation for every failure;
  `--json` and `--quick` modes; required vs optional check distinction
- `BUILD_SPEC_V2.md` — layer-by-layer build plan (L0–L9) with acceptance tests,
  SQLite and LanceDB schemas, module layout, and a per-stage performance budget
- `app/` package skeleton matching the build spec, with layer ownership recorded in
  every module docstring
- `app/core/version.py` — single source of truth for the version, read from `VERSION`
- Versioning: `VERSION`, `CHANGELOG.md`, `docs/VERSIONING.md`, `.gitignore`, `.env.example`
- `tests/` skeleton with fixture folders for healthy and deliberately corrupt files

### Fixed
Faults found by the first real installer run, corrected rather than worked around:

- **`Install Git` failed with exit code `-1978335189`.** winget returns
  `UPDATE_NOT_APPLICABLE` when a package is already installed and current — not a failure.
  Every install step now carries a `-Verify` block; if the command works afterwards, the
  exit code is ignored.
- **`Could not open requirements file`.** The installer created a subfolder, `Set-Location`'d
  into it, and looked for files that were never there — and because it had been launched from
  `C:\Windows\system32`, that is where the project landed. The project folder is now the
  script's own folder (`$PSScriptRoot`), and `requirements.txt` and `doctor.py` are checked
  for in preflight before anything is installed.
- **Both model downloads raised tracebacks.** Pure cascade: pip never ran, so `fastembed`
  was not importable. Package install is a required step and now aborts by default.
- **`ollama pull mistral` stalled on `pulling manifest`.** A freshly installed Ollama has no
  service listening. The step now waits up to 60s for `127.0.0.1:11434`, starting
  `ollama serve` if needed, before pulling.
- **`doctor.py` was run from the wrong directory** and could not be found. Absolute paths
  throughout; `Push-Location`/`Pop-Location` around anything that must change directory.
- **Braille progress spinners rendered as `â ‹`.** Console output encoding is now set to UTF-8.
- **`continue` inside a `switch` inside a `while`** continued the switch, not the loop, so
  "Retry" would not have retried. Replaced with `if`/`elseif`.
- **`.env` was written with a UTF-8 BOM** by PS 5.1's `Set-Content -Encoding UTF8`, which
  breaks some `.env` parsers. Now written BOM-free.

### Changed
- **Dependency pins refreshed.** The draft pins were indicative and badly stale — notably
  `fastembed==0.4.2`, which predates the current `TextCrossEncoder` rerank API:
  PyQt6 6.7.1→6.11.0, lancedb 0.15.0→0.37.1, fastembed 0.4.2→0.8.0,
  pymupdf 1.24.10→1.28.2, python-docx 1.1.2→1.2.0, pywin32 306→312,
  networkx 3.3→3.6.1, pydantic 2.9.2→2.13.4, python-dotenv 1.0.1→1.2.3,
  loguru 0.7.2→0.7.3, tqdm 4.66.5→4.70.0, requests 2.32.3→2.34.2.
  Verified by round-trip, not just version number.
- **Free-space check reconciled.** The installer demanded 150GB while `doctor.py` checked
  for 250GB against the *current directory*. Both now check the drive holding `DATA_PATH`
  against 150GB, overridable via `-RequiredFreeGB`.
- **Model cache location made explicit.** Both `FASTEMBED_CACHE_PATH` and the `cache_dir=`
  constructor argument are set, so models cannot silently land in the user profile instead
  of the index drive.
- V1's layered build plan superseded — it assumed FastAPI, PostgreSQL and Qdrant.

[Unreleased]: https://example.invalid/compare/v0.1.0...HEAD
[0.1.0]: https://example.invalid/releases/tag/v0.1.0
