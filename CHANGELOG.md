# Changelog

**Doc version:** 3.14 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2

All notable changes to this project are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows the scheme in `docs/VERSIONING.md`.

## [Unreleased]

### Fixed — every document told you to type a command PowerShell refuses to run

```
leasha : The term 'leasha' is not recognized as the name of a cmdlet...
```

PowerShell does not run commands from the current directory — a deliberate
protection against a malicious `ls.exe` left in a folder you happen to be
standing in. So `leasha` fails where `.\leasha` works, and **every example in
every document said `leasha`**. A documentation bug, which is the kind nothing
catches.

- Every copy-pasteable line now reads `.\leasha`.
- `.\add-to-path.ps1` puts the folder on your **user** PATH so the `.\` can be
  dropped — no administrator rights, nothing changed for anybody else on the
  machine, and `-Remove` undoes it. Safe to run twice.
- The installer ends by showing the command that actually works, and offering
  the PATH step.
- `docs/TROUBLESHOOTING.md` answers the error using the words the error itself
  uses, because somebody hitting this will paste it in looking for it.
- A test fails if any document goes back to bare `leasha`. It caught the one
  legitimate exception on its first run — the troubleshooting guide quotes the
  failing message verbatim, and has to.


### Added — the file-types editor, and the work order is complete

Step 6, the last of six. Settings now lists every file type with a checkbox,
what reads it, and its size limit. Anything switched off is never opened at all.

**Only `enabled` is editable, deliberately.** Size caps and extractor routing
are per-format decisions with real consequences — an OCR cap raised to 100MB is
minutes of work per image — and they belong in `config\extractors.toml` beside
the comments explaining each one. A checkbox that could silently make indexing
twenty times slower is not a kindness.

**Changes are stored as differences, never as a snapshot**, and this reversed an
earlier decision. A user entry originally *replaced* the packaged rule outright,
on the reasoning that half a rule from each file matches neither and cannot be
reasoned about. The editor showed that wrong in the case that matters most:
turning `.png` off is one key, and writing the whole rule to say so would pin
`extractor` and `max_bytes` at today's values — so a later release improving
either would have the improvement silently discarded. A user entry now **patches**
the packaged rule; a brand-new extension must still be complete, because there
is nothing to patch.

The panel also says what it does *not* do: a format switched on does not
retrospectively index the files already skipped, and somebody not told that
concludes the setting did not work.


### Added — OCR for images and scanned PDFs, on by default and honest about it

Step 5 of the file-types work order, and the owner's explicit override of "OCR
is out of scope for V2", taken knowing it can dominate an index run.

**That is a decision somebody is entitled to make about their own machine — but
it is only a real decision if the cost is visible**, so it was measured rather
than estimated:

| | |
|---|---|
| A full page of text | **3.6 seconds** |
| Roughly | **8× the cost of embedding one passage** |
| 1,000 scanned pages | 1 hour |
| 10,000 | 10 hours |
| 100,000 | 100 hours |

Every document records `ocr_seconds` and `ocr_confidence`, so slow indexing is
attributable rather than mysterious — "indexing got slow" with no attribution is
a complaint nobody can act on. To switch it off, set `enabled = false` on the
image lines in your own `<DATA_PATH>\extractors.toml`.

**RapidOCR rather than Tesseract**: the models ship inside the wheel, so there
is no separate binary and no `TESSDATA_PREFIX` for an installer to get wrong on
a machine nobody can log into. Tesseract stays reachable as a Tier 2 converter
for anybody who prefers it.

Two things it declines to spend time on. Images below 64×64 are icons, bullets
and spacers — a document-heavy corpus holds thousands, each costing a model call
to yield nothing. And a page that already has a text layer is never OCR'd:
running it over a searchable PDF costs seconds to produce a worse copy of text
already extracted.

The engine is a seam, so every path — missing package, engine failure, low
confidence, an unexpected return shape — is tested with a fake on a machine
where OCR is not installed. One of those tests caught a real crash: RapidOCR's
return shape has changed between versions, and a future one returning a bare
number would have passed the truthiness check and then failed on iteration,
taking an index worker with it.


### Added — Tier 2 converters: a dozen dead formats, one implementation

Step 4 of the file-types work order. LibreOffice alone now covers `.doc`,
`.xls`, `.ppt`, `.rtf`, `.pages`, `.numbers`, `.key`, `.wpd` and `.pub`; pandoc
covers `.epub` and `.fb2`. Adding a format is a line in a text file rather than
a parser.

That leverage also makes this the most dangerous module in the application,
because it runs programs. Every decision in it narrows what that can mean, and
each is asserted by a test rather than trusted:

- **The allow-list is in code, not configuration.** `soffice`, `libreoffice`,
  `pandoc`, `xstexporter`, `tesseract` — nothing else, ever. Config chooses
  among allowed converters; it cannot introduce one. `extractors.toml` is a file
  a person edits, and on a shared or synced machine it is a file *someone else*
  might edit; a configuration format that can name any executable is a way to
  run anything. Anything off the list is `ERR_CONVERTER_BLOCKED`, refused
  **before** it is even resolved.
- **The command never reaches a shell.** `subprocess.run(list, shell=False)`,
  always. Filenames come from the corpus being indexed — precisely the input not
  to trust — and a shell would interpret `;`, `&&`, `|` and backticks in one. A
  test converts a file literally named `report; rm -rf ~.doc` to prove it stays
  a single argument.
- **The absolute path invoked is resolved and logged**, every run. `soffice` on
  `PATH` is whatever `PATH` says today.
- **Every temporary directory is removed in a `finally`.** A 100GB run leaking
  one per converted file fills the disk, and the failure then appears somewhere
  else entirely.
- **The timeout is capped at five minutes** regardless of what config asks for.

A converted document is repointed at the **original** file, because a result
linking to `/tmp/leasha-convert-xyz/report.txt` is worse than no result: it
looks like an answer and cannot be opened.

Converters still ship disabled — the binary may not be installed, and a format
that fails on every file is worse than one that says plainly it is off. An
unconfigured `.doc` therefore stays `ERR_UNSUPPORTED_TYPE` ("this app does not
do that") rather than becoming `ERR_CONVERTER_MISSING` ("something is broken")
on every file in the corpus. `doctor` reports which binaries were found so
Settings can offer exactly those.


### Added — Google Workspace pointers are findable instead of invisible

Step 3 of the file-types work order. A `.gdoc` is not a document: it is a few
hundred bytes of JSON holding a URL, and Drive for Desktop leaves thousands of
them in a synced folder.

**The reason to index them is the failure they otherwise cause.** Unsupported,
they are skipped and invisible — somebody searches for a document they know
exists, finds nothing, and concludes the search is broken. Indexed, the pointer
is findable by name and says plainly *"the text of this document is not stored
on this machine"*, which turns a mystery into an answer.

**It never fetches the URL.** Not once, not optionally, not behind a flag. A
fetch would be an authenticated request telling Google what is being indexed and
when, from an application whose whole proposition is that nothing leaves the
machine — and it would look like a small convenience while doing it. Two tests
assert it: no network module is importable from that file, and no call in it
resembles a fetch.

Both of Drive's historic JSON formats are read, because old files keep the old
shape, and the file id is recovered from the URL when Drive did not write it as
a field — it is stable across renames and is what somebody pastes to find the
file again.


### Added — OpenDocument (.odt, .ods, .odp), with no new dependency

Step 2 of the file-types work order. An ODF file is a ZIP holding
`content.xml`, and both halves are in the standard library.

**`odfpy` was the obvious choice and is the wrong one.** It publishes no wheel,
so pip builds it from source, which needs a compiler present on every machine
that installs this. A dependency that can fail at install time, on Windows, for
a format most corpora hold a handful of, is a poor trade against a hundred lines.

Two bugs found by running it against documents rather than reasoning about it:

- **Inline formatting scrambled sentences.** ODF marks emphasis with nested
  elements, so `<p>Findings from the <span>annual</span> inspection.</p>` holds
  three fragments — the paragraph's text, the span's, and the span's *tail*.
  Ending the paragraph at the first fragment produced "Findings from the /
  inspection. annual". Fixed by walking with enter and exit events rather than
  text alone.
- **Spreadsheets lost their rows.** A `<text:p>` inside a cell closed before the
  cell did, so every cell landed on its own line and "Licence" ended up two
  lines from "12400". That adjacency is the entire reason a spreadsheet is worth
  indexing: a number beside a label is a fact; a number alone is noise.

Namespaces are matched on local name, not URI — ODF's has changed between
versions, and another office suite writing its own would otherwise yield
nothing at all, silently. Comments and tracked changes are left out: an aside is
not the document, and indexing it puts words in a file its author never wrote
there.

`ERR_FILE_TOO_LARGE` joins the registry, checked from the archive header — the
point of a size limit is not to allocate the gigabyte in order to discover it is
a gigabyte.


### Fixed — the Indexing page was blank, and said nothing about why

`refresh_totals` ended in `except: return`. A store read that failed left an
empty label with nothing to explain it — and a blank page is the worst possible
answer to "is my index working", because it is indistinguishable from an empty
index, a broken one, and a bug in the page itself.

It is now a panel that **always produces rows, including for failure**:

| | |
|---|---|
| Documents | 355 indexed |
| Searchable passages | 3,355 |
| **Meaning-based search covers** | **5%** — 154 of 3,355 have a vector |
| Skipped | 12 — ERR_UNSUPPORTED_TYPE (12) |
| Index location | `D:\KnowledgeGraphData` · 1.1 GB |
| Last run | 2 hours ago |

The coverage line is the one that matters: it is the number that was invisible
for weeks while meaning-based search silently did a twentieth of its job. The
location line answers "is the index where I configured it", which previously
needed the command line.

Read in a worker, because it opens the vector store and measures a folder.

### Added — Reset index

Deletes everything indexed and starts over. The confirmation says plainly what
is *not* at risk: no document is touched, the index is derived from them, and
the only real cost is the time to rebuild. Saved folders, schedule and theme all
survive — a reset that forgot which folders to index would be one nobody could
recover from without setting the application up again. Cursors are cleared,
because they point at chunk ids that no longer exist, and the database is
vacuumed so the space actually comes back.

### Fixed — the Files tab could not do anything with a file

No right-click, no Enter, and double-click *revealed* in Explorer rather than
opening — everywhere else in Windows, double-clicking a file opens it. A list of
files you cannot act on is a list of disappointments.

One menu now serves both lists (`widgets/file_menu.py`): Open, Show in folder,
**Search inside this file**, Copy path, Copy file name. Every action is checked
before it is offered — a file that has moved is greyed out, and a "re-index this
folder" action appears in its place, because that is the thing that would
actually help.

"Search inside this file" is the bridge that was missing: found it by name, now
find what is in it.

### Fixed — a menu item that did nothing at all

The results menu offered "Add to document". Layer 7 was cancelled before it was
built and the menu item outlived it, emitting a signal nothing was connected to.
Clicking it did nothing, silently.

### Fixed — a Windows path on a non-Windows platform created a nonsense folder

A backslash is a legal filename character on Linux and macOS, so
`Path("D:\Data").mkdir(parents=True)` cheerfully creates a directory *named*
`D:\Data`. Running the CLI from a Linux sandbox against a Windows `.env`
littered the project root with folders called `D:\KnowledgeGraphData` and
`D:\KnowledgeGraphData\cache`. Nothing raised, nothing warned, and the index
appeared correctly configured while writing somewhere else entirely.

Configuration now refuses a Windows path on a platform that has no idea what it
means, with a sentence rather than a silent mkdir.


### Fixed — plain-English search returned nothing at all

Twenty sentences of the kind somebody actually types, against a corpus with
known answers. **Nineteen returned zero results.** Not badly ranked - not ranked
at all. The application's stated purpose is to let somebody "write in normal
text what I am looking for", and the keyword half was mathematically incapable
of it.

Two causes, both found by running the measurement rather than by any unit test:

- **Every term was ANDed, stopwords included.** "drawings of the pump station"
  became `drawings AND of AND the AND pump AND station`, and the document -
  "Pump station general arrangement drawings" - contains neither "of" nor "the".
  Stopwords are now dropped from the FTS expression only; they stay in the terms
  used for highlighting and in the text sent to the embedder, where "from Dave"
  and "for Dave" genuinely differ.
- **ANDing the remaining content words was still too strict.** People describe
  documents with words that are *about* them rather than *in* them - "email",
  "version", "deck". One such word excluded everything. Terms are now joined
  with OR and BM25 ranks by how much matched.

The OR threshold was chosen by measurement, not taste:

| terms before OR | empty results | recall@1 | recall@3 |
|---|---|---|---|
| 1 | **0** | **70%** | **95%** |
| 2 | 2 | 65% | 85% |
| 3 | 6 | 50% | 65% |
| 4 | 11 | 35% | 45% |

Three was the first guess. Precision is not lost: this is the retrieval stage,
BM25 ranks by how much matched, and fusion and reranking follow. Anybody wanting
a strict match has `"quoted phrases"` and an explicit `AND` - the latter now
honoured rather than overridden, which a test caught.

### Added — a measurement of whether search actually works

`app/search/evaluate.py` and a twenty-sentence corpus with known answers. It
reports recall **split by whether the sentence carried a constraint**, because
one number cannot distinguish "search is bad" from "search is fine at topics
and blind to constraints" - and those have completely different fixes.

The split is the finding, and it is the one the work order predicted:

| | plain sentence | with translation |
|---|---|---|
| overall @1 | 50% | **75%** |
| constrained @1 | 50% | **92%** |
| sender | 50% | 100% |
| type | 67% | 100% |
| recipient | 0% | 100% |
| attachment | 0% | 100% |

**This is what justifies Layer 8a.** A plain sentence cannot honour "from
Chris": the words go into the text search and the sender field is never
consulted. Translated to `from:chris`, it is a filter.

Two of the twenty questions were wrong when first written - one pointed at a
message the named person *sent* rather than received, another described a
five-month-old document as "over a year ago". Both scored zero for reasons that
had nothing to do with search. A test now asserts every question's answer
exists in the corpus.

**What it cannot tell you**, stated because a benchmark believed beyond its
evidence is worse than none: how well search works on the owner's real archive.
Twenty-one documents is a small, clean corpus with no near-duplicates and no
twelve years of drift. Every number here is optimistic, and the twenty real
sentences remain the measurement that matters.


### Added — the search box is now worth typing into

Every filter below existed or was one small change away; almost none of them
were reachable, because nothing in the application ever said they were there.

| Type | Does |
|---|---|
| `/type pdf` | only this kind of file |
| `/from dave` `/to priya` | email from / to this person |
| `/subject licence` | subject contains |
| `/has attachment` | with, or `no-attachment` without |
| `/after` `/before` | date range, including `last month` and `30d` |
| `/path leeds` | inside matching folders |
| `/name invoice` | files **called** this — a different question from `/path` |
| `/size >1mb` | above or below a size |
| `"exact phrase"` `-word` `A OR B` `NOT word` `word*` | operators |

`AND`, `OR` and `NOT` are recognised **only in capitals**. "salt and pepper" and
"one or two" are things people genuinely search for, and a boolean feature that
broke them would cost more than it delivers.

`/type pdf` is rewritten to `type:pdf` before parsing, so the parser never learns
about slashes: one grammar, one set of tests. An unrecognised `/word` is left
exactly as typed — `12/03`, `D:/Projects` and `/var/log` all survive, because
silently rewriting a query is how a search box loses trust.

One catalogue in `app/search/commands.py` feeds the `/` dropdown, `app.cli
commands`, and the grammar Layer 8a hands the model, with a test that fails if
any of the three drift from what the parser accepts.

### Fixed — three filters that looked present and did nothing

- **`_OPERATOR` carried its own hardcoded list of field names**, separate from
  the alias table. Adding `to:`, `subject:` and `has:` to the aliases therefore
  achieved nothing at all: the regex never matched them, the handler was
  unreachable, and the words became ordinary search terms. The pattern is built
  from the alias table now, and a test asserts every documented alias matches.
- **`from:` matched with `LOWER(sender) IN (...)`** — an exact comparison
  against the whole address, so `from:dave` never found `dave.smith@acme.com`.
  Nobody searches that way. Every mail field matches on any part now.
- **`upsert_file` stored whatever extension it was handed.** `files.ext` holds
  no leading dot and `type:pdf` compares against exactly that, so a caller
  passing `".pdf"` wrote a row that was indexed, searchable by text, and
  invisible to every filter, with nothing to explain it. The store normalises
  its own invariant now.

### Added — `app.cli embed-bench`, after an estimate was wrong

Embedding was measured at 1.53 passages/second and called "twenty to sixty times
too slow". That was wrong: the estimate assumed short sentences, and this
application embeds 512-token passages. The arithmetic for a 512-token
transformer on a CPU predicts 1.5/sec, so nothing was misconfigured.

**A throughput number without the sequence length beside it is not a number**,
and a projection built on the wrong one sends somebody optimising the wrong
thing for a week. So `embed-bench` measures rather than predicts: it reads the
ONNX weight dtypes to answer "is the model already quantised", reports which
execution providers onnxruntime can actually see, times real embedding at
several sequence lengths, and projects the corpus from the measured rate.


### Changed — the application is called **Leasha**

The name lived in nine files - window title, QApplication name, CLI banner,
argparse description, diagnostic header, a SQL comment, the package docstring
and two places in `doctor.py`. `app/core/branding.py` now holds it once, beside
`version.py` and for the same reason: **anything a person reads should have
exactly one definition.** A test fails if any module spells it out again.

`leasha.cmd` is the launcher: `leasha` opens the window, `leasha --debug`
records the session, and anything else (`leasha stats`, `leasha formats`) goes to
the command line tool. The Python package stays `app` - that is plumbing, not a
name anybody reads.

### Added — a debug recorder, so a bug report is evidence

`--debug`, or the switch in Settings, writes one JSONL file per session under
`logs\sessions\`: every tab change, button, search, error and timing, with a
millisecond timestamp. Three rules, and they are the design.

- **Off unless asked.** A tool that watches by default is one people stop
  trusting, and this application's whole promise is that nothing leaves the
  machine.
- **It can never cause a failure.** A recorder that raises would turn a small
  bug into a crash, inside the handler for the bug you were chasing.
- **Shape, never content.** A search is its length and its result counts, not
  the query. A file is an extension, not a name. A first version also kept the
  first 60 characters of long strings as a "head" - a content leak wearing a
  debugging hat, caught by its own test, and exactly what would have made
  session files unsafe to send. Sending them is the only thing they are for.

### Added — `app.cli ollama`, because "is it up" was the wrong question

Enrichment sat for 200 seconds and produced nothing. `/api/tags` answered - the
service was running - and the first `generate` then waited on a long read
timeout for a model that was not installed. `health()` and "this will work" are
different questions and were being conflated.

The command asks four separately: is anything listening, which models exist, is
the configured one among them, and does a trial completion actually return (in
at most 30 seconds - this is the command people run *because* something is
hanging). `EntityEnricher` now checks `has_model()` before opening anything, so
the same failure costs milliseconds.

### Added — meaning-based search says when it is not working

`vector.search` returns `[]` for an empty vector store, a failed embedding or a
LanceDB hiccup, and search carries on with keyword results. That is right - half
a search beats none - and it makes the failure **invisible**: results look thin,
and nothing distinguishes "the corpus is thin" from "the semantic half is dead".
The same shape as the sentinel bug that hid every PST.

So `SearchResponse` now carries `keyword_count` and `vector_count`, the status
bar says "keyword results only" when the second is zero, the engine logs it, and
`app.cli stats` compares the passage count against the vector count and says in
words what to run. `app.cli reembed` is that command: it rebuilds LanceDB from
SQLite without re-reading a single document, which is the entire point of one
store being the authority and the other being derived.

### Fixed — the guard against dead-object tracebacks never ran

`_emit` existed to swallow `RuntimeError` when sip has deleted a worker's
`WorkerSignals` at shutdown. It could not work: it took the bound signal as an
argument, so `self.signals.finished` was evaluated at the *call site*, before
`_emit` was entered, and the exception was raised while building the arguments -
outside the try/except written to catch it. The `except` clause then tried
`failed` and the `finally` tried `done`, each failing identically, so one dead
object produced three nested tracebacks: precisely what it was written to
prevent. It now takes the signal's *name* and looks it up inside the try.

### Fixed — closing the window raced its own background threads

A graph run was 200 seconds into waiting on Ollama when the window closed.
Closing tore down the QApplication and both stores while that thread was still
running and holding a cursor. `closeEvent` now asks every job to stop, then
drains the pool for up to four seconds while still pumping events - waiting
without pumping would freeze the window during the one operation nobody will
wait out.


### Added — file types are configuration, not code

`config/extractors.toml` now decides which extensions are indexed, what reads
them, and how large a file is worth opening. It is a **three-tier** model, and
the boundary is the design rather than a limitation:

| Tier | What config may say | Example |
|---|---|---|
| 1 | routing and policy | `.log` → plaintext |
| 2 | run an external converter, then read its output | `.doc` → LibreOffice → txt |
| 3 | nothing. This is code. | PDF, PST, OCR |

Config stops at tier 2 deliberately. A configuration format expressive enough to
describe parsing is a programming language with no debugger, no type checker and
no tests — strictly worse than the Python it set out to replace.

- **`app/core/formats.py`** loads, validates and merges two files: the packaged
  defaults (tracked in git, never written to) and `<DATA_PATH>\extractors.toml`
  (this machine's overrides). An upgrade therefore delivers new defaults without
  discarding anybody's choices, and deleting the user file restores shipped
  behaviour exactly.
- **Everything is checked at load.** An unknown key, an extractor name nothing
  provides, an uppercase extension, a `schema_version` from the future, a
  converter command written as a string — each is an `ERR_CONFIG_INVALID` naming
  the offending item, raised while the app is starting rather than three hours
  into a 100GB run on one file.
- **`app.cli formats`** prints what is indexed, what is off, and where the two
  files live. It is the answer to "why was that file not indexed?", which
  otherwise needs a debugger or a guess.
- A converter command must be a **list**, never a string. A string has to be
  split to be run, and the obvious way to split a command line is a shell.
- Every converter ships **disabled**: the binary may not be installed, and a
  format that fails on every file is worse than one that says plainly it is off.
- A **disabled** route is exempt from the extractor-name check, which is what
  lets config and code ship in separate releases. The check runs the instant the
  line is enabled.

### Fixed — the window froze and had to be killed from Task Manager

Reported as *"the program crashed when i was clicking around, the thread is
stuck, ctrl c does not work in powershell and i had to end task"*. Nothing had
crashed. Four separate pieces of blocking work were running on the UI thread,
and a frozen window is indistinguishable from a dead one.

- **"Run doctor" ran `subprocess.run(timeout=120)` on the UI thread.** Doctor
  probes Outlook over COM and opens LanceDB, so this is seconds at best and the
  timeout says two minutes is possible. For all of it the event loop was
  stopped. Now a `CallableWorker`, with the subprocess call and the text
  rendering moved into `presenter.py` where they are tested.
- **The Graph panel read the store while painting.** `refresh()` was
  `top_entities(500)` plus `edges_among` over 96,712 edges — on every switch to
  the Graph tab, and waiting on the SQLite lock whenever an index run held a
  write. Now read in a worker; the painting method is handed data.
- **Settings counted searches by fetching them.**
  `len(recent_searches(limit=100_000))` built a hundred thousand dictionaries to
  produce one number. `SqliteStore.count_searches()` is a `COUNT(*)`.
- **Clearing search history deleted on the UI thread**, freezing the window at
  the exact moment somebody had asked for something to be erased.

### Fixed — Ctrl+C did nothing, so Task Manager was the only way out

Python does not deliver signals from inside C code. Once `application.exec()` is
running the interpreter never returns from Qt's event loop, so a `SIGINT` sets a
flag nothing ever looks at. `app/main.py` now installs a handler that quits
cleanly — closing the window, releasing the single-instance lock, flushing
SQLite — plus a `QTimer` that does nothing four times a second purely to hand
control back to Python often enough for the handler to run.

### Fixed — the theme hook multiplied every time the theme changed

`_apply_theme` connected `colorSchemeChanged` to a lambda calling `_apply_theme`,
from inside `_apply_theme`. Each theme change added another connection, so one
flick of the system switch re-entered the handler once per change ever made,
each re-entry connecting again. Qt does not warn about duplicate connections.

### Fixed — Settings could not be scrolled, at any window size

Only the Indexing tab had a scroll area. Settings is six group boxes stacked
vertically and had none, so its lower half was unreachable on a short window and
marooned at the top of a maximised one. `app/ui/widgets/scroll.py` holds the
wrapper — including `setWidgetResizable(True)`, the line whose absence leaves a
narrow column of content inside a maximised window — and it is applied where
tabs are added, so it cannot be forgotten for the next view.

A wrapped view is no longer the widget in its tab, which silently breaks both
`tabs.setCurrentWidget(view)` and `tabs.widget(i) is view`. `MainWindow` now
keeps a view→index map and a `_show()` helper; a test fails if
`setCurrentWidget` reappears.

### Fixed — Ollama spent 120 seconds finding out nothing was listening

An enrichment run reported `elapsed_s: 120.09, chunks_processed: 0`. `requests`
applies a single timeout float to **both** the connect and the read, so the
two-minute budget meant for a local model composing a paragraph was also being
spent discovering the socket would not open. Connect now has its own three
seconds: Ollama is a process on this machine, and it either answers immediately
or is not going to.

### Fixed — six index runs started in seven seconds

Ordinary clicking. `IndexingView.start` did refuse the extra runs, but silently,
and only after `MainWindow._start_indexing` had built a `Pipeline` and an
`Embedder` — loading the ONNX model — purely to discard them. The guard now runs
first, and says so in the status bar.

**Layers 2, 3, 4, 5 and 6 code-complete.** All of Layer 2's eight acceptance criteria and all of
Layer 3's eight pass. The one thing no test can
reach - that `Win32ComSession` drives real Outlook - is a single `xfail(run=False)` plus a
manual `app.cli extract --mailbox`. `VERSION` stays at 0.3.2 until that has been run once.

### Fixed — CRITICAL: every PST was silently skipped, always

**`_classify` used `None` to mean two different things.** It was the "this file
has not changed, skip it" answer. It was *also* the perfectly ordinary "changed,
but there is no content hash" answer - which is what every file read through
another application returns, because its bytes are not what gets parsed and it
may be held open. Every `.pst` and every `.ost`, therefore, was classified as
**unchanged on the very first run, before it had ever been indexed**, and never
indexed at all.

It counted as `unchanged`, not `skipped`. No error appeared anywhere, nothing was
written to the skip ledger, and the run reported complete success. The symptom
was `{'seen': 7, 'indexed': 0, 'unchanged': 6, 'skipped': 0}` and a user saying
"I am not sure I have ever seen the PST extract work" - which was exactly right.

The sentinel is now a distinct object with a name. `tests/unit/test_index_freshness.py`
pins it from both directions: an archive is indexed the first time it is seen,
and an untouched archive is never opened again.

### Fixed — five bugs from the first time a human opened the window

Every one of these was invisible to 900 passing tests, because every one needed
a display, a light-mode machine, or a maximised window.

- **Indexing from the GUI was impossible.** The memory ceiling was absolute, and
  the GUI starts above 1.2GB before reading a file - Qt, the ONNX runtime and
  both stores are already resident where the CLI starts at ~200MB. So it paused
  on its first check at 1,597MB and never resumed: `seen: 1, indexed: 0` after 96
  seconds of nothing. **The ceiling now applies to growth above a baseline taken
  when the run starts**, which is also the number that was always meant: not
  "how big is this process" but "is indexing running away". An unknown baseline
  falls back to the absolute figure, which is the safe direction.
- **`RuntimeError: wrapped C/C++ object of type WorkerSignals has been deleted`.**
  `QThreadPool.start()` owns the runnable on the C++ side, but nothing on the
  Python side held the signals object; once the local variable went out of scope
  Python collected it and the still-running worker emitted into a corpse. Every
  worker now goes through `workers.run()`, which retains it until it reports
  itself done, and a test fails if any view calls `pool.start()` directly. It
  only bit when work outlived the function that started it, so it looked
  intermittent and unrelated to anything.
- **The indexing progress bar spun forever.** `start()` was called with no total,
  so the range was set to `(0, 0)` - Qt's indeterminate animation - and the value
  was only ever set `if self._total_estimate`, which was zero. It now grows its
  denominator from what the walker has found so far, because a true total cannot
  be known before the walk finishes.
- **The indexing page showed nothing about the index.** Opening it answered none
  of "is there an index, how big, how old", which is the only reason to open it.
  It now carries a totals line, refreshed on every visit and after every run.
- **Maximising broke the layout.** The skipped-files scroll area took all the
  stretch while its contents were hidden, so a maximised window was mostly empty
  panel with the controls squashed at the top. It now claims space only when it
  has something to show.

### Fixed — the app was dark on a light-mode machine

Hardcoding a dark palette is not a style choice, it is a bug: the application
looked like it belonged to a different operating system, and on a bright screen
it is harder to read rather than easier.

`app/ui/theme.py` holds two palettes with identical token sets - a test fails if
they drift - and the sheet is written against tokens, since Qt stylesheets have
no variables. The OS preference comes from `QStyleHints.colorScheme()` and is
followed live, so flipping the Windows switch changes the window without a
restart. Settings offers Follow Windows / Always light / Always dark.

The `highlight` token needed genuinely different values rather than one shared
colour: search-term yellow on white is nearly invisible, which is the whole
reason these are two palettes and not one with a flag.

### Fixed — noise on top of a real failure

At 12GB the machine was swapping and SQLite came back with nulls, which produced
`int() argument must be ... not 'NoneType'` and three `ERR_UNEXPECTED` reports
**per keystroke** about a store that was closing. `generation` returns 0 for a
missing or NULL value, and the search engine checks `store.is_open` before
reading - a background search outliving the window is expected, not a bug worth
a traceback.

### Fixed — the progress line stopped updating whenever anything was logged

Reported as "it stopped printing or giving indication it is working", with a
warning about an unreadable `.vsd` immediately above.

A `\r` progress line and a logger writing to the same console destroy each
other: the warning lands on top of the line, the next carriage return overwrites
the warning, and what remains is a mangled line that never changes again. From
the outside that is indistinguishable from a hung process, which is exactly the
thing the heartbeat was added to prevent.

`ProgressLine` now owns the console. Anything logged goes through a sink that
wipes the line, lets the message land on its own row, and repaints - the same
discipline `pip` and `apt` use. No curses and no ANSI cursor codes: one carriage
return and some spaces, which behaves the same in a plain console, in Windows
Terminal, and when piped to a file (where it disables itself entirely).

Two traps, both found by shipping it and looking at the output:

- `setup_logging` clears every handler, so the progress sink has to be added
  *after* it. The other way round removes the sink silently and the warnings
  disappear altogether - worse than the mangled line.
- **`setup_logging` is also idempotent**, because both the CLI and the UI call
  it, so reconfiguring for the progress line needs `force=True`. Without it the
  original INFO console sink survives, the progress sink is added on top, and
  every line prints **twice** - once by the handler that respects the progress
  line and once by the handler that walks straight over it. Which is exactly
  what the first attempt did.

- **Java's log4j complaint is quietened.** mpxj ships log4j-api with no binding,
  so the JVM printed `main ERROR Log4j API could not find a logging provider`
  straight to stderr the first time it read a `.mpp`. Harmless, and it appears
  mid-run looking exactly like a failure. Three `-D` properties are passed on JVM
  start; an unrecognising JVM ignores them rather than refusing to start. Not
  verifiable here without a real `.mpp`, so it is best-effort by design.

### Fixed — the run summary was reporting two different units as one number

From a real run: `seen: 8, indexed: 17, unchanged: 335`. Every number is
correct and the line is unreadable, because a `.pst` is one **file** and
hundreds of **documents** and both were being called the same thing.

- `seen` counts files; `indexed` counts documents. Said so now.
- **`unchanged_documents` split out from `unchanged`.** "335 unchanged messages
  inside one changed archive, 17 rewritten" is a completely different story from
  "335 unchanged files", and only one of them was true. The new number is also
  the one that shows per-message indexing paying for itself.
- **`bytes_read` reported 0.0 MB for a 64-second run over 100MB.** Bytes were
  credited when a file's first document was *written*, so an archive whose first
  message happened to be unchanged reported nothing for the entire file. They
  are credited on arrival now, written or skipped.

### Added — Visio and Project, without COM

Three formats, three honestly different answers, because pretending otherwise
would be worse than the gap:

| | what it is | what we get |
|---|---|---|
| `.vsdx` / `.vsdm` | a ZIP of XML, like every modern Office format | **all shape text, per page** |
| `.vsd` | a 2003 OLE compound binary | title, author, subject - and the name |
| `.mpp` / `.mpt` | proprietary, no open specification | title, author, subject - and the name |

- **No COM, and it was not needed.** `.vsdx` is a documented OPC package; the OLE
  summary stream in `.vsd` and `.mpp` is a documented structure `olefile` reads
  in pure Python. COM would additionally have required Visio and Project to be
  *installed*, which on an indexing machine they generally are not.
- **The `vsdx` package is optional.** When it is absent the page XML is read
  straight out of the ZIP - a diagram is mostly labels, and losing them to a
  missing optional dependency would be a poor trade. That fallback is not
  theoretical: it fired on the first test fixture and is what made it pass.
- Labels split across styled runs are rejoined with `itertext()`. Visio splits a
  label the moment any of it is bold, and reading `element.text` alone truncates
  at exactly the tag number somebody would search for.
- **A file we cannot read is still worth indexing.** `.vsd` and `.mpp` produce a
  document of their filename plus summary properties, carrying a warning that
  says *why* nothing inside is searchable and, for `.vsd`, that re-saving as
  `.vsdx` fixes it. A plan indexed by name comes back when you search for the
  project; one the app has never heard of does not exist.
- **`.mpp` contents are reachable at a price.** `mpxj` reads them properly but
  bundles 32 JARs and needs a JVM and JPype - against this application's whole
  premise. The hook is present and guarded; `pip install mpxj jpype1` turns it on.
- A test asserts `diagrams.py` imports no `win32com`, `pythoncom` or `comtypes`.
- **`doctor.py` reports what each format can actually do here** - "full shape
  text", "full task list (mpxj)", "name + document properties" - because the
  difference is invisible until somebody searches for text they know is in a
  diagram and finds nothing. It flags only states naming something installable:
  `.vsd: name + document properties` is the *best achievable* outcome for a
  format with no open specification, and reporting it as a problem would send
  somebody installing a package that changes nothing.
- **The mpxj integration was wrong and is now verified.** The first version
  imported `net.sf.mpxj.reader`; mpxj moved to `org.mpxj` around version 14, so
  it would have failed on every modern install - silently, behind a broad
  `except`, reporting the plan as merely unreadable. Both packages are tried,
  and it was tested against mpxj 16.7.0 with a real JVM rather than assumed.

### Fixed — two things that would not have survived 200,000 emails

Prompted by "my PSTs have possibly 200K+ mails". Both are invisible at test
scale and fatal at real scale.

- **`_prune_missing` materialised every row** - all 200,000 `FileRecord` objects -
  to filter down to the few thousand real files, at the end of every run.
  `iter_files(source_kind=...)` filters in SQL now, and only the ids to delete
  are held. Deleting while iterating a cursor over the same table was also
  quietly unsafe; the ids are collected first.
- **`merge_contained_entities` was O(n²) with a query per pair.** On a corpus
  that size the entity table runs to tens of thousands of rows - billions of
  comparisons - and the symptom is an index run that appears to hang at the very
  end, which is the hardest kind of failure to diagnose. Entities are bucketed
  by first word, which is where containment that matters actually lives
  ("AVEVA Group" inside "AVEVA Group Limited"), with a cap for any word that
  begins thousands of names.

### Added — finding a file by its NAME (schema v4)

`chunks_fts` indexes what documents *say*. **Nothing indexed what they are
called**, so a file named `Invoice 2024.pdf` whose contents never used those
words could not be found at all - which is how most people look for most files.
No existing test could have caught it: every one of them asked about content.

- **`files_fts`, tokenised with `trigram`**, so "voice" finds "Invoice". Filename
  search *is* substring search - people type the middle of a name and expect a
  hit - and a word tokeniser cannot do that at any price. Falls back to
  `unicode61` with prefix indexes if trigram is somehow unavailable.
- **A Files tab** (`Ctrl+P`) and **`app.cli files`**. No embedding, no reranking,
  no snippets: one FTS5 lookup over a table of filenames, fast enough to run on
  every keystroke. Rows show size and age, because "yesterday" answers "is this
  the one I was working on" and a timestamp requires arithmetic.
- **Mail is deliberately absent.** A message's key is synthetic and mail would
  outnumber documents ten to one; mail is searched from the search tab.
- A file indexed **by name only** - a scanned PDF, something locked - says so in
  its row rather than being hidden. "I can see it but cannot search inside it"
  is real and useful, and hiding it invites the same fruitless search twice.
- The migration backfills from the existing index, so no re-index is needed, and
  it is safe to re-run: an FTS5 table rejects a rowid it already holds, so a
  plain INSERT would fail and leave the database stuck between versions.

### Added — scope chips: Everything / Mail only / Documents only

A *filter*, not a mode. You should never have to decide whether a thing was an
email or a document **before** typing, because the usual answer is "I do not
remember, that is why I am searching".

`source_kind` was already a column and `from:` was already an operator, so this
is a WHERE clause rather than a second search path. `ParsedQuery.scoped()`
returns a copy - the class is frozen so it can be a cache key - and **the scope
is part of that key**: without it "All" and "Mail" share an entry for the same
typed text and whichever ran first answers for both. Loose `.eml` files count as
mail, because that is what they are to the person searching.

### Fixed — per-message indexing had made embedding much slower

Reported from a real run: "this method is very slow compared to the other".
Correct, and a regression from the streaming change. Embedding happened once per
document, so an email meant a batch of about three chunks - the size at which
ONNX spends its time on per-call overhead rather than on matrix work.

Embedding now batches **across** documents (`EMBED_BATCH = 256`) in the consumer.
A file is marked INDEXED only after its vectors are written, never before: the
reverse leaves it invisible to semantic search and never retried. A test counts
embedding calls rather than measuring elapsed time - flaky, and it would not say
why.

### Changed — extraction streams, and the unit of work is a document not a file

Found because a 100MB `.pst` took six minutes and showed nothing. `app.cli extract`
returned in seconds, which located the cost precisely: reading the archive was
never the problem.

```python
documents = list(extract(candidate.path))   # the ENTIRE archive, in memory
```

Every message and every attachment's extracted text was materialised before a
single chunk was made - then all the chunks, then **all the embeddings in one
call**. Nothing written, nothing committed and nothing on screen until the whole
archive finished. A 3GB archive would have exhausted memory rather than finishing
slowly.

`_extract_stream` is a generator yielding one `_Extracted` per document, so:

- **memory is flat** - one message at a time rather than a whole archive;
- **work commits as it goes** - an interrupted archive keeps what it read;
- **progress is visible** - `current_item` counts messages, so the screen shows
  `reading 2007.pst [1,284] 94s` instead of nothing;
- **a search result names the email**, not the `.pst` it lives in;
- **re-indexing a changed archive re-reads only what changed** - by a hash of the
  message *text*, because an archive's bytes move whenever Outlook opens it while
  a fifteen-year-old email does not change at all. One new message in 30GB now
  costs one embedding.

Three bugs caught while making it work, each by a test written to fail first:

- **The archive lost its own `files` row**, so the walker had nothing to compare
  against and would have re-read the whole thing on **every run, forever** -
  destroying the exact property incremental indexing exists for. Streams now
  close with a marker row carrying the container's size and mtime.
- **An extractor that forgets `virtual_path` silently overwrote every message
  onto one row**, leaving the archive as a single entry holding only its last
  email, with no error anywhere. Duplicate keys are now made unique and logged
  by name - losing mail to an extractor bug is far worse than an ugly key.
- `stats.bytes_read` counted the archive's size once per message, making the
  throughput figure meaningless.

### Fixed — the resource governor could deadlock the run it was protecting

Found while a real 100MB PST refused to finish. Both bugs were introduced by the
governor added earlier the same day, and both present identically: an index run
that never ends and looks merely slow.

- **The pause happened in the consumer - the only thread that drains the results
  queue.** While it waited, the extraction workers blocked handing over results
  they were still holding in memory. So memory never fell, so the memory pause
  never cleared, and the run hung permanently. **Backpressure belongs at the
  intake, never at the drain**: the waiting moved to `_produce`, which holds
  nothing but a path, so pausing it starves the workers of new work while
  everything already in flight keeps draining - which is what actually brings
  memory down. Three tests drive the governor permanently over its ceilings and
  assert the run still finishes.
- **The indexer counted its own CPU as a reason to stop.** Four workers on a
  four-core laptop saturate the processor by themselves, so it paused, watched
  CPU fall, resumed, spiked and paused again - throttling itself to a crawl on a
  completely idle machine. `Snapshot.other_cpu_percent` subtracts this process's
  own share first (dividing by the core count, because `Process.cpu_percent` is
  per-core while `cpu_percent` is already averaged). Its own load was always
  handled better by below-normal priority anyway.
- **A third bug, caught by the test written for the second:** with the wait moved
  to the producer, a full disk ended the run *silently* - complete success, zero
  files indexed, no error. The same shape as the sentinel bug from earlier the
  same day, which is why it was worth writing the test that could only fail.

### Fixed — a run over few large files showed nothing at all

- **Progress fired every 50 files.** A folder of ten documents plus one 100MB mail
  archive never reaches fifty, so the callback fired exactly once, at the end. The
  screen stayed blank for the whole run, which is indistinguishable from a hang -
  and the correct response to a hang is to kill it. Now checkpoints on **two
  seconds or fifty files, whichever comes first**.
- **A 100MB archive is a single file**, so nothing reaches the consumer until the
  whole thing is parsed and even a time-based tick showed nothing during the
  slowest part. `IndexStats.current` now carries the file being read and how long
  it has been on it, redrawn in place rather than scrolling.

### Added — the indexer is now configurable and stays out of the way

Prompted by "this is designed to run on a working machine". An indexer that makes
Excel stutter gets switched off and never switched back on, and then none of the
rest of this matters.

- **`app/index/resources.py`** - four ceilings, every one configurable, every one
  a ceiling rather than a target. **Memory** pauses and drains rather than
  aborting; **CPU** pauses while the machine is busy, with hysteresis so a
  transient spike does not stall the run; **battery** pauses until mains;
  **disk** is the only one that stops, because it is the only failure that
  damages something outside this application and does not resolve itself while
  the indexer keeps writing. The whole decision is a pure function of a
  `Snapshot`, so every threshold and recovery path is tested with invented
  numbers - no test has to exhaust a real machine's memory.
- **Below-normal CPU and background I/O priority**, applied before the first file
  is read. The cheapest courtesy available and the most effective.
- **Default workers is now half the cores, capped at four** - not `cores - 1`,
  which on an 8-core laptop handed seven cores to a background task and left one
  for the person. Past four the disk is the wall anyway.
- **`app/index/schedule.py`** - manual / on launch / every N hours / daily at a
  time. Pure arithmetic on two timestamps, because scheduling bugs are the ones
  that never reproduce: a laptop opened after a fortnight away indexes **once**,
  not fourteen times, and a clock moved backwards does not trigger a second run.
  A `MIN_GAP_S` backstop means a bug here cannot become a machine that never idles.
- **`app/ui/scheduler.py`** - a one-minute `QTimer` and one guard: a scheduled run
  never starts on top of a run already going. The single-instance lock protects
  the database from a second *process*; nothing protected it from this
  application starting a second run over its own, which is the mistake a timer
  makes at 02:00 with nobody watching.
- **An Indexing panel in Settings** and `--memory-mb`, `--cpu-percent`,
  `--full-speed` on `app.cli index`. `--full-speed` is named for what it costs.
- `IndexStats` now reports `paused_seconds` and `pauses`, because a four-hour run
  that was mostly *waiting* looks identical to one that was slow, and the fix is
  the opposite in each case.
- `psutil==7.1.3` pinned - verified to publish a `cp37-abi3-win_amd64` wheel, so
  no compiler. Every import is guarded: without it the disk guard and worker cap
  still apply, and `doctor.py` reports the ceilings as INACTIVE rather than
  letting somebody believe in a limit that is doing nothing.
- **`app/ui/indexing_settings.py`** extracted, because `settings_view.py` had
  reached 342 lines. The view-length guard now globs every view module instead of
  checking a hand-written list - that drift is exactly what it exists to catch.

### Added — Layer 6, the knowledge graph
- **Schema v3**: `entities`, `entity_mentions`, `entity_edges`. Additive, like v2, and for
  the same reason - the graph is derived entirely from `chunks`, so it is built at leisure
  on an existing index without re-reading a single file, and `--rebuild` is always safe.
- **`app/graph/cooccurrence.py`** - the default, and deliberately not an LLM. Emails,
  filenames, acronyms and capitalised n-grams, with edges weighted by **normalised PMI**.
  Raw co-occurrence produces a hairball centred on the commonest word in the corpus; PMI
  asks whether two things appear together *more than chance predicts*, which is the
  question someone drawing the diagram by hand would ask. Normalised because raw PMI's
  ceiling depends on how rare a pair is, so a threshold chosen today silently stops
  filtering as the index grows.
  Pure functions throughout, so the part that decides what counts as a thing is tested
  with literal strings on any machine.
- **`app/graph/builder.py`** - resumable two-pass build. Two passes because an edge's score
  depends on corpus-wide totals that do not exist until the last chunk has been read.
  **The cursor is committed in the same transaction as the batch it describes**, which is
  not tidiness: edge weights accumulate, so a replayed batch double-counts with no error
  and no way to detect it afterwards.
- **`app/graph/render.py`** - networkx metrics (centrality, sampled betweenness, community
  detection) and a self-contained pyvis page. Capped at 5,000 nodes, and the page **says**
  it is capped - a missing node otherwise reads as evidence that nothing connects there.
- **`app/graph/entities_llm.py`** and **`app/llm/ollama.py`** - optional typed extraction.
  It only ever adds and refines, never deletes, so the deterministic graph stays intact and
  enrichment stays reversible. Ollama stopping mid-run is treated as normal rather than
  exceptional: checkpoint, pause with `ERR_OLLAMA_DOWN`, resume exactly there next time.
- **A Graph tab**: entity table, what each connects to, the passages behind it, and
  "search for this entity". The strength of a link is shown in words - "strongly linked" -
  because `0.62` means nothing to anyone who has not read the PMI definition.
- **`app.cli graph`** with `--rebuild`, `--enrich`, `--entity`, `--html`, `--top`.

### Fixed — found by running Layer 6 on Windows, against a real corpus

Two classes of failure, and neither could have been found in development. The first
needed Windows; the second needed documents nobody wrote for a test.

- **THE CRASH: the graph page could not be written on Windows at all.**
  `pyvis.write_html` calls `open(path, "w+")` with no `encoding`, so the file is written
  in the **process locale encoding** - cp1252 on a UK Windows install. The page carries
  `’ — · …` from entity names, from tooltips and from the inlined vis-network library;
  cp1252 can encode none of them, so the render raised `UnicodeEncodeError` after
  building the entire 264KB document. Every `--html` render and four tests died on it.
  Invisible on Linux and macOS, whose default is already UTF-8.
  Fixed by generating the HTML and writing it here with an explicit encoding, which also
  removed a redundant read-modify-write. A test now asserts on the *bytes*. The rest of
  `app/` was audited for the same pattern; this was the only instance.

- **The graph's top 25 entities on a real corpus were English words.** "Connect",
  "Enterprise", "System", "Optimize", "Access", "Learn", "Use", "How", "Customers",
  "Ability", "Slide" - and, because a slide heading is set in capitals, the "acronyms"
  `DATA`, `CLOUD` and `DESIGN`. 442 entities, and the ones that mattered were buried.
  Four separate causes, each fixed and each pinned by a test named for the wrong output:
  - **ALL-CAPS words broke name runs**, so "AVEVA System Platform" fragmented into an
    acronym and a leftover "System". They now join a run; a *lone* capitalised word is
    still an acronym. This one fix removed most of the noise, because most of it was
    debris from a shattered product name.
  - **A `COMMON_WORDS` blocklist** for single-word entities, and for runs made entirely
    of ordinary words. "PI System" and "Customer FIRST" survive - the rule is about what
    a run is *made of*, not about any word appearing in it.
  - **"and" welded separate names together**: "SCADA and MES" became one entity. A
    connector now joins only when the word beside it is Title Case, which is what
    distinguishes "Work and Pensions" from a conjunction between two acronyms.
  - **Possessives made duplicate nodes.** "AVEVA’s" and "AVEVA" sat side by side with
    the same visible label. Both apostrophes are stripped, because a Word document and a
    PDF disagree about which one they use.
  - An imperative opening a sentence ("Discover AVEVA Insight") no longer joins the name.
  - A run of initials no longer forms an entity - a regression the ALL-CAPS change
    introduced and a test caught: "A B C" briefly became the entity "B C".

- **Second pass over the same corpus.** With the ordinary nouns gone, what surfaced
  underneath was slide-bullet grammar: "Provide", "Accelerate", "Ideal", "Operational",
  "Flexible", plus heading numbering ("II") and ALL-CAPS adjectives ("OPEN", "HYBRID").
  - **A lone Title-Case word that only ever opens a sentence is no longer an entity.**
    Its capital is grammar, and a slide bullet is its own sentence. This is evidential
    rather than another blocklist entry, because no list keeps up with the supply of
    verbs - and it costs nothing for a real name, which is mentioned mid-sentence
    somewhere and still collected there.
  - Roman numerals break a name run instead of joining it, so "OPEN HYBRID II" can no
    longer slip past the all-ordinary-words check on the strength of its "II".
  - **`entities.merge_contained_entities()`**: "AVEVA Group" and "AVEVA Group Limited"
    were two nodes joined to each other and to all the same neighbours. A short name is
    now folded into a longer one **only when every chunk mentioning the short one also
    mentions the long one** - the evidence that it is never used on its own. A textual
    prefix test would have destroyed "AVEVA", which is a prefix of the same string and a
    more important entity in its own right; word boundaries stop "PI" being read as part
    of "PIPELINE". It runs before scoring, so the survivor's PMI reflects the merged
    evidence rather than half of it.

### Fixed — found while building the graph
- **The rendered graph page called out to a CDN.** pyvis emits two jsdelivr tags for
  Bootstrap *even with* `cdn_resources="in_line"`. On a machine with no internet those
  requests hang and fail, on a page whose entire premise is that nothing leaves the
  machine - and it renders perfectly on a connected developer machine, which is how this
  would have shipped. Stripped now, with an acceptance test that greps for `https://`.
