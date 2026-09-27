# Work order (One thread): a window that never waits, an indexer that shows its work, and code that is ready for a Mac

**Doc version:** 1.0 · **Updated:** 2026-09-27 · **Applies to:** app v0.3.3
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

- [ ] **2a** `app.cli index` gains a machine-readable mode that writes one JSON line per
      event to standard output (progress, activity, heartbeat, finished) and reads
      commands (pause, resume, stop) on standard input. The existing human-readable output
      is unchanged.
- [ ] **2b** A supervisor in the window starts that process, reads its lines without ever
      blocking the interface thread, and turns them into the same `IndexStats` updates the
      page already draws. Pause and Stop reach the child. The run lock, the external-run
      watch and the schedule keep working.
- [ ] **2c** Crash handling: if the child dies, the page says so in plain words, the run
      carries on from its last saved position when started again (0w's interrupted-run
      notice), and the file it was reading is named.
- [ ] **2d** Measured, before and after, on the same synthetic corpus: the window's
      longest stall and p99 (the lag monitor) while indexing, files per minute, and memory.
      The change lands only if the window is better and throughput is no worse.
- [ ] **2e** The in-process path is kept behind a setting until 2d is confirmed on the
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
- [ ] **5b** Reading across several processes where reading is CPU-bound, feeding one
      writer. Lands only with a measured gain.
- [ ] **5c** Embedding: group texts of similar length in a batch so less padding is wasted.
      Lands only with a measured gain and identical vectors.
- [ ] **5d** Database writes: larger transactions and bulk inserts on the one writer; bigger
      vector-store appends. Lands only with a measured gain and the resume guarantees
      intact.

## 6. Search in every box

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

- [ ] **7a** Code that hard-codes `\` when joining or splitting paths uses the system's own
      separator. **Paths already stored in an index are not changed**, so nothing on the
      owner's machine is reindexed or migrated.
- [ ] **7b** Whether a drive compares names with or without regard to letter case is
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

Found by a review of the running window once §4 lands, and added here as dated items
before they are built. Each one must be measured no slower, must not reword an existing
label, and gets its own pytest-qt scenario.

## 10. Close-out

- [ ] **10a** `LOCAL_KNOWLEDGE_GRAPH_V2.md`: the indexer process and the osbridge package
      described for a beginner.
- [ ] **10b** `HANDOFF.md`, `docs/ORDER_REGISTER.md`, `CHANGELOG.md` brought up to date.
- [ ] **10c** `docs/MAC_VERIFICATION.md` complete and ordered for a first session on a Mac.

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
