# Changelog

All notable changes to this project are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows the scheme in `docs/VERSIONING.md`.

## [Unreleased]

### Planned
- Layer 2 - extraction: PDF/Office/plaintext/PST parsers and chunking

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
