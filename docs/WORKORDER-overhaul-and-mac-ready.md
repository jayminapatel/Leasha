# Work order (One thread): a window that never waits, an indexer that shows its work, and code that is ready for a Mac

**Doc version:** 1.5 · **Updated:** 2026-09-30 · **Applies to:** app v0.3.3
**Thread:** One thread, run as a master thread that coordinates helper threads (each in
its own git worktree) and merges their work
**Status:** RELEASED by the owner 2026-09-27, with the instruction to build all of it.
**Register:** queue letter **0x**. Builds on **0w**
(`WORKORDER-dates-live-log-and-interrupted-runs.md`), which was still being built in
another session when this order was written. **Any section of this order that touches a
file 0w touches waits until 0w is merged into `main`**, and is then built on top of it.

## Where this came from

The owner asked for five outcomes: the window never freezes; search behaves the same,
well, in every box, including natural language and date ranges; indexing is as fast as
the hardware allows; an interrupted index recovers without corruption or starting again;
and the Indexing page says, constantly and in plain words, what it is doing, with a
smoothly moving bar and a timestamped log. The same conversation added: progress
*inside* one file (a mail archive or a zip is one file with many things in it); keep
every change Mac-compatible; and make any UI improvements that help.

The design was agreed in conversation first. The owner confirmed every recommendation on
2026-09-27; they are recorded as decisions in §D.

## D. Decisions (owner, 2026-09-27)

1. **Indexing runs in its own process, started and supervised by the window.** No FastAPI
   daemon and no network port: the one-process, no-services rule (HANDOFF §1) stands for
   everything the person uses. The window and search stay one process; only the indexer
   moves out, because Python's global interpreter lock lets indexing threads take time
   from the window however carefully the work is threaded. The two talk over the child's
   standard input and output, which works the same on Windows and macOS.
2. **Date ranges use 0w's syntax.** 0w builds `date:A..B` and a `/date` command. The owner
   had confirmed `/between X and Y`; to avoid two syntaxes for one thing, `/between` is
   built here as an **alias of `/date`** that also accepts `X and Y` and `X to Y`. `/from`
   keeps its meaning (email from this person).
> **2026-09-27 note on D3:** running the macOS job on every commit used up the account's Actions
> allowance within an afternoon (macOS bills at about ten times the Windows rate; a run took about
> 50 minutes), after which GitHub refused every job, Windows included. The macOS job now runs only
> when started by hand (Actions → CI → Run workflow) or weekly; Windows still runs on every PR commit.
3. **A macOS runner is added to CI** (GitHub Actions `macos-14`, Apple Silicon). It starts
   as a non-blocking report and becomes blocking section by section, as each part is made
   to pass there.
4. **This order goes ahead now.** It overrides "working version first" (CLAUDE.md) and the
   parking of the macOS port (`docs/PARKED-IDEAS.md` §6, "Nothing here may be started" and
   "the Windows release has to be finished first") for the scope below only. The
   hardware-specific Mac work in §P stays parked.
5. **Windows is platform one; macOS is platform two.** Where a choice works better on
   Windows, Windows wins, and the Mac gets the best version that does not cost Windows
   anything.