- **Filenames swallowed whole clauses.** A space-tolerant filename pattern has no
  left-hand delimiter inside prose, so it walked backwards and produced the entity
  "about the HACCP review and attached Pasteuriser Report.docx".
- **The calendar leaked in one full stop at a time.** "Tuesday." is not in the stopword
  list; "Tuesday" is.
- **Connectors counted against the name-length cap**, so "Department for Work and
  Pensions" was rejected as a heading.
- **`_names` closed over its loop variable.** It worked, but by timing - the generator was
  always drained before the variable was rebound. Rewritten as a plain function taking the
  run as an argument.
- **`--strict-markers` was declared twice in `pyproject.toml`**, one block silently
  overwriting the other.

### Added — reading .pst without Outlook
Reassessed after a direct test rather than from the architecture doc's assumption. `pypff` is
not on PyPI, but **`libpff-python` builds and imports**, and the capability is real. The doc was
right about wheels and was being read as "cannot be done".

- **`app/extract/pst_libpff.py`** - reads archives from the file. No Outlook, no COM, no file
  lock, no changes to the user's mail profile - and, uniquely among the approaches tried here,
  **testable on any machine**, which finally puts a floor under the largest untested surface in
  the project. 26 tests drive a fake `pypff`; one more pins the real library's API so a version
  bump that renames an accessor fails in a second rather than inside a 30GB archive.
