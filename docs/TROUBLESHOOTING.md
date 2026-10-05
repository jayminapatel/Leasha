# Troubleshooting

**Doc version:** 1.10 · **Updated:** 2026-10-06 · **Applies to:** app v0.3.4

Written for someone who codes as a hobby: no assumed knowledge, just where to look and what
things mean.

## The one command

When anything goes wrong, in the window: Settings › Storage & maintenance › **Save a support
bundle…**. Or from a terminal:

```powershell
cd D:\Local\GitHub\SearchProject
venv\Scripts\python.exe -m app.cli diagnose
```

`D:\Local\GitHub\SearchProject` here, and elsewhere in this file, is the folder Leasha runs
from source in on the owner's laptop; use your own. A copy put in by the installer has no
`venv`: in its install folder, `.\leasha-cli.exe diagnose` does the same (`leasha-cli.exe`
runs any `app.cli` command).

It writes a zip to `logs\diagnostics\`. Open `summary.txt` inside it first: it lists anything
already known to be wrong. Send the whole zip when asking for help - it contains the
environment report, both store summaries, the recent logs, the installed package versions and
your configuration, so nobody has to ask you twenty questions.

In VS Code: **Terminal > Run Task > Diagnose (bundle for troubleshooting)**.

## "The term 'leasha' is not recognized"

```
leasha : The term 'leasha' is not recognized as the name of a cmdlet...
```

Type `.\leasha` instead - with a dot and a backslash in front.

PowerShell does not run commands from the folder you are standing in, on
purpose: it is what stops a malicious `ls.exe` left in a downloads folder from
running when you type `ls`. The `.\` says "yes, I mean this one, here".

To stop needing it:

```powershell
cd D:\Local\GitHub\SearchProject
.\add-to-path.ps1
```

That adds the folder to **your** PATH - no administrator rights, nothing changed
for anybody else using the computer, and `.\add-to-path.ps1 -Remove` undoes it.
Open a new terminal afterwards: one that is already open keeps the PATH it
started with.

## "That file was not indexed and I do not know why"

```powershell
venv\Scripts\python.exe -m app.cli formats
```

It prints every file type the application knows about, which extractor reads it,
the size limit, and - separately - everything that is **switched off**. A file
type in the "Off" list is not opened at all, so nothing about it will ever appear
in a search.

It also names the two configuration files it read:

- `config\extractors.toml` ships with the application and is replaced on
  upgrade. **Do not edit it**; your changes would be lost.
- `<your data folder>\extractors.toml` is yours. It is merged over the top, so
  it only needs the lines you want to change, and **deleting it always restores
  the shipped behaviour exactly** - which makes it a safe thing to try.

If the command prints an error instead of a list, your file has a mistake in it.
The message names the exact key, for example:

```
[ERR_CONFIG_INVALID] Configuration problem with 'extensions..log.extracter':
  unknown key(s): extracter
  FIX: Expected only: enabled, extractor, max_bytes, note.
