# Leasha — architecture and installation

**Doc version:** 2.4 · **Updated:** 2026-08-26 · **Applies to:** app v0.3.3

> **Renamed.** This document was `LOCAL_KNOWLEDGE_GRAPH_V2.md`, and the application was
> "Local Knowledge Graph Search + Office Suite". Neither name fits any more: the knowledge
> graph was removed and the Office builder cancelled, because neither was what the tool is
> for. See `HANDOFF.md` §3a for the decision and what would reverse it.
>
> The filename is kept for now so existing links and the installer do not break. It is a
> Layer 9 job to rename it once nothing points at it.

**Project Type:** Windows desktop app, embedded single-process architecture
**Target OS:** Windows 10/11 only, single user
**Tech Stack:** PyQt6 + SQLite/FTS5 + LanceDB + FastEmbed (ONNX) + PyMuPDF — Ollama optional
**Data Scale:** 100GB
**Search Target:** <300ms warm search, <3s first search after launch (model load)
**Status:** 🟡 Installer + doctor written and corrected — run `install.ps1`, then build from `BUILD_SPEC_V2.md`

**Files in this folder:**

| File | Purpose |
|---|---|
| `run-install.cmd` | **Start here.** Parse-checks, bypasses execution policy, then runs the installer. |
| `install.ps1` | The installer itself. |
| `scripts\parse-check.ps1` | Real PowerShell parse + encoding audit; logs to `logs\parse-check.log`. |
| `requirements.txt` | Pinned dependencies, all verified to ship Windows wheels. |
| `doctor.py` | Environment verification with a fix for every failure. |
| `BUILD_SPEC_V2.md` | Layer-by-layer build plan (L0–L9) with acceptance tests. |
| `LOCAL_KNOWLEDGE_GRAPH_V2.md` | This document — architecture and contracts. |

---

## WHAT CHANGED FROM V1 AND WHY

V1 required six cooperating processes (PostgreSQL, Redis, Qdrant, Ollama, FastAPI, PyQt6). For a single-user desktop app that is five points of failure before one search runs. V2 is **one process + one optional helper (Ollama)**.

| V1 | V2 | Why |
|---|---|---|
| PostgreSQL 15 service | **SQLite (WAL mode) + FTS5** | Zero admin, no service, no passwords, BM25 keyword search built in |
| Qdrant server | **LanceDB (embedded)** | In-process, single folder on disk, real ANN indexing — sub-300ms at 20–30M vectors. (sqlite-vec is simpler but brute-force; too slow above ~2M chunks) |
| Redis service | **diskcache + in-process LRU** | A cache server for one client is pure overhead |
| FastAPI + HTTP + 2nd venv | **Engine runs in QThread/worker process inside the app** | No ports, no network timeouts, no client-server friction |
| Ollama Mistral 7B for embeddings + rerank | **FastEmbed (ONNX, in-process) for embeddings; small cross-encoder for rerank; Ollama optional for RAG answers only** | Ollama crash no longer kills search; 7B rerank could never hit <2s on CPU |
| pdfplumber | **PyMuPDF (fitz)** | C-based, 10–20× faster — matters at 100GB. (AGPL: fine for personal use) |
| pypff for PST | **libpff (default, optional install) / win32com Outlook MAPI (fallback, and always for .ost)** | Revised 2026-08-24: pypff is not on PyPI, but `libpff-python` compiles and works. It needs Build Tools for VS, so it stays optional and unpinned and every import is guarded — but "no wheel" was being read as "impossible", and it is not. NOTE: extract-msg reads only .msg files, NOT .pst archives — do not use it for PST |
| Chocolatey bootstrap in admin PowerShell | **winget** (built into Windows 10/11) | No third-party bootstrap, no execution-policy gymnastics |

---

## ARCHITECTURE

