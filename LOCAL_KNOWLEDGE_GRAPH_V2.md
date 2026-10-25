# Leasha — architecture and installation

**Doc version:** 2.17 · **Updated:** 2026-10-25 · **Applies to:** app v1.1.0

> **Renamed.** This document was `LOCAL_KNOWLEDGE_GRAPH_V2.md`, and the application was
> "Local Knowledge Graph Search + Office Suite". Neither name fits any more: the knowledge
> graph was removed and the Office builder cancelled, because neither was what the tool is
> for. See `HANDOFF.md` §3a for the decision and what would reverse it.
>
> The filename is kept for now so existing links and the installer do not break. It is a
> Layer 9 job to rename it once nothing points at it.

**Project Type:** Windows desktop app, embedded single-process architecture
**Target OS:** Windows 11 and Windows 10 22H2, single user. The code also runs on macOS (`docs/MAC_VERIFICATION.md`), so far checked only on GitHub's Mac runners; Linux is not a supported target
**Tech Stack:** PySide6 (Qt 6.11) + SQLite/FTS5 + LanceDB + FastEmbed and ONNX Runtime + PyMuPDF — Ollama optional
**Data Scale:** 100GB
**Search Target:** <300ms warm search, <3s first search after launch (model load)
**Status:** app v1.0.0. Run from source with `install.ps1`, or install a packaged copy with the Windows installer built from `packaging\` (`BUILD_SPEC_V2.md`, Layer 9)

**Files in this folder:**

| File | Purpose |
|---|---|
| `run-install.cmd` | **Start here.** Parse-checks, bypasses execution policy, then runs the installer. |
| `install.ps1` | The installer itself. |
| `scripts\parse-check.ps1` | Real PowerShell parse + encoding audit; logs to `logs\parse-check.log`. |
| `requirements.txt` | Pinned dependencies, all verified to ship Windows wheels. |
| `doctor.py` | Environment verification with a fix for every failure. |
| `packaging\` | The Windows installer: PyInstaller build and Inno Setup script (`BUILD_SPEC_V2.md`, Layer 9). |
| `BUILD_SPEC_V2.md` | Layer-by-layer build plan (L0–L9) with acceptance tests. |
| `LOCAL_KNOWLEDGE_GRAPH_V2.md` | This document — architecture and contracts. |

---

## WHAT CHANGED FROM V1 AND WHY

V1 required six cooperating processes (PostgreSQL, Redis, Qdrant, Ollama, FastAPI, PyQt6). For a single-user desktop app that is five points of failure before one search runs. V2 is **one process + one optional helper (Ollama)**. Indexing, the readers and folder watching can each be moved into child processes of that one application (all off by default); there are still no services and no ports.

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
|                    PySide6 Desktop Shell                    |
|  +---------------------+        +------------------------+  |
|  | Search, Chat tabs   | <----> | Controller (signals)   |  |
|  | Indexing, Settings  |        | QThread worker pool    |  |
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
|  |  * Rerank: FastEmbed TextCrossEncoder (optional)      |  |
|  |  * Local models: ONNX Runtime (chat, photo tags,      |  |
|  |    speech) - app/ort                                  |  |
|  +-------------------------------------------------------+  |
|                                                             |
|  Optional external: Ollama (Chat, Interpret, Describe)      |
|  — app degrades gracefully if absent                        |
+-------------------------------------------------------------+
```

**Search pipeline:** FTS5 BM25 + LanceDB ANN in parallel → reciprocal rank fusion → (optional) cross-encoder rerank of top 30 → results. No LLM in the search hot path.

### Around the picture (order 0x and after) - read this if you are new here

Three things were added around the picture above. None of them changes the one-process,
no-services rule for what the person uses: the window and search are still one process with no
ports and no server.

**1. The indexer can run as a second process** (`app/index/child_run.py`, `app/index/run_events.py`).
Python lets only one thread run Python code at a time (the "global interpreter lock"), so an index
running on threads inside the window's process takes turns with the window, however carefully it is
threaded. With the setting *Index in a separate process* on, the window starts
`python -m app.cli index --events jsonl` as a child process instead:

```
Window process                                   Child process (app.cli index)
  IndexingView  <- stats rebuilt from JSON  <---  stdout: one JSON line per event
  ChildIndexRun (reads lines on the page's        (progress, heartbeat, finished)
                 existing index thread)     --->  stdin: pause / resume / stop
```