- **`PstBackend` with `auto` / `libpff` / `outlook`.** `auto` prefers direct reading and falls
  back to Outlook. `.ost` always goes to Outlook whatever is asked: it *is* the Cached Exchange
  Mode file, libpff reads it poorly, and the live mailbox is Outlook's own business. The
  division is libpff for offline archives, Outlook for live mail - each doing what it is
  actually good at.
- **`app.cli convert` and a Settings button** - export an archive to a folder of `.eml`. The
  permanent escape hatch: afterwards the mail needs neither Outlook nor libpff, any mail client
  can open it, and the folder is added as an index root automatically rather than leaving one
  more step to remember.
- **`libpff-python` is deliberately NOT pinned.** It has no Windows wheel and compiles during
  install, which is exactly what the rule at the top of `requirements.txt` exists to prevent.
  Every import is guarded, so its absence changes nothing; `doctor.py` reports which route is
  available and what the other would cost.
- Filenames from message subjects are sanitised - a subject is attacker-controlled text that
  becomes a path, and a test asserts `../../etc/passwd` cannot escape the destination folder.

### Added — Layer 5, the desktop app
- **`app/ui/presenter.py`** - every UI decision that is not drawing, and **it imports no Qt**.
  Widgets cannot be instantiated without a display, so logic inside them could only ever be
  checked by a person clicking; this module holds the snippet windowing, highlight offsets,
  tier selection, ETA phrasing and skip grouping, and 44 tests cover it. Two further tests
  enforce the split itself: one fails if Qt ever appears in the presenter's imports, one if a
  view module grows past 250 lines, because a long view is where untested logic hides.