```
+-------------------------------------------------------------+
|                     PyQt6 Desktop Shell                     |
|  +---------------------+        +------------------------+  |
|  | Search UI / Graph   | <----> | Controller (signals)   |  |
|  | Office doc builder  |        | QThread worker pool    |  |
|  +---------------------+        +-----------+------------+  |
|                                             |               |
|  +------------------------------------------v------------+  |
|  |                   Embedded Engine (in-process)        |  |
|  |  * Metadata + keyword: SQLite WAL + FTS5 (one file)   |  |
|  |  * Vectors: LanceDB (one folder, ANN index)           |  |
|  |  * Cache: diskcache (one folder)                      |  |
|  |  * Parsing: PyMuPDF, python-docx, openpyxl,           |  |
|  |             python-pptx, win32com (PST via Outlook)   |  |
|  |  * Embeddings: FastEmbed ONNX (bge-small-en-v1.5)     |  |
|  |  * Rerank: FastEmbed TextCrossEncoder (optional)       |  |
|  +-------------------------------------------------------+  |
|                                                             |
|  Optional external: Ollama (RAG answers, entity extraction) |
|  — app degrades gracefully if absent                        |
+-------------------------------------------------------------+
```

**Search pipeline:** FTS5 BM25 + LanceDB ANN in parallel → reciprocal rank fusion → (optional) cross-encoder rerank of top 30 → results. No LLM in the search hot path.

---

# ⚡ QUICK START — INSTALLATION (Windows only)

Because the stack is embedded, installation is: **Python → project → pip install → (optional) Ollama.**
No services, no ports, no database passwords.

The installer asks exactly ONE question — *where to build the index* (usually a different
drive from the app). Everything else runs unattended and `doctor.py` runs automatically at the end.

## Step 1: Run the installer

Use the wrapper - it is the reliable entry point:

```
cd D:\SearchProject
run-install.cmd
```

`run-install.cmd` bypasses the execution policy for one process (so a Restricted or
AllSigned machine policy cannot block it), parse-checks the scripts with the real
PowerShell parser before running them, and keeps the window open so nothing scrolls away.
Arguments pass straight through: `run-install.cmd -Preflight`.

Running `.\install.ps1` directly from a normal PowerShell window also works, provided your
execution policy allows it.

The script installs into **its own folder**, not your current directory. This matters: an earlier
run launched from `C:\Windows\system32` created the project there and then could not find
`requirements.txt` or `doctor.py`. `install.ps1` now resolves everything from `$PSScriptRoot`.

**Parameters**

