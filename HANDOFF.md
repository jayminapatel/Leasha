# Handoff

**Doc version:** 4.1 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2

Read this first if you are picking the project up cold - a new machine, a new chat, a new
person, or yourself in three months. It answers: where is it, what works, what is next, and
what will bite you.

`docs/PROJECT_INSTRUCTIONS.md` is the companion: the standing rules for *how* to work here.
This document is the state; that one is the contract.

---

## 1. What this is

**Leasha.** A Windows desktop app that searches ~100GB of local files and Outlook email,
combining keyword and semantic search, driven by a plain-English description of what you are
looking for.

That last clause is the whole scope, and it narrowed deliberately - see §3a. There is no
knowledge graph and no document generation; both were built or planned, and both were removed
because they were not what the tool is for.

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

**Version 0.3.2. Layers 0-6 code-complete. 938 tests passing, 1 xfailed (real-Outlook COM, deliberately).**

| Layer | What it is | State |
|---|---|---|
| L0 | Foundation: config, errors, logging, single-instance, CLI | **Done** |
| L1 | Storage: SQLite/FTS5, LanceDB, migrations | **Done** |
| L2 | Extraction: PDF, Office, plaintext, Outlook/PST, chunking | **Code-complete** - one manual check left, see below |
| L3 | Indexing pipeline: walker, workers, resumable cursor | **Code-complete** - needs a real-scale run |
| L4 | Search: BM25 + ANN, RRF fusion, rerank, filters | **Code-complete** - and now measured, see §3b |
| L5 | PyQt6 UI shell | **Code-complete** - opened, and eleven bugs found by opening it |
| ~~L6~~ | ~~Knowledge graph~~ | **Removed** - §3a |
| ~~L7~~ | ~~Office document builder~~ | **Cancelled** - never requested, never started |
| **L8a** | Natural-language query translation | **Code-complete** - justified by measurement, §3b |
| L8b | Prose answers over results | **Deferred** until L8a has been used in anger |
| L9 | Hardening and packaging | Not started |
| ~~L10~~ | ~~Adaptive tuning~~ | **Cancelled** - speculative |

### What is **Next**

In order, and the first two need the owner rather than the code:

1. **Open the window and use it.** Every UI fault in this project was found by somebody
   clicking, never by a test. Eleven so far.
2. **Index one full 200K-message PST.** The only thing that has never been done at scale, and
   the one that will find what the fixtures cannot.
3. **Decide chunk size and model precision** - §7, question 1. Both are far cheaper now than
   after 100GB is indexed, and both should be settled by `app.cli evaluate` before and after
   rather than by argument.
4. **Layer 9**: hardening and packaging.

The file-type work order is **complete** - all six steps. OpenDocument and Google Drive
pointers read natively, Tier 2 converters cover a dozen dead Office formats through
LibreOffice and pandoc, OCR reads images and scanned PDFs, and Settings has a checkbox per
type. `app.cli formats` prints what is on and what is off.

## 3a. The scope change, and what would reverse it

**This section matters more than any code in the repository.** It records a decision that
cost two layers, and the reasoning is what stops it being re-litigated or accidentally undone.

The owner, asked what the tool was for:

> "Local search is my main objective, but I want to write in normal text what I am looking
> for, and this complexity to solve may need AI."

Everything follows from that sentence.

**The knowledge graph was removed.** It worked - all four acceptance criteria passed - and it
was never in the search path, nothing depended on it, and the only user did not want it. A
feature that is finished, tested and unwanted still costs maintenance forever. Git preserves
it at tag `v0.3.2`.

*What would justify reviving it:* somebody asking "who else was involved in this?" or "what
else touches this project?" repeatedly, and search not answering it. Entity co-occurrence is
a genuinely good answer to that question - it was simply not the question being asked.

**Layer 7, the Office document builder, was cancelled.** It was a 225-byte stub. Nobody had
ever asked for it; it was in the plan because the plan was written before the purpose was
clear.

*What would justify building it:* a repeated need to get results *out* - into a report, a
spreadsheet, an email. Until somebody asks twice, exporting is a feature looking for a user.

**Layer 10, adaptive tuning, was cancelled.** Ranking that changes based on what you clicked
is unpredictable ranking, and this application's entire value is that you can trust what it
returns. The usage log still records searches, so the option remains open.