- `app/ui/workers.py` - `QThreadPool` wrappers so the UI thread never does I/O. Each converts
  what escapes into an `AppError`: an exception leaving a `QRunnable` vanishes, and the UI
  would wait forever for a signal that never comes.
- `app/ui/search_view.py` - two debounce timers, and **generation-tagged dispatch** so a slow
  search landing after newer typing is dropped rather than overwriting fresher results.
- `app/ui/results_view.py` - path, location, why it matched, and a snippet centred on the hit
  with terms picked out. A result whose file has vanished is **marked, not hidden** - that is a
  genuine finding, and dropping it silently would make the count disagree with the list.
- `app/ui/indexing_view.py` - progress **by file count, never by bytes**: measured on the real
  corpus, a 40MB deck yields fewer chunks than a 30KB Word document, so a byte-based bar sits
  frozen and then races. Plus the skipped-files panel, grouped by cause, biggest first.
- `app/ui/settings_view.py` - roots, toggles, `doctor.py --json` rendered inline, and **clear
  search history**, because the usage log is a record of what someone searched on their own
  machine and must be theirs to erase.
- `app/main.py` - the entry point. Settings first, then the single-instance lock **before**
  either store is opened, then the window; models warm on a background thread.

### Added — Layer 4, search
- **Schema v2**: `searches` and `search_hits`, plus a migration so an existing index gains them
  without a rebuild. Built seven layers before anything reads them because this is the one part
  of adaptive tuning that **cannot be added later** - in six months there is no record of what
  was searched or what turned out to be useful. Local, never transmitted, and clearable.