6. **Nothing degrades.** No change may make the interface, search or indexing slower or
   worse on Windows. Every performance claim is measured before and after (non-negotiable
   #9); a change that cannot show it is no worse does not land.
7. **The PySide6 migration stays dropped** (decided the same day; `ORDER_REGISTER.md`).

## R. Rules for every section

- **The Mac rule.** Every line of code this order writes or moves must work on macOS as
  well as Windows. Windows-only calls live in one package, `app/core/osbridge/`, which has
  a Windows implementation and a macOS/POSIX implementation of each operation. A new
  load-bearing guard test (§1c) fails if a Windows-only call appears anywhere else.
- **Wording.** No existing label, message, tooltip or released item is reworded. Messages
  that name a Windows path (for example `venv\Scripts\python.exe`) keep producing exactly
  the same text on Windows and produce the Mac equivalent on a Mac.
- **What cannot be checked here is said, not assumed.** Behaviour inferred for macOS is
  marked **(UNCONFIRMED on macOS)** in the code comment and listed in
  `docs/MAC_VERIFICATION.md` with the exact steps to check it on a real Mac. Windows-only
  behaviour and real-index figures are listed for the owner in each section's closing
  note.
- **Comments.** Every class, method and non-obvious block this order writes is explained
  in plain English, for a beginner, in the style the codebase already uses: why, not just
  what.
- **Helper threads.** Each runs in its own git worktree on its own branch. The master
  thread reviews and merges. Two helpers never own the same file at the same time.
- **Done for an item** means: change, tests, targeted tests green against the recorded
  baseline, the Mac CI job no worse than before, committed by name, and a dated note if the
  build diverged from the item's words.

## 0. Setup

> **2026-09-27, 0a recorded:** `main` at `2362f3d`, Linux sandbox, Python 3.12.3, offscreen,
> `scripts/run_suite.py -j 4`: 6,210 passed, 38 failed, 61 skipped, and one of the four
> processes died (native crash, exit -11) in `test_worker_signal_owner.py`, so the tests after
> it in that process did not run. All 38 failures and the crash are pre-existing on `main`;
> they are the bar later sections are measured against, not work for this order. (One of
> them is the deliberate `indexing_view.py` 299/250 guard, which §4a fixes.)
- [x] **0a** Record a test baseline on `main` (Linux sandbox, Python 3.12) so later
      failures can be told apart from ones that were already there.
> **2026-09-27, 0b checked:** resolved with `uv pip compile` for macOS 14 arm64 (no pywin32,
> every other pin resolves) and for Windows x64 (pywin32 312 still installed). On macOS 13 the
> install cannot work at all: onnxruntime 1.24.4 has no wheel older than `macosx_14_0_arm64`.
- [x] **0b** `requirements.txt` and `requirements-dev.txt`: platform markers on the
      Windows-only packages (`pywin32; sys_platform == "win32"`,
      `pywinauto; sys_platform == "win32"`), so installing on a Mac does not fail. No
      version changes.
- [x] **0c** CI: a `macos-14` job alongside the Windows one, non-blocking at first, running
      the same suite offscreen, with its pass/fail/skip counts in the job summary.
- [x] **0d** `doctor.py`'s platform check: macOS is reported as "supported for files,
      photos and code; mail from Outlook needs Windows" rather than failing outright. Its
      Windows result is unchanged.
- [x] **0e** `docs/MAC_VERIFICATION.md`: the checklist the owner will run on a real Mac,
      started now and added to by every section.

## 1. One home for platform code (`app/core/osbridge/`)

> **2026-09-27, §1 built:** `app/core/osbridge/` (`launch`, `priority`, `programs`, `paths`,
> `cloudfs`), with the Windows code moved unchanged and every old name still working. The guard
> `test_no_windows_only_call_outside_osbridge` reads the code with Python's own parser (comments and
> docstrings never count), is on the load-bearing table, and its allow-list is shrink-only - a second
> test fails when a listed file no longer needs its entry. After 0w merged, `workers.open_in_explorer`
> moved too (Finder `open -R` on a Mac) and left the list; the walker now carries the stat's BSD flags
> so an iCloud file that is not downloaded is recognised without being opened. Still on the list:
> `email_pst.py` (Outlook COM is Windows-only by nature), the parked §P files, `lo_session`/`lo_server`
> job objects and `single_instance`'s named mutex. 58 branch tests run every Mac path with the
> platform faked. Deliberately not built: per-thread priority on a Mac (QoS classes, UNCONFIRMED);
> the process is lowered with `nice` instead. `config.py` still requires `DATA_PATH`.
- [x] **1a** The package, with a plain-English module docstring explaining why it exists.
      Operations: open a file with its default app; show a file in Explorer or Finder;
      lower the priority of the current process and of a thread; find an installed
      program (LibreOffice, its Python, Tesseract, DWG converters, editors, media
      players); the default data folder
      (`%LOCALAPPDATA%\Leasha` on Windows, `~/Library/Application Support/Leasha` on a
      Mac); whether a file is a cloud placeholder (OneDrive attributes on Windows, the
      iCloud "dataless" flag on a Mac); the interpreter path shown in messages.
- [x] **1b** Move the existing Windows code behind it with **identical behaviour on
      Windows**: `core/priority.py`, `core/media_open.py`, `ui/workers.open_in_explorer`,
      `extract/converter.resolve_binary`'s Windows locations, `ui/editors.py`'s lookup,
      `core/winfs.py`. Callers keep their names; only the implementation moves.
- [x] **1c** The guard test `test_no_windows_only_call_outside_osbridge`: no
      `ctypes.windll`/`WinDLL`, `winreg`, `win32com`, `pythoncom`, `msvcrt`,
      `os.startfile`, `explorer`, `powershell` subprocess, `.exe` lookup or hard-coded
      `"\\"` path join outside `app/core/osbridge/`. Files not yet moved are listed in an
      allow-list **that may only shrink**, each with the section that will move it. Seen to
      fail against a deliberate violation before it lands. Added to the load-bearing table
      in `WORKORDER-CONVENTIONS.md` §0.