**Layer 8 became 8a and 8b, and 8b is deferred.** Translating a sentence into a query is
cheap, safe and testable. Generating prose answers is none of those things, and a 7B model
stating something wrong confidently over technical material is worse than no answer.

*What would justify L8b:* L8a in daily use, and the owner saying "now I want it to just tell
me". Not before.

**The three `entities` tables stay in the schema, empty and commented as deprecated.**
Dropping them needs a migration to v5, and migrations only step forward - so reviving the
graph would then need a v6 to undo the v5, and the database would carry a permanent record of
a decision that was reversed. An empty table costs nothing. Drop them at L9 if it still seems
worthwhile.

## 3b. What search actually does, measured

Twenty sentences against a corpus with known answers, run by
`app.cli evaluate --builtin`. **This is the first time search itself was measured rather than
its parts.** Two faults surfaced that a thousand passing unit tests had not, because every
test asked "does this function return what I expect" and none asked "does search work".

Nineteen of twenty plain sentences returned **zero results**. Every term was ANDed, stopwords
included, so `drawings of the pump station` required the document to contain "of" and "the".

Fixed, the split is the finding - and it is what justifies L8a:

| at rank 1 | plain sentence | translated |
|---|---|---|
| overall | 50% | **75%** |
| topic only | 88% | 88% |
| with a constraint | 50% | **92%** |
| sender | 50% | 100% |
| recipient | 0% | 100% |
| attachment | 0% | 100% |

Topic matching is decent; constraints are ignored entirely. "From Chris" goes into the text
search and the sender field is never consulted. Translated to `from:chris`, it is a filter.

**The numbers are optimistic.** Twenty-one clean documents, no near-duplicates, no years of
drift. The owner's own twenty sentences against the real archive remain the measurement that
counts, and are deferred until enough is indexed for the answer to mean anything:

```powershell
venv\Scripts\python.exe -m app.cli evaluate --questions mine.txt
```

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

### Two ways to read a .pst, and when each applies

| | libpff (direct) | Outlook (MAPI) |
|---|---|---|
| Needs Outlook installed | no | **yes, classic** |
| Locks the archive | no | **yes** |
| Touches your mail profile | no | **attaches the store** |
| Works from any thread | yes | needs `CoInitialize` |
| Testable off Windows | **yes** | no |
| Reads `.ost` (live mailbox) | poorly | **yes** |
| Install cost | Build Tools for VS | none |

`auto` (the default) prefers libpff and falls back to Outlook. `.ost` always goes to Outlook.
Change it in Settings; the choice persists in `index_state`.

**Not installed by default.** `libpff-python` compiles on Windows and needs Build Tools for
Visual Studio, which breaks the "every pin ships a wheel" rule - so it is optional, every import
is guarded, and `doctor.py` tells you which route you have.

**The third option is conversion.** `app.cli convert "D:\SearchData\2007.pst"`, or the button
in Settings, writes the archive out as `.eml` files. After that the mail needs neither Outlook
nor libpff ever again, and the folder is added as an index root automatically.

**Read the run summary carefully: `seen` is files, `indexed` is documents.** A
`.pst` is one file and thousands of messages. `unchanged` counts files the walker
skipped whole; `unchanged_documents` counts messages inside an archive it did
read but whose text had not moved - that second number is per-message indexing
paying for itself, and conflating the two made a healthy run look broken.

**Indexing an archive is per-message, and that is load-bearing.** `_extract_stream`
yields one document at a time; each message becomes its own `files` row keyed by its
`virtual_path`, and the archive gets a marker row carrying its own size and mtime so the
walker can skip it whole next time. Change detection inside an archive hashes the message
*text*, never the file's bytes - a `.pst` looks modified whenever Outlook opens it.

**If you write an extractor that yields more than one document per file, set
`virtual_path` on every one.** Without it they all share the file's path and overwrite
each other into a single row. The pipeline makes duplicate keys unique and logs the
extractor by name rather than losing the data, but that is a safety net, not a design.

**Known gaps:** loose `.eml` files on disk record attachment *names* only. The libpff backend
does the same; only the Outlook backend extracts attachment *contents*, because reading
attachment bytes through libpff means walking MAPI record sets and is a job of its own.

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
venv\Scripts\python.exe -m pytest tests -q    # 938 passed, 1 xfailed (~5 min)
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

