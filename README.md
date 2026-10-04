# Leasha

**Doc version:** 3.1 · **Updated:** 2026-10-04 · **Applies to:** app v0.3.4

**Search everything on this machine — by describing it in plain English.**

One headline feature. Type what you remember about a document, and get the document:

```
the safety report Dave sent about Leeds before the audit
```

Keyword and meaning-based search run together over ~100GB of local files and Outlook
archives. Filters — sender, recipient, subject, date, file type, size, state — are typed with
`/` and offered as you type. Windows 10/11, single user, **fully local**.

**Two illustrated guides** cover everything below in more depth, with screenshots of every
page:

| Guide | For |
|---|---|
| [`docs/USER_GUIDE.html`](docs/USER_GUIDE.html) | Using Leasha: every page, button, filter, setting and shortcut |
| [`docs/TECHNICAL_REFERENCE.html`](docs/TECHNICAL_REFERENCE.html) | How it is built: layers, processes, data, indexing, search, chat, the complete command line |

Both are single self-contained files: open them in any browser, offline.

## What it does

| Page | What it is for |
|---|---|
| **Search** | One box over files, mail and code. Words and meaning together; `/` for filters; a preview pane; pinned working set; saved searches |
| **Files** | Find a file by any part of its name — `voice` finds `Invoice 2024.pdf` |
| **Mail** | Your mail as a table you can sort and filter, newest first |
| **Code** | Every repository under your folders, with git history search (`/history`, `/introduced`, `/branch` …) |
| **Chat** | Ask about your files and get an answer in plain words, with every source one click away |
| **Offline** | Catalogue a removable drive once; its files stay findable after you unplug it |
| **Reports** | Digital Inheritance, the Space Report (duplicates, only copies), and the Life Timeline |
| **Indexing** | Start, pause and watch a run; what gets read from files, mail, attachments, zips, pictures, video and code; schedule; tuning |
| **Settings** | Folders, file types, search behaviour, models, appearance, storage and maintenance |

Plus a search box that appears over any program (`Ctrl+Alt+L`), folder watching that indexes a
file seconds after it is saved (off by default), and `leasha://` links.

## How it stays private

**One process for search.** SQLite/FTS5 for metadata and keyword search, LanceDB for vectors,
FastEmbed ONNX for embeddings — all embedded libraries. No services, no ports, no passwords,
and nothing leaves the machine. Indexing can optionally run in a child process of its own,
still with no ports.

**Models run inside Leasha.** Photo tags (Florence-2), speech (Whisper) and the Chat model
(Qwen 2.5 by default) run on ONNX Runtime in the application, downloaded once from Settings ›
Models & AI and then used offline. Ollama is an optional alternative engine for Chat, Interpret
and Describe (`CHAT_ENGINE=ollama`), and **no model is ever in the retrieval path**: with every
model missing, search works exactly as it always does.

**One exception, off by default and yours to switch on:** the Chat tab can look things up on
the web to add to what your own files say. Your files always come first, only a short search
question you can see (never a file name, a passage or your mail) is sent, and search,
indexing and reading stay fully offline either way.

Leasha only ever **reads** your files. It never changes, moves or deletes one.

## Install

From the folder you cloned or unpacked Leasha into:

```
run-install.cmd
```

`run-install.cmd` bypasses the execution policy, parse-checks the scripts before running
them, and keeps the window open. Add `-Preflight` to check without installing anything.

One question: where to build the index. Then:

```powershell
venv\Scripts\python.exe doctor.py     # must print READY
```

The index defaults to `%LOCALAPPDATA%\Leasha`, which is private to your Windows
account. Any other location is accepted; re-running the installer on a machine that
already has one leaves it exactly where it is.

**Leasha and shared computers.** Everything Leasha indexes and everything you
search stays on this computer - nothing is ever sent anywhere. On a computer
with separate Windows accounts, each account gets its own private index: you
find your files, others find theirs, and Windows keeps them apart. On a
computer where people share one login, Leasha works like the rest of that login
- anyone using it can find anything it can read. If that matters in your home,
give each person their own Windows account before installing, or choose the
folders Leasha indexes so shared spaces stay shared and private ones stay out.
Leasha's index contains copies of text from your files — treat the index as being as
sensitive as the most sensitive thing you index.

Every installer parameter is in `docs/TECHNICAL_REFERENCE.html` (Install scripts) and
`LOCAL_KNOWLEDGE_GRAPH_V2.md`.

## Use

```powershell
.\leasha                    # open the window
.\leasha commands           # the filters you can type
.\leasha search boiler quote /from dave
.\leasha evaluate --builtin # does search actually work?
```