- `app/search/keyword.py` - BM25 over the sanitised expression, with **filters applied in SQL**.
  Fetching 100 rows and discarding 90 to honour `type:pdf` leaves the 10 best of the wrong set;
  filtering in the query makes the top 100 the top 100 *that match the filter*.
- `app/search/vector.py` - one query embedding per search, ANN with eligible file ids pushed
  down as a prefilter. Above 2,000 ids the prefilter is dropped, because an `IN (...)` list
  that long costs more than the search it was meant to narrow.
- `app/search/rerank.py` - cross-encoder over the top 30, lazily loaded, **never able to fail a
  search**. The model is optional and 1.1GB; missing, half-downloaded and deleted-mid-session
  are all handled identically - log once, keep the fused order, succeed. Returning nothing
  because an optional precision step could not load would be worse than never having it.
- `app/search/engine.py` - parallel dispatch, RRF, generation-keyed cache, and the two-tier
  design: BM25-only while typing, full hybrid on Enter. `SearchResult.explain()` says *why*
  something matched, because trust comes from being able to ask.
- `app.cli search` with typed operators, `--limit` and `--no-rerank`.
- 42 Layer 4 acceptance tests against a real index, including a golden set with recall@10 as a
  regression guard.

### Added — Layer 3, the indexing pipeline
- `app/index/walker.py` - roots, allowlist, exclusions, change detection.
  **Exclusions prune during the walk, never filter after it**: descending into a 40,000-file
  `node_modules` and discarding it costs the whole subtree, so a test counts the directories
  actually visited rather than the output - a filter-afterwards implementation produces
  identical output and takes a hundred times longer.
  **The hash decides; mtime only decides whether to hash.** robocopy, a restore from backup,
  cloud sync and archive extraction all reset mtime while leaving bytes identical. Trusting
  mtime alone would re-index the whole corpus every time any of those happened, and a test
  touches a file and asserts it is *not* re-indexed.