Each line is plain JSON, so the page reads the child's progress exactly as it reads an in-process
run. If the child dies, the page says so and names the file it was reading; the child takes the run
lock itself, so an interrupted run carries on next time; and when the window goes, the child's
stdin closes, which it treats as Stop. The setting is **off by default** until it has been measured
on a real index (order 0x item 2d).

**2. Progress inside one file** (`app/extract/progress.py`, `app/index/live_progress.py`,
`app/ui/presenter/live_progress.py`). A mail archive or a zip is one file with thousands of things in
it. Each reader keeps a small per-thread "frame" saying where it is - `Archive2019.pst › Inbox ›
message 4,512 of 18,300` - and updates it with one assignment per message (no locking, no text
formatting). The pipeline copies those frames into `IndexStats.workers` about once a second, and the
presenter turns them into the sentences on the Indexing page: a headline, one line per reader, and a
heartbeat ("last activity 2 s ago").

**3. One home for platform code** (`app/core/osbridge/`). Everything that differs between Windows and
macOS - opening a file, showing it in Explorer or Finder, lowering priority, finding installed
programs, the default data folder, cloud placeholders, path separators and letter case - lives here,
with a Windows version and a macOS/POSIX version of each. Windows is platform one: its versions are the
code that was already there, moved unchanged. The load-bearing test
`test_no_windows_only_call_outside_osbridge` fails if a Windows-only call appears anywhere else.

Also worth knowing: every SQLite connection is now a `_GuardedConnection` that counts calls in
flight, so closing the store never closes a connection another thread is still reading from (that
was a native crash).

### Accounts, the contact hub and live mail (order 1k)

A fourth addition, and the one place where the picture above is not the whole story: Leasha can
connect to the person's own Google, Microsoft, Apple, IMAP and CardDAV accounts. Everything it
fetches is stored and searched on this machine only; search, indexing and reading files stay
offline exactly as before. Accounts are off until a person adds one, and nothing syncs until the
schedule or *Sync now* says so.

```
Settings > Accounts, Contacts page          app/ui/accounts, app/ui/contacts (Qt; every read and write on a worker)
        |  AccountScheduler (app/ui/account_scheduler.py): a 30 s timer that only asks the policy
        v
AccountService (app/accounts/service.py)    the verbs: add, test, reconnect, remove, sync now, due work, undo
   |-- policy.py        when a sync is due: pure functions of timestamps (manual / every N minutes / daily)
   |-- ContactSyncEngine (engine.py)  --->  ContactProvider: google_people, graph_contacts, carddav
   |-- MailSyncer (mail_sync.py)      --->  MailProvider: gmail_api, graph_mail, imap_mail
   |-- SecretStore (secrets.py)             Windows Credential Manager, via keyring; nowhere else
   |-- http.py                              the only code that opens a socket to an account:
   |                                        an allow-list of hosts, TLS on, redirects not followed,
   v                                        Retry-After honoured
ContactHub (app/contacts/hub.py)            the master copy of the contact book, in SQLite
```

**The account worker.** There is one worker for all accounts (rule 5: not the UI thread), resumable
(rule 4) and one failure domain per account: an account that fails is recorded with its error code
and the rest carry on. A sync starts only on the schedule or on *Sync now* (opening Leasha, waking
from sleep and the network returning start nothing). A due time that fell while the machine slept is
served once, never once per missed time; after Leasha was closed, an account whose last sync is older
than its interval syncs once, a minute after the window opens. A second *Sync now* while one runs is
folded into it, not queued. Contacts go before mail when both are due, and a first mail fetch gives
way between pages to a due contact sync. A failure backs off 1, 2, 4, 8 ... minutes up to the
schedule's own interval, and the provider's `Retry-After` always wins.

**The contact hub.** SQLite is the authority (rule 6), and the hub is the master copy: the accounts
hold copies that are kept in step with it. A contact is a row in `contacts` with typed, repeatable
rows in `contact_fields`, a `contact_links` row for each account it is **Kept in** (the account's
own id, its version tag, and a snapshot of what the two last agreed on - the base of a three-way
merge), groups, tags, smart groups, and `contact_journal`, which holds a whole-contact snapshot
before and after every change, whoever made it, so any change, a merge, a clean-up or a whole sync
can be undone. `contact_sources` is the receipt behind each field and `contacts_fts` makes a
contact's words searchable. A search that names a contact exactly (or by the start of its name, an email
address or a phone number) shows that contact's card above the results
(`app/ui/widgets/person_card.py`; it reads on a worker and never touches the search), and the editor's
*From your index* panel and the MCP tool `contact_activity` read the same facts
(`app/contacts/activity.py`). A contact made in Leasha goes to the default account; nothing is copied
to every account unless the person asks. Fields an account cannot hold are never removed from Leasha
by a round trip: each account is written only what it can hold (`Capabilities`, `project()`).

