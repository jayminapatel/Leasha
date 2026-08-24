# Local Knowledge Graph Search + Office Suite

Hybrid semantic + keyword search over ~100GB of local files and PST email archives, with a
knowledge graph and Office document generation. Windows 10/11, single user, fully local.

**One process.** SQLite/FTS5 for metadata and keyword search, LanceDB for vectors, FastEmbed
ONNX for embeddings — all embedded. No services, no ports, no passwords. Ollama is optional
and is never called by search.

**Version:** see `VERSION` · **Status:** environment and plan complete, Layer 0 not started

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

Full detail, including every installer parameter, in `LOCAL_KNOWLEDGE_GRAPH_V2.md`.

## Documents

| File | What it is |
|---|---|
| `LOCAL_KNOWLEDGE_GRAPH_V2.md` | Architecture, error contract, PST strategy, honest performance numbers |
| `BUILD_SPEC_V2.md` | Layer-by-layer build plan (L0–L9), schemas, acceptance tests, performance budget |
| `docs/VERSIONING.md` | Version scheme, git conventions, release checklist |
| `CHANGELOG.md` | What changed and why |

## Layout

```
app/core      L0  config, AppError, logging, single-instance, version
app/storage   L1  SQLite/FTS5 + LanceDB
app/extract   L2  PDF, Office, plaintext, PST/EML parsers + chunking
app/index     L3  walker, resumable pipeline, embedder
app/search    L4  BM25 + ANN, RRF fusion, optional rerank
app/ui        L5  PyQt6 shell
app/graph     L6  co-occurrence baseline, optional LLM entities
app/office    L7  DOCX/XLSX/PPTX builder
app/llm       L8  optional Ollama client
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