- [x] **1d** Tests that run every macOS branch with the platform set to `darwin` and the
      system calls faked, so the Mac logic is exercised on every run.

## 2. The indexer in its own process

> **2026-09-27, 2a-2c and 2e built; 2d measured, not yet confirmed.** `app.cli index --events jsonl`
> streams one JSON line per event (`app/index/run_events.py`); IndexStats crosses by value type, so
> fields other sections add travel on their own, and a test shows the page's headline and reader lines
> read the same from the child as in-process. `app/index/child_run.py` runs it with `Popen` on the
> page's existing one-thread index pool - not QProcess, which would have meant changing the view -
> and turns a crash or a silent exit into `ERR_INDEX_PROCESS_ENDED` naming the file it was reading.
> The child takes the run lock as "the window", so 0w's interrupted-run notice works unchanged; it
> lowers its own priority through the CLI's existing path; closing its stdin means Stop, then exit
> after 60 s, so it never outlives the window. The setting is "Index in a separate process"
> (`INDEX_SEPARATE_PROCESS`), **off by default**. 2d, Linux sandbox, fake embedder, shared noisy
> machine: the window's heartbeat p99 improved (21 → 8 ms small, 10.5 → 4 ms medium), throughput was
> equal on the medium corpus (1,488 vs 1,486 files/min) and 16% lower on the small one (the child's
> 2-3 s start-up), and the longest stall did not improve on medium (55 vs 54 ms). That is not enough to
> switch it on for everyone: 2d stays open until the owner's real-index comparison (HANDOFF checklist).
- [x] **2a** `app.cli index` gains a machine-readable mode that writes one JSON line per
      event to standard output (progress, activity, heartbeat, finished) and reads
      commands (pause, resume, stop) on standard input. The existing human-readable output
      is unchanged.
- [x] **2b** A supervisor in the window starts that process, reads its lines without ever
      blocking the interface thread, and turns them into the same `IndexStats` updates the
      page already draws. Pause and Stop reach the child. The run lock, the external-run
      watch and the schedule keep working.
- [x] **2c** Crash handling: if the child dies, the page says so in plain words, the run
      carries on from its last saved position when started again (0w's interrupted-run
      notice), and the file it was reading is named.
> **2026-09-30, 2d on the owner's laptop: the Windows comparison was NOT taken.** The owner needed the
> machine for a real index run before it could be started, so there are no Windows figures for the
> window's longest stall or p99, in process or in a separate process, and nothing here suggests a
> default either way. The one figure taken is not the comparison: the small synthetic corpus (seed 1,
> 602 files, 2,940 documents, 4,082 passages), **fake embedder**, in process, `--full-speed`, **no
> heartbeat probe**, 11 workers, Windows 11, i7-1365U (12 logical processors), shared busy machine,
> the code at `1264ad0`: 57.2 s, 632 files a minute, peak memory 166 MB. How the Linux figures were
> made, read from `pipeline_bench.py` and the HANDOFF checklist: `app.cli bench-pipeline --probe`, once
> as it is and once with `--child-process`, on the small and the medium corpus, fake embedder; the
> probe is the lag monitor's own 50 ms heartbeat on the main thread beside a stand-in window. What
> remains, on a quiet machine with Leasha not indexing (the child takes the machine-wide index lock,
> and on Windows the probe shows a small stand-in window on the desktop): three runs each way,
> alternating, of `venv\Scripts\python.exe -m app.cli bench-pipeline --probe --size medium --corpus
> D:\LeashaBench\medium --embedder fake --out <file>.json` and the same with `--child-process`; then
> once each way with `--embedder real`. 2d stays open, and the default stays off.
- [ ] **2d** Measured, before and after, on the same synthetic corpus: the window's
      longest stall and p99 (the lag monitor) while indexing, files per minute, and memory.
      The change lands only if the window is better and throughput is no worse.
> **2026-09-27, owner's decision: the setting stays for good.** "Index in a separate process" remains
> optional and configurable permanently. The in-process path is **not** retired, whatever 2d shows;
> the "then retired in a later change" below no longer holds. 2d now decides only which way the
> setting is switched by default.
- [x] **2e** The in-process path is kept behind a setting until 2d is confirmed on the
      owner's real index, then retired in a later change.

## 3. Progress you can read, inside one file too

