# Handoff

**Doc version:** 1.7 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2

Read this first if you are picking the project up cold - a new machine, a new chat, a new
person, or yourself in three months. It answers: where is it, what works, what is next, and
what will bite you.

`docs/PROJECT_INSTRUCTIONS.md` is the companion: the standing rules for *how* to work here.
This document is the state; that one is the contract.

---

## 1. What this is

A Windows desktop app that searches ~100GB of local files and Outlook email, combining
keyword and semantic search, with a knowledge graph and Office document generation.

Everything runs in **one process**. SQLite/FTS5 for metadata and keyword search, LanceDB for
vectors, FastEmbed ONNX for embeddings - all embedded libraries, no services, no ports, no
passwords. Ollama is optional and is never called by search.

That "one process" decision is the whole reason V2 exists. V1 required six cooperating
processes and was five points of failure before a single search ran.

## 2. Where everything lives

| What | Where |
|---|---|
| Code, docs, venv | `D:\SearchProject` |
| The index (vectors, FTS, cache, models) | `D:\KnowledgeGraphData` - set by `DATA_PATH` in `.env` |
| Machine-specific config | `D:\SearchProject\.env` - **gitignored**, written by the installer |
| Logs and diagnostics | `D:\SearchProject\logs\` - gitignored contents, tracked structure |

**The index is never inside the project folder**, and nothing in it is original data. It is
entirely rebuildable from your documents, so deleting it is always safe.

## 3. Current state

**Version 0.3.2. Layers 0-5 code-complete. 634 tests passing, 1 xfailed (real-Outlook COM, deliberately).**

| Layer | What it is | State |
|---|---|---|
| L0 | Foundation: config, errors, logging, single-instance, CLI | **Done** - v0.2.0 |
| L1 | Storage: SQLite/FTS5, LanceDB, migrations | **Done** - v0.3.0 |
| L2 | Extraction: PDF, Office, plaintext, Outlook/PST, chunking | **Code-complete** - one manual check left, see below |
| L3 | Indexing pipeline: walker, workers, resumable cursor | **Code-complete** - needs a real-scale run |
| L4 | Search: BM25 + ANN, RRF fusion, rerank | **Code-complete** - p95 needs the real corpus |
| L5 | PyQt6 UI shell | **Code-complete** - needs a human to run it |
| L6 | Knowledge graph | **Next** |
| L7 | Office document builder | Not started |
| L8 | Optional RAG answers | Not started |
| L9 | Hardening and packaging | Not started |
| L10 | Adaptive tuning (self-tuning / learning) | Not started - deliberately last |

### Built out of order, deliberately

`app/search/fusion.py` (reciprocal rank fusion) and `app/search/query.py` (query parsing and
the FTS5 sanitiser) are Layer 4 modules that already exist and are fully tested. Both are
**pure functions with no dependency on layers 2 or 3**, so building them early cost nothing
and removed risk from the layer where the performance budget is tightest.

Do not read this as permission to skip ahead generally. It worked because those two modules
take plain data in and return plain data out. Anything touching storage, extraction or the
pipeline must respect the order.

### Layer 2: done, with one thing only you can check

All eight acceptance criteria pass. `base.py`, `chunker.py`, `pdf.py`, `office.py`,
`plaintext.py`, `email_files.py` and `email_pst.py` are built and tested.

**The one outstanding item.** `Win32ComSession` is the only code that talks to COM, and no
test anywhere can prove it drives real Outlook. Everything above it - the folder walk,
conversation grouping, attachment dedup, `ERR_OUTLOOK_BUSY` handling - is tested against a
fake MAPI session and runs on any machine. So:

```powershell
venv\Scripts\python.exe -m app.cli extract --mailbox
```

with Outlook open. Until that has been run once and the counts look sane, treat Layer 2 as
code-complete but **not signed off**, and do not bump `VERSION` to 0.4.0.

**Design decisions inside PST worth not relitigating:**

- **Identity is the `EntryID`, never a folder path.** Moving a message between folders must
  not make it look like a new message. On a mailbox that gets reorganised, path-keying is the
  difference between an index that settles and one that grows forever.
- **Attachments dedup by content hash**, and the hash set belongs to the caller so Layer 3 can
  persist it in `files.content_hash`. 30GB of archives holds the same deck mailed round the
  team eight times; without this you get eight identical results and eight times the embedding.
- **`Deleted Items`, junk and sync-conflict folders are skipped by default.** On a fifteen-year
  archive Deleted Items is often a third of the messages, all of them things the owner threw
  away. Override with `skip_folders=frozenset()`.
- **A closed folder costs that folder, not the run.** By the time Outlook gets closed mid-index,
  thousands of messages may already be read; losing them would be unforgivable.
- **Nothing reaches past the Cached Exchange Mode cache.** Coverage is whatever Outlook already
  holds locally, and `store_cached_only` is recorded on every document so the gap is visible.

**Known gap:** loose `.eml` files on disk still record attachment *names* only; their contents
are not extracted. PST attachments are. Worth closing when it matters.

**Fixtures are generated, not committed** - `tests/fixtures/generate.py`, called automatically
by a session fixture in `conftest.py`. Binary test files in git cannot be reviewed in a diff,
and an editor that opens and re-saves one silently destroys the corruption it was testing.
Delete the fixture folders freely; the next test run rebuilds them.

### What works right now

**The app itself:**

```powershell
cd D:\SearchProject
venv\Scripts\python.exe -m app.main
```

Search bar focused on launch. `Ctrl+K` returns to it from anywhere, `Ctrl+I` shows indexing,
`Ctrl+,` settings, `Esc` clears, `F5` indexes. Add folders in Settings first, or drag them onto
the window. **Nobody has run this yet** - Qt cannot be started without a display, so it is the
one part of the project verified by reading rather than by testing. Expect wiring problems, not
logic problems: the decisions all live in `app/ui/presenter.py`, which has 44 tests and is
forbidden by another test from importing Qt at all.

**The command line:**

```powershell
venv\Scripts\python.exe -m app.cli stats      # resolved config + both store summaries
venv\Scripts\python.exe -m app.cli init       # create/migrate both stores, safe to re-run
venv\Scripts\python.exe -m app.cli doctor     # environment verification
venv\Scripts\python.exe -m app.cli diagnose   # troubleshooting bundle -> logs\diagnostics\
venv\Scripts\python.exe -m pytest tests -q    # 634 passed, 1 xfailed (~5 min)
venv\Scripts\python.exe -m pytest tests -q -m "not slow"   # fast loop, skips Layer 3 acceptance
```

**Layer 2's entry point - point it at your own documents:**

```powershell
venv\Scripts\python.exe -m app.cli extract "D:\Docs\report.pdf" --chunks
venv\Scripts\python.exe -m app.cli extract "D:\Docs" --limit 200
venv\Scripts\python.exe -m app.cli extract "D:\Docs" --out report.json
venv\Scripts\python.exe -m app.cli extract --mailbox     # Outlook archives + cached mailbox
```

**Layer 3 - actually build the index (this one writes):**

```powershell
venv\Scripts\python.exe -m app.cli index "D:\SearchData"
venv\Scripts\python.exe -m app.cli index "D:\SearchData" --first "D:\SearchData\Current"
```

`--first` is repeatable and ordered, so search becomes useful on the folders you care about
within minutes rather than after the whole corpus. Re-running is cheap: an unchanged file costs
a `stat()`, and a test asserts the second pass never opens one.

`extract` is read-only and `index` writes - that distinction is deliberate. `extract` is safe
to point at anything and is how you find out whether extraction works on *your* files rather
than on synthetic fixtures; it also reports MB/s, which is the evidence for the throughput
question in section 7.

**Layer 4 - search it:**

```powershell
venv\Scripts\python.exe -m app.cli search "site survey" type:pdf after:2024
venv\Scripts\python.exe -m app.cli search "valve replacement" --limit 5 --no-rerank
```

Typed operators work anywhere in the query: `type:` `after:` `before:` `path:` `from:`,
`"phrases"` and `-exclusions`. Every result says *why* it matched - keyword, meaning, or both
agreeing, which is the strongest signal the pipeline produces.

Every search and every result is recorded in `searches` / `search_hits`, and opening a result
marks it. That data is what Layer 10's tuning is derived from, it is local and clearable, and
it is collected now because it cannot be reconstructed later.

## 4. Resuming from cold

On the existing machine:

```powershell
cd D:\SearchProject
venv\Scripts\python.exe doctor.py             # must print READY
venv\Scripts\python.exe -m pytest tests -q    # must be green before you change anything
```

On a **new** machine:

1. Copy or clone `D:\SearchProject`. Do **not** copy `venv\` - it hardcodes paths. Do not
   copy `.env`; the installer writes a correct one.
2. Run `run-install.cmd`. It asks one question: where to build the index.
3. `venv\Scripts\python.exe -m pip install -r requirements-dev.txt` for the test tools.
4. `venv\Scripts\python.exe doctor.py` until it prints READY.
5. `venv\Scripts\python.exe -m pytest tests -q` - green before touching anything.
6. Read `docs/PROJECT_INSTRUCTIONS.md`, then `BUILD_SPEC_V2.md` for the next layer.

The index does not transfer usefully between machines: absolute paths are baked into it.
Rebuild it on the new machine.

## 5. Decisions already made, and why

Reopening these without new evidence wastes time. The reasoning matters more than the choice.

| Decision | Why | What would change it |
|---|---|---|
| One embedded process, no services | V1's six processes were five failure points before one search ran | Nothing plausible for a single-user desktop app |
| SQLite is the authority; LanceDB is derived | Lets a crash mid-write be recoverable: vectors rebuild from `chunks` without re-reading a document | Nothing - this asymmetry is load-bearing |
| Ollama never in the search hot path | Search must work with it stopped, crashed or uninstalled | Nothing |
| FastEmbed ONNX, not Ollama, for embeddings | An Ollama crash would otherwise kill search; a 7B rerank could never hit <2s on CPU | A local embedding server that is genuinely more reliable than in-process |
| RRF fusion, not score normalisation | BM25 scores and cosine distances are not comparable; RRF throws the scores away and fuses on rank, so there is nothing to calibrate or drift | Measured recall showing weighted normalisation beats it |
| ANN index only past 100k rows | A flat scan beats a badly trained IVF_PQ index below that | Benchmarks on the real corpus |
| Cloud placeholders skipped by default | Reading a OneDrive placeholder downloads the whole file; a naive walk would hydrate an entire library | Nothing - it is opt-in, which is the correct default |
| Token count estimated, not tokenized | Loading the real tokenizer would drag the embedding model into extraction, which must run with no model present. `token_cost` is biased high because guessing low means silent truncation at embed time, while guessing high only means slightly smaller chunks | Measured recall showing the estimate costs real results |
| Test fixtures generated, not committed | A binary fixture in git cannot be reviewed in a diff, and an editor that opens and re-saves one silently destroys the corruption it was testing | Nothing - a generator is strictly better |
| A skip is a value, not an exception | A corrupt file in a 100GB run is Tuesday, not an emergency. Extractors raise a precise `AppError` the caller records and moves past; non-fatal problems ride along in `Document.warnings` so a degraded file is still indexed | Nothing - this is non-negotiable #4 |
| `win32com` MAPI for email, never `pypff` | `pypff` has no reliable Windows wheels. `extract-msg` reads `.msg` only, not `.pst` | A maintained PST library with Windows wheels |
| Every `.ps1` ASCII-only **and** UTF-8 with BOM | PowerShell 5.1 decodes a BOM-less file as ANSI; one em dash became a smart quote and killed the installer at parse time, silently | Dropping Windows PowerShell 5.1 support |
| pydantic, not pydantic-settings | It is a separate distribution and is not installed. A dependency to save a dozen lines is a bad trade | Adding it to `requirements.txt` for a real reason |

## 6. Traps

Things that have already caused real failures, or will.

**PowerShell file encoding.** Covered above. `scripts\parse-check.ps1` enforces it and
`run-install.cmd` runs that check before the installer. VS Code is configured to save `.ps1`
as `utf8bom`. Do not defeat any of these.

**A PowerShell function returns everything it emits.** `& $python $script` followed by
`return $LASTEXITCODE` hands back `@(<stdout>, 0)`, not `0`. This reported a completely
successful model download as a failure. Route child output to `Write-Host` and report through
a script-scoped variable.

**winget's exit code lies.** `-1978335189` means "already installed, nothing to upgrade".
Every install step carries a `-Verify` block that ignores the exit code if the command works.

**LanceDB deletes are not reached by SQLite's cascade.** `store.delete_file(id)` cascades to
chunks, messages and FTS. It cannot touch vectors. Call `vectors.delete_by_file_ids([id])`
alongside it, every time, or search will keep returning rows whose source no longer exists.

**FTS5 external-content tables do not self-maintain.** The `chunks_ai` / `chunks_ad` /
`chunks_au` triggers in `schema.sql` are what keep search from silently returning stale text.

**Outlook must stay open** during an email index run, and Cached Exchange Mode means older
mail is not local. Read the email section of `LOCAL_KNOWLEDGE_GRAPH_V2.md` before Layer 2.

**Filesystem timestamps have a resolution, and Windows' is coarse.** Two writes inside one tick
share an mtime; if the edit preserves the file's size, no cheap check can see it. The walker
hashes anything modified in the last two seconds for exactly this reason
(`RECENT_EDIT_WINDOW_S`). Do not "optimise" that away - it was found by a real Windows run
after passing on Linux, and the failure it prevents is silent and permanent.

**Test corpora must be aged.** A fixture written moments before the test is inside that window,
so incremental tests would exercise the hot-file path instead of the thing they are named
after. `age()` in the Layer 3 and 4 acceptance files backdates them by an hour.

**Porter stemming is narrower than you expect.** It relates `approve`/`approved`, but *not*
`reconcile`/`reconciliation`. A test assertion about stemming cost an hour once - the test was
wrong, not the tokenizer.

## 7. Open questions

Not blockers, but decide them deliberately rather than by accident.

1. **Indexing throughput is unmeasured.** The spec says "days on CPU" for 100GB. Layer 3's
   acceptance criteria include measuring it on a 10k-file corpus, because the UI's ETA and the
   value of folder prioritisation both depend on the real number. If it comes back at two
   weeks rather than three days, prioritisation stops being a nicety.
2. **Cached Exchange Mode window.** How much of the live mailbox to index, and whether to
   fetch beyond the local cache, needs a UI decision in Layer 5.
3. **OCR is out of scope for V2.** Image-only PDFs are marked, not read. Revisit only if the
   real corpus turns out to be full of scans.
4. **Packaging.** PyInstaller may fight the ONNX runtime and Qt plugins. Layer 9 says ship the
   venv plus a shortcut rather than let packaging block a working app.

## 8. Where to look

| Document | For |
|---|---|
| `docs/PROJECT_INSTRUCTIONS.md` | The rules. Read before writing code |
| `BUILD_SPEC_V2.md` | What each layer delivers and its acceptance tests |
| `LOCAL_KNOWLEDGE_GRAPH_V2.md` | Architecture, error contract, email and cloud strategy |
| `docs/TROUBLESHOOTING.md` | When something breaks |
| `docs/VSCODE.md` | Editor setup |
| `docs/VERSIONING.md` | Version scheme, git conventions, release checklist |
| `CHANGELOG.md` | What changed, when, and why |

## 9. Keeping this document true

This file is updated **at every release**, alongside `VERSION` and `CHANGELOG.md`. The
release checklist in `docs/VERSIONING.md` requires it, and
`tests/unit/test_handoff_current.py` fails the suite if its **Applies to** version falls
behind `VERSION`.

A handoff document that has quietly gone stale is worse than none: it is confidently wrong,
and someone will act on it.