| Parameter | Effect |
|---|---|
| `-DataPath "E:\KnowledgeGraphData"` | Skips the one question. |
| `-ProjectPath "D:\SearchProject"` | Overrides the install location (default: the script's folder). |
| `-OnError Ask` | **Default.** On failure, prompts **[R]etry / [C]ontinue / [A]bort**. |
| `-OnError Continue` | Unattended: log the failure and carry on. |
| `-OnError Abort` | Unattended: stop at the first failure. |
| `-SkipOptional` | Skip the rerank model, Ollama and mistral. |
| `-RequiredFreeGB 150` | Free-space threshold on the index drive. |
| `-Preflight` | Run only the cheap checks and stop. Nothing is installed or downloaded. |

Zero-touch example:

```powershell
.\install.ps1 -DataPath "E:\KnowledgeGraphData" -OnError Continue -SkipOptional
```

**Check before you commit to the downloads.** Everything up to the disk check is local and
takes seconds; everything after it installs software or pulls gigabytes. `-Preflight` stops
at exactly that boundary and reports what it found:

```powershell
.\install.ps1 -Preflight
```

**Every run is logged.** A transcript is written to `logs\install-<timestamp>.log` before
anything can fail, so a failed run always leaves evidence — no copying console output, and
no losing it when the window scrolls. The log path is printed on exit.

## Error handling in the installer

Every step reports **ERROR** (what happened) and **FIX** (the exact command that resolves it),
then — under the default `-OnError Ask` — asks what to do:

```
==> Install Git
    ERROR: winget exited with code -1978335189
    FIX:   Run manually: winget install --id Git.Git -e
    This component is REQUIRED — the app will not start without it.
    [R]etry after applying the fix / [C]ontinue anyway / [A]bort  (Enter = A)
```

The default answer is **Abort** for required steps and **Continue** for optional ones.
Choosing Retry re-runs that step alone, so you can fix a problem in another window and
carry on without restarting the whole install.

### Failures the installer now handles correctly

These all came out of the first real run and are fixed rather than papered over:

| Symptom | Cause | Fix applied |
|---|---|---|
| `Install Git → ERROR: winget exited with code -1978335189` | winget returns `UPDATE_NOT_APPLICABLE` when a package is already installed and current. Not a failure. | Every install step has a `-Verify` block. If the command works afterwards, the exit code is ignored. |
| `Could not open requirements file` | The script `Set-Location`'d into a new subfolder that had no `requirements.txt`. | The project folder is the script's own folder, and both `requirements.txt` and `doctor.py` are checked for in preflight before anything is installed. |
| `Download embedding model → Traceback` | Cascade: pip never ran, so `fastembed` was not importable. | Package install is a required step; aborting there by default stops the pointless cascade. |
| `ollama pull` stalls on `pulling manifest` | A freshly installed Ollama has no service listening yet. | The step waits up to 60s for `127.0.0.1:11434`, starting `ollama serve` if needed, before pulling. |
| `can't open file 'C:\Windows\system32\local-knowledge-graph\doctor.py'` | Same cwd bug, at the verification step. | Absolute paths throughout; `Push-Location`/`Pop-Location` around anything that must change directory. |
| **Script produced no output at all** - no log, no venv, no `.env` | `install.ps1` was UTF-8 **without a BOM**. Windows PowerShell 5.1 decodes a BOM-less file with the ANSI codepage, so each em dash (`E2 80 94`) had its third byte read as cp1252 `0x94` = U+201D - a smart quote PowerShell treats as a string delimiter. Parse error before line 1 executed. | Every `.ps1` is ASCII-only **and** saved UTF-8 with a BOM. `scripts\parse-check.ps1` audits both, and `run-install.cmd` runs it before the installer. |
| Braille spinners rendered as `â ‹` | Console not in UTF-8. | `[Console]::OutputEncoding` set to UTF-8 at the top. |

Design rules the script follows (and the app must follow too):

- `$ErrorActionPreference = "Stop"` — nothing fails silently.
- Native commands are checked via `$LASTEXITCODE`; they do not throw on their own.
- Every step is idempotent — safe to re-run after fixing a failure.
- Every failure prints **ERROR**, **FIX**, and asks what to do.
- `.env` is written UTF-8 **without a BOM** — PS 5.1's `Set-Content -Encoding UTF8` adds one, and it breaks some `.env` parsers.
- The script never claims success unless every step actually succeeded *and* `doctor.py` reports READY.

## Step 2: Verify

`doctor.py` runs automatically at the end of the installer. To re-check at any time:

```powershell
cd D:\SearchProject
venv\Scripts\python.exe doctor.py
venv\Scripts\python.exe doctor.py --quick    # skip model loading
venv\Scripts\python.exe doctor.py --json     # machine-readable, for the Settings panel
```

Exit code is `0` for READY, `1` for NOT READY. **Optional components can fail without
blocking readiness** — only hard requirements gate the app. `doctor.py` distinguishes
`PASS` / `WARN` (optional failure) / `FAIL` (required failure).

What it checks: Python version and venv, Windows platform, `.env` and `DATA_PATH`, every
required import, `pywin32` (optional), SQLite FTS5 create+match, SQLite WAL, each index
subfolder is writable, free space **on the index drive**, a LanceDB write + vector search
round-trip, embedding model load + dimension, rerank model (optional), Outlook MAPI
(optional), and Ollama reachability plus whether the configured model is actually pulled
(optional).

Models downloaded by the installer (all cached under `<DataPath>\models` — offline after first install):

| Model | Size | Role | Required? |
|---|---|---|---|
| BAAI/bge-small-en-v1.5 | ~130MB | Embeddings (in-process ONNX, 384-dim) | Yes |
| BAAI/bge-reranker-base | ~1.1GB | Result reranking (Settings toggle) | Optional |
| mistral (via Ollama) | ~4.1GB | AI answers / entity extraction | Optional |

FastEmbed caches models via the `FASTEMBED_CACHE_PATH` environment variable **and** the
`cache_dir=` constructor argument. The installer and `doctor.py` set both, so the cache
never silently lands in the user profile instead of the index drive.

---

## requirements.txt

The real file lives beside this document. Every pin was re-verified against PyPI on
**2026-08-24** and each one publishes a `cp312 win_amd64` (or `py3-none-any`) wheel —
**no C++ build tools required**.

The V1/V2 draft pins were badly stale; several were more than a year behind and one
(`fastembed==0.4.2`) predates the current `TextCrossEncoder` rerank API. Corrected:

| Package | Draft pin | Verified pin |
|---|---|---|
| PyQt6 | 6.7.1 | **6.11.0** |
| lancedb | 0.15.0 | **0.37.1** |
| fastembed | 0.4.2 | **0.8.0** |
| pymupdf | 1.24.10 | **1.28.2** |
| python-docx | 1.1.2 | **1.2.0** |
| pywin32 | 306 | **312** |
| networkx | 3.3 | **3.6.1** |
| pydantic | 2.9.2 | **2.13.4** |
| python-dotenv | 1.0.1 | **1.2.3** |
| loguru | 0.7.2 | **0.7.3** |
| tqdm | 4.66.5 | **4.70.0** |
| requests | 2.32.3 | **2.34.2** |
| openpyxl, python-pptx, diskcache, pyvis | — | unchanged (already current) |

Verified by round-trip, not just by version number: `lancedb==0.37.1` was tested with a
384-dim table create → `search().limit(1).to_list()`, and `fastembed==0.8.0` was confirmed
to expose `fastembed.rerank.cross_encoder.TextCrossEncoder` with `cache_dir` support and
`BAAI/bge-reranker-base` in its model registry.

Re-verify before a build:

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt --dry-run
```

---

## doctor.py — verification WITH remediation

Replaces V1's `verify_setup.py`. The real file lives beside this document; see **Step 2**
above for what it checks and how to run it.

The principle, which the app must inherit: **every check that fails says what failed, why,
and how to fix it** — and optional components (rerank, Outlook, Ollama) can fail without
blocking readiness. Only hard requirements gate the app.

`doctor.py` deliberately uses **stdlib only at import time** and parses `.env` by hand, so
it still runs and gives useful output *before* `pip install` has succeeded — which is
exactly when you most need it.

One inconsistency in the draft is resolved here: the installer demanded 150GB free while
`doctor.py` checked for 250GB, and it checked the **current directory** rather than the
index drive. Both now check the drive holding `DATA_PATH` against the same
`150GB` threshold (overridable via `-RequiredFreeGB`).

---

## ERROR HANDLING CONTRACT (applies to the whole app)

Every error surfaced anywhere — installer, doctor, indexing pipeline, search, UI — must carry a structured payload, never a bare stack trace:

```python
from pydantic import BaseModel
from typing import Optional
from enum import Enum

class ActionType(str, Enum):
    AUTO_FIX = "AUTO_FIX"        # app fixes it itself, informs user
    USER_RETRY = "USER_RETRY"    # user does something, then retries
    RUN_COMMAND = "RUN_COMMAND"  # exact command shown, copy-paste
    SKIP_CONTINUE = "SKIP_CONTINUE"  # item skipped, batch continues

class AppError(BaseModel):
    code: str            # e.g. ERR_FILE_CORRUPT
    component: str       # e.g. indexer.pst
    message: str         # what happened, plain English
    details: Optional[str] = None   # technical detail, collapsed in UI
    suggestion: str      # what the user should do
    action_type: ActionType
    action_payload: Optional[str] = None  # command / settings deep-link / button id
```

### Required recovery mappings (minimum set)

| Code | Trigger | Message + Suggestion | Action |
|---|---|---|---|
| `ERR_MODEL_LOAD` | Embedding model missing/corrupt | "Embedding model failed to load. First run needs internet once (~130MB); afterwards fully offline." | `USER_RETRY` + retry button |
| `ERR_OLLAMA_DOWN` | AI-answer requested, Ollama unreachable | "Ollama is not running — search still works. Start Ollama to enable AI answers." | `AUTO_FIX`: fall back to non-LLM results; offer "Start Ollama" button |
| `ERR_FILE_CORRUPT` | Unreadable PST/Office/PDF | "Cannot read 'archive_2024.pst' (encrypted or damaged). Run scanpst.exe to repair, or skip." | `SKIP_CONTINUE`: mark `SKIPPED_CORRUPTED` in DB, batch never halts |
| `ERR_FILE_LOCKED` | File open in another app | "File is locked by another program. Close it or it will be retried on the next incremental pass." | `SKIP_CONTINUE` + auto-retry next sync |
| `ERR_DISK_SPACE` | <5GB free mid-index | "Indexing paused to prevent data loss. Free space on C: or move the data path in Settings." | `USER_RETRY`: pause queue, preserve cursor, interactive resume |
| `ERR_DB_LOCKED` | SQLite/LanceDB lock (second app instance) | "Another copy of the app is running. Close it and retry." | `USER_RETRY` |
| `ERR_OUTLOOK_MISSING` | PST indexing without Outlook | "PST indexing needs Outlook (or convert with XstReader). Other file types index normally." | `RUN_COMMAND`: link to XstReader instructions |
| `ERR_ENCODING` | Undecodable text file | "Could not decode 'notes.txt'. Indexed with replacement characters; original untouched." | `AUTO_FIX` + log |

**Pipeline rule: a single bad file must never halt a 100GB indexing run.** Log it, mark it, surface a "N files skipped — review" panel, continue.

---

## EMAIL STRATEGY (PST *and* the live mailbox)

The V1 framing of "PST indexing" was too narrow. `win32com` drives Outlook's MAPI layer, and
MAPI enumerates **every store Outlook currently has open** - which includes the live Exchange
or Microsoft 365 mailbox, not only `.pst` archives.

`Outlook.Application` -> `GetNamespace("MAPI")` -> `.Stores` returns each store; each store
exposes its folder tree; each folder exposes its items. A `.pst` file is simply one kind of
store. So the same code path indexes:

| Source | Works | Notes |
|---|---|---|
| `.pst` archive files | Yes | Attached in Outlook, or opened on demand |
| Live Exchange / M365 mailbox | Yes | Inbox, Sent, custom folders, the lot |
| Additional mailboxes | Yes | Any account configured in the Outlook profile |
| Shared / delegate mailboxes | Yes, if open in Outlook | Whatever the profile can already see |
| Public folders | Usually | Depends on the Exchange configuration |
| Live mail with Outlook closed | **No** | MAPI needs Outlook running |

Three constraints that shape the design:

1. **Cached Exchange Mode governs what is local.** Outlook keeps a local `.ost` of the
   mailbox, but by default only a sliding window - often 12 months. Older mail is fetched from
   the server on access. Indexing it is possible but turns a local operation into thousands of
   network round trips. The UI must show the cached window and let the user decide, rather
   than silently pulling years of mail over the network.
2. **Outlook must stay open** during an email index run. If it closes mid-run the store
   handles die; that surfaces as `ERR_OUTLOOK_BUSY` (`SKIP_CONTINUE`) and the remaining
   folders are retried on the next pass rather than failing the run.
3. **Read-only, always.** The indexer opens items and reads properties. It never marks as
   read, never moves, never deletes.

Ordering: **archives first, live mailbox second.** A `.pst` is static and entirely local, so
it indexes fast and predictably. The live mailbox is the part that can involve the network.

Fallbacks, unchanged:

- **No Outlook installed:** XstReader or `readpst` to convert `.pst` -> EML, then index the
  EML folder with the stdlib `email` module.
- **Optional:** `libpff-python` — reads `.pst` with no Outlook, no COM and no file lock, and is
  the only PST route testable off Windows. Not pinned: it compiles at install. See
  `app/extract/pst_libpff.py`.
- **Never:** `extract-msg` for `.pst` (it reads `.msg`
  files only - fine for standalone messages, wrong for archives).

Email features retained: thread grouping, conversation view, sender and date filters,
attachment indexing through the normal extractor registry.

---

## ONEDRIVE AND SHAREPOINT

Both are supported, with one significant caveat that shapes the walker.

**How.** The OneDrive sync client presents OneDrive and any synced SharePoint document
library as ordinary local folders (`C:\Users\<you>\OneDrive - <Org>\...`). Add that path as
an index root and it works like any other folder - no API, no tokens, no cloud calls, and the
"fully local" promise is intact.

**The caveat: Files On-Demand.** By default the sync client stores most files as
*placeholders*. The name, size and modified date are on disk; the contents are not. **Reading
a placeholder downloads the entire file.** Pointing an indexer at a 500GB SharePoint library
with this enabled would quietly hydrate the whole library: filling the disk, saturating the
connection, and running for days.

So placeholders are detected and skipped by default. `app/core/winfs.py` checks three
attributes:

| Attribute | Value | Meaning |
|---|---|---|
| `FILE_ATTRIBUTE_OFFLINE` | `0x00001000` | Content is not local |
| `FILE_ATTRIBUTE_RECALL_ON_OPEN` | `0x00040000` | Opening triggers a fetch |
| `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` | `0x00400000` | Reading triggers a fetch |

Any of them set means skip, with `ERR_CLOUD_ONLY` (`SKIP_CONTINUE`) recording the reason so
the review panel can report "3,412 files skipped: stored online only" and offer the fix.
A file pinned with **Always keep on this device** has none of these bits and indexes normally.

The Settings toggle *Index cloud-only files* opts in, with a blunt warning about disk and
bandwidth. Default off: silently downloading half a terabyte is not a reasonable default.

**What is deliberately not built:** talking to SharePoint or Graph over the network. That
would mean OAuth, tokens, tenant permissions and an app registration - and it would break the
core promise that this thing works entirely offline. Sync the library locally and point the
indexer at it.

---

## SCALE & PERFORMANCE (honest numbers)

| Metric | V1 claim | V2 target | Basis |
|---|---|---|---|
| Warm search | "<500ms cached" | **<300ms** | FTS5 BM25 + LanceDB ANN in parallel, RRF fusion, no LLM in hot path |
| First search after launch | "<2s cold" | **<3s** | ONNX model load ~1–2s once per session |
| Rerank | Mistral 7B (impossible <2s on CPU) | Cross-encoder top-30, ~100–300ms, **optional toggle** | bge-reranker-base ONNX |
| Initial index, 100GB | "30–50h GPU / 80–150h CPU" | **Days on CPU — set expectations in UI**; prioritised folders first, background, resumable | bge-small ~384-dim helps; PyMuPDF removes the parse bottleneck |
| RAM | 16–32GB | **8GB min, 16GB comfortable** | No Postgres/Redis/Qdrant resident |
| Disk | 200GB free | **150GB free on the index drive**, excluding the corpus itself | vectors + FTS + cache + models ≈ 50% of corpus size. The installer and `doctor.py` now use this same figure, measured on the `DATA_PATH` drive. |
| Services to manage | 5 | **0** (1 optional: Ollama) | |

Indexing must be: background QThread pool, resumable (cursor persisted), incremental (mtime+hash), prioritisable (user picks "index this folder first").

---

## CORE REQUIREMENTS (carried over, amended)

- [x] Semantic + keyword hybrid search over 100GB local data
- [x] Fully local & private — embeddings in-process; **no cloud, no network required after first model download**
- [x] PST email search via Outlook MAPI (thread grouping, filters)
- [x] Drag-drop instant indexing
- [x] Create/edit Office documents (DOCX, XLSX, PPTX) from results
- [x] Knowledge graph with entity extraction (via optional Ollama; degrade to co-occurrence graph without it)
- [x] Configurable index roots, file-type filters, exclusions
- [x] Click-to-open file/folder from results; missing-file detection
- [x] Keyboard shortcuts; dark mode; CSV/JSON export
- [x] **Every error states the error AND suggests the solution** (AppError contract above)
- [x] Single-instance app; graceful shutdown persists index cursor

---

## NEXT STEPS

1. `cd D:\SearchProject` and run `.\install.ps1`
2. Run `venv\Scripts\python.exe doctor.py` until it prints **READY**
3. Read `BUILD_SPEC_V2.md` — it defines Layers 0–9 with acceptance tests
4. Open a new chat, paste **both** this document and `BUILD_SPEC_V2.md`
5. Say: **"Environment verified by doctor.py. Start Layer 0."**

The layer-by-layer plan has been regenerated against the V2 embedded architecture and
lives in `BUILD_SPEC_V2.md`. The V1 layers assumed FastAPI/Postgres/Qdrant and are void.
