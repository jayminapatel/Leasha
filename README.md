# Leasha

**Doc version:** 2.0 · **Updated:** 2026-08-26 · **Applies to:** app v0.3.3

**Search everything on this machine — by describing it in plain English.**

One headline feature. Type what you remember about a document, and get the document:

```
the safety report Dave sent about Leeds before the audit
```

Keyword and meaning-based search run together over ~100GB of local files and Outlook
archives. Filters — sender, recipient, subject, date, file type, size — are typed with `/`
and offered as you type. Windows 10/11, single user, **fully local**.

**One process.** SQLite/FTS5 for metadata and keyword search, LanceDB for vectors, FastEmbed
ONNX for embeddings — all embedded libraries. No services, no ports, no passwords, and
nothing leaves the machine.

Ollama is optional and **never touches the retrieval path**. It does one job: turning a
sentence into the filter syntax the search box already understands, visibly, so you can
correct it. With Ollama stopped, search works exactly as it always does.

```powershell
cd D:\SearchProject
.\leasha                    # open the window
.\leasha commands           # the filters you can type
.\leasha evaluate --builtin # does search actually work?
```

**The `.\` is not optional**, and it is not a typo. PowerShell deliberately does
not run commands from the current directory - a protection against a malicious
`ls.exe` dropped in a folder you happen to be standing in. To drop the `.\` and
run `leasha` from anywhere:

```powershell
.\add-to-path.ps1     # undo with -Remove; user PATH only, no admin rights
```

---

## Install

```
cd D:\SearchProject
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

Full detail, including every installer parameter, in `LOCAL_KNOWLEDGE_GRAPH_V2.md`.

## Developing

Open the folder in VS Code, or double-click `SearchProject.code-workspace`. See
`docs/VSCODE.md`. Then:

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pytest tests -q
venv\Scripts\python.exe -m app.cli stats
```

## When something breaks

```powershell
venv\Scripts\python.exe -m app.cli diagnose
```

Writes a zip to `logs\diagnostics\` with everything needed to work out what went wrong.
See `docs/TROUBLESHOOTING.md`.

## Documents

| File | What it is |
|---|---|
| `HANDOFF.md` | **Start here if picking this up cold.** State, decisions, traps, how to resume |
| `docs/PROJECT_INSTRUCTIONS.md` | The standing rules for working on this project |
| `LOCAL_KNOWLEDGE_GRAPH_V2.md` | Architecture, error contract, PST strategy, honest performance numbers |
| `BUILD_SPEC_V2.md` | Layer-by-layer build plan (L0–L9), schemas, acceptance tests, performance budget |
| `docs/VERSIONING.md` | Version scheme, git conventions, release checklist |
| `docs/VSCODE.md` | Opening and working on the project in VS Code |
| `docs/TROUBLESHOOTING.md` | Where the logs are, what errors mean, common fixes |
| `CHANGELOG.md` | What changed and why |

## Layout

```
app/core      L0  config, AppError, logging, single-instance, version
app/storage   L1  SQLite/FTS5 + LanceDB
app/extract   L2  PDF, Office, plaintext, PST/EML parsers + chunking
app/index     L3  walker, resumable pipeline, embedder
app/search    L4  BM25 + ANN, RRF fusion, rerank, filters, evaluation
app/ui        L5  PyQt6 shell
app/llm       L8  optional Ollama client (query translation only)
tests/        unit, integration, and fixtures (healthy + deliberately corrupt)
```

## Non-negotiables

These are design constraints, not preferences. They are the reason V2 exists.

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