- `app/index/embedder.py` - batched FastEmbed wrapper, lazily loaded, with three guards for
  things that otherwise fail in silence: a dimension mismatch (what changing `EMBED_MODEL`
  looks like), an un-normalised vector (raises nothing, ranks wrong forever), and a batch that
  returns the wrong number of vectors (which would pair chunks with other chunks' vectors).
  The encoder is injectable, so all of it is tested with no model on disk.
- `app/index/pipeline.py` - bounded priority queue, N extraction workers, one embed-and-write
  consumer. Embedding is deliberately not parallelised: ONNX already uses every core inside one
  call. **Both queues are bounded** - without backpressure the process dies of memory around
  hour three having written nothing.
  **Resumability is the `files` table, not a saved position**: a restart re-walks and skips
  what is already `INDEXED` for the cost of a `stat()`, which is more robust than an offset
  that goes wrong the moment the corpus changes underneath it.
  **Chunks and vectors are written before the file is marked `INDEXED`** - a crash between them
  leaves a file that looks unfinished and gets redone, where the reverse would leave it marked
  done with no chunks, invisible to search and never retried.
- `app.cli index` - Layer 3's entry point, with `--first` for folder prioritisation (repeatable
  and ordered), `--fast`, `--no-prune`, `--include-cloud`. Takes the single-instance lock,
  because unlike `extract` it writes.