**The sync engine, in two phases.** One call syncs every chosen account:

1. **Pull**, account by account. Each page of changes is applied and its change token (Google
   `syncToken`, Graph delta link, CardDAV `sync-token` or etag set) is saved in one transaction, so
   a crash costs at most the page in flight. A change is merged into the hub field by field,
   three-way against the snapshot: a change on one side only wins; when both changed, the newest
   edit wins (or the account order decides, or the contact waits, by the person's rule), the loser
   stays in the journal, and the contact carries a *changed in two places* marker. A new contact is
   never auto-merged with one already here; a lookalike is only counted for the Duplicates screen. A
   contact an account has deleted is only flagged *Deleted elsewhere*.
2. **Push**, account by account, only to accounts whose direction is Two-way. The hub's changes
   go to every account in the contact's Kept-in set. An update carries the version tag it was based
   on; if the account refuses it as stale the engine re-reads, re-merges and retries (three times),
   and never overwrites blind. Pulling from every account before pushing to any is what lets a change
   made in one account reach the others in the one sync.

The **first sync of an account is a dry run**: it reads and plans and writes nothing - not the hub,
not the account, not the saved token - until the person approves. **The delete brake** counts the
deletes a push would make; more than the limit (20 contacts or 5% of the book by default) stops the
push with `ERR_SYNC_DELETE_BRAKE` and deletes nothing until the person allows it. Every run is a row in
`sync_runs` (counts, trigger, error, and the plan of a dry run), and **undoing a sync** puts the
hub back and pushes the old values to the accounts as well (`AccountService.undo_run`).

**The live mail path.** `MailSyncer` builds the account's `MailProvider` behind a wrapper
(`_ReadOnly`) that has `pull`, `list_folders` and `close` and nothing else, so no code on this path
can move, flag, mark read or delete anything in a mailbox (rule 10 stands for mail). Gmail uses the
Gmail API's history, Microsoft uses Graph delta per folder, and iCloud and any other server use IMAP
(`UIDVALIDITY`, `CONDSTORE` where offered; a Gmail account may be read over IMAP with an app
password instead). A page's messages and its resume token are written in one transaction
(`MailIndexer.apply_page`). A fetched message is an ordinary `files` row whose path is
`account://<account id>/<folder>/<remote id>`, with an ordinary `messages` row (migration 38 adds
`account_id`, `remote_id`, `labels` and `message_id`), passed through the same chunker and store
calls as every other mail, so search, the Mail page, previews and attachments need no second code
path. Words are searchable at once and meaning follows (`PARTIAL` rows, then embeddings), and a
sync never fails for want of a model. Mail deleted or moved on the server is followed in the index.
A message that a connected account and an Outlook archive both hold is one message, matched by its
`Message-ID`: the live copy is the one searched, and the archive copy is taken out of the index and
recorded in `message_places` so it can still be found in Outlook. Spam and Trash are left out by
default; how far back, which folders and whether attachments are indexed are the account's own
options (`mail_sync.MailOptions`).

**Error codes.** Every failure on this path is an `AppError` (the contract below). The block in
`app/core/errors.py` is: `ERR_ACCOUNT_SIGNIN_EXPIRED`, `ERR_ACCOUNT_ADMIN_CONSENT`,
`ERR_ACCOUNT_NOT_CONFIGURED`, `ERR_ACCOUNT_UNREACHABLE`, `ERR_ACCOUNT_SECRET_STORE`,
`ERR_ACCOUNT_REJECTED`, `ERR_PROVIDER_LIMIT`, `ERR_SYNC_CONFLICT_HELD`, `ERR_SYNC_DELETE_BRAKE`,
`ERR_SYNC_NEEDS_APPROVAL` and `ERR_SYNC_FAILED`. They are listed with their messages in the table
under the error contract.

Built since: folder watching (`app.cli watch`, a child process started from the Indexing page,
off by default); the Indexing page *What gets read*, with `MAIL_ATTACHMENTS` deciding what is read
from an email attachment; the Chat tab (`app/chat`); local models on ONNX Runtime
(`app/ort`); and accounts, the contact hub and live mail (below). The schema is at
`CURRENT_VERSION = 38`. `docs/TECHNICAL_REFERENCE.html` describes
the architecture as it stands; where it and this document disagree on an authority question, this
document wins and should be corrected.

---

# ⚡ QUICK START — INSTALLATION (Windows only)

Because the stack is embedded, installation is: **Python → project → pip install → (optional) Ollama.**
No services, no ports, no database passwords.

This is installation from source. A packaged copy installs with `Leasha-Setup-<version>.exe`
instead, which needs no Python; `BUILD_SPEC_V2.md` Layer 9 describes it.

The installer asks exactly ONE question — *where to build the index* (usually a different
drive from the app). Everything else runs unattended and `doctor.py` runs automatically at the end.

## Step 1: Run the installer

Use the wrapper - it is the reliable entry point:

```
cd D:\Local\GitHub\Leasha
run-install.cmd
```

`D:\Local\GitHub\Leasha` is the owner's clone; use the folder yours is in.

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
| `-SkipOptional` | Skip the rerank model, Ollama and its `qwen2.5:1.5b`. |
| `-RequiredFreeGB 300` | Free-space threshold on the index drive. |
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
cd D:\Local\GitHub\Leasha
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

Models downloaded by the installers (all cached under `<DataPath>\models` — offline after
first install). Both installers, Settings › Models & AI and `leasha-cli models download`
fetch through one list, `app/core/model_catalogue.py`, so they cannot disagree about which
model is the one in force:

| Model | Size | Role | Required? |
|---|---|---|---|
| BAAI/bge-small-en-v1.5 | ~130MB | Embeddings (in-process ONNX, 384-dim) | Yes |
| Xenova/ms-marco-MiniLM-L-6-v2 | small | Result reranking (Settings toggle). The default since the 9.2x measurement; `BAAI/bge-reranker-base` scored the same and was slower | Optional |
| qwen2.5:1.5b (via Ollama, `OLLAMA_MODEL`) | ~1GB | Chat, Interpret and Describe, only when `CHAT_ENGINE=ollama` | Optional |

By default Chat, Interpret and Describe do not use Ollama: the chat model (Qwen 2.5 1.5B),
photo tags (Florence-2) and speech (Whisper) run on ONNX Runtime inside Leasha (`app/ort`,
`CHAT_ENGINE=onnx`). The chat model is downloaded from Settings › Models & AI. There is no
entity extraction.

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
| PySide6 | PyQt6 6.7.1 | **6.11.0** |
| lancedb | 0.15.0 | **0.37.1** |
| fastembed | 0.4.2 | **0.8.0** |
| pymupdf | 1.24.10 | **1.28.2** |
| python-docx | 1.1.2 | **1.2.0** |
| pywin32 | 306 | **312** |
| pydantic | 2.9.2 | **2.13.4** |
| python-dotenv | 1.0.1 | **1.2.3** |
| loguru | 0.7.2 | **0.7.3** |
| tqdm | 4.66.5 | **4.70.0** |
| requests | 2.32.3 | **2.34.2** |
| openpyxl, python-pptx, diskcache | — | unchanged (already current) |

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

Free space is checked on the drive holding `DATA_PATH` - never the current directory - against
one figure, **300GB** (`REQUIRED_FREE_GB`): Leasha's default, `install.ps1 -RequiredFreeGB`,
`doctor.py` and the Windows installer all use it, and Settings can change it.

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

Accounts and sync (order 1k) add the codes below. None halts anything but the account it names, and
each says what to do next:

| Code | Trigger | Suggestion (as shown) | Action |
|---|---|---|---|
| `ERR_ACCOUNT_SIGNIN_EXPIRED` | A refresh token expired or was revoked, or an app password was revoked | "Open Settings > Accounts, choose the account and press Reconnect. Nothing already in Leasha is lost." | `USER_RETRY` |
| `ERR_ACCOUNT_ADMIN_CONSENT` | A work account whose administrator has not approved the app | "Ask your IT administrator to approve the app registration you made (or to allow users to consent to apps) for the permissions Mail.Read and Contacts.ReadWrite, then press Reconnect." | `USER_RETRY` |
| `ERR_ACCOUNT_NOT_CONFIGURED` | Google or Microsoft chosen with no client ID pasted | "Open Settings > Accounts and follow the step-by-step guide to make your own, then paste its client ID there." | `USER_RETRY` |
| `ERR_ACCOUNT_UNREACHABLE` | No answer, a wrong server, or a host that is not on the allow-list | "Check the internet connection and the server details, then press Sync now." | `USER_RETRY` |
| `ERR_ACCOUNT_SECRET_STORE` | Windows Credential Manager could not keep or read a secret | "Leasha never keeps a password or token anywhere else. Unlock or repair Credential Manager and try again." | `USER_RETRY` |
| `ERR_ACCOUNT_REJECTED` | The server answered and refused | "Press Test connection in the account's panel; if it keeps happening, send the log file." | `USER_RETRY` |
| `ERR_PROVIDER_LIMIT` | HTTP 429 or 503 | "Nothing to do - Leasha waits as long as the provider asks and tries again." | `AUTO_FIX` |
| `ERR_SYNC_CONFLICT_HELD` | Contacts changed in two places | "Open Contacts > Changed in two places and choose which value to keep." | `USER_RETRY` |
| `ERR_SYNC_DELETE_BRAKE` | A push would delete more than the safety limit | "Check the list in the sync log. If it is right, press Allow in the sync log and sync again; if not, press Undo." | `USER_RETRY` |
| `ERR_SYNC_NEEDS_APPROVAL` | The first sync of an account (a dry run) | "Read what it would add, merge and change, then press Approve to run it for real." | `USER_RETRY` |
| `ERR_SYNC_FAILED` | Anything else that stopped one account's sync | "Press Sync now to try again; the other accounts were not affected." | `USER_RETRY` |

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

**Mail that does not come through Outlook.** A connected Google, Microsoft, Apple or other IMAP
account is a second, independent route in, with no Outlook and no MAPI: see *The live mail path*
under *Accounts, the contact hub and live mail*. It is read only in the same sense as the rest of
this section, and a message that both a connected account and an Outlook archive hold is indexed once,
the live copy winning.

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

**What is deliberately not built:** reading OneDrive or SharePoint *files* over the network. A
Microsoft account (above) brings in the person's mail and contacts through Graph, with the person's
own app registration, and never files or libraries. Sync the library locally and point the indexer
at it.

---

## PRIVACY: WHAT MAY LEAVE THIS COMPUTER

Leasha is offline by design, and the rule is about where the data lives, not only where it came
from: **everything fetched is stored and searched on this machine only.** Search, indexing, reading
files and the index never open a connection to anywhere else, even with accounts connected
(`tests/unit/test_accounts_network_guard.py` pins that a search, a Chat turn with the web switch off
and a folder scan open no non-loopback connection with accounts connected; a full index run with the
models is the same path plus an embedder that opens no socket, not yet checked on Windows). Exactly three kinds of thing may leave, and each is off until the person sets it up:

1. **Sign-in and the provider calls that fetch mail and contacts.** Only to the providers' own hosts,
   and only for an account the person added. Google: `accounts.google.com`, `oauth2.googleapis.com`,
   `people.googleapis.com`, `gmail.googleapis.com`, `www.googleapis.com`. Microsoft:
   `login.microsoftonline.com`, `graph.microsoft.com`. Apple: `contacts.icloud.com` (and the numbered
   `pNN-contacts.icloud.com` host it moves a person to) and `imap.mail.me.com`. An IMAP or CardDAV
   account reaches exactly the one server the person typed in. The list is `PROVIDER_HOSTS` in
   `app/accounts/http.py`; `AllowedHosts` refuses any other host before a socket is opened, only
   `https` is allowed (a loopback test server excepted), redirects are never followed, and
   `tests/unit/test_accounts_network_guard.py` pins the list.
2. **Contact changes written back to the person's own accounts.** A contact made or changed in Leasha
   goes to the accounts in its Kept-in set whose direction is Two-way (and a contact moved to the
   Bin leaves them). Every outward write is journalled and can be undone. Mail is the opposite:
   nothing is ever written to a mailbox.
3. **Enrichment look-ups the person has switched on.** The five sources that read only the index
   (signatures, company from a work email's domain, a photo from named faces, contact history,
   suggested contacts) send nothing. The three internet sources are each a separate switch, **off by
   default**: Gravatar (a SHA-256 hash of the email address, never the address, to `www.gravatar.com`),
   a company's own icon (the company's own domain only, fetched from that domain), and Wikidata (a
   company name only, never a person, to `www.wikidata.org`). Their hosts are `ENRICHMENT_HOSTS` in
   `app/accounts/http.py`, kept apart so an account cannot reach them by accident. They run only on
   demand, never on a timer, and what they propose is kept only if the person accepts it.