```

Fix that key, or delete the file and start again.

## The interpreted query was wrong

Press **Interpret** (or Ctrl+Enter) and the query it builds goes **into the search box**. If
it got something wrong, edit it and press Enter. That is the whole fix, and it is why the
query is shown rather than applied invisibly.

If it consistently misreads you, it is worth knowing what it is allowed to produce:

```powershell
venv\Scripts\python.exe -m app.cli commands
```

The model can only emit those filters. It cannot invent one - anything it makes up is
rejected and your original words are searched instead.

Interpret, Chat and Describe run inside Leasha by default (`CHAT_ENGINE=onnx`). If nothing
happens when you press Interpret, the likely cause is that no chat model has been downloaded
yet: Settings › Models & AI › **Download**. Interpret is given up to 45 seconds. Plain Enter
always works and never uses the model.

Ollama matters only when Settings › Models & AI says they run on Ollama; then Interpret doing
nothing means Ollama is not running:

```powershell
venv\Scripts\python.exe -m app.cli ollama
```

## Search does not find something I know is there

In order of how often it is the cause:

1. **Is it indexed?** `app.cli files <part of the name>` finds it by filename alone. If that
   comes back empty, the file was never indexed and no search will find it.
2. **Is meaning-based search working?** `app.cli stats` says so in words at the bottom, and
   tells you what to run if not. A corpus that is only partly embedded is findable by exact
   words only.
3. **Is the file type switched on?** `app.cli formats` lists what is indexed and what is off.
   Anything in the "Off" list is never opened at all.
4. **Are you fighting a filter?** `/type pdf` on a Word document finds nothing, correctly.

## The window has stopped responding

**This should not happen, and it is a bug.** Every long operation - indexing,
embedding, OCR, document conversion, query interpretation, the environment
check - runs off the interface thread and reports progress back. The window is
meant to stay interactive throughout: you can search while indexing, change
settings while OCR runs, and close it at any moment.

This entry used to say "wait ten seconds", and named building the graph and the
environment check as things that legitimately froze the window. That was
documenting a defect as expected behaviour, which is how a bug becomes a feature
nobody fixes. A guard test now checks that no interface module blocks, and both
named causes have been moved onto workers.

So if the window does stop responding, please report it with what you were
doing at the time. Press **Ctrl+C** in the PowerShell window you started it
from - that closes the application properly, releasing the lock on the index and
flushing the database, which ending the task from Task Manager does not.

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
are in the Leasha folder (`D:\Local\GitHub\SearchProject` on the owner's laptop), and you are using `venv\Scripts\python.exe` rather than a plain
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
optional. Search never calls the LLM. With the default `CHAT_ENGINE=onnx`, Ollama being down
affects nothing; set to `ollama`, it affects AI answers only.

## If you need to start the index over

The index is entirely rebuildable - nothing in it is original data.

The easy way is Indexing › **Reset index…** in the window. From a terminal, with
`<DATA_PATH>` standing for your index folder (`%LOCALAPPDATA%\Leasha` for a copy run from
source, `%LOCALAPPDATA%\Leasha\Data` by default for an installed one, `D:\Leasha\Data` on the
owner's machine; `leasha stats` prints it):

```powershell
# stop the app first, then:
Remove-Item -Recurse -Force <DATA_PATH>\vectors
Remove-Item -Force <DATA_PATH>\fts\knowledge.db*
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

## Newer problems and what they mean (added 1 October 2026)

| You see | What it means, and what to do |
|---|---|
| "Keyword results only - meaning-based search returned nothing" in a window left open for hours | Fixed on 2026-10-01: a window opened before an index run created the vector table answered keyword-only until restarted. Update; on an older copy, restart Leasha |
| An attachment you expect is not found by its contents | Indexing › What gets read › Email attachments decides. The default reads Office, PDF, text, CSV and HTML; pictures are kept by name only. Attachments indexed before 2026-10-01 keep what they had until their archive is read again |
| `ERR_INDEX_PROCESS_ENDED` | "Index in a separate process" is on and the child process stopped. Start again; the run carries on where it left off. `logs\index-process-stderr.log` holds what it printed |
| "the graphics card failed ... done again on the processor" in a run | DirectML reported a device error (for example `887A0005`). The batch is redone on the processor, so nothing is lost. If it repeats, set Indexing › Tuning › Run models on to **Processor** |
| A folder cannot be renamed or moved | "Index files as soon as they are saved" is on (Indexing › Schedule). Switch it off first |
| Files show **TimedOut** | They took longer than their time limit. Indexing › Status › Timed-out files › **Retry with a longer time limit**, or `leasha index --retry-timed-out` |
| Chat says the chat model is not downloaded | Settings › Models & AI › **Download** beside the chat model |

