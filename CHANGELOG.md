# Changelog

**Doc version:** 1.2 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2

All notable changes to this project are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows the scheme in `docs/VERSIONING.md`.

## [Unreleased]

**Layer 2, files complete; Outlook outstanding.** Six of the eight acceptance criteria pass.
PST needs `win32com` against a live Outlook and cannot be verified anywhere else, so it is a
separate pass rather than untested code that looks finished. Layer 2 is **not done** and
`VERSION` is deliberately not bumped to 0.4.0.

### Added
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
- `extract-msg==0.56.1` in `requirements.txt`.

### Fixed
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

### Planned
- `app/extract/email_pst.py` - Outlook MAPI: stores, folders, conversation grouping, attachment
  recursion through the registry, and `ERR_OUTLOOK_BUSY` when Outlook closes mid-run.
  Attachment recursion for `.eml` lands with it; today attachment names are recorded but their
  contents are not extracted.
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