- 79 Layer 3 tests: 30 walker, 19 embedder, 25 acceptance, 9 CLI.

### Added — Layer 2
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
- **`app.cli extract`** - Layer 2's headless entry point, which ground rule 7 requires and the
  layer had shipped without. Files or folders, `--chunks` to see every chunk with its page,
  token estimate and offsets, `--text` for the whole document, `--json` for a machine-readable
  dump, `--limit` when pointed at something large. Read-only by construction: it opens no store
  and writes nothing, so it is safe to point at anything. Cloud placeholders are checked
  **before** the file is opened, because opening one is what triggers the download; a test
  asserts it. Prints per-file timing and an overall MB/s - the first real input to the
  throughput question Layer 3 has to answer.
- 23 tests for the command, including one asserting it never opens a store.
- **`app/extract/email_pst.py`** - Outlook archives and the live mailbox, via MAPI.
  **The COM calls are confined to `Win32ComSession`**; the walk, the conversation grouping,
  the attachment dedup and every error path work against small duck types and are tested with
  a fake on any machine. Only the adapter needs Windows, which is the smallest untested
  surface this could have had - the alternative was shipping the whole thing unverified.
  Identity is the `EntryID`, never a folder path: moving a message between folders must not
  make it look like a new one, or an index over a mailbox people reorganise never settles.
  Attachments are **deduplicated by content hash**, and the hash set is the caller's, so
  Layer 3 persists it in `files.content_hash` and dedups across runs rather than within one.
  `Deleted Items`, junk and sync-conflict folders are skipped by default. A `com_error` in one
  folder becomes `ERR_OUTLOOK_BUSY` and the walk continues - by the time Outlook gets closed,
  thousands of messages may already have been read.
- `app.cli extract --mailbox` - walk Outlook rather than a path, reporting counts per store,
  attachment totals and any folders that could not be read. Still writes nothing.
- 27 PST tests driving a fake MAPI session, plus acceptance criteria 5 and 6 implemented
  against it in `test_layer2_acceptance.py`.