> **2026-09-27, 3a-3d built, and 4d with them.** Readers report where they are through per-thread
> frames (`app/extract/progress.py`): one attribute store per message (about 5 ns), no lock, no
> formatting, no pipeline import. mbox counts message n of m from the table of contents it already
> builds (measured: no second pass), zip member n of m and its name, `.olm` n of m, and a libpff
> PST its folder ("Inbox/Projects") and message n of m **within that folder** - a whole-archive
> total would need a second walk, so there is none. Nesting reads `backup.zip › mail.mbox › message
> 812`. `IndexStats.workers` holds one line per reader (numbered 1..N), `last_activity` moves only when
> something changed, and the writer reports "batch n of m". OCR marks its reader's line "OCR" for
> the length of the call. The page shows a headline, a line per reader with its own clock, and a
> heartbeat that says "last activity 2 s ago" and, after a minute with nothing new, what could
> normally take that long - never that the run has hung. Not wired: the Outlook (COM) reader, whose
> folder walk reports nothing inside the archive yet. Trap: `.olm` and PST readers yield one message
> ahead of what the pipeline has received (`with_closing_warning`).
- [x] **3a** A fixed list of stages, each with plain words the page shows: finding files,
      opening an archive, reading a folder, reading messages, extracting attachments,
      reading inside a zip, OCR, chunking, embedding (batch n of m), writing, saving the
      resume point.