Nothing else leaves: no telemetry, no index content, no passages, no mail or contacts to anyone but the
account they came from. The Chat tab's optional web lookup is separate and unchanged (a short
sanitised query, off by default). **Secrets** - refresh tokens, app passwords, the Google client
secret - are kept only in Windows Credential Manager (macOS Keychain), through `keyring`
(`app/accounts/secrets.py`): never in SQLite, `.env`, a log or a crash report, and a test greps the
log and the database after a sign-in. Leasha ships with no client ID of its own: each person makes
their own registrations and pastes the IDs into Settings > Accounts. The MCP server for AI programs
stays read-only (seven tools, none of which writes), bound to `127.0.0.1` with Leasha's key, and
never returns a token, a password or a client ID.

---

## SCALE & PERFORMANCE (honest numbers)

| Metric | V1 claim | V2 target | Basis |
|---|---|---|---|
| Warm search | "<500ms cached" | **<300ms** - not met | FTS5 BM25 + LanceDB ANN in parallel, RRF fusion, no LLM in hot path. Measured 2026-10-09 on the owner's index (216,149 files, 386,665 passages), no rerank: 177, 238, 664 and 976 ms warm, 1,959 ms for the first query of the session |
| First search after launch | "<2s cold" | **<3s** | ONNX model load ~1–2s once per session |
| Rerank | Mistral 7B (impossible <2s on CPU) | Cross-encoder top-30, **optional toggle, off by default** | ms-marco-MiniLM-L-6-v2 ONNX (bge-reranker-base measured 9.2x slower for the same scores). Measured 2026-10-08: 576 ms on the graphics card, 1,109 ms on the processor, 818 ms median; the list is drawn without it and re-ordered in place when it lands |
| Initial index, 100GB | "30–50h GPU / 80–150h CPU" | **Days on CPU — set expectations in UI**; prioritised folders first, background, resumable | bge-small ~384-dim helps; PyMuPDF removes the parse bottleneck |
| RAM | 16–32GB | **8GB min, 16GB comfortable** | No Postgres/Redis/Qdrant resident |
| Disk | 200GB free | **300GB free on the index drive** (the threshold Leasha, `install.ps1`, `doctor.py` and the Windows installer all check), excluding the corpus itself | vectors + FTS + cache + models ≈ 50% of corpus size; the threshold leaves room above that |
| Services to manage | 5 | **0** (1 optional: Ollama) | |