Logs also live in `logs\runs\` (one file per command or window session) and `logs\sessions\`
(recordings made with "Record what I do" or `--debug`).

## Newer problems and what they mean (added 4 October 2026)

| You see | What it means, and what to do |
|---|---|
| The window vanished while a folder picker was open, with no message | Fixed on 2026-10-02. Windows raised a harmless error inside the picker and the crash recorder itself fell over reading another thread. If it happens on 0.3.4 or later it is something new: send `logs\crash.log` and the newest file in `%LOCALAPPDATA%\CrashDumps` |
| A mail archive (.pst) seems stuck for hours | Probably slow, not stuck: a large archive read through Outlook manages a few hundred to a few thousand messages an hour. The Indexing page now shows the folder and the count moving. Reading the file directly (Indexing > What gets read > Outlook archives) is much faster |
| Every run reads all the mail archives again although nothing changed | Fixed on 2026-10-02. Outlook changes an archive's date just by opening it; Leasha now compares a fingerprint from inside the file and skips an archive whose contents have not moved. The first run after updating still reads each one once |
| One folder or archive needs reading again, not all of them | Settings > What's indexed: the play button on that line ("Index now"). On the Offline page, the Rescan button on the drive's line |
| "files type pst" (or "type pdf", "pdf files") finds nothing | From 2026-10-02 these words are read as a file type. If it still finds nothing, the index holds none: `leasha stats` says how many files are indexed. After a reset the index is empty until a run finishes |
| Chat lists files when you asked for a number, a date or an address | Fixed on 2026-10-04: a question that names a value is read and quoted. If the answer is "not found", the value is not in what has been indexed |
| On a Mac, a drive on the Offline page shows as unplugged although it is plugged in | Three causes. It is encrypted and has not been unlocked: unlock it in Finder, then leave the Offline page and come back to it (the list is read again each time the page is opened). It was scanned on Windows: a Mac knows the same drive by a different identity, so scan it once on the Mac and it is a source of its own there. It is a network share: shares are not followed on a Mac yet |
| On a Mac, choosing a drive to scan says it is a folder, not a drive | Choose the drive itself (its line under Locations in Finder, which is `/Volumes/<name>`), not a folder inside it. A few kinds of disk give a Mac no identity to remember them by; those cannot be scanned |
| On a Mac, Hardware ID always says "Not available" | The drive's hardware serial is read on Windows only. Nothing depends on it; a drive is recognised by its own identity |
| The Offline list says "Not available" under Hardware ID | The drive's own serial has not been read yet. Rescan the drive while it is plugged in; a network share shows its address instead, because it has no hardware |
| Files from inside emails show as PST with the archive's size | Fixed after 0.3.4: an attachment has its own type and size. Rows indexed before get their type when the index next opens; their size is blank until the archive is read again - Settings > What's indexed > **Rescan archived folders now**, or re-index |
| An AI program says Leasha's AI access is stopped, or cannot connect | Leasha must be open with AI access started: Settings > Models & AI > AI programs > **Start**. If the port was changed, press **Connect** again for that program and restart it. A program connected by address also needs the key, which Connect and **Copy address and key** include |
| Start says the port is already in use | Another program uses that port. Choose another **Port for AI programs**, press Start, then Connect each program again |
| Open on a file from an email or inside a zip says it could not take it out | The archive or zip has moved or changed since it was indexed, or the message was indexed through Outlook. Use **Open in Outlook** (for mail) or **Show in folder** (for a zip), or index it again with Index now on its line. The copy Leasha opens is read-only and lives in its own cache folder; it is removed when Leasha closes |
| `*.pst` (or `*.pdf`) on the Files page finds everything but the files of that type | Fixed after 0.3.4: a star, a dot and an extension is read as the type. On 0.3.4 and earlier type `/type pst` instead |
| Which version is this? | Help > About Leasha, or `leasha --version` |
| Windows says it protected your PC when the installer is opened | The installer is not signed yet. **More info**, then **Run anyway** |
| Where is the settings file of an installed copy? | `<install folder>\_internal\.env`. The installer writes it only if there is none, so installing again keeps your settings |
| "this installed copy of Leasha cannot add packages to itself" | A copy put in by the installer cannot install a missing reader. Install the next version of Leasha, or run Leasha from source |