### Visio and Project: what reads what

| format | out of the box | with the optional extra |
|---|---|---|
| `.vsdx` `.vsdm` | **full shape text** (built-in ZIP reader) | same, via `vsdx` |
| `.vsd` | name + title/author/subject | *nothing more exists* - no open spec |
| `.mpp` `.mpt` | name + title/author/subject | **full task list**, via `mpxj` + `jpype1` + a JRE |

`pip install olefile` is the only one that is close to required - without it
`.vsd` and `.mpp` are indexed by name alone. `mpxj` bundles 32 JARs and needs
Java, which is why it is not a dependency. **`doctor.py` says which are active.**

mpxj's Java package moved from `net.sf.mpxj` to `org.mpxj` at version 14 and
both are tried. Assuming one is how the first version of this silently read
nothing on every modern install.

**Formats we cannot fully read are indexed anyway.** `.vsd` and `.mpp` have no
open specification for their contents, so they produce a document of the filename
plus OLE summary properties, carrying a warning that says why. A plan indexed by
name comes back when you search for its project; one the app has never heard of
does not exist. `.vsdx` is read properly - it is a ZIP of XML - and needs no COM.

**At 200,000 messages, anything that iterates all of `files` is a bug.** Two were
found and fixed: `_prune_missing` built a `FileRecord` for every message to
discard 98% of them, and `merge_contained_entities` compared entities
all-against-all. Both were invisible at test scale. Filter in SQL, and bucket
before comparing.

**Three kinds of search, and they are genuinely different:**

| | what it answers | how |
|---|---|---|
| Search tab | what documents *say* | BM25 + ANN + rerank, scope chips for Mail / Documents |
| Files tab (`Ctrl+P`) | what files are *called* | one trigram FTS5 lookup, no model, instant |
| Graph tab (`Ctrl+G`) | what things appear *together* | co-occurrence + PMI |

The Files tab exists because until schema v4 **nothing indexed filenames at all**
- a file named `Invoice 2024.pdf` whose contents never said those words was
unfindable. Trigram tokenisation means "voice" matches "Invoice"; a word
tokeniser cannot, and the feature feels broken without it.

The scope chips are a filter on `source_kind`, not a separate search path - and
the scope is part of the search cache key, or "All" and "Mail" collide.

**Layer 6's entry point - the knowledge graph:**

```powershell
venv\Scripts\python.exe -m app.cli graph                          # build it, list what it found
venv\Scripts\python.exe -m app.cli graph --entity "Acme Water Ltd"  # connections + the documents
venv\Scripts\python.exe -m app.cli graph --html D:\graph.html       # a self-contained page
venv\Scripts\python.exe -m app.cli graph --enrich                  # typed entities, needs Ollama
```

Or the **Graph** tab in the app (`Ctrl+G`): build, browse, and click through to a search.

Three things about it that are easy to get wrong later:

- **The graph is derived from `chunks` and nothing else.** `--rebuild` is always safe and
  costs no re-reading of files. Nothing in Layer 4 reads these tables, so a build can run
  for an hour while someone searches.
- **Edge weights accumulate**, so the cursor is committed in the *same transaction* as the
  batch. Splitting them would double-count a replayed batch silently - no error, and
  nothing that could detect it after the fact.
- **A pair seen in one passage, or no more often than chance predicts, is dropped.** That
  is `min_weight=2` and `min_npmi=0.0`, and on a small test corpus it can legitimately
  empty the graph: if every entity appears in every chunk, there is by definition no
  information in it. That is correct, and it has already confused one test.

**`cooccurrence.COMMON_WORDS` is a judgement call, and it is meant to be argued with.**
The first run against real slide decks returned "Connect", "Enterprise", "System",
"DATA", "CLOUD", "DESIGN" as the most important things in the corpus. They are all
capitalised English words. The blocklist rejects them **as single-word entities only** -
"PI System" and "Customer FIRST" are untouched. The cost is real: a company genuinely
called "Connect" is invisible as a one-word node. If the corpus changes character, this
list is the first thing to revisit, and every entry has a test somewhere.

