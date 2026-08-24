# Troubleshooting

Written for someone who codes as a hobby: no assumed knowledge, just where to look and what
things mean.

## The one command

When anything goes wrong:

```powershell
cd D:\SearchProject
venv\Scripts\python.exe -m app.cli diagnose
```

It writes a zip to `logs\diagnostics\`. Open `summary.txt` inside it first: it lists anything
already known to be wrong. Send the whole zip when asking for help - it contains the
environment report, both store summaries, the recent logs, the installed package versions and
your configuration, so nobody has to ask you twenty questions.

In VS Code: **Terminal > Run Task > Diagnose (bundle for troubleshooting)**.

## What is in the logs folder

```
logs\
  app\           Everything the app did, one file per day. Start here.
  errors\        Errors only, one JSON object per line.
  install\       Installer transcripts, one per run.
  crash\         Unhandled crash reports.
  diagnostics\   The zips produced by 'diagnose'.
```

**`app\app_YYYY-MM-DD.log`** is the main narrative. Each line looks like:

```
2026-08-24 13:45:08.123 | WARNING  | indexer.pdf | ERR_FILE_CORRUPT | pdf:extract:88 | Cannot read 'report.pdf'...
```

Reading left to right: when, how serious, which part of the app, the error code, the exact
line of code, then the message. The error code is the useful bit - it is the same string
every time that particular thing goes wrong, so it is what to search for.

**`errors\errors_YYYY-MM-DD.jsonl`** holds only warnings and errors, one JSON object per
line. This exists because during a 100GB index run you do not want to read a log - you want
to count things. To see which failures dominate:

```powershell
Get-Content logs\errors\errors_*.jsonl |
  ForEach-Object { ($_ | ConvertFrom-Json).record.extra.error_code } |
  Group-Object | Sort-Object Count -Descending
```

That tells you "3,412 ERR_CLOUD_ONLY, 12 ERR_FILE_CORRUPT" in one line, which is a completely
different problem from twelve unrelated failures.

## How errors are meant to read

Every error in this app carries four things: a **code**, a plain-English **message**, a
**suggestion**, and an **action type**. If you ever see a bare Python traceback reach the
screen, that is a bug in the app, not just in whatever failed - report it.

Action types tell you how serious it is:

| Action | Meaning |
|---|---|
| `SKIP_CONTINUE` | This one item was skipped. The run carried on. Usually nothing to do. |
| `AUTO_FIX` | The app handled it and is telling you what it did. |
| `USER_RETRY` | Needs you to do something, then retry. |
| `RUN_COMMAND` | There is an exact command to run; it is in the message. |

## Common problems

### `No module named pytest`

`pytest` is a development tool, not part of the app. Install it once:

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

### `No module named app`

You are running the wrong Python, or from the wrong folder. Both of these must be true: you
are in `D:\SearchProject`, and you are using `venv\Scripts\python.exe` rather than a plain
`python`.

### The installer produces no output at all

Not even a log file. That means the script died before running - almost always a syntax or
encoding fault. Check it directly:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\parse-check.ps1
```

Results land in `logs\parse-check.log`. This exact failure happened once: a `.ps1` saved as
UTF-8 *without* a byte order mark, where an em dash decoded as a smart quote and PowerShell
read it as a string delimiter. Hence the rule that every `.ps1` here is ASCII-only or
BOM-prefixed, and why VS Code is configured to enforce it.

### `running scripts is disabled on this system`

Windows execution policy. Use `run-install.cmd`, which bypasses it for one process rather
than changing a machine-wide setting.

### `ERR_DB_LOCKED` - "Another copy of the application is already running"

Two copies cannot share one index without corrupting it, so the second refuses to start.
Close the other one. If you are sure nothing else is running, a previous copy may not have
exited cleanly - check Task Manager for a stray `python.exe`.

### `ERR_CONFIG_INVALID`

Something in `.env` is wrong, and the message names the exact key. `.env` is created by the
installer and holds machine-specific paths. To start over, delete it and re-run
`run-install.cmd`. `.env.example` shows what a valid one looks like.

### `ERR_CLOUD_ONLY` - thousands of them

You pointed the indexer at a OneDrive or SharePoint folder using **Files On-Demand**. Those
files exist as placeholders: the name and size are local, the contents are not. Reading one
downloads it in full.

Skipping them is deliberate - otherwise indexing would quietly download your entire cloud
library. To include a folder, right-click it in Explorer and choose **Always keep on this
device**, wait for it to sync, then re-index. Only do that if you have the disk space.

### `ERR_FILE_CORRUPT` or `ERR_FILE_LOCKED` during indexing

Normal, and not a problem. One bad file never halts a run - it is recorded and skipped, and
locked files are retried automatically on the next pass. Check the totals with
`app.cli stats`; a handful out of hundreds of thousands is expected.

### Search feels slow

The target is under 300ms warm, under 3s for the first search after launch (an ONNX model
loads once per session). If it is consistently worse, the per-stage timings are logged at
DEBUG level in `logs\app\` - they say which stage overran, which is the whole point of
breaking the budget down by stage.

## Checking on things

```powershell
venv\Scripts\python.exe -m app.cli stats     # configuration and both stores
venv\Scripts\python.exe doctor.py            # environment verification
venv\Scripts\python.exe doctor.py --quick    # same, skipping model loading
```

`doctor.py` distinguishes **FAIL** (required - the app will not work) from **WARN**
(optional - a feature is unavailable but core search is fine). Ollama and Outlook are
optional. Search never calls the LLM, so Ollama being down affects AI answers only.

## If you need to start the index over

The index is entirely rebuildable - nothing in it is original data.

```powershell
# stop the app first, then:
Remove-Item -Recurse -Force D:\KnowledgeGraphData\vectors
Remove-Item -Force D:\KnowledgeGraphData\fts\knowledge.db*
venv\Scripts\python.exe -m app.cli init
```

Your actual documents are untouched: the app only ever reads them.

If only the *vectors* look wrong, they can be rebuilt without re-reading a single document,
because SQLite keeps the text of every chunk. SQLite is the authority; LanceDB is derived.

## Reading a diagnostic bundle yourself

Unzip it. `summary.txt` is the headline. `report.json` has everything:

| Section | What it tells you |
|---|---|
| `version` | App version and git commit |
| `system` | OS, Python, whether the venv is active |
| `config` | Every resolved setting |
| `disk` | Free space on the index and project drives |
| `stores` | Row counts, schema version, integrity check, cursors |
| `packages` | Installed versions of every dependency |
| `git` | Branch, status, recent commits |
| `doctor` | The full environment report |

If a section says `_collection_failed`, that section itself broke - which is diagnostic
information in its own right, and the reason the bundle is built section by section rather
than all or nothing.