- **PPTX now reads grouped shapes, tables and charts.** `slide.shapes` yields top-level shapes
  only, and a group is one opaque shape with no text frame - so reading `has_text_frame` alone
  silently lost every word inside every group, and grouping is how slides get built. On a
  deck-heavy corpus that was not an edge case, it was most of the content. Tables and charts
  had the same problem: a comparison table and a chart's category labels are exactly what
  people search for, and neither has a text frame. A test reconstructs the old logic and
  asserts it finds none of it, so the fix cannot quietly rot.
- **A picture-heavy deck is now flagged.** A PDF with no text is skipped outright, but a deck
  always has a title, so it indexed "successfully" while most of its content stayed
  unsearchable. Below 400 characters per megabyte it now carries an `ERR_NO_TEXT_LAYER`
  warning and is still indexed - the PPTX analogue of a scanned PDF.
- `app.cli extract --out PATH` writes the JSON itself, in UTF-8. Windows PowerShell 5.1's `>`
  redirection emits UTF-16LE with a BOM, which every JSON reader then rejects - the same
  encoding trap that killed `install.ps1` at parse time.
- Skipped files now carry their size, and the summary separates bytes *seen* from bytes *read*.
  A 100MB archive that vanished from the totals because it was skipped made them a lie.
- `extract-msg==0.56.1` in `requirements.txt`.

- `doctor.py` now checks **Git on PATH**. It never did, despite `install.ps1` installing Git and
  the whole release process in `docs/VERSIONING.md` depending on it. Optional, because the app
  runs fine without it - `build_info()` degrades to a version with no commit - but every
  convention silently stops working and the failure surfaces as "git is not recognized" long
  after the installer said it was done. The usual cause is not a missing install but a
  PowerShell window opened *before* Git was installed, which keeps its stale PATH until closed,
  so the fix leads with refreshing PATH in place rather than reinstalling.

### Removed
- `ERR_PST_NOT_BUILT`. It existed for one afternoon to make the gap visible; PST is built, so
  a `.pst` now either indexes or fails for a real reason. A code nothing raises is a lie in the
  registry.

### Fixed — caught while building the libpff backend
- **Every PST message would have had no recipients.** `To` and `Cc` header values were joined
  with a space before parsing. `email.utils.getaddresses` was hardened against malformed input
  (CVE-2023-27043) and now returns *nothing at all* rather than doing its best - so the whole
  recipient list came back empty, silently, for every message in every archive. Joined with a
  comma now, and a test asserts three addresses across two headers.

### Fixed — found by indexing a real folder containing a real .pst
The GUI reported `seen: 7, indexed: 0, unchanged: 6, skipped: 0` and success. The seventh file
- an Outlook archive - was seen, then vanished from the accounting entirely. Three faults, in
the order they compound:

- **One unreadable file ended the entire walk.** A `.pst` that Outlook holds open cannot be
  read, so `content_hash()` raised `PermissionError` - **on the walker thread**, where one
  escaping exception abandons every file not yet reached. It was caught by a handler that
  logged a single line and let `run()` report success. This is a direct violation of
  non-negotiable #4, *one bad file never halts a batch*, in the one place that rule matters
  most. `has_changed()` now returns "changed, unhashed" instead of raising, `_classify()`
  cannot raise at all, and a walk that really does stop early sets `stopped_early` so the run
  says so rather than claiming to have finished.
- **A `.pst` should never have been byte-hashed.** It is read through Outlook, which holds the
  lock, and its bytes are not what gets parsed. Extractors now declare `reads_externally`, and
  those files are change-detected on mtime and size alone.
- **COM was being used from worker threads without `CoInitialize`.** COM is per-thread; without
  it `Dispatch` fails everywhere except the main thread. That is exactly why
  `app.cli extract --mailbox` worked from the command line while indexing the same archive from
  the GUI did nothing - the difference was never Outlook, it was which thread asked.

### Fixed — found by the first run on real Windows
Four failures that Linux hid. Three were real bugs; the platform difference is the point.

- **The search cache never invalidated.** `generation` is a `@property`, and the engine called
  it as `generation()`. The `TypeError` landed in a broad `except` that fell back to `-1`, so
  every search keyed on the same value and **stale results would have been served forever** -
  including hits on text that had just been deleted. The exact failure the test was written to
  catch, hidden by the exception handler meant to make the cache robust. The fallback now logs
  loudly: a cache that cannot tell it is stale is a lie, and it must say so.
- **Locked files re-queued themselves mid-run.** `_candidates()` yielded the walk, then queried
  the store for previously-locked files - lazily, so by the time it ran, files *this run* had
  just marked locked were already in the results. Each was retried immediately, while its lock
  was by definition still held: double the work, double-counted skips. The retry list is now
  snapshotted before the walk begins.
- **As-you-type could not match the word being typed.** Every term was quoted exactly, so
  someone typing "pump st" searched for the literal word "st" and got nothing. The interim tier
  - the one whose entire purpose is to feel instant - stayed empty until the moment a word was
  finished. The last term is now a prefix, but only for that tier: turning every term into a
  prefix would make "cat" match "catastrophe" in a committed search.
- **An edit inside the filesystem's timestamp resolution was invisible.** Two writes in one tick
  produce identical mtimes; if the edit also preserves the size - an overtype, a corrected
  figure - the cheap tier said "unchanged" and the new contents never reached the index.
  Silently, permanently. NTFS and the Windows clock are coarser than ext4's, so the window is
  real on the target platform and absent on the development one. Files modified within two
  seconds are now always hashed, which during an index run is approximately none of them.
- `pytest`'s `--basetemp` sits inside the project, so pytest tried to collect its own scratch
  directory. `norecursedirs` now excludes it.

### Fixed
- Three wiring bugs found by reading the UI back rather than running it: `clicked` and
  `triggered` emit a `bool`, so binding a keyword-only slot to them would have raised
  `TypeError` the first time anyone pressed the button or F5; `finished` was connected inside
  the start handler, so the tenth index run would have refreshed the status bar ten times; and
  index roots were never persisted, so every restart forgot which folders to index. Settings
  that vanish on restart are not settings - they now live in `index_state`, with the index they
  describe.
- **The search cache handed out its stored object and then mutated it.** Stamping `from_cache`
  and `elapsed_ms` on a cached `SearchResponse` changed what every earlier caller was still
  holding, so a response somebody got two searches ago would silently start claiming it came
  from a cache it had not. diskcache pickles and so returns a fresh object; an in-memory cache
  does not, and the engine must not depend on which it was handed. Found by an acceptance test.
- **`request_stop()` did not stop anything.** The flag was set but the consumer loop never
  checked it, so a run continued to completion after being asked to stop - the UI's pause
  button would have done nothing, and the disk guard only worked by accident. Found by an
  acceptance test that stopped a run halfway and got a complete one.
- **The prune step never ran.** It was guarded on the same event that `run()`'s cleanup always
  sets, so "did we stop early?" was permanently true and deleted files were never removed from
  the index. Two meanings had been folded into one flag; they are now separate, and the
  distinction matters: an interrupted walk has not seen the whole corpus, so pruning after one
  would delete perfectly good rows.
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
- **`app.cli extract PATH --json` failed** with "unrecognized arguments". `--json` and `--env`
  were declared on the parent parser, which in argparse means they are only accepted *before*
  the subcommand - not the order anyone types. Both now work either side, via a shared parent
  with `default=argparse.SUPPRESS`; without SUPPRESS the subparser writes its own default over
  the already-parsed global and `--json extract` silently stops being JSON.
- **The test suite crashed after passing.** Every test went green, then pytest raised
  `PermissionError: [WinError 5]` from `pytest_sessionfinish` while cleaning its temp root: it
  keeps a `pytest-current` junction under `%LOCALAPPDATA%\Temp`, and creating or resolving a
  junction needs a privilege a normal Windows account may not have. The suite passed and the
  process still exited non-zero. `--basetemp=.pytest_tmp` in `pyproject.toml` sidesteps the
  junction entirely and puts scratch I/O on the project's own drive.
- A binary file with a text extension was skipped with `ERR_NO_TEXT_LAYER`'s default advice,
  which talks about scanned documents and OCR - nonsense for a renamed database. It now carries
  advice about the extension instead. An error giving the wrong fix is worse than one giving none.

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

### Docs
- `BUILD_SPEC_V2.md` → **2.3** (Layer 2 build notes, the CLI entry point, acceptance boxes
  ticked and the two PST criteria left visibly open), `CHANGELOG.md` → **1.3**,
  `HANDOFF.md` → **1.2**, `README.md` → **1.3**, `tests/fixtures/README.md` → **1.1**.

### Planned
- Attachment recursion for `.eml` files on disk. PST attachments are extracted; loose `.eml`
  files still only record attachment names.
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