**Two rules do more work than the blocklist, and are worth understanding before
changing anything here.** First, *a lone Title-Case word that only ever opens a sentence
is discarded* - a slide bullet is its own sentence, so "Provide real-time insight"
otherwise contributes the entity "Provide", and no list of verbs is ever complete. A real
name survives because it is mentioned mid-sentence somewhere. Second,
`merge_contained_entities` folds "AVEVA Group" into "AVEVA Group Limited" **only when
every chunk mentioning the short form also mentions the long one**. A plain prefix test
would have deleted "AVEVA" itself.

## 4. Resuming from cold

On the existing machine:

```powershell
cd D:\SearchProject
venv\Scripts\python.exe doctor.py             # must print READY
venv\Scripts\python.exe -m pytest tests -q    # must be green before you change anything
```

`.\leasha` is the shortcut for everything else - `.\leasha stats`,
`.\leasha evaluate --builtin`, `.\leasha` on its own for the window. **The `.\`
is required** unless somebody has run `.\add-to-path.ps1`: PowerShell does not
run commands from the current directory, and the error it gives
("The term 'leasha' is not recognized") does not say so.

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
| Search terms are ORed, not ANDed | Measured: ANDing every word left nineteen of twenty plain sentences returning **nothing**. People describe documents with words that are *about* them rather than *in* them | A measurement showing precision loss that ranking does not recover |
| The model fills in a form that already exists | L8a emits the operator syntax `parse_query` already accepts, so a bad translation is caught by tested code and there is no injection surface - the model cannot express anything a person could not have typed | Nothing; this is what makes translation safe rather than merely useful |
| A translation is always visible and editable | Invisible query rewriting makes search unpredictable, and unpredictable search over your own archive is worse than blunt search, because you stop trusting it | Nothing |
| Translation never blocks a search | Ollama missing, slow or answering nonsense all fall back to the raw text. The worst case is the behaviour before it existed | Nothing |
| One catalogue for the filters | `commands.py` feeds the `/` dropdown, `app.cli commands` and the model's prompt. Three descriptions of one grammar is how a filter gets offered that the parser rejects | Nothing |

## 6. Traps

Things that have already caused real failures, or will.

**A throughput number without its conditions is not a number.** Embedding was measured at
1.53 passages/second and called "twenty times too slow", on the assumption that a small model
should manage tens per second - true for short sentences, false for the 512-token passages
this app embeds. Then the tool written to settle it reported the *reranker's* file size while
timing the *embedder*. Then a single-pass measurement swung 44% between runs. Three
corrections, all the same mistake: reporting a measurement without what produced it.
`app.cli embed-bench` now repeats each measurement and refuses to be trusted when the spread
is wide.

**A feature nobody can find delivers nothing.** `type:pdf from:dave` parsed correctly, was
tested and shipped in Layer 4 - and went unused for months, because nothing in the
application ever said it existed. The `/` dropdown is not a convenience; it is the difference
between a search box that is a bag of words and one that can answer a real question.

**Test the thing, not its parts.** Every fault in §3b was invisible to a thousand passing
tests, because each asked "does this function return what I expect" and none asked "does
search work". `app.cli evaluate` is the guard now.

**A benchmark can be wrong, and it will be believed.** Two of the twenty evaluation questions
were unanswerable when written - one pointed at a message the named person *sent* rather than
received. Both scored zero for reasons that had nothing to do with search. A test asserts
every question's answer exists in the corpus.

**Every Qt worker must go through `workers.run()`, never `pool.start()`.** The
pool owns the runnable on the C++ side but nothing owns the Python-side signals
object; without a reference it is collected mid-flight and the worker emits into
a deleted object. A test fails if any view bypasses it.

**The memory ceiling is on growth above a baseline, not on absolute RSS.** The
GUI starts above 1.2GB before reading anything. An absolute cap made indexing
from the window impossible - it paused on the first check and never resumed.

**Backpressure belongs at the intake, never at the drain.** The resource
governor originally paused the pipeline's *consumer* - the only thread draining
the results queue. While it waited, workers blocked holding every chunk they had
parsed, so memory never fell and the memory pause never cleared. The run hung
permanently while looking merely slow. Any future throttle goes in `_produce`,
which holds nothing; the consumer may check for a hard stop but must never wait.

**A background job must never treat its own load as a reason to yield.** The CPU
governor counted the indexer's own four workers as "the machine is busy" and
throttled itself to a crawl on an idle machine. `Snapshot.other_cpu_percent`
subtracts our own share. Below-normal priority was already doing the real work.

**A sentinel must never share a value with a real answer.** `_classify` returned
`None` for "unchanged" and `None` for "changed, but no hash" - and the second is
what every `.pst` returns. Every archive was therefore skipped on its first ever
run, counted as `unchanged` rather than `skipped`, with no error and a report of
complete success. It survived two rounds of "the PST did not index" because
every number the run printed said it had worked. If a function returns
`Optional[X]` and also needs a "no result" answer, make the sentinel an object
with a name.

**Never call `open()`, `write_text()` or `read_text()` without `encoding=`.** Python on
Windows defaults to the *process locale* encoding - cp1252 on a UK install - not UTF-8.
This has already broken one feature completely: `pyvis.write_html` opens the file with no
encoding, and the graph page carries `’ — · …` from entity names, tooltips and the inlined
vis-network library. Every `--html` render on Windows raised `UnicodeEncodeError` after
building the entire 264KB document. It passed on Linux and macOS, whose default is already
UTF-8, so nothing in development came close to catching it.

Two rules follow. Always pass `encoding="utf-8"` explicitly, including to third-party code
that writes files - if a library will not take one, generate the string and write it here.
And when a test asserts on a written file, **assert on the bytes**: `read_bytes().decode
("utf-8")` fails loudly on a locale-encoded file, where `read_text()` on the machine that
wrote it succeeds and proves nothing.

**More generally: a green suite on Linux says nothing about Windows.** Path semantics,
file locking, ACLs, COM, mtime granularity and now text encoding have each produced a
failure that only the real machine could show. Every layer so far has had at least one.
Treat a sandbox run as necessary, never as sufficient.

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

**COM is per-thread.** Anything touching Outlook must call `pythoncom.CoInitialize()` on its
own thread first. The indexing pipeline extracts on worker threads, so this is not optional -
and its absence produced the most confusing symptom of the project so far: the CLI could read
the mailbox and the GUI could not, because one ran on the main thread and the other did not.

**A file held open by another program cannot be hashed.** `.pst` is the case that matters, and
extractors now declare `reads_externally` so those files are never read for a hash. More
generally: nothing on the walker thread may raise, because one exception there abandons every
file not yet reached and the run still reports success.

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

1. **Indexing cost is measured, and it is the main constraint.** `app.cli
   embed-bench` on the owner's machine: **1,770-2,020 tokens/second**, twelve threads,
   fp16 model. That is roughly **56-64 hours for a 100GB corpus**, and `reembed` reports
   the time as **100% model** - not I/O, so no amount of tuning the stores will help.

   | Lever | Worth | Costs |
   |---|---|---|
   | int8 model instead of fp16 | **~2x** | small accuracy loss; a **re-embed** |
   | fewer chunks (quoted replies stripped) | ~1.8x on mail | nothing - already done |
   | narrowing the index roots | linear | the documents you leave out |
   | ~~256-token chunks~~ | **~1.1x** | not worth a re-index |

   **Chunk size was claimed here as a 1.5x lever and it is not.** That came from one
   run whose 512-token measurement was depressed; four runs now exist and per *token*
   throughput is roughly flat. The first measurement of all said 1.02x and was closest
   to the truth - it was dismissed because two noisier runs disagreed, which is the
   whole argument for reporting spread rather than a single number.

   **int8 is the only lever left that is worth real money**, and it is far cheaper now
   than after 100GB is indexed. It has not been decided. Settle it by running
   `app.cli evaluate` before and after rather than by argument.

2. **Cached Exchange Mode window.** How much of the live mailbox to index, and whether to
   fetch beyond the local cache.
3. **OCR is being built** - it was out of scope for V2 and the owner overrode that
   deliberately, knowing it may add days to a first 100GB run. Images and scanned PDFs are
   routed in `config/extractors.toml` and switched off until `app/extract/ocr.py` lands.
4. **Packaging.** PyInstaller may fight the ONNX runtime and Qt plugins. Layer 9 says ship the
   venv plus a shortcut rather than let packaging block a working app.
5. **The owner's own twenty sentences.** Deferred until enough is indexed for the answer to
   mean anything. The synthetic corpus is a floor, not a substitute.

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