- [x] **3b** Readers report where they are inside one file: PST folder and message n of
      m (libpff's per-folder counts; the Outlook reader where it can), mbox message n of m,
      zip member n of m and its name, with nesting shown as `backup.zip › mail.mbox ›
      message 812`.
- [x] **3c** Several files at once: one line per worker, not one shared name.
- [x] **3d** A heartbeat once a second, so the page can say "working, last activity 2 s
      ago" and warn in plain words when nothing has happened for a while.

## 4. The Indexing page

> **2026-09-27, 4a-4c and 4e built.** 4a: the row is its own widget and the guard is green at
> 244/250 without raising it or editing a test. 4b: the "now" line's slot is built on 0w's
> existing wording (`READING_WORDS`) and waits for §3's presenter function at the adapter point in
> `widgets/indexing_headline.py`. 4c: the bar glides over one paint interval in 50 ms steps, never
> backwards on the same total, busy when the total is unknown, and stops when the page is hidden
> or minimised; measured offscreen at about 3.7 ms of UI-thread time a second more than jumping
> (noisy, shared machine). 4e: All / Warnings and errors, and Copy of the visible lines with their
> times. Found and fixed on the way: Tab now moves through the buttons left to right, and the log's
> caption no longer floats above its box when the skipped-files panel is hidden.
- [x] **4a** First, move the Start/Stop/Pause/Reset row into its own widget file (the view
      is over its 250-line guard; the guard is not raised).
- [x] **4b** One plain headline sentence for what is happening now, from §3's stages.
- [x] **4c** A progress bar that glides between updates instead of jumping, and moves as a
      busy bar when the total is not known yet. Animation costs are measured: no more than
      the existing 0.25 s paint throttle allows.
- [x] **4d** The per-worker lines from 3c, each with its own "n s on this item" clock, and
      a per-file bar for archives only.
- [x] **4e** 0w's timestamped log, with a filter (all / warnings and errors) and Copy.

## 5. Indexing speed, measured one change at a time

> **2026-09-27, 5a built:** `app.cli bench-pipeline` (`app/index/synthetic_corpus.py`,
> `app/index/pipeline_bench.py`): a seeded, byte-identical corpus on every platform, the real
> pipeline into a throwaway data folder, and a report that carries its conditions (corpus digest,
> REAL/FAKE embedder, machine, commit). `--probe` is 2d's instrument: the lag monitor's own heartbeat
> on the main thread while the pipeline runs on a worker. First figures, **Linux sandbox, fake
> embedder, not representative**: medium corpus (3,002 files, 13,900 documents) 115 s, 1,563
> files/min; heartbeat p99 4-6 ms, worst 46-120 ms, no stalls. With the model out, the SQLite
> write stage is 87-96% of the run and grows faster than the corpus (about 4 ms a document small,
> 7 ms medium) - the first lead for 5d, not yet diagnosed.
- [x] **5a** A repeatable benchmark: a synthetic corpus with a large mbox, a zip with
      hundreds of members, and ordinary documents; figures recorded per stage.
> **2026-09-27, 5d built; 5b and 5c measured, not landed.** Fake embedder, Linux sandbox (4 CPUs),
> interleaved runs: medium corpus 126.7 s -> 66.3 s (-47.7%), small 16.41 s -> 13.25 s (-19.3%),
> identical documents, chunks and vectors. **Correction to 5a's note:** its "SQLite write stage
> 87-96%" was mostly the writing thread waiting for Python's interpreter lock (the run used under one
> core of four); the real per-document growth was SQLite's 2 MB default page cache. What landed, each
> measured against its parent: camelCase splitting only in words that can split; documents arriving
> back to back share one transaction (256 documents or 0.1 s, committed before every resume cursor,
> so a crash redoes at most one group); the writing thread's page cache sized to a quarter of the file
> (16-256 MB, given back after the run); **schema v28**, whose `chunks_au` trigger re-indexes a passage
> only when its text changes (marking it embedded used to re-index every word). **5b** - reading in
> spawned processes - prototyped at -30% to -43%, not landed: progress frames, OCR/media/LibreOffice
> state and in-process test fakes are per process, so it needs its own careful change, off by default.
> **5c** not landed: no model here to prove vectors identical; padding is an estimated 32-35% of the
> model's work and grouping by length would cut about 27-30%.
> **2026-09-27, 5b built and measured; landed behind a switch, off by default.**
> `app/index/read_process.py`: each extraction thread gets one reader process, started with the
> thread and kept for the run (`python -m app.index.read_process`, an argument list, the window's own
> interpreter). Files go to it one at a time and the documents come back **one at a time, as they are
> read** - nothing is buffered whole, and a full pipe holds the child back exactly as the bounded
> queue holds a thread back. Only readers that are pure file parsing move (`PROCESS_READERS`: text,
> Office Open XML, ODF, RTF, `.eml`/`.msg`/`.mbox`/`.emlx`/`.olm`, EPUB, FB2); OCR, PDF, zip, PST,
> LibreOffice and media stay on the thread, where their process-wide state lives. Inner progress
> still shows (the child sends its frame stack four times a second); a reader that dies costs one
> file (`ERR_READER_PROCESS_ENDED`) and a fresh child reads the next. The setting is "Read files in
> separate processes" (`INDEX_READ_PROCESSES`, Indexing › Tuning), plus `bench-pipeline
> --read-processes`. **Measured**, Linux sandbox (4 CPUs), fake embedder, `--full-speed`, three runs
> each way interleaved: small 12.8 s -> 6.6 s (-49%), medium 63.4 s -> 34.8 s (-45%, 2,840 -> 5,180
> files/min); identical documents, passages and vectors both times; one reader process peaked at
> 40 MB over the whole medium corpus. **Off by default** until the owner's Windows run (HANDOFF
> checklist): Windows starts processes more slowly than Linux, and the real model changes the
> balance. `tests/unit/test_read_process.py` pins same-answer, crash, abandon and end-to-end.
- [x] **5b** Reading across several processes where reading is CPU-bound, feeding one
      writer. Lands only with a measured gain.
> **2026-09-30, 5c built and measured on the owner's laptop; landed for the one case where the vectors
> are identical.** A gathered batch is taken shortest first before it is cut into model calls, so a
> call holds neighbours in length, and every vector is put back on its own passage
> (`embedder.length_order`; `Embedder.embed` for anything over one call; `Pipeline._embed_sliced` /
> `_embed_grouped`). It is not a setting. `Embedder.groups_by_length` decides where it applies, from
> what was measured. **Measured** with the real `BAAI/bge-small-en-v1.5`, i7-1365U, Windows 11,
> processor only, 4 threads, 2,048 of the 4,082 passages the pipeline itself cut from the small
> synthetic corpus (seed 1; 138-2,117 characters, median 1,572), eight batches of 256 at 32 to a call,
> the two orders alternating batch by batch. **The machine was shared and busy the whole time** (other threads'
> tests and the owner's own work; 77-100% processor in use), so the times are noisy and only the paired
> comparison means anything:
>
> | model file, processor | padding read by the model | time, as given -> shortest first | median of the 8 pairs (range) | vectors |
> |---|---|---|---|---|
> | ordinary file (66 MB) | 34.8% -> 13.8% | 1,164.6 s -> 906.4 s (-22%) | -27% (-39% to +28%) | **identical to the last bit**, 2,048 of 2,048 (max difference 0.0) |
> | smaller int8 file ("Use the smaller model file") | 34.8% -> 13.8% | 487.7 s -> 289.2 s (-41%) | -40% (-46% to -37%) | **all 2,048 differ**: up to 0.032 in one component, mean 0.0032, cosine as low as 0.9939 |
>
> The project's tests accept 1e-6 for a vector (float32 precision: `test_embedder.py`,
> `test_numpy_pyarrow_hotpath.py`); there is no tolerance written for a change of batch order, and the
> ordinary file did not need one. **So it is on for the ordinary file on the processor and off
> everywhere else:** off for the int8 file, which fails the condition by four orders of magnitude (that
> file works out its number ranges per model call, so a passage's vector depends on which passages
> share the call - true of it with or without this change, and worth knowing on its own); off on the
> graphics card, which takes the whole batch in one call so there is nothing to group; off until the
> model has loaded. **The graphics card could not be measured:** in three runs on the Iris Xe today
> DirectML stopped within the first two batches each time (`887A0005`, "the GPU device instance has
> been suspended") and the run carried on on the processor, as designed. **A stop mid-batch** now leaves
> finished passages scattered through the batch rather than at its start, so `_embed_pending` keeps
> every file all of whose passages have a vector and leaves the rest for the next run; a stop already
> asked for (the run's last flush) embeds the leading passages in the order they came, as before.
> **Found on the way and fixed:** twice, the call *before* DirectML reported the failure came back in
> 4-6 s instead of about 40, raised nothing, and held empty vectors (38 of 256 all zeros in the run
> that counted them). Those would have been stored and their passages marked done, never findable by
> meaning. On the graphics card a zero or not-a-number vector now sends the batch through the existing
> one retry on the processor (`Embedder._redo_empty_from_the_card`). **Not done:** a whole-run
> `bench-pipeline --embedder real` before and after (the owner needed the machine); the figures above are
> the model calls the pipeline makes, timed on their own. `tests/integration/test_layer3_acceptance.py`
> was not run for the same reason. Tests: `test_embedder.py`, `test_stop_mid_batch.py`; the real-model
> check runs with `LEASHA_REAL_EMBED_CACHE=<model folder>` and passed here.
- [x] **5c** Embedding: group texts of similar length in a batch so less padding is wasted.
      Lands only with a measured gain and identical vectors.
- [x] **5d** Database writes: larger transactions and bulk inserts on the one writer; bigger
      vector-store appends. Lands only with a measured gain and the resume guarantees
      intact.

## 6. Search in every box

> **2026-09-30, the last sentence of the note below is out of date: the cause is fixed.** A Files,
> Mail or Code view that is let go is now freed at once, by reference count, with its window and its
> column-width timer. Traced with `gc.get_referents`, what held a view was the registry that stops
> the timer crash (`view_options._WATCHERS`): it keeps each width watcher's tick function, which
> closed over the table's header (Files, Mail) and the View button (Code), and from those the view's
> own `lambda ...: self...` slots led back to the view - so it was never freed, not even by
> `gc.collect()`. The watcher now holds only weak references to the table and the button, and the
> views hand their children methods or `view_options.weak_slot` callbacks instead of lambdas that
> close over `self`. Pinned by `tests/unit/test_views_are_freed.py`, which lets each view go with
> the cyclic collector switched off; the timer-crash test
> (`test_a_watcher_whose_python_side_was_collected_does_not_crash_on_its_next_tick`) is unchanged
> and green. The explicit deletes have gone from the Files, Mail and Code tests, each shown freed
> without one; the Search view was measured too, still outlives its last reference, and keeps its
> explicit delete in `test_date_everywhere.py` and `test_date_forms_every_box.py`.

> **2026-09-27, 6a-6c built.** `/between` is `/date` under another name: `between:A and B` and
> `A to B` are joined into `A..B` before parsing (`query.join_between_words`), so it reaches the same
> after/before everywhere, Mail's sent date included, and is offered in every box's `/` menu.
> **One behaviour change to note:** `between` used to be a second spelling of git's `/range` in the
> Code tab; kept, it would have hidden `/range` from the menus and sent a date range to git as a
> commit range. `/range v5.0..v6.0` works as before. Only ISO values work (the date parser has no
> month names). Plain English: "between March and June 2024", "from 1 Jan 2024 to 5 Feb 2024" and
> year ranges are *offered* as chips, never applied; anything that needs a guess (a year-less range
> in the future or across New Year, a backwards range, an impossible day) stays as words. The audit
> (Search, Files, Mail, Code, mini-search, Timeline, Chat) found three larger gaps, left as they are:
> the Timeline takes single values per box by 0w's design, Chat has no source search box, and the
> mini-search has nowhere to explain a mistyped date. Found on the way: the Files, Mail and Code views
> sit in Python reference cycles once let go, which can crash a test run when a column-width timer
> fires into a collected closure; tests now delete them explicitly, the cause is not yet fixed.
- [x] **6a** `/between` as an alias of 0w's `/date`, accepting `X and Y` and `X to Y`
      (decision D2), offered wherever `/date` is.
- [x] **6b** Plain English: "between March and June 2024", "from 1 Jan to 5 Feb", "since
      last Easter" is *not* guessed (an ambiguous date filter hides documents silently).
      Only unambiguous phrases become filters, as `translate_rules` already insists.
- [x] **6c** An audit of every search box (Search, Files, Mail, Code, Timeline, the
      mini-search, Chat's source search) proving each uses the same parser and offers the
      same date forms, with a pytest-qt scenario per box.
- [ ] **6d** Semantic search: fixed from the owner's evidence (a failing query, what was
      expected, `app.cli stats` and `app.cli diagnose` output). **Blocked on that evidence**;
      nothing here is guessed.

## 7. Paths and letter case, ready for a Mac

> **2026-09-27, §7 built** (`app/core/osbridge/pathnames.py`). `federate` joins with `/` on a Mac;
> on Windows it runs the old expression, pinned against a verbatim copy over 84 root/path pairs. The
> walker, the pipeline's seen/prune/retry sets, `media_backlog`, `scan` and the archive resume key use
> `path_key`, which is exactly `str.lower()` on Windows and never touches the disk there; on a Mac or
> Linux it follows a per-folder probe (a case-swapped `lstat` compared by device and inode, cached).
> A real case-sensitive folder holding `Report.txt` and `report.txt` now keeps both rows through a
> full index and a rerun; with the old key put back, three of those tests fail. Kept in the Windows
> format on every system, because they are stored keys: `archives.normalise`, the cloud-content
> opt-in, the repos table's `COLLATE NOCASE` (two repositories differing only by case would share a
> row on a case-sensitive disk - attribution, not a lost file). Cost on Windows: about 20 ns a call in
> a micro-benchmark. **(UNCONFIRMED on macOS:** the APFS probe, and Unicode normalisation.)
- [x] **7a** Code that hard-codes `\` when joining or splitting paths uses the system's own
      separator. **Paths already stored in an index are not changed**, so nothing on the
      owner's machine is reindexed or migrated.
- [x] **7b** Whether a drive compares names with or without regard to letter case is
      decided per indexed folder, by probing it when it is added. Windows behaviour is
      unchanged (case-insensitive); a case-sensitive Mac volume is respected.

## 8. Mail files from a Mac

> **2026-09-27, 8a-8c built** (`app/extract/email_emlx.py`, `app/extract/email_olm.py`, 28 tests).
> `.emlx` goes through the same `document_from_message` as `.eml`, so the two are identical in
> shape. `.olm` streams one message at a time through `archive.py`'s own guards and resumes like
> mbox. Two limits differ from a plain zip on purpose: the member cap is the scan's 200,000 (an
> export has a member per message and per attachment), and archive.py's 512MB total budget is not
> applied (it would stop a real mailbox part-way, silently). **The whole `.olm` XML layout is
> (UNCONFIRMED)** - taken from public descriptions, parsed defensively, and first checked on the
> owner's Mac (`docs/MAC_VERIFICATION.md` §6). Found on the way and fixed: `.mbox` (like `.olm`)
> was dropped unread by the walker's 2GB ceiling although it is streamed, so a Google Takeout
> export over 2GB never reached the reader; `test_a_mail_archive_over_the_ceiling_is_still_read`.
- [x] **8a** Apple Mail `.emlx` reader (one message per file plus its partial-download
      variant), producing the same mail document as `.eml`.
- [x] **8b** Outlook for Mac `.olm` export reader (a zip of per-message XML), streamed one
      message at a time with a resume position, like mbox.
- [x] **8c** Built and tested against fixtures made here; checking against a real export is
      listed in `docs/MAC_VERIFICATION.md` **(UNCONFIRMED on macOS)**.

## 9. UI improvements

> **2026-09-27: review done** - every page grabbed in light and dark at 1100x760 and 760x560, and
> text contrast measured against WCAG AA. Fourteen findings; nine built, each with a pytest-qt
> scenario in `tests/unit/test_ui_review_0x9.py` that fails before its fix. Page grabs timed the same
> before and after, within noise. Proposed, not built: Files and Mail columns squashed on first open
> (in `view_options.py`, the five-times-fixed area - only when nothing is saved); Indexing › Status
> saying "Nothing indexed yet." above a document count; unticked checkboxes invisible under the Fusion
> style (needs Windows 11 evidence first - the rule that causes it fixed a Windows problem); the
> preview crowding narrow search results; two small alignment items.


Found by a review of the running window once §4 lands, and added here as dated items
before they are built. Each one must be measured no slower, must not reword an existing
label, and gets its own pytest-qt scenario.

- [x] **9a** Short window: the rail shows icons instead of labels cut in half
- [x] **9b** Light theme: the chosen page's rail icon can be seen (was 1.24:1)
- [x] **9c** Dark theme: the Open button's text 2.8:1 to 6.5:1 (new token `accent_on`)
- [x] **9d** `text_faint` reaches WCAG AA in both themes
- [x] **9e** An empty index no longer says "Up to date" (new wording: "Nothing yet")
- [x] **9f** Suggested searches and chips have round ends (`radius_pill` 999 to 11 px; Qt draws none past half the height)
- [x] **9g** Settings and Indexing category names shown in full
- [x] **9h** Timeline controls wrap instead of being cut off or overlapping (`widgets/flow_layout.py`)
- [x] **9i** Chat's Fast/Thoughtful box no longer stretches across the pane
- [x] **9j** Indexing › Status says "N documents ready to search." before any run when the index has
      documents - the Search page's own sentence, no new text
- [x] **9k** Suggested searches wrap whole instead of being cut mid-text (`FlowLayout` `centred`; the page
      sizes the box itself, since Qt's height-for-width cost about 1 ms on every resize)
- [x] **9l** Reports names no longer carry a blank indent (key under `UserRole`, `timeline_host.REPORT_KEY`);
      the Recent activity card keeps the style's margins
- [x] **9m** Files and Mail open with their Name/From columns at their own width: a fit over an empty
      table no longer uses up the table's one fit while no widths are saved; saved widths behave exactly
      as before, pinned by a test that passes both before and after

> **2026-09-27 (later): four of the review's proposed items built**, each with a scenario in
> `test_ui_review_0x9.py` that fails before its fix. One consequence to settle on Windows:
> `test_grab_ui.py`'s `search-home` golden at 1024x600 now drifts past its tolerance in the Linux
> sandbox (14 against 12), because the sandbox's wider font makes the pills wrap there - the fix
> working. The goldens were captured on the Windows venv, where the pills should stay on one line at
> that width **(UNCONFIRMED)**. Run it there; regenerate the three `search-home` goldens only if it
> drifts on Windows too. Not regenerated from Linux.

## 10. Close-out

- [x] **10a** `LOCAL_KNOWLEDGE_GRAPH_V2.md`: the indexer process and the osbridge package
      described for a beginner.
- [x] **10b** `HANDOFF.md`, `docs/ORDER_REGISTER.md`, `CHANGELOG.md` brought up to date.
- [x] **10c** `docs/MAC_VERIFICATION.md` complete and ordered for a first session on a Mac.

> **2026-09-27, close-out.** 46 + 2 items done; four stay open by decision, each with its reason in
> its own dated note: **2d** (whether the separate indexing process becomes the default - the owner's
> real-index comparison), **5b** (reading in several processes - prototyped, needs its own careful
> change), **5c** (length-grouped embedding - needs the model to prove identical vectors) and **6d**
> (semantic search - waits for the owner's example). After the order, at the owner's request: every
> action button follows one system (`widgets/buttons.py`: natural width, one height, a 16 px icon,
> primary / secondary / danger), the Indexing pill reads as a rail button, and a maximised window
> stays maximised.

## P. Parked: hardware-specific macOS work

Each has a `docs/MAC_VERIFICATION.md` entry. None may be started under this order.

- CoreML acceleration, whether onnxruntime's Mac wheel offers it **(UNCONFIRMED)**, and
  re-measuring every tuning rate on Apple Silicon.
- Hardware detection on a Mac (performance/efficiency cores, GPU, disk kind through
  `sysctl` and `system_profiler`). Until then a Mac reports "unknown" honestly, as today.
- Offline Media drives on a Mac (volume identity and removable-drive detection).
- The global hotkey and "search the selected text", which need macOS Accessibility
  permission.
- The `.app` bundle, `.icns` icon, signing and notarisation, and a Mac installer.
- Indexing a live Apple Mail or Outlook for Mac mailbox (as opposed to its files).

## Done means

Every box above ticked or carrying a dated note that says why not; the suite no worse
than the 0a baseline on Linux and on the Windows CI; the Mac CI job passing the sections
made blocking; the owner's Windows checks and `docs/MAC_VERIFICATION.md` handed over.

Acceptance sentence: start a large index and keep working in the window without it ever
catching; read, on the Indexing page, exactly what it is doing right now, down to the
message inside a mail archive; pull the plug and carry on with nothing lost; type a date
range in any box and get the same answer everywhere; and open the same code on a Mac
without a single Windows-only call in the way.