**The `.\` is not optional**, and it is not a typo. PowerShell deliberately does
not run commands from the current directory - a protection against a malicious
`ls.exe` dropped in a folder you happen to be standing in. To drop the `.\` and
run `leasha` from anywhere:

```powershell
.\add-to-path.ps1     # undo with -Remove; user PATH only, no admin rights
```

First run: Settings › What's indexed › **Add all four** (or **Add folder…**), then Indexing ›
**Start indexing** (`F5`). Words are searchable first; meaning catches up behind them.

`leasha` with no arguments opens the window; anything else runs one of 32 headless commands
(`python -m app.cli <command>`). The full list, with every option, is in the technical
reference.

## Your Google data

Download your Google Takeout, put the zip in an indexed folder — Leasha does the rest.
Nothing connects to Google. Every email, note, and photo is instantly searchable by content
and date, completely offline.

The Takeout archive is processed like any other zip: Gmail is indexed as mail, Keep notes
as text, and photos with their metadata intact. No setup, no configuration, no import wizard.

## Developing

Open the folder in VS Code, or double-click `Leasha.code-workspace`. See
`docs/VSCODE.md`. Then:

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pytest tests -q
venv\Scripts\python.exe -m app.cli stats
```

Never run two test runs at once in one copy (they share `.pytest_tmp`); pass `--basetemp`
to the second. `tools\grab_ui.py` renders every page of the real window to PNG.

## When something breaks

```powershell
venv\Scripts\python.exe -m app.cli diagnose
```

Writes a zip to `logs\diagnostics\` with everything needed to work out what went wrong.
See `docs/TROUBLESHOOTING.md`.

## Documents

| File | What it is |
|---|---|
| `CLAUDE.md`, `AGENTS.md` | The front door for an assistant session: what to read, in what order |
| `HANDOFF.md` | **Start here if picking this up cold.** State, decisions, traps, how to resume |
| `docs/PROJECT_INSTRUCTIONS.md` | The standing rules for working on this project |
| `docs/ORDER_REGISTER.md` | Every work order, its status and where it sits in the queue |
| `docs/GLOSSARY.md` | What the terms mean here |
| `docs/USER_GUIDE.html` | The illustrated user guide |
| `docs/TECHNICAL_REFERENCE.html` | Architecture, data, internals and the complete command line |
| `LOCAL_KNOWLEDGE_GRAPH_V2.md` | Architecture, error contract, PST strategy, honest performance numbers |
| `BUILD_SPEC_V2.md` | Layer-by-layer build plan (L0–L9), schemas, acceptance tests, performance budget |
| `docs/VERSIONING.md` | Version scheme, git conventions, release checklist |
| `docs/VSCODE.md`, `docs/VISUALSTUDIO.md` | Opening and working on the project in an editor |
| `docs/TROUBLESHOOTING.md` | Where the logs are, what errors mean, common fixes |
| `docs/adding-a-file-type.md` | Teaching Leasha a new file type |
| `docs/REFERENCE-model-backends.md` | Which model runs on what |
| `docs/MAC_VERIFICATION.md` | The checks for a first run on a Mac |
| `docs/THIRD_PARTY_NOTICES.md` | Licences of what Leasha depends on |
| `CHANGELOG.md` | What changed and why |
| `archive/README.md` | Retired documents, kept for reference |

## Layout

```
app/core      L0  config, settings registry, AppError, logging, locks, osbridge
app/storage   L1  SQLite/FTS5 + LanceDB (schema v30)
app/extract   L2  PDF, Office, plaintext, PST/EML/mbox, zip, OCR, media + chunking
app/ort       L2  ONNX Runtime models inside Leasha: Florence-2, Whisper, chat LLM
app/index     L3  walker, resumable pipeline, embedder, governor, folder watch
app/search    L4  BM25 + ANN, RRF fusion, rerank, filters, git search, evaluation
app/reports   L4  read-only reports: Digital Inheritance, Space Report, Life Timeline
app/ui        L5  PyQt6 window
app/shell     L5  `leasha shell`, the terminal search prompt
app/llm       L8  engine choice (ONNX or Ollama) for Interpret, Chat and Describe
app/chat      L8b the Chat tab's engine
app/cli           `python -m app.cli`, the headless entry point
tests/        unit, integration, and fixtures (healthy + deliberately corrupt)
```

## Non-negotiables

These are design constraints, not preferences. They are the reason V2 exists. The full twelve
are in `docs/PROJECT_INSTRUCTIONS.md`.

- **No service in the search hot path.** Search must work with Ollama stopped, uninstalled,
  or crashed.
- **Every error states what happened AND how to fix it.** Structured `AppError`, never a bare
  traceback, never a silent `except: pass`.
- **One bad file never halts a 100GB run.** Log it, mark it, count it, continue.
- **Everything long-running is resumable.** The cursor is persisted before it is needed.
- **The UI thread never does I/O.**
- **SQLite is the authority.** LanceDB is derived and can always be rebuilt from it.
- **The retrieval path never calls a model.** Query *translation* may, once, before the
  search, and always visibly. Keyword and semantic search stay instant.
- **A translated query is shown and editable.** Invisible rewriting makes search
  unpredictable, and unpredictable search over your own archive is worse than blunt search.
- **Every `.ps1` is ASCII-only or UTF-8 with a BOM.** PowerShell 5.1 reads a BOM-less file as
  the ANSI codepage; one em dash is enough to kill the script at parse time, silently.

## Targets

| Metric | Target |
|---|---|
| Warm search (p95) | <300ms |
| First search after launch | <3s |
| RAM | 8GB minimum, 16GB comfortable |
| Free disk on the index drive | 150GB for a 100GB corpus |
| Services to manage | 0 (1 optional) |

Measured, not assumed — the per-stage budget is in `BUILD_SPEC_V2.md`.