Indexing must be: background QThread pool, resumable (cursor persisted), incremental (mtime+hash), prioritisable (user picks "index this folder first").

---

## CORE REQUIREMENTS (carried over, amended)

- [x] Semantic + keyword hybrid search over 100GB local data
- [x] Fully local & private — embeddings in-process; **search and indexing need no network after the first model download.** Accounts and enrichment are the only things that may connect, each off until set up (*PRIVACY*, above)
- [x] PST email search via Outlook MAPI (thread grouping, filters)
- [x] Drag-drop instant indexing
- [ ] ~~Create/edit Office documents (DOCX, XLSX, PPTX) from results~~ — cancelled (Layer 7)
- [ ] ~~Knowledge graph with entity extraction (via optional Ollama; degrade to co-occurrence graph without it)~~ — removed (Layer 6)
- [x] Configurable index roots, file-type filters, exclusions
- [x] Click-to-open file/folder from results; missing-file detection
- [x] Keyboard shortcuts; dark mode; CSV/JSON export
- [x] **Every error states the error AND suggests the solution** (AppError contract above)
- [x] Single-instance app; graceful shutdown persists index cursor
- [x] Accounts (Google, Microsoft, Apple, IMAP, CardDAV): contacts kept in step both ways, with a dry run first, a delete brake and undo; mail fetched read only and searched like any other

---

## Where to start

1. Install it: the README's **Install** section (Windows installer, or from source on Windows,
   macOS or Linux), then `doctor.py` until it prints **READY**.
2. Read, in this order: `HANDOFF.md` (where things are), `docs/PROJECT_INSTRUCTIONS.md` (how
   to work), `docs/ORDER_REGISTER.md` (what is next) - the order `CLAUDE.md` gives.
3. `BUILD_SPEC_V2.md` defines each layer and its acceptance tests; this document is the
   architecture they sit in.
