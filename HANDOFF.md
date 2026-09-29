# Handoff

**Doc version:** 7.18 · **Updated:** 2026-09-29 · **Applies to:** app v0.3.3

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
| The index (vectors, FTS, cache, models) | `D:\Leasha\Data` - set by `DATA_PATH` in `.env` |
| Machine-specific config | `D:\SearchProject\.env` - **gitignored**, written by the installer |
| Logs and diagnostics | `D:\SearchProject\logs\` - gitignored contents, tracked structure |

**The index is never inside the project folder**, and nothing in it is original data. It is
entirely rebuildable from your documents, so deleting it is always safe.

**Only `DATA_PATH`, `PROJECT_PATH` and `LOG_PATH` are pinned in `.env`.** Everything else -
`VECTOR_PATH`, `FTS_DB`, `CACHE_PATH`, `MODEL_CACHE`, `STATE_PATH` - derives from
`DATA_PATH` and is deliberately left unset, because a pinned sub-path outranks `DATA_PATH`
and would strand part of the index on the old drive the day it moves. See
`app/core/settings_registry.LOCATION_KEYS`, which also stops "restore defaults" removing the
three that have no default to fall back on. It did once, on 2026-08-26, and the application
could not start at all: `load_settings` refuses before logging exists, so there was no log
line, no traceback and no window.

## 3. Current state

**2026-09-27 (later) - order 0x is active, run as a master thread.** The owner released
`docs/WORKORDER-overhaul-and-mac-ready.md`: the indexer moves into its own process so the
window never waits on it; the Indexing page says what it is doing down to the message inside
an archive; indexing speed work, measured; `/between` and plain-English date ranges in every
box; and every line written or moved made to work on macOS too. Its §D holds the owner's
decisions: indexer as a child process (no FastAPI, no port), Windows first and Mac second,
nothing may degrade, and a `macos-14` CI job (non-blocking at first). It overrides "working
version first" and the Mac parking for its own scope only; hardware-specific Mac work stays
parked in its §P. **It builds on 0w**, which shipped the same day and is merged into the
0x branch. The owner's real-Mac checks accumulate in `docs/MAC_VERIFICATION.md`.
Section 0 is done (baseline, requirement markers, the Mac CI job, `doctor.py` on a Mac).
**2026-09-27 (later still) - traps found while clearing the red tests, and the child indexer.**
- **Qt's default timers can fire up to 5% early.** `SearchView`'s 400 ms idle timer fired at
  379-399 ms, the view re-measured the gap, decided "interim", and the full search (the one that
  logs to `searches`, corrects spelling and writes notices) never ran until Enter. Each timer now
  passes the interval its own firing proves. This was not a 0w regression; it happened before too.
- **The `view_options.py` timer crash is fixed** (the "open" trap above): `remember_widths`' `look`
  closure and its timer formed a cycle the garbage collector cleared while the C++ timer lived, so a
  tick called a function with no globals (seen in a core dump). A module-level `_WATCHERS` keeps each
  running `look` reachable while its timer exists.
- **`gui_mainwindow` hides its window before closing the store.** Qt 6's `app.quit()` sends a close
  event to every *visible* top-level window, and a leaked visible window's `closeEvent` then saved its
  geometry to a closed store.
- **`scripts/run_suite.py` names "the last file it started" even when every test finished** and the
  crash came from garbage collection at exit - read the per-test results before blaming that file.
- **Never run `scripts/regen_vs_project.py` mid-merge without the fix now in it**: with conflicts
  unresolved, `git ls-files` lists a path once per stage, which is how three docs came to be listed
  three times each. It now de-duplicates.
- **`SqliteStore.close()` used to crash the process if a reader was mid-query on another thread**
  (a native -11 / 0xC0000005 with no traceback; the write lock covered writers, not readers).
  Connections are now `_GuardedConnection`: every call into SQLite is counted, and `close()` retires,
  `interrupt()`s, waits (5 s cap) and only then closes. A cut-short reader gets "was closed while a
  worker was using it", which workers treat as shutdown. Pinned by `test_close_during_read.py` (child
  process). **Trap:** never reach SQLite except through the store's connection and cursor methods - a
  raw `sqlite3.Cursor(conn)` bypasses the count and brings the crash back. Cost: about 2 us more per tiny
  query and 0.7 us per row when iterating a cursor; `fetchall` and `executemany` unchanged.
- **`view_options._apply_widths` (0x 9m):** a fit over an empty table no longer marks the table as
  fitted *while no widths are saved*; with any saved width it behaves exactly as before. Reports list
  keys are read with `timeline_host.REPORT_KEY` (UserRole), never role 1. On the Search home,
  `FlowLayout(height_for_width=False)` plus the page sizing the box itself avoids Qt's ~1 ms per resize.
- **Theme (0x §9):** `radius_pill` is 11 px, not 999 (Qt draws no rounding past half a widget's
  height); new token `accent_on`; `text_faint` darkened in light and lightened in dark to reach WCAG AA
  (old values noted beside the new); new `widgets/flow_layout.py`.
- **Button system (2026-09-27):** every action button goes through `widgets/buttons.py`; `BUTTONS`
  holds each button's words, icon and kind (primary, secondary, danger); `theme.BUTTON` sizes give 28 px
  buttons. A new QPushButton needs a table entry or `test_button_system` goes red; flat buttons and
  `buttonSystem="exempt"` are skipped. The Indexing pill is an icon, a status dot (`PillState.tone`) and
  one word; the count is in its tooltip.
- **Schema v28 (0x 5d):** `chunks_au` fires only on `UPDATE OF text, symbols`. During a run the
  writing thread's connection holds up to 256 MB of page cache (a quarter of the file) and gives it back
  at the end. Documents are written in groups of up to 256 / 0.1 s, always committed before a resume
  cursor. A new migration must be numbered after 28.
- **Any new "have we seen this file" set must use `osbridge.path_key`, never `.lower()`** (0x §7).
  The walker and the prune pass share one set; mixing keys drops or duplicates files on a
  case-sensitive disk. On Windows `path_key` is `str.lower()` byte for byte.
- **The child indexer** (0x §2, off by default): a new monotonic-clock field in IndexStats must go in
  `run_events.MONOTONIC_FIELDS`; commands reach the pipeline only from its first progress tick; closing
  the child's stdin means Stop then exit after 60 s, so never run `--events` in-process with stdin at
  end-of-file (under pytest it stops the run and later calls `os._exit`).

**2026-09-27 (later) - the owner's feedback: three bugs fixed, order 0w built and SHIPPED.** Seven items came
in. Three were bugs with a verified cause and were fixed directly. Three became order 0w
(`docs/WORKORDER-dates-live-log-and-interrupted-runs.md`), released and built the same day. The
seventh, general jerkiness, has no measured cause and is not ordered. It needs the lag-monitor
numbers from a real run (the owner-run step further down this section). What is new and load-bearing:

- **UI state writes are queued, not synchronous** (`app/ui/state_writes.py`). The page-switch
  freeze was `set_state` on the UI thread waiting on `SqliteStore._write_lock`, which the indexer
  holds for every batch. Every UI-side `set_state`/`set_states` now goes through `save_state`/
  `save_states`: one thread, in order, fire-and-forget. `test_ui_never_blocks` no longer exempts
  them; only `closeEvent`'s geometry save may stay synchronous. `_drain_workers` drains the queue
  before the store closes. `IndexWorker.run` waits for it (`settle_before_run`, 5 s cap) before
  a run reads its settings. **Trap:** a test that closes its store straight after a UI write
  must drain `state_writes.pool()` first, or it reads the old value on Windows CI.
- **Mail is dated by when it was sent.** `files.taken_at_ns` now holds a message's sent date,
  written at index time and backfilled by **schema v27**. `after:`/`before:`, result dates,
  recency and browse all use it. The Search tab **applies** recognised filters
  (`translate_rules.apply`), which reverses order 0c 3b (dated note in that order).
- **Progress phases** (`IndexStats.phase`, `Pipeline._announce_phase`). `on_progress` is now
  called *before* the first file is read. A test that stops "on the first tick" must stop on the
  first tick with something indexed.
- **A run log** (`IndexStats.activity`, `app/index/activity.py`). New notice sites must use
  `IndexStats.add_notice`, which records the time.
- **Interrupted runs** (`app/index/interrupted.py`), read from the `run:active` record without
  its mutex. **PST folder resume** for libpff through `resume:archive:<path hash>` keys (under
  `resume:`, so a reset clears them). Outlook is deliberately not resumable; see the 0w 3b note.
- **Dates** (0w §1): `date:` ranges and times of day are parsed in `app/search/query.py` into the
  same `after`/`before` every box already used. Bad dates land in `ParsedQuery.date_problems`
  *and* stay in `unknown_operators`. The Mail tab's `before` now includes its last day. In Code,
  `after:` still means git history; `/date` means the index.
- **The timeline's "near the bottom?" check flushes pending layout first**
  (`TimelineList._more_once_laid_out`). Without it, `main` fetched an unrequested second page
  about half the time.
- `Leasha.pyproj` is regenerated and `test_vs_project` is green again.
  `scripts/regen_vs_project.py` runs on Linux too.

**Not verified on the owner's machine:** none of this has run on Windows with real data. Worth one
real run: switch pages during a large index, type "mail from 2017", watch the bar and the log
through a full run, then end Leasha from Task Manager mid-archive and relaunch.

**Owner testing for order 0x (on Windows), added 2026-09-27.** Same rule: tick, or a dated note.

- [ ] **`/between` in every box.** In Search, Files, Mail, Code and the mini-search (Alt+Space), type
      `/bet`: `/between` sits under `/date`. `/between 2024-03-01 and 2024-06-30`, then `… to …`, match
      `/date 2024-03-01..2024-06-30`; Search shows one chip and removing it leaves no stray "and". On
      Mail, `/between 2023-12-01 and 2023-12-31` includes the 31st. `/between 2024-03-01 and
      2024-13-01` says what is wrong. In Code, `/range v1..v2` still runs git.
- [ ] **Plain-English ranges.** "letters between March and June 2024" offers after 2024-03-01 and
      before 2024-06-30; "from 1 Oct to 5 Nov" offers nothing.

> **2026-09-27:** the owner decided the setting stays optional and configurable for good; the
> in-process path is never retired. This check now decides only the default.

- [ ] **Read files in separate processes (off by default, 0x §5b).** Indexing › Tuning: turn it on,
      run `app.cli bench-pipeline --size medium --full-speed` with and without `--read-processes`
      (on Linux: 63 s -> 35 s). Then a real index with it on: Task Manager shows one extra
      `pythonw` per reader, each well under 100 MB; the page still shows "message N of M" inside a
      large `.mbox`; Pause, Stop and closing the window leave no reader `pythonw` behind. If the
      Windows numbers hold, it becomes the default in its own small change.
- [ ] **Time limits and Force skip (0z lane B).** Indexing › Tuning › Coverage shows "Time limit per
      file" (120 s) and "Skip a mailbox or archive after no progress for" (600 s). During a real index,
      press "Force skip reader N" on a large PDF: within a second that reader moves on and the log
      says "... you pressed Force skip on the Indexing page"; the file shows as skipped and is left
      alone next run. Repeat with "Read files in separate processes" on (on a `.docx` or `.mbox`: its
      `pythonw` is replaced, Task Manager count unchanged) and with "Index in a separate process" on.
      A large real `.pst` must **not** be cut off while its message count moves. UNCONFIRMED on
      Windows: whether a hung Outlook (COM) read lets go when interrupted, or is left behind and
      replaced - the log line "did not let go ... left behind" says which.
- [ ] **Search inside the code (order 0y §2).** In the Code tab, type a class or function name from
      one of your repositories: its definition is the first row (Match "Definition"), with the Line
      and the line of Code, then files whose name matches, then Mentions. Open the file and check the
      line number is right.
- [ ] **Code tab fixes (order 0y §1).** Run a history search (`/repo <name> something /history`):
      no black console window flashes up. Start one on a large repository and press Esc (or the
      button, which reads "Stop"): it ends at once and says "History search stopped". Right-click a
      file in a repository › "Ignore this repository": its files leave the Code list and stay
      searchable elsewhere; "Undo" in the note brings them straight back.
- [ ] **Index in a separate process (off by default).** Indexing › Tuning › Strategy: turn it on,
      start a large index, click round every page, then compare the log's `shutdown: window
      responsiveness` line with a run with it off. Pause, Resume and Stop work, and a Stop is not
      "did not finish". End the window from Task Manager mid-run: the child `pythonw` goes within a
      minute. End only the child: the page says "The indexing process stopped unexpectedly while
      reading …", the next open shows the interrupted notice, and Start carries on. Close the window
      mid-run: no Leasha process remains. Then `app.cli bench-pipeline --probe --size medium`, with
      and without `--child-process`, on the real machine - that settles whether it becomes the default.
- [ ] **UI review fixes (0x §9).** At 125%, shrink the window to about 600 px tall: the rail shows icons
      only. Light theme: the chosen rail icon is navy; dark: the Open button's text is dark on lavender.
      Chips have round ends. Settings shows "Storage & maintenance" in full. Reports › Browse your
      timeline at about 800 px wide wraps its months. `text_faint` still reads quieter than `text_dim`.
      **Two answers wanted:** are unticked checkboxes visible in Settings › Appearance on Windows 11?
      And does Indexing › Status ever say "Nothing indexed yet." with documents present on the real
      index? Each decides a proposed fix.
- [ ] **The UI goldens.** `venv\Scripts\python.exe -m pytest tests/unit/test_grab_ui.py` on Windows. It
      drifts on `search-home` at 1024x600 in the Linux sandbox because the pills now wrap there (wider
      font). If it passes on Windows, nothing to do; if it drifts there too, look at the grab and, if it
      is right, regenerate the three `search-home` goldens with `tools/grab_ui.py`.
- [ ] **Indexing speed (0x 5d) and schema v28.** Open the real index once with the new build:
      `schema_version` reads 28 and search still works. Then, alternating `ea8ce87` and this build, 3
      runs each: `venv\Scripts\python.exe -m app.cli bench-pipeline --size medium --corpus
      D:\LeashaBench\medium --embedder real --full-speed --out D:\LeashaBench\<commit>-N.json`.
- [ ] **Maximised stays maximised.** Maximise, close to the tray, bring it back: still maximised. Quit
      fully while maximised and start again: opens maximised.
- [ ] **Buttons and the pill.** Look round every page, light and dark, at 100% and 125%: buttons their
      own width with icons, one height; Start filled, Reset and Clear logs red. The pill reads "Up to
      date" on one line with Windows' font (it wraps on Linux). Then regenerate the 12 goldens in
      `tests/golden/ui-redesign/` on Windows with `tools/grab_ui.py` - they drift by design (the pill and
      the preview buttons); never regenerate them on Linux.
- [ ] **Close while a search is loading.** During a large index, open the `/` popup, keep typing and
      close the window: a clean exit, nothing in `crash.log`. The log's `shutdown: sqlite store closed`
      stays well under a second.
- [ ] **Where it is inside an archive.** Index a real `.pst` (libpff) and a large `.mbox`: the page
      shows the folder and "message n of m" (for a PST, n of m within the folder), one line per
      reader, and "last activity" keeps moving. Check folder names read naturally ("Inbox/...", not
      "Top of Personal Folders/..."), also on a non-English Outlook if you have one.
- [ ] **The Indexing page.** At 125% and 150%: the log's filter and Copy line up with its caption; the
      bar glides during a scanned run and shows a moving block before the total is known; minimise
      and restore mid-run and the bar is right at once. Tab moves left to right through the buttons.

**Owner testing to do later (on Windows, with the real index).** Deferred by the owner
2026-09-27 when this was merged. Tick each box here, and put anything that fails in a dated
note under it. Everything above passed offscreen tests in a Linux sandbox and the Windows CI.
None of it has met a real display, real data, a real PST or Outlook.

- [ ] **Page switch while indexing.** Start a large index and click round every page on the
      rail. No freeze. Afterwards, read the log's `shutdown: window responsiveness this session`
      line (beats, p50/p99/worst, stalls) and any `unresponsive for N ms` lines. Record them here:
      they are also the measurement general jerkiness (item 3b of the feedback) is waiting for.
- [ ] **Settings survive a quick Start.** Change an archive mode or cloud folder, press Start
      at once, and check the run used the new setting.
- [ ] **"mail from 2017".** The Search tab shows mail only, sent in 2017, newest first, with
      removable chips. Remove a chip and the words come back as search terms. Try "invoice 2017"
      too: 2017 should stay a search word.
- [ ] **Schema v27 on the real index.** First open after updating: note how long the backfill
      took (logged) and that Outlook mail now shows its sent date.
- [ ] **Dates in every box.** `date:2017-03..2017-06`, `date:..2017`,
      `after:2017-03-01T10:00` and `/date` in Search, Files, Mail, Code and the mini-search. Then
      a bad one, `date:2017-13`, which should say what is wrong. On the Mail tab, check
      `before:2024` now includes 31 December.
- [ ] **The progress bar and the live log through a whole run.** The bar visibly animates
      during warm-up, planning and tidying (there was once a "frozen full bar"). "What the run is
      doing" has a time on every line, stays put when scrolled up, and follows at the bottom.
      Check it at 125% display scaling.
- [ ] **An interrupted run.** End Leasha from Task Manager part-way through a large `.pst` and
      relaunch. The Indexing page should say the last run did not finish, and list the archive as
      not finished. Start again: that archive carries on from its folder (libpff), and the final
      message count equals an uninterrupted run. Repeat once with a pulled plug if you can.
- [ ] **A normal Stop or Pause is not reported as "did not finish".**
- [ ] **`app.cli index` and `app.cli stats`** print the timestamped lines and the
      did-not-finish note in the Windows console, with no stray characters.

**2026-09-27 - the branches were folded back into main; two fixes had been left behind.**
Every `claude/*` branch on origin was checked against `main` by patch, not by hash (a
shallow clone makes them all look hundreds of commits ahead - `git fetch --unshallow`
first). All but seven commits were already in main. Of those seven, five had been
superseded by main's own later versions (the minimize/restore repaint, 0q's last items,
0r's deferred page construction, the archive-key separator normalisation, the converter
tests mocking `resolve_binary`), and the `.svg`/`qt`-marker collection fix was a
regenerated `GitSearch.txt` and solution files. Two were genuinely missing and are now
ported, originally from `thread-cleanup-commits` (2026-09-16):

- **Start could come back mid-resolve.** The 4-second external-run poll
  (`IndexController._poll_external_run` → `_show_external_run` → `_go_idle`) had no
  notion of `_resolving_index`; `IndexingView.is_running()` is False for the whole
  resolve, so a tick landing then re-enabled Start and invited the second click
  non-negotiable #5 forbids. Both methods now check the flag.
  `test_the_watch_timers_poll_does_not_re_enable_start_while_resolving` fails on the
  unfixed controller and passes on the fixed one (offscreen, Linux sandbox - not yet
  seen on a real display).
- **`test_docs_versioned.py` walked pytest's own basetemp.** `--basetemp=.pytest_tmp`
  lives inside the project, so another test's header-less fixture markdown could fail
  the version checks. Directories starting `.pytest_tmp` are now pruned.

After the merge, the stale branches are deleted; nothing on them is lost that main
does not now carry.

**2026-09-20 (closing pass) - four things that said nothing, and a Pause button.** `CHANGELOG.md`
has the full list; this is what a reader needs to know that is not obvious from it.

*The pattern worth carrying forward.* Four defects closed today were **silent**: the wrong answer
looked exactly like a right one, so no test and no person could see them.
`/newest` and `/oldest` were accepted from the command line and dropped (the raw query never
reached `expand_slashes`, so the sort parsed to empty); `--interpret` was accepted with
`--builtin` and ignored; the Chat ship floor was measured with **no vectors at all**, so
meaning-based questions failed before the model was asked and that was reported as the model's
quality (real figure **84.0%**, not 73.9% - and the floor was NOT lowered to meet it); and the
picture stack's OCR probe drove the graphics card ungated, which at four threads returned
**no text from 131 photographs while reporting success**. Each now has a guard that fails loudly.
When something here looks fine, ask what it would look like if it were broken - three of these
were found only by checking the *dates* of results, or the *count* of words, not by reading code.

*The crash that killed three suite runs is explained and fixed, and my first answer was wrong.*
It was not memory. `QApplication.setStyleSheet` re-polishes every widget alive in the process,
the suite leaks widgets, and the walk reached one already freed. Four sites fixed plus a guard;
the application itself never does this (it themes its window). A deferred call with a **lambda**
is the same shape - measured on PyQt6 6.11: a bound method of a QObject is auto-cancelled when
that object dies, a lambda fires anyway - so `app/ui/later.py` ties them to an owner, and
`when_done()` does the same for a worker's answer.

*`test_close_ends_the_app.py` is load-sensitive, and that cost real time to establish.* It failed
three times today and was read, in turn, as order 0u item 6d reproduced, then as a regression from
the Pause work. It is neither: on a quiet machine it **passes**, and a faulthandler dump showed the
child starving in `walker.content_hash` at background I/O priority while four agents held the CPU.
`test_dynamic_workers.py` starves the same way. **6d remains open and unreproduced** - do not read
a red run of that file on a busy box as the nine-hour incident.

*New and worth knowing:* the Indexing page has a **Pause** button (it holds the run; Stop still
ends it; the page says whether the pause is yours or the machine's, because it runs through the
governor as one more reason to wait); `.doc` now reads its WordArt and **counts** what it cannot
reach inside embedded objects rather than paying LibreOffice for words LibreOffice does not have
either; the extractor registry loads on first read (38 fewer modules before the window).

*2026-09-27 note - the paragraph below is no longer true.* Order 0x §4a did the work it asks
for: the Start/Stop/Pause/Reset row is `app/ui/widgets/indexing_controls.py`, the bar is
`widgets/indexing_bar.py` (`GlidingBar`: a plain `setValue` snaps, `glide_to` slides) and the
"now" line is `widgets/indexing_headline.py`. `indexing_view.py` is 244 code lines against the
unchanged 250 guard, and `test_every_qt_view_keeps_its_logic_in_the_presenter` is green. Only
string-free code moved, so neither `test_pages_reorg` nor `test_ui_never_blocks` was edited.
Headroom is 6 lines: new Indexing-page code goes in a new `widgets/indexing_*.py`.

*One test is red on purpose, and it should stay red until somebody does the work.*
`test_presenter.py::test_every_qt_view_keeps_its_logic_in_the_presenter` says
`indexing_view.py` is **299 code lines against a 250 guard**, because the Pause button added
85 lines of genuine view code to a file that had already been split once for this. Moving
`start`, `stop` and `refresh_totals` out was tried and reverted: `test_pages_reorg` requires
"Stopping after the current file...", "Indexing..." and "Everything indexed so far is kept."
to be in **that file**, and `test_ui_never_blocks` requires `refresh_totals` to start a worker
and `signals.progress.connect` to appear there - so the two guards pull opposite ways and the
cheap move breaks the other one. **The guard was not raised to make the change pass**, which
is the same refusal the chat floor got. The real fix is a controls widget (the Start/Stop/
Pause/Reset row and its handlers) as its own file, the way `settings_shelves.py` and
`results_items.py` were carved out - an hour of careful work, not a line-count edit.

*Open, and honest about it:* order 0r 2b is not ticked - the warm minimum was already 1466 ms
**before** the lazy-registry change, across runs spanning 1466-11159 ms, and that spread cannot
support the claim either way; it needs a quiet machine. The test-suite widget leak was measured
(one file leaves ~120 widgets per test) and **deliberately left**: nothing walks all widgets any
more, and a blanket teardown cannot see a widget parked in a module global. `gpu_exclusive` is
process-wide only, so two Leasha processes sharing one card are uncoordinated.

**2026-09-20 (late) - the outstanding-work pass: what changed, what was decided, and the short
list that is still yours.** Read this first; the paragraphs under it are the earlier state of the
same day and are kept for the reasoning.

*What changed.* The full list is in `CHANGELOG.md` ("The outstanding-work pass"). In one breath:
twelve agents fixed the bugs this file used to list (`/oldest` order, the slow filter-only browse,
a worker exception hanging a run, the close-window hang, the results selection and offline-row
bugs, unbounded DB waits, four view/UI faults) and built what was outstanding (the Life Timeline,
a conversational Chat with optional web, video/audio on PyAV, in-process readers for `.doc`
`.ppt` `.pub` `.key` `.pages` `.numbers` `.mobi` and faster `.docx` `.pptx` `.xlsx`, a warm
LibreOffice fallback, a measured nightly, real-app UI journeys). The UI-responsiveness branch from
another session (`94677e7`) is merged: **the window's index run now lowers its own threads and
their children, not the whole process, and the command line still lowers the process.**
Nothing is pushed; the branch is `claude/outstanding-work-bugs-7edba5`.

*Decisions made on the owner's delegation (all recorded in the orders):*
- **Packaging** (`202626082213`): PyInstaller one-folder; per-user, per-machine optional; the
  installer asks where the index goes (default `%LOCALAPPDATA%\Leasha\Data`, checked against
  `REQUIRED_FREE_GB`); no update check inside the app; supported Windows 11 and 10 22H2, tested on
  11 only; unsigned until the repository is public.
- *2026-09-27 note - the item below is reversed. The owner dropped the PySide6 migration
  (`202626270238`, now DROPPED in the register): Leasha stays on PyQt6. Packaging no longer
  waits on the migration; it waits on an open owner decision about which licence a
  distributed build carries, since PyQt6 is GPL-3.0-only and `LICENSE` is MIT. Do not start
  or promote the order.*
- **PySide6 first.** PyQt6 6.11.0's metadata reads `GPL-3.0-only`; the project is MIT. The
  migration (`202626270238`, DRAFT, 0/10) is now the first item of Layer 9 and must precede any
  packaged release. It is not started, and it changes the venv the running app uses, so it wants
  its own session.
- **PST 4a:** retry a partial read next pass only when the cause was transient (Outlook busy).
- **Chat** stays RELEASED and is a conversation (local sources first, receipts kept for claims
  about your files). **Optional web augmentation is a recorded scope exception** (off by default,
  only a visible short query leaves, never a file name or passage) - `docs/PROJECT_INSTRUCTIONS.md`.
- **Video/audio** promoted (the file keeps its `-DRAFT` name), **off by default**: speech is about
  12x real time on the processor and the owner's `VideosMaster` (1,463 clips, 46 GB, 12.3 h) is
  hours of work. **PyAV only, no ffmpeg.** x264/x265 GPL DLLs ship inside the `av` wheel (never
  called): resolve that in `docs/THIRD_PARTY_NOTICES.md` before the venv is ever shipped.
- **Life Timeline** hold lifted and built. **A folder is not an Offline Media source** (no stable
  volume identity); the released tab label "Choose a drive or folder to catalogue" now
  contradicts the refusal - left unreworded, the owner's call.
- **`ORDER_REGISTER` housekeeping:** `ACTIVE_WORK.md` is retired as a tracker; the 0q session
  handoff was renamed so its checkboxes stop counting as an open order.

*Owner-run, and only these* (each is something no session here can do or should):
1. **Real-index run**: start a large index on the real data, type and switch pages while it runs,
   close the window, and read the log line `shutdown: window responsiveness this session` (beats,
   p50/p99/worst, stalls) and any "the window has not responded for N ms" dumps; then repeat once
   with `LEASHA_SWITCH_INTERVAL_MS=5`; keep the 1 ms default only if p99 is no worse and files/min
   drop little. Also the order 0u real-corpus ETA and the 9-hour "closed but still running"
   incident (`docs/WORKORDER-202626191300-indexing-that-works.md` 6d): one mechanism is closed
   (a second visible window), the cause of the 2026-09-17 case is not.
2. **How Outlook holds a `.pst`** (PST order 1e): needs Outlook left running with a `.pst`
   attached; a session here started it once and it exited. **Starting Outlook attached every
   archive in `D:\OutlookArchive` and moved their modified times to about 09:24 on 2026-09-20**, so
   the indexer will re-read them; nothing in them was written. `SCANPST.EXE` was also running on
   `2013.pst` and the `.log` files show real damage (FLT row failures, AMap errors).
3. **Register the nightly**: `scripts\install-nightly.ps1 -WhatIf`, then plain. Not registered.
4. **The PST scale run and the terabyte survey** (`owner-pst-scale-run`, `terabyte-scale`): hours
   of real-corpus work. **Offline Media II** hardware items (LTFS tape ordering, a UNC test) need
   hardware this machine does not have; 2a is out of scope and 2c deferred by the owner.
5. **Chat quality floor** (order 4b, 85%): no real model measured here reached it (see the chat
   order's dated notes for the figures); either accept a lower floor or choose a stronger model.

*Traps found this session* (add to §6 below): the app finds its `.env` from **its code's location**,
so it must never be launched from a worktree (a "Cannot start" box appears: use
`tests/unit/e2e_support.py::build_scratch_install`); a test that closes a real `MainWindow`
must run in a child process (`tests/unit/close_scenario_child.py` - it froze the run three
times); **LibreOffice 26.8 can crash or balloon to 8-11 GB on some real files**, so never loop
real LibreOffice, and its crashes are silent to the person now (error mode inherited by the
child); **a native crash in the suite is usually a leaked Qt widget, not a bug in
the test that died**  - three runs died with `0xC0000005` / `0xC0000374` inside one rail test
that passes alone. **Memory was blamed first and that was wrong**: it happened again at `-j 3` with
14.8 GB free. The real cause is that tests build real widgets and let Python drop them without
`deleteLater`, and `QApplication.setStyleSheet` re-polishes *every* widget alive in the process, so
on a long run the walk reaches one that is already gone. Reproduced with the fourteen files before
it, and neither half of those crashes alone - it is the *number* of leaked widgets, not one file.
Fixed by scoping that stylesheet to the widget under test; **the leak itself is still there**, so
anything else that walks all widgets (a theme switch in the running app) could meet it. `scripts/run_suite.py` also sizes its processes to free memory now, which is prudence, not the cure; `--timeout` on every pytest run,
because a hung test does not stop by itself - though **a test that looks hung on a busy machine is usually the resource governor doing its job**: `wait_while_throttled` pauses while other processes hold the CPU, which is why two agents reported a "psutil hang" that was nothing of the kind (the probe itself measures 25-35 ms here against a 2 s poll). A test whose pipeline must not be throttled passes `ResourceLimits(cpu_percent=0)`, as `test_offline_media.py` does; the `WORKORDER-*.md` name is reserved for orders
(the register counts every match).

**2026-09-20 - the "known red" list below is superseded: it is down from 57 to 2, and
most of it was four causes, not 57 problems.** The last full run
(`scripts/run_suite.py -j 4`) had 8 failures; five of those were fixed after it (project file,
one repo test, two staging tests, one timing test), which leaves **two**:

- **`test_presenter.py::test_every_qt_view_keeps_its_logic_in_the_presenter`** - a load-bearing
  guard. `indexing_view.py` was fixed (292 -> 241 code lines; layout and painting moved to
  `app/ui/widgets/indexing_layout.py`), which showed that **`results_view.py` (294) and
  `settings_view.py` (475)** had been hiding behind it. `settings_view.py` needs about 226 lines
  moved (its `__init__` alone is 178), and `test_pages_reorg.py` requires every pre-existing
  label and tooltip to stay verbatim *in that file*, so only string-free code can move. It is a
  refactor of the Settings screen and needs the `tools/grab_ui.py` goldens to verify.
- **`test_ui_redesign.py::test_the_rail_labels_are_the_tab_titles_verbatim`** - an owner
  decision (section 7).

**What the failures were.** About 22 tests failed because **the developer's home folder is itself
a git repository** (`C:\Users\JayminPatel(INDEFF)\.git`, created 2026-09-19 11:06, zero commits -
an aborted `git add` of the home folder for the *separate* JJOB project: 1,611 staged blobs,
914 MB, and a remote URL spelt `jaymin-patel` that does not exist; the real JJOB repository is
`D:\LocalSync\GDrive\jjobs`, four commits and level with its remote. **Deleted 2026-09-20 at the
owner's word**, after checking nothing unique was in it) and Windows' temp
directory was under it, so "this folder is not a repository" found one; the suite is now bounded at
its temp tree (`tests/conftest.py`: `enclosing_repo` and `GIT_CEILING_DIRECTORIES`). About ten more
read the machine (a real LibreOffice, a real captioning model, a clock captured at import, a
subprocess with no `SYSTEMROOT`/`TEMP`); about ten were stale against deliberate design changes
(`PARTIAL` status, "try again with fewer words", the reranker's three-failure budget, the QAction
rerank toggle) and were updated with the reason written in.

**Real bugs this turned up, all fixed with tests:**
- **An interrupted bulk index run left the word index unable to index again.** `drop_fts_triggers`
  writes a dirty flag then drops the FTS triggers; the resume-time rebuild repaired the rows already
  written but **never put the triggers back**, so every chunk indexed after a resume was silently
  missing from keyword search. Only with `bulk_fts=on`, but silent when it happens
  (`test_fts_bulk_recovery.py`; `CONTENT_TRIGGERS` in `migrations.py` is pinned to a fresh database).
- **`leasha --env X open <link>` ignored `--env`.**
- **Re-staging over an existing install failed on Windows** whenever a shipped file was read-only
  (`assets\leasha-logo.png` is): `scripts/stage.py` used a bare `rmtree`.

**Fixed 2026-09-20 - the filter-only browse plan** (kept for the reasoning). `type:pdf` with no
terms was planned by SQLite as "walk `idx_files_ext`, then sort": 177 ms at 200,000 files (50% pdf).
Forcing `idx_files_mtime` was 0.6 ms for pdf but 2.9 s for a type with no matches, so `INDEXED BY`
was never the fix. The adaptive query is in `app/search/keyword.py::_filter_only`: take up to `limit`
matching ids first; if that is every match, read them directly; otherwise walk the newest N files
(N = max(4 x limit, 400)) and use the result only if the window is full, falling back to the plain
statement otherwise. Measured at 200,000 rows, limit 100, min of 5: `type:pdf` 80 ms to 1.4 ms,
`type:txt` 76 to 8 ms, a type with no matches 0.03 ms; `type:xlsx` and `type:dwg` are 10 ms slower
(they pay a failed 400-row probe first - accepted). The strict `xfail` is now a passing test.

**Also worth knowing:** `test_prompt_examples`'s length cap moved 1,901 -> 2,100 (the prompt had grown
to 2,084 from `/on` and the video/audio kind words - raise it again only with a latency
measurement); the docs-header check now skips `_Knowledge/prompt_log/views` (generated, untracked,
"never edit it"); timing tests are load-sensitive, so two were rewritten to compare work rather than
wall clock.

**2026-09-19 (evening) - everything that can be built without the owner's machine is
built and merged, and the suite now runs to the end.** For whoever picks this up:

- **Restart the app.** The copy that was running predates all of this.
- **What landed on `main` today:** the three structural splits of the remediation order
  (`app/cli/` a package; `app/ui/presenter/` a package with worker bodies in
  `app/ui/tasks.py`; `SettingsController` and `IndexController` out of `shell.py`); the
  unreachable features wired in (People in photos window, saved-search dialogs, repo health
  note, restart-needed notes, the search check, deep-link registration); the **Chat tab**
  (`app/chat/`, `app/ui/chat_*.py`) and its `evaluate --chat` harness; **video and audio**
  (`app/cli media`, off by default); the UI Redesign's scenario tests and twelve working
  goldens; order 0r 2b's deferred page construction; order 0n's interactive Space Report
  table; and the suite fixes below.
- **Suite:** the last full run (`python scripts/run_suite.py -j 4`, eleven minutes, on the merged
  `main`) finished with **8,273 passed, 58 failed and no process crashed.** 57 of the failures are
  in the known families under "Known red" below; the other is
  `test_stages.py::test_a_second_run_reports_its_own_time_not_the_first_ones`, a wall-clock timing
  test that passes on its own and failed once under four-process load (timing tests here are
  load-sensitive: `test_idle_tune_and_space_report_ui.py`, `test_gui_scenarios.py::test_escape...`
  and the first-contact box have done the same).
- **Run it with `python scripts/run_suite.py`, not one long `pytest tests`** - see the
  2026-09-19 trap in section 6. It splits the files across processes and reports a process
  that died as CRASHED with the last file it started.
- **Real bugs the full-suite run found and fixed today:** the Chat page took keyboard focus
  on every visit, so arrowing down the rail stopped at Chat; `/type` did not offer the new
  `video`, `movie`, `audio` and `recording` words; a late search answer painted into a
  destroyed results view (`RuntimeError` inside a Qt slot); and, found by asking why Chat's
  controls did nothing, **the Chat tab's pins, removals and Fast/Thoughtful never reached the
  real engine** (its Qt tests ran against a fake). All four have tests against the real thing.
- **Known red, not regressions:** the git-repository detection tests (`test_repos_acceptance`,
  five in `test_cli_wiring`); `test_archive_reading` (3); layer 2/3/4 acceptance (4);
  `test_clip_lane_wiring` (2); `test_converter_discovery` (2); the two `test_deeplink` CLI tests
  (WinError 10106 on this machine); `test_docs_versioned` on `_Knowledge/prompt_log/views/*.md`;
  `test_git_view`, `test_match_marker`, `test_prompt_examples`, `test_query_plans`,
  `test_rerank_off_by_default`, `test_scale_limits`, `test_speed_work`, `test_staging` (2);
  `test_ui_redesign.py::test_the_rail_labels_are_the_tab_titles_verbatim` (an owner decision -
  section 7); and `test_presenter.py::test_every_qt_view_keeps_its_logic_in_the_presenter`,
  because `indexing_view.py` is 292 lines against a 250-line guard, which predates all of this.
- **Deliberately not built:** pywinauto black-box journeys and the scheduled nightly task
  (0m 3a/3b/5b - they need the owner's desktop); the Life Timeline (0n section 4, held);
  the PySide6 migration (held; *dropped by the owner 2026-09-27*); cloud volumes and cloud connectors (removed from scope);
  install and distribution (`202626082213`); on-tape ordering and the UNC test (0l - need the
  hardware); the OCR order's section 3 measurement; terabyte-scale and PST owner runs.
- **Built but not measured on the owner's machine:** 0r 2b (<1.5 s window-visible), 0n 3c
  (measured on 200,000 *synthetic* rows only), video/audio (ffmpeg was not found here and no
  faster-whisper throughput figure exists), Chat 4b/4c (real-model quality and latency).

**2026-09-16 — the shell is mid-redesign and the working tree is uncommitted.**
Work order `202626160950` (UI Redesign — one shell for Windows and macOS) was
drafted, released and built the same day, in a Linux sandbox that has no PyQt6.
What that means for whoever picks this up on the Windows machine:

- **Nothing from that session is committed.** `git status` shows ~14 modified
  files under `app/ui/`, `tests/unit/`, `docs/` and eleven new modules
  (`widgets/rail.py`, `toast.py`, `chips.py`, `segmented.py`, `search_home.py`,
  `skeleton.py`, `icons.py`; `rail_state.py`, `chips_logic.py`, `kind_badge.py`,
  `inspector.py`), `assets/icons/`, `tools/grab_ui.py`, four new test files.
  Commit by name once the Qt suite is green — the checkpoint rule in
  `WORKORDER-CONVENTIONS.md` §5a applies before anything that touches the tree.
- **The Qt half is untested until the Windows venv runs it.** The order's
  delivery note (top of `docs/WORKORDER-202626160950-ui-redesign.md`) lists the
  six commands in order; the 34 open items are exactly the ones those runs
  close. The Qt-free half (42 tests, the verbatim walk, the eight load-bearing
  tests) is green.
- **`self.tabs` no longer exists; it is `self.rail`** with the same surface.
  There is no `QStatusBar`; messages go through `MainWindow.notify(text, ms,
  level=)` to a toast. `interpret_button` and `rerank_toggle` are `QAction`s in
  the `⋯` menu, `scope` is a `SegmentedControl` with the combo's surface.
- **Three `*_view.py` files were over the 250-line guard before this order**
  (`test_every_qt_view_keeps_its_logic_in_the_presenter` was already red) —
  not this order's doing and not fixed by it; `search_view.py` is held at 249.
- `WORKORDER-space-report-and-idle-tune-ui-wiring` (raised by the crash-recovery
  session the same day) was picked up in the same pass: the Space Report is on
  the Reports page and the idle-tune scheduler is in the shell, tests in
  `test_idle_tune_and_space_report_ui.py`, unticked until the Windows run.

**Version 0.3.3. Eight of the nine live layers are code-complete; L8b is deferred by decision
and L9 has not been started. 6,509 tests collected, 2 deselected (JVM - see below) and 1
xfailed (real-Outlook COM, deliberately).**

Eleven layers were numbered and three are dead: L6 was removed, L7 and L10 were cancelled.
The line above counts the nine that are still live, and *code-complete is not the same as
verified* - three of them carry a check nobody has run yet, and those are §11's list.

| Layer | What it is | State |
|---|---|---|
| L0 | Foundation: config, errors, logging, single-instance, CLI | **Done** - acceptance suite passes |
| L1 | Storage: SQLite/FTS5, LanceDB, migrations | **Done** - schema at v16, acceptance suite passes |
| L2 | Extraction: PDF, Office, plaintext, Outlook/PST, chunking | **Code-complete** - one manual check left, see below |
| L3 | Indexing pipeline: walker, workers, resumable cursor | **Code-complete** - never run at real scale |
| L4 | Search: BM25 + ANN, RRF fusion, rerank, filters | **Code-complete** - measured, §3b. One acceptance box open: first search under 3s needs the real ONNX load |
| L5 | PyQt6 UI shell | **Code-complete** - and still where every fault is found, by opening it |
| ~~L6~~ | ~~Knowledge graph~~ | **Removed** - §3a |
| ~~L7~~ | ~~Office document builder~~ | **Cancelled** - never requested, never started |
| **L8a** | Natural-language query translation | **Code-complete** - justified by measurement, §3b |
| **Repos** | Repository awareness: `repos` table, `repo:`, `code` scope | **Phase 1 done** - see below. Phase 2 (history) **not authorised** |
| L8b | Prose answers over results | **Deferred** until L8a has been used in anger |
| L9 | Hardening and packaging | **Not started.** `docs/WORKORDER-202626082213-install-and-distribution.md` is a draft: three decisions taken, **five marked [FINALISE]** and none answerable from the code |
| ~~L10~~ | ~~Adaptive tuning~~ | **Cancelled** - speculative |

**The previous version of this table said "Layers 0-6 code-complete" and "1582 tests".** Both
were wrong: L6 was removed rather than completed, and the suite has more than doubled since.
A state document that has quietly gone stale is worse than none, because somebody acts on it -
which is the argument `ensure_log_dirs` already makes about its generated README, and it
applies here with more force.

### B4 answered: history search is its own job, not a mode of the search box

`HANDOFF-ui-to-backend.md` B4 asked for a decision between three products. The
UI thread's position was option 2 — working tree indexed, history queried live
and separately. **That is the answer, and it is now backed by a number rather
than a preference.**

`app.cli gitsearch` exists to produce that number. First run, against this
repository:

| | commits searched | elapsed |
|---|---|---|
| `git log -S` | 75 | **1.59s** |
| `git grep` (one revision) | 1 | 0.33s |

**Seventy-five commits already costs five times the entire 300ms budget.** That
is not a marginal call. `git log -S` diffs every commit, so the cost is
proportional to history, and this repository has one of the smallest histories
anybody will point it at.

This is one small repository on one machine, so **it is a direction, not a
extrapolation** — run it on the largest repository available and write those
rows in here before anything is built on top:

```powershell
venv\Scripts\python.exe -m app.cli gitsearch --repo "D:\SomeBigRepo" "connection string"
```

Every row carries what it was measured under, and a depth deeper than the
repository is flagged `representative: false` rather than reported as fact — a
"50,000 commits" figure taken against 800 commits is the sort of number that
ends up justifying the wrong build.

**What this means for the three asks:**

| Ask | Answer |
|---|---|
| Current branch / all branches | **Not buildable as asked.** Other branches are not on disk as files, so the walker cannot see them. It would need history indexed, which is the row below. |
| Current files / full history | **Live query, separate action.** Never behind Enter. |
| A specific commit | **Comes with the above**, as a `--rev` on the same live query. |

**The result row the UI asked for**, if and when it is built: `commit` (short
sha), `date`, `author`, `path`, plus the matching line. A hit in a file that no
longer exists is meaningless without the first three, which is exactly why they
are in the list.

**On GitPython:** it would not help with this. It mostly wraps the same `git`
subprocess, and what matters for a slow cancellable job is streaming, a hard
timeout and killing the process — all of which are more direct without it. It
earns its place only if phase 2 ever traverses commits and diffs as objects,
and this measurement is what says whether that is ever worth doing.

### Standing rule: nothing fails silently

From the owner, 2026-08-25, and it applies everywhere rather than to the one
case that produced it:

> For all things it should not fail silently it should notify in some way.

The case: a search returned sixty keyword hits and zero vector hits. There
*was* a warning — `no vector hits ... meaning-based search may not be working`
— and it went to the log. Visible to somebody running from a console, and to
nobody else. In the window the search looked like it had worked.

**A degraded result that is indistinguishable from a good one is the failure
nobody ever reports.** It is worse than a crash, because a crash gets fixed.

What this means in practice, and what has been done about it so far:

- **A log line is not a notification.** `SearchResponse.notices` carries
  degradations out to whoever is asking — `Notice(code, message)`, because the
  contract is that the UI never parses a message string to decide anything.
  `app.cli search` prints them above the results; `--json` lists them before
  `results`, because a degradation buried under twenty result objects has been
  reported and read by nobody.
- **The judgement lives in one place.** `keyword_count` and `vector_count` were
  already on the response for exactly this and were not enough: raw numbers
  mean every caller has to know the rule that turns them into a conclusion.
- **Say what still works, and name the remedy.** A warning without an action is
  a warning somebody has to research. Every notice says which half of search is
  unaffected and which command fixes it.
- **Confirm success too.** `app.cli stats` says *"all 3,355 passages have a
  vector"* when the stores agree — silence on success is indistinguishable from
  the check not running.
- **Three diagnostics this session could not see the problem they existed
  for**: `doctor` reported its own hardcoded defaults rather than the
  application's, `leasha --help` crashed for everyone, and `stats` printed both
  halves of the embedding gap in different sections and left the reader to
  notice. Assume a diagnostic is lying until it has been run.

**Still to do:** the window does not draw `notices` yet. Backend emits them and
the CLI shows them; `app/ui/` is the other thread's, and the task is filed.

### Repository awareness, phase 1 (schema v6)

The request behind this was a 56-flag specification for a git search platform.
The finding that set the scope: **source code was already indexed.**
`TEXT_EXTENSIONS` covers `.py .js .ts .cs .java .sql` and twenty more, so a
`.cs` file inside a repository on an indexed root has been searchable by
keyword and by meaning all along. The gap was not extraction, embedding or
search. It was that nothing recorded which repository a file belonged to.

So phase 1 is three things and no more:

- **Detection during the existing walk.** `.git` is already in
  `DEFAULT_EXCLUDE_DIRS`, so the walker stood next to the evidence on every
  pass and threw it away. It now notices, at the cost of a membership test
  against a list `os.walk` has already built. Handles `.git` as a *file* -
  submodules and linked worktrees - which anything looking only at the
  subdirectory list walks straight past.
- **`repo:` filter and a `code` scope.** `code` means *in a repository*, not
  *has a code extension*; `type:code` still answers the second and is
  untouched. A `.md` in a repository is in scope, a `.py` in Downloads is not.
- **`app.cli repos`**, so this is checkable headless before any UI exists.

No settings, no new error codes, no git subprocess, no new extractor. Schema
v6 is additive - a new table and one nullable column - so an existing 100GB
index gains it in seconds and needs no re-index. `repo_id` stays NULL until
the next indexing run attributes it.

**Two bugs worth knowing about, both found by running it rather than reading
it.** `repo:a,b` returned nothing, because each name became its own AND clause
and a file belongs to exactly one repository - repeated names now OR. And
`repos.name` stored the full path rather than the basename, because
`Path(r"D:\SearchProject").name` does not split backslashes off Windows;
`_basename` exists in `sqlite_store.py` for exactly that and is now used.

**Phase 2 - history search - is deliberately not built**, and is gated on a
measurement rather than an opinion. Searching a repository's full history is
O(commits x changed files); on a 50,000-commit repository that is minutes,
against a contract of p95 under 300ms warm. Before any of it is designed,
`app.cli gitsearch --repo <path> --rev <expr> "<pattern>"` needs to be run
against the largest repository available and its numbers written here: elapsed
at 1k, 10k and 50k commits, and peak memory. Those decide whether it can be a
mode of the search box or has to be a separate, explicitly slow, cancellable
job wired to its own button. Building the UI first is how the 300ms budget
gets lost by accident.

### Session close, 2026-09-20 - PST resilience and the real-window pass

**Two things landed, both merged with `origin/main`.** (1) `WORKORDER-pst-resilience.md`
(register row 0v, 11 done / 7 open): a `.pst` held open is now `ERR_FILE_LOCKED` (retried),
not `ERR_FILE_CORRUPT` (settled); `auto` falls back to Outlook when libpff finds it held;
one bad message costs one message; skipped items are counted (`ERR_PST_PARTIAL`, the CLI
`Partial` line). (2) The UI Redesign order's 2026-09-20 note: six faults found by grabbing
the real window at 125% - pill text clipped, a grey box behind every label, an unreadable
toast, result rows wider than their pane, missing page margins, the taskbar pin. The
owner decided the rail entry **stays "Offline"** and the test now says so.

**What is now untrue if you read older text:** "Offline Media" on the rail (it is
"Offline"); the register's old "owner decision waiting" for it (removed); the claim that
offscreen goldens show what the owner sees (they do not - see the trap "Look at the real
window"); `test_the_rail_labels_are_the_tab_titles_verbatim` is no longer red.

**Superseded by the section at the top of section 3 (2026-09-20, late): items 2 and 3 below are done, 1 is
mostly done (1e and 6c: 6c measured, 1e still needs a live Outlook), and 4 stands.** The original list:

**What the next thread needs, in the order to do it:**

1. **Owner-run, PST (order 0v):** the owner's archives are probably in `D:\OutlookArchive`
   (seen on the taskbar; not searched). 1e: with Outlook running and a `.pst` attached, run
   `app.cli extract "<that.pst>" --limit 50` - how Outlook holds a `.pst` is unmeasured.
   6c: damage a copy of a `.pst` and index the copy; no damaged archive has ever been tried.
2. **Code, PST:** 3d (show the `ERR_PST_PARTIAL` count in the Indexing tab, with a pytest-qt
   scenario), 3e (`pipeline.py`: a warning on an *unchanged* last message is not counted -
   count warnings before the `_already_current` skip), 5a (key `drain_busy_folders` by store).
   4a is the owner's call: retry a partial read next pass?
3. **UI Redesign 9j is the only open box.** `tools/bench_results_paint.py <tree>` against a
   `git worktree` of `3da478a` and of this tree, three runs each, alternating, on an idle
   machine; compare the minimum. This machine differed 3-4x run to run, so no verdict was
   given. Separately noticed and unaddressed: building results costs about 3.5 ms a row
   (2,000 rows took 7 s, the same before and after the redesign).
4. **Not done on purpose:** a pinned taskbar button was not observed (the running button
   was); `set_window_relaunch` is the unverified fix - unpin and re-pin Leasha to test it.

### What is **Next**

> **Read `docs/ORDER_REGISTER.md` first — it is now the register.** This section is
> the reasoning; that table is the state. Every order, its status, its queue position
> and its done/open count live there, counted from the checkboxes rather than
> asserted. Before it existed the queue lived here and each order's `**Status:**`
> line pointed back at this section, which is a circular register — and by
> 2026-08-30 this half had gone stale while the orders had moved on.
>
> **Corrected on 2026-08-30, against the tree at `e3c9682`.** Three things below were
> wrong in the most misleading direction available, which is the same fault this
> section already records itself committing once:
>
> - **`202626270046` (0a) is finished** — 25 items, 0 open — while its header still
>   reads ACTIVE and this section still queues work behind it.
> - **`202626270257` (0d, privacy defaults) is finished** — 9 of 9.
> - **`202626270326` (0e, workspace features) is a third built, not future work.**
>   §1 the colour log, §2 the pop-out preview windows and §3a the global-hotkey
>   mini-search shipped in `aa9fb28`, `ec40c40` and `b1fdd7c`. 16 of 30 items are
>   ticked. The text below still describes the whole order as something to start
>   after the search-experience order.
>
> Also corrected: the L1 row above said schema v13; `migrations.CURRENT_VERSION` is
> **16**.
>
> **Corrected again on 2026-09-07 — six orders are now SHIPPED, and the numbered
> text below is stale for every one of them.** Read the register, not this list.
> `202626270157` (0c) closed at 26/0, `202626270326` (0e) at 30/0 and
> `202626271137` (0s) at 17/0; `202626270257` (0d), `202626270509` (0g) and
> `202626271317` (0p) turned out to have been finished for some time and needed
> only their status corrected — the "finished or lying" case the register's §4
> warns about, found by recounting rather than by reading the table.
>
> Two further corrections of the same kind: `migrations.CURRENT_VERSION` is now
> **18** (v17 pHash, v18 `files.taken_at_ns`), not 16 as the line above says. And
> `202626270510` (0h) stands at 12/1 **deliberately** — its last item's proof needs
> the real CLIP model, which the build sandbox cannot download; the order carries
> the command that closes it on a machine which can.
>
> **Corrected a third time, same day — `202626270508` (0f) is now SHIPPED too, at
> 17/0**, closed by a second batch of four parallel lanes: the shot date now reaches
> result display and sort order (not just storage and the `after:`/`before:`
> filter), and the OCR ladder's rung-1 white-fraction cutoff is a real, labelled
> setting on the Index Tuning screen rather than a hardcoded literal.
> `202626271601` (0r, the splash) moved from 15/3 to **17/1** in the same batch — a
> stale pytest-qt checklist item (the same "note never revisited after the
> underlying fix landed" pattern as 0c/0e/0d/0g/0p) is now honestly ticked, a real
> startup-import-order bug was found and fixed (the search/storage stack was
> importing before the splash ever showed), and Mail/Code tab construction now
> defers past `window.show()` — but Files/Indexing/Settings still don't, so §2b
> stays open and 0r stays `RELEASED`. Full account, including the measured numbers
> and the two pre-existing test failures ruled out as regressions, is in
> `docs/ORDER_REGISTER.md`'s own 2026-09-07 second-pass note.
>
> **Corrected again, 2026-09-15 — both stale in the direction this section
> keeps finding itself wrong in.** `202626270510` (0h)'s "12/1 deliberately"
> two paragraphs up is no longer true: run from this session, on the real
> machine rather than the build sandbox its last item was gated on, `0h`
> closed at 13/0 the same day. And `202626270513` (0k,
> offline media drives) is now SHIPPED at 17/0 too: §3 (search/browse
> decoration) was the one section this document's queue list below still
> describes as future work, and it closed the same session — the inline
> offline-volume badge on a result row, preview from the index working
> offline (a real, pre-existing bug fixed along the way: opening a file
> found by browsing to a catalogued volume in the Files tab tried its
> letter-free synthetic path directly, exactly the bug 1b/3a had already
> fixed once for search results), and the Files tab's volume picker. Full
> account in `docs/ORDER_REGISTER.md`'s own 2026-09-15 notes and the order
> files' own dated entries.
>
> **Corrected a further time, 2026-09-16 — `202626270514` (0l, offline
> media network/cloud) moves from 8/9 to 12/5.** The queue list below still
> reads as if 0k's tab, which 0l's own §1a/1d/3d/3b-3 were waiting on, had
> not shipped - it has (see the correction just above), and all four closed
> the same day it did: 1a's interactive rename-suggestion dialog, the tab's
> own help line (1d and 3b-3 share it), and the online-only results badge
> (3d), riding 0k's own badge machinery exactly as the order asks. **2a and
> 2b were assessed, not attempted** - both are real, substantial features
> (a cataloguable cloud volume kind with a browser-routed Open action; a
> folder-scoped download opt-in with a size cap) and neither shares much
> beyond the read-guard 0l's own §3 already proved, so building either
> partially and calling it done would have been the confidently-wrong kind
> of state this document warns about elsewhere. 3b-2's on-tape ordering and
> one §4 test stay open for the reason they always have: no LTFS tape or
> mapped network drive exists on this machine to check real behaviour
> against. Full account in `docs/ORDER_REGISTER.md`'s own 2026-09-16 note
> and the order file's own dated entries.
>
> **2026-09-15 — 0b's remainder: one of five closed, four re-verified still
> blocked.** `docs/WORKORDER-202626270114-index-tuning.md` §6c (numpy/pyarrow
> end-to-end) is done: `Embedder.embed()` returns a float32 `numpy.ndarray` instead
> of widening to float64 and `.tolist()`-ing it, `VectorStore.add` builds one
> `pyarrow.Table` per batch directly instead of a `list[dict]` LanceDB converted a
> second time, measured 38.6% faster (median, every trial individually faster) on a
> real LanceDB write path. §5e, §6d, §6h and §6i were each
> re-checked against current code, not assumed unchanged, and each is still blocked
> on the same thing the 2026-08-27/2026-09-05 notes already found: §5e needs an
> idle-detection scheduler in `app/ui/shell.py`, a file several concurrent worktrees
> are editing this week; §6d needs a third `FileStatus` and an owner decision
> about what "indexed but not embedded" means; §6h needs a new `onnx` dependency
> or an unvetted external model repo, and fastembed's catalogue still offers neither;
> §6i's own condition (§6a shows conversion matters) is not met. 0b moves
> from 35/5 to **36/4**. See `docs/ORDER_REGISTER.md`'s own 2026-09-15 note and the
> order's per-item dated notes for the full account.

In order, and grouped by what is actually blocking.

**Start with `docs/REVIEW-2026-08-26.md`.** Five parallel review passes over the current
tree, every finding checked against `file:line` before publication: **11 High, 20 Medium**,
and eleven of the previous review's forty-two findings still live. Its own priority plan is
better sequenced than anything that could be restated here, so it is the list rather than a
line on the list. The headline items are not cosmetic - `.doc` conversion is dead on Windows
while Settings reports it working (H10), a broken embedding model fails the *whole* search
instead of degrading to keyword (H4), every skipped file is re-extracted on every incremental
run forever (H1), and a database migrated through v10 permanently loses the two indexes that
make "newest first" and `after:`/`before:` fast (H2).

**An earlier draft of this section claimed the opposite** - that nothing outstanding was code
that had not been written, and that everything left was a measurement, a run or a decision.
That was written before this review was read, and it was wrong in the most misleading
direction available: it would have sent somebody to packaging while `.doc` files silently
fail to convert on the platform that ships. The sequencing below now puts the review first.

**Correctness, from the review — this week**

0. `REVIEW-2026-08-26.md` §"Priority plan" in its order: H4 (vector degrades rather than
   fails), H10 (`resolve_binary` at `converter.py:333`), H1 (skipped files re-extracted),
   H2 + H3 (the two migration repairs, **before anybody migrates a large index**), then the
   three one-liners: M7 `quoted_removed`, M1 shutdown-guard hoist, M9 clear-search.

**From the owner, 2026-08-27 — two new orders, in this sequence**

After the review's open search items (H5, H6) are closed:

0a. **`docs/WORKORDER-202626270046-slash-menu-context-and-metadata.md`** (ACTIVE) —
    context-aware `/` menu with value counts, and the CLI half: `leasha shell` on
    prompt_toolkit with a live dropdown as the primary deliverable, PowerShell
    tab-completion for one-shot commands. Its header carries the internal sequence.
0b. **`docs/WORKORDER-202626270114-index-tuning.md`** (RELEASED by the owner) — the
    Index Tuning section on the Indexing page: Defaults / Auto-tune / Manual modes,
    ComputeProfile detection with machine-derived envelopes (this machine: i7-1365U,
    2P+8E hybrid, 32 GB, Iris Xe — which IS DirectML-capable; its §0 baseline block
    has the measured facts and three corrections they force. A discrete-GPU machine
    follows later — the design must light up on it with zero reconfiguration), and
    the §6 pipeline speed work gated by stage timers. It
    reworks `pipeline.py`/`resources.py`/`embedder.py`/`vector_store.py` — do not
    interleave it with other pipeline work.

0c. **`docs/WORKORDER-202626270157-search-experience.md`** (RELEASED by the
    owner) — after index tuning: the universal first tab (typo tolerance,
    visible relaxation, plain notices, recency + version folding), the
    per-surface `SearchPolicy` seam with every behaviour on-by-default and
    switch-off-able, the rules-based translator (built now, tuned later — the
    owner's agenda puts tuning after indexing), coder search (paste-an-error
    verbatim routing, open-in-editor at line, `/changed` pickaxe), more-like-
    this, attachment-first results, and tooltips-state-the-effect everywhere.
    Its §0 carries the owner's product principles; read them before starting.

0d. **`docs/WORKORDER-202626270257-privacy-defaults.md`** (RELEASED by the
    owner) — small and self-contained, schedulable into any gap: new installs
    default the index to `%LOCALAPPDATA%\Leasha` (choosable as today), roots
    start empty with own-profile suggestions, the shared-computer paragraph
    goes into README and installer, and the owner's existing install is
    grandfathered untouched. Its decisions are settled by the owner — do not
    relitigate them.

0e. **`docs/WORKORDER-202626270326-workspace-features.md`** (RELEASED by the
    owner, full scope) — after the search-experience order: colour-coded
    actionable log with pop-out + stay-on-top, copy-not-move pop-out previews
    (find/print/zoom/rotate incl. PDFs, per-file rotation memory), global-
    hotkey mini-search, drag-out, pinned working set, timeline strip, viewer
    upgrades (SVG/TIFF/Markdown/spreadsheet grids/EPUB/HEIC/LibreOffice
    full-layout button), and DWG via the user-installed converter path —
    subprocess only, never LibreDWG's bindings (licence rule, enforced by
    test). Everything view-only: no code path writes to a user file.

**Owner, 2026-08-28 — the design-day batch, RELEASED, in this exact order
after 0e (each order's header carries its scope discipline — the tangent
guard; if an idea isn't in the order being executed, it belongs to another
order or to the owner):**

0f. `WORKORDER-202626270508-media-by-default-and-ocr-ladder.md` — images on
    by default, the universal OCR ladder, EXIF-date rule, image hygiene.
    Foundation for everything below.
0g. `WORKORDER-202626270509-mbox-takeout-chats.md` — mbox extractor (MUST),
    Takeout-as-files, the index-sensitivity sentence. Small; any gap.
0h. `WORKORDER-202626270510-pictures-one-clip-lane.md` — CLIP lane, reverse
    image search, pHash/burst folding, grid+lightbox.
0i. `WORKORDER-202626270511-pictures-two-tags-and-enrichment.md` — Florence
    tags as AI-labelled segments, the unified enrichment backlog, on-demand
    Describe, offline places, era hints, /shows.
0j. `WORKORDER-202626270512-photo-tagger-people.md` — people naming, Google
    Photos UX fully local, off-by-default guardrails, /who.
0k. `WORKORDER-202626270513-offline-media-one-drives.md` — the killer case:
    volume identity, letters never stored, manual Scan/Rescan/Delete, the
    Offline Media tab. Deepest storage change — no interleaving.
0l. `WORKORDER-202626270514-offline-media-two-network-cloud.md` — network
    shares (no credentials ever), cloud mounts (never hydrate by accident),
    the per-file placeholder model.
0n. `WORKORDER-202626270602-reports-and-timeline.md` (RELEASED) — scheduled
    AFTER 0l and BEFORE 0m: the Reports section (Digital Inheritance
    catalogue-book PDF, the Space Report with uniqueness warnings) and the
    Life Timeline browsing surface. Read-only over existing tables; 0m stays
    last so its scenarios cover these surfaces too.

0o. `WORKORDER-202626271137-review-adoptions.md` (RELEASED) —
    **gap-schedulable**, items independent with per-item prerequisites: the
    seven adoptions from the owner's five-AI review ("Why this result?",
    match-type indication, saved searches, selection-to-search, mini-search
    count chips, spreadsheet cell locators, leasha:// links). Import nothing
    from those documents beyond these seven. 0m still last.

0p. `WORKORDER-202626271317-tables-sort-and-alignment.md` (RELEASED,
    gap-schedulable) — owner's report: sorting on header click goes global
    across every table (SORT_ROLE payloads everywhere, id-keyed selection,
    ranked views get a restorable "Relevance" order superseding
    files_view:111's deliberate refusal with its reasoning honoured), and
    every header aligns the same way as its column (shared spec in
    ResultTable, walker test).
    *Note 2026-08-28: v1.1 adds owner's §4 — the main window remembers
    its last state (maximised/normal + geometry) across launches, with
    the off-screen/minimised edge cases handled. The order also records
    the owner's Indexing-page-layout report as OUT of scope (deferred to
    the pages reorg).*
    *Note 2026-08-28 (later): §5 added — column widths forgotten, FIFTH
    report. Owner: LOW priority, do LAST in this order. Diagnose via the
    planted DEBUG lines first; deliverable includes the real-mouse
    pywinauto drag regression test this bug class has never had.*

0r. `WORKORDER-202626271510-results-presentation.md` (RELEASED,
    gap-schedulable) — the results rows made world class: snippet windows
    centred on the match and cut at sentence boundaries (two lines in
    comfortable), a painted expansion chevron ("matched in 5 places"),
    real file icons, sender-first mail rows, monospace code snippets,
    left-elided locations + twin disambiguation, friendly dates
    (register-gated), hover/pixel-scroll/terminator, the stable-update
    rule, keyboard-first flow from the search box, and an accessibility
    verification pass. Scope boundary: does NOT touch 0157 §2e's
    thumbnails, 0p's tables, or 0o's landed markers.

0s. `WORKORDER-202626271601-splash-and-fast-lifecycle.md` (RELEASED,
    gap-schedulable) — the branded splash (design settled by the owner on a
    live mock — navy, signature stripe, tagline "Forgets nothing. Tells no
    one. Outlives the drives.", five rotating killer-case lines with vector
    icons on ALL moments), plus startup made fast underneath it (splash
    <300ms, deferred window population, honest handover wait, installer
    model prefetch) and close made instant (hide-first, timed tail,
    PRAGMA optimize to idle, os._exit after clean store close). Measured
    numbers recorded in the order. Logo asset: assets/leasha-logo.png.

0q. `WORKORDER-202626271328-pages-reorg.md` (**DRAFT — do not execute**) —
    the pages reorg, ready and waiting: Settings gains categories + filter
    box + last-category memory (labels relocate verbatim, registry surfaces
    updated so test_settings_reachable enforces); Indexing splits into
    Status · Schedule · Tuning (0114's screen slots in whole) and the
    owner-reported broken layout is fixed BY the split. Promotion
    condition: 0114 and 0157 fully ticked — the owner promotes, this line
    then gains a queue position.

0m. `WORKORDER-202626270547-test-automation.md` (**HELD by the owner
    2026-08-28 — do not start; he will say when.** Was RELEASED/LAST; the
    LAST intent stands at release. The per-order pytest-qt scenario
    convention continues meanwhile.) — **LAST of the
    batch, deliberately**: pytest-qt scenarios pressing real keys in the
    assembled app (every order's acceptance sentence becomes a scenario),
    hypothesis property tests for the parser-shaped code, five pywinauto
    black-box journeys, theme-golden visual diffs, and the nightly system
    loop on the owner's machine + windows CI for contributors. Owner has
    installed pytest-qt/pywinauto/hypothesis.

HELD (not for execution until the owner promotes it):
`WORKORDER-202626270611-chat-tab.md` — the Chat tab, fully designed: agentic
retrieval loop, no-sentence-without-a-receipt verification, aggregate
questions answered by queries not generation, absence protocol, inline
result-set answers, context shelf, measured floors before shipping. The
owner will schedule it himself; promotion adds a dated note to the old
search-and-chat scope order. Do not start.

Draft (not for execution): `WORKORDER-202626270515-video-audio-DRAFT.md` —
    video/audio epoch, promoted only by the owner after the picture stack.
    Phones-as-drives (decided) also await their own future order.

Owner, 2026-08-27: the `cli.py` package split (review §"Priority plan" item 12 /
remediation order §7) is deliberately **last** — after every order above has
landed, when nothing else is feeding the file. Do not fold it into the
slash-menu or index-tuning work.

Owner, same day, generalised: **a fully working version comes first.** All of
remediation §7 (the presenter split, the shell controllers, the cli split) and
any other restructure-for-its-own-sake waits until the feature orders above are
done and the product works end to end; then optimisation and splits get looked
at together. Fixes and measured performance work are not deferred by this —
only reorganisation is.

**Verification: things the fixtures cannot tell us**

1. **Index one full 200K-message PST**, and do the ~50GB pass. The largest unknown in the
   project. L2 and L3 are code-complete against fixtures, and a fixture cannot find what a
   real archive will.
2. **Outlook COM, once, with Outlook open.** `Win32ComSession` is the only code that talks to
   COM and no test can exercise it - the one xfail in the suite is exactly this. Until it has
   run once and the counts look sane, L2 is honestly incomplete.
3. **The full suite on Windows**, not the non-Qt subset, plus `doctor` reporting READY and
   somebody opening the window and using it.

**Measurement: two questions that should not be settled by argument**

4. **Plain semantic search over twenty real sentences** - does meaning-based retrieval earn
   its place at all, on this corpus?
5. **Chunk size and model precision** - §7, question 1. `app.cli evaluate` before and after.
6. **Re-tune `AND_TERM_LIMIT`** against a realistic corpus, and close L4's last acceptance
   box: first search under 3s, which needs the real ONNX load.

**Decision, then build**

7. **Layer 9: hardening and packaging.**
   `docs/WORKORDER-202626082213-install-and-distribution.md` is drafted and its own §7 says
   not to start building until **five [FINALISE] questions** are answered: freeze with
   PyInstaller or ship `uv` plus an embedded Python; per-user or per-machine; where the index
   defaults to on a machine whose C: drive is not 150GB; whether the app checks for its own
   updates; and the minimum Windows version. Each changes what gets built, and none can be
   answered from the code.

**Open the window and use it** is not a numbered item because it is continuous, and it is
still how every UI fault here has been found - by clicking, never by a test. The six reports
fixed on 2026-08-26 all came from the owner doing exactly that.

**Closed since the last revision of this list.** The three items that used to sit at the top
are done: `WORKORDER-202626081059-search-quality.md` (all seven findings),
`WORKORDER-202626081149-code-tab.md` (repository attribution can be undone, and the tab no
longer hides files silently), and `WORKORDER-inbound-ui-fixes.md`. So is the run-lock work -
the command line and the window can now index without excluding each other, and the window
draws a run it did not start.

The file-type work order is **complete** - all six steps, plus the follow-on work in 0.3.3.
OpenDocument and Google Drive pointers read natively, Tier 2 converters cover a dozen dead
Office formats through LibreOffice and pandoc, OCR reads images and scanned PDFs, and
AutoCAD `.dxf` is read for its notes and title-block attributes. `app.cli formats` prints
what is on and what is off.

**Settings now manages file types rather than listing them.** Every supported type is
shown - including the ones claimed in code, which the table used to omit entirely, so
there was no `.pdf` row and no way to switch PDFs off. Each carries a **Status**: ready,
limited, cannot read, or off, with the exact fix command on the row. Double-click edits a
type's reader and size cap; **Reset to defaults** deletes the override file, restoring
every shipped type, reader and limit.

`app/core/format_health.py` is the single source of that status, read by both Settings and
`doctor.py`. **An extractor with a new optional dependency declares it with `requires` and
appears in both places without either being edited** - see `docs/adding-a-file-type.md`,
which is the guide to adding a format at any of the three tiers.

Two known gaps, both deliberate: `.dwg` needs LibreDWG's `dwg2dxf` on PATH and ships
disabled, so DWG files are found by name only until it is installed and switched on; and
the JVM-starting mpxj tests are excluded from the default run (`pytest -m jvm` to run
them), because `startJVM` can take the host process down with a Windows access violation
and lose the whole suite's result with it.

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
- **A held-open archive is a lock, never corruption (2026-09-20, `WORKORDER-pst-resilience.md`).**
  `looks_locked` (`extract/base.py`) tells the two apart from libpff's message, measured on this
  machine. `ERR_FILE_LOCKED` is retried every pass; `ERR_FILE_CORRUPT` is settled, so mislabelling
  a lock dropped the archive from the index for good. In `auto`, a lock on the libpff path falls
  back to Outlook - only before the first message, or the archive is indexed twice.
- **Skips are counted, not only logged.** `ERR_PST_PARTIAL` rides on an archive's last message
  and reaches `warned_by_code` and the CLI's `Partial` line. **Not yet true:** the Indexing tab
  does not show it, and on an incremental run whose last message is unchanged it is not counted
  (order 0v, 3d and 3e). How Outlook holds a `.pst` it has attached is unmeasured (1e).

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

**Known gaps:** loose `.eml` files on disk record attachment *names* only.

**2026-09-23:** the libpff backend used to do the same - names only, no content, because
reading attachment bytes through libpff means walking a MAPI record set rather than the
one-line `SaveAsFile` Outlook COM offers. Fixed: `pypff.attachment` exposes `get_size()`
and `read_buffer(size)`, which is enough. `pst_libpff._attachment_documents` now mirrors
`email_pst._attachment_documents` - same size cap, same content-hash dedup, same
`virtual_path`/`meta` shape - so both backends extract attachment contents identically.

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

### 5a. Decisions taken in conversation, written down 2026-08-30

Everything above was already here. Everything below was settled by the owner between
2026-08-26 and 2026-08-29 and existed only in an assistant memory note or in a chat
transcript — which is to say it was one lost thread away from being reopened blind.
Dated, because several of them supersede an earlier position.

| Date | Decision | Made by | Why | Supersedes |
|---|---|---|---|---|
| 2026-08-27 | **Working version first.** No structural refactor — `cli.py` split, `presenter.py` split, `shell.py` controllers — until the feature orders are done. Bug fixes and measured performance work are exempt | Owner | He intends to publish, to friends and to children's machines. A working product beats internal tidiness | — |
| 2026-08-27 | **Progressive disclosure by tab.** Tab one is the universal surface and must pass the eight-year-old test; Files, Mail and Code are power surfaces and keep the `/` grammar. Same engine, different contracts | Owner | — | — |
| 2026-08-27 | **DWG via a user-installed converter, subprocess only.** LibreDWG or ODA File Converter, detected like LibreOffice, never bundled, never via Python bindings | Owner | Linking would impose GPL on the app; invoking a binary the user installed is mere aggregation | — |
| 2026-08-28 | **Offline Media, not "removable drives".** The category is anything catalogued then disconnected; drives are only kind 1 | Owner | A further use case was coming that the narrow name would not have covered | The removable-drives framing of 2026-08-27 |
| 2026-08-28 | **Fully manual.** A source joins the list because the user pressed Scan. No arrival prompts, no automatic anything | Owner | "Nothing happens to a removable drive unless you pressed the button" — the trust, kid-proof and borrowed-stick answer in one | An earlier auto-offer design |
| 2026-08-28 | **Drive letters are never stored.** Identity is the volume GUID plus hardware serial; paths are `(volume_id, relative_path)` resolved at open time. Network sources normalise to UNC | Owner | The letter is assumed different on every plug-in | — |
| 2026-08-28 | **EXIF `DateTimeOriginal` is the date for photos**, falling back to file time only when absent | Owner | File mtime is a lie on old photo corpora — twenty years of drive-to-drive copies reset it. Without this, `after:`, era hints and the timeline give confidently wrong answers on exactly the shelf-drive corpus that motivates the feature | Using file time uniformly |
| 2026-08-28 | **Face detection and clustering automatic; identity only ever from the user.** Off by default, one plain-words switch, deletable, names never leave the machine | Owner | The principled line moved from "no faces" to "no *automatic* identification" — the same place digiKam, Immich and Apple landed. Face embeddings are biometric-adjacent, so the guardrails are load-bearing, not decoration | The earlier faces-never rule |
| 2026-08-28 | **"If we can index, we will index."** No content policing, no format deny-lists, no header sniffs. Leasha reads exactly what the login can read | Owner | It is a lens, not a censor. The onus to encrypt sits with the generating system. Folded in by disclosure instead: indexing copies text, so the index is as sensitive as the most sensitive thing in it | An assistant-proposed skip-list and content guards |
| 2026-08-28 | **Phones are Offline Media kind 4**, with the landing folder as the recommended daily flow | Owner | — | Phones as a separate strand |
| 2026-08-28 | **Cloud sources only through the vendor's own desktop mount.** No OAuth, no network code in Leasha. API connectors deferred indefinitely | Owner | It would bend the "nothing is ever sent anywhere" paragraph, and start a connector treadmill. The fair ask is "install Google's own Drive for Desktop" | — |
| 2026-08-28 | **Slow scans of photo and video drives are accepted** — "expected, small price to pay" | Owner | Never trade corpus coverage for speed on media drives. The ladder and trickle enrichment manage the cost; they do not cut the corpus | — |
| 2026-08-28 | **mbox is a must; bookmarks are withdrawn; calendar and contacts dropped** | Owner | One stdlib extractor unlocks Takeout Gmail, Thunderbird and Unix mail. Sync products own the bookmark space | — |
| 2026-08-28 | **History search is its own job, not a mode of the search box** | Backend measurement, ratified by owner | `git log -S` cost 1.59s over 75 commits against a 300ms budget, and the cost is proportional to history. Not a marginal call | Option 1 and option 3 of `HANDOFF-ui-to-backend.md` B4 |
| 2026-09-27 | **Indexing in a separate process stays optional for good.** The setting (Indexing › Tuning › Strategy) is permanent; the in-process path is kept. The 2d measurement decides only the default | Owner | The user keeps the choice either way | Order 0x item 2e's "then retired in a later change" |
| 2026-08-26 | **`LICENSE` added: MIT** | Owner | — | **Reopens a closed question.** `202626082213` concluded free SignPath code signing was unavailable *because* the repository had no OSS licence. That premise no longer holds — see `docs/ORDER_REGISTER.md` §5 |

## 6. Traps

Things that have already caused real failures, or will.

**2026-09-19 - the test suite died silently three times in one process. Run it with
`scripts/run_suite.py`.** `python -m pytest tests` ended part-way (about test 3,800 of 8,400)
with exit code `0xC0000005` (-1073741819, a native access violation), **no traceback, no
Windows event and an empty stderr**, so every later test simply never reported - and `-q`
output looks the same whether a run finished or the process vanished. Cause, shown by a fault
stack written to a file and by the same tests completing when the process was kept off the
card: `EMBED_DEVICE` defaults to `auto`, which on a machine with DirectML is the graphics card,
so the real-model tests (embedder, CLIP, reranker, OCR) built DirectML ONNX sessions inside the
pytest process, and after enough earlier tests had loaded torch, pyarrow and more ONNX sessions
a later run - RapidOCR's text detector, inside `InferenceSession.run` - crashed the process.
It also meant the suite competed with the running app for the same card. **`tests/conftest.py`
now pins the suite to the processor** (`EMBED_DEVICE=auto` in the environment opts back in on
purpose). It may be a relative of the `onnxruntime` / `onnxruntime-directml` overwrite trap
below (UNCONFIRMED - it was not checked which binaries were installed at the time). **A second
native crash is open and unfixed:** one run died with an access violation in
`app/ui/view_options.py` (`look`, the column-width watcher's timer) during a Qt event pump
between tests. Its cause is not established and it has no reproduction; that code has a
four-attempt history of column-width bugs, so it was left alone rather than edited blind. If
the app itself ever vanishes on close, start there. `logs/crash/crash.log` is where the app's
own fault handler writes; the test process's is written where you point `faulthandler` at it.

**2026-09-13 — `onnxruntime` and `onnxruntime-directml` overwrite each other, and
the venv is currently half-uninstalled.** The two distributions unpack into the
same `venv\Lib\site-packages\onnxruntime\` directory; whichever is installed
second wins, and both `.dist-info` directories survive, so `pip list` shows two
packages over one set of binaries. On 2026-09-12 at 21:10 a plain `onnxruntime`
1.30.0 landed over the DirectML 1.24.4 build, and the 23:36 run fell to the CPU
— five times slower, the resource governor breached by model residency alone,
and the compute fingerprint changed so every measured rate was discarded. It
reached the owner as an ETA of "about 1823 days". `install.ps1` ships the same
trap to any machine: the CPU wheel arrives first from unpinned `fastembed` and
`rapidocr-onnxruntime` requirements, DirectML second, and the next pip command
that re-resolves either one reverts it. **Work order `202626130120` (0t) is the
fix.**

**The repair is not just `pip uninstall`.** That was attempted and failed with
`WinError 5` because the running app held `onnxruntime.dll` open. It left
`site-packages\~nnxruntime\`, `~nnxruntime-1.30.0.dist-info\` and an
`onnxruntime\` package with no `__init__.py` — which still *imports*, as an
empty namespace package, so `onnxruntime.get_available_providers` raises
`AttributeError` rather than anything that names the cause. A reinstall then
reports "already satisfied" from the surviving DirectML `.dist-info` and writes
nothing. **Close Leasha first, delete the `~` remnants and both dist-infos, then
`pip install --force-reinstall --no-deps onnxruntime-directml==<pinned>`.**

**2026-09-15 — this trap is now guarded against; work order `202626130120`
(0t) is SHIPPED, 24/24.** `onnxruntime==1.24.4` is pinned directly in
`requirements.txt`, no longer left to an unpinned transitive requirement, and
`onnxruntime-directml==1.24.4` is documented in the same comment for
`install.ps1` to install. `install.ps1` now installs the DirectML wheel
unconditionally and last, on any Windows machine with a display adapter,
using `--force-reinstall --no-deps` so it always wins the directory
regardless of install order, and verifies `DmlExecutionProvider` immediately
afterward — failing the step loudly if it is absent, rather than trusting
that the wheel did something. Before touching any package it now checks for
a Leasha process holding the venv and clears stray `~*` stash directories
from a previous failed uninstall, so the WinError 5 sequence above cannot
recur silently. `doctor.py` gained a required check
(`check_onnxruntime_integrity`) that fails on a version mismatch between the
two distributions or a stash remnant — though a matching-version pair is the
healthy, intended state now, since the installer's own fix means a correctly
configured DirectML machine always carries both — and an optional one
(`check_gpu_provider_intent`) that warns when an adapter has no provider.
`Pipeline.run()` reports a lost-provider notice through `IndexStats.notices`
the moment a run starts, so the Indexing tab shows it without anybody having
to read a log. Verified against this session's own real Windows venv, which
has a genuine Intel Iris Xe adapter: reproduced the original fault first (a
plain `pip install -r requirements.txt` against the pre-fix file pulled
`onnxruntime` 1.30.0, exactly as this trap describes), then fixed it and
confirmed `onnxruntime.get_available_providers()` returns
`['DmlExecutionProvider', 'CPUExecutionProvider']` and `doctor.py` prints
READY.

**2026-09-19 — the 0t guard was defeated four days later, by a documented
command, and the ETA of "55 days" was mostly not about that.** Three separate
findings from one session; keep them apart.

*The guard.* The venv was found with `onnxruntime` 1.30.0 (CPU) over the
DirectML files, providers `['AzureExecutionProvider', 'CPUExecutionProvider']`.
Cause: `requirements.txt` documented the optional face-detection install as
`pip install insightface==0.7.3 opencv-python==4.11.0.86 onnxruntime` with the
last name **unpinned**. With only `onnxruntime-directml` present, pip does not
count `onnxruntime` as satisfied, so it fetched the newest CPU wheel. Both
`.dist-info` folders were dated 2026-09-15 17:52. The line is now pinned to
`==1.24.4`. Repaired in place: plain 1.24.4, then DirectML 1.24.4 last, both
`--force-reinstall --no-deps`; `DmlExecutionProvider` is listed again. **Any
manual `pip` in this venv can do this again — check the providers after it.**

*The ETA is honest; the rate was the fault, and it is now explained.*
`format_eta` is remaining / recent rate and was right: 114,614 files in the saved
scan at about 1.4 files/minute is about 55 days. The last run's stage timing put
87% of its time in `embed` (85,162 s of 97,444 s; 0.57 vectors/s). Restoring
DirectML did **not** speed that up - measured with the real `Embedder`, CPU 12
threads about 8/s, DirectML on the Intel Iris Xe about 7/s. The causes, found by
running the indexer on 90 real documents in a scratch index and changing one
setting at a time: `threads 1` (the envelope charged each of four extraction
workers a whole core; they were busy 6% of the time) - and, much less, a
256-passage model call (fastembed's own default; 32 is about 10% faster and a
third of the memory, measured interleaved). 1 thread: 1,055 s; four 4-thread
runs: 222-587 s. **This laptop (15 W i7-1365U) varies about 2.5x run to run with
the owner's other applications, so the size of the gain is a range, roughly
1.8x-4.8x, not a number.** `resolved batch 512` came from a
stored `embed_per_second` of 106,666,662, produced by `index_bench` timing a
generator it never iterated. All fixed - work order `202626191300` (0u), which
carries the numbers. `.env` has `EMBED_DEVICE=cpu`; only the Compute box on the
Tuning screen writes it, so it was chosen as "Processor", and it is left alone.
Also fixed: every `.ppt` (79 of 79) was `ERR_CONVERTER_FAILED` because the rule
used `txt:Text`, a Writer filter. **Those 79 rows stay `SKIPPED` until one
`app.cli index --retry-skipped`**, because a settled skip is never re-attempted.

*Closing the window did not stop the run.* `IndexingView` runs its worker in its
own `QThreadPool`; `MainWindow._drain_workers` waited only on the global pool,
saw nothing, and the log read `closing: took 0.0s` while conversions carried on
for three minutes. Separately `Pipeline._extract_worker` checked the stop flag
only when its queue was empty, and the queue is bounded and kept full. Both
fixed, with `tests/unit/test_close_waits_for_index_run.py`. The same defect is in
the run log of 2026-09-17: `closing` at 11:27:20, the event loop returning at
20:19:29. **Not found: why the event loop stays alive after `closeEvent` while a
run is in the pool.** No stack could be taken before the process was stopped, so
`app/ui/exit_watchdog.py` now logs every thread's stack 30 s after a close and
ends a surviving process at 300 s - armed only by `main()`. **Read the stacks
before reasoning about it.** Also open: a stop that arrives mid-batch still waits
for the batch, because vectors are written before an archive's completion marker.

*Applied and reset, 2026-09-19.* Order 0u is merged to `main` (`63e14ed`). **The
index was cleared** - moved, not deleted, to `D:\Leasha\Data-index-backup-20260919-1435`
- so it is **empty now**: the first run is a full one, with the saved scan total
(114,614 files) for its ETA. Folders and UI settings were carried over. Auto
resolves 4 workers / 4 threads / batch 256. **`EMBED_QUANTISED` is now ON**
(owner's decision, same day, index empty): 1.85x faster, and it changes the
neighbours (top-5 overlap 76% against fp16), so **do not mix vectors from the two
models** - going back to fp16 means a full rebuild. Verified loaded from
`models\BAAI--bge-small-en-v1.5-int8-local`. `.env` is not in git; a copy of the
old one is `.env.before-int8-20260919`. `REQUIRED_FREE_GB=300` will warn on every run
(217GB free); it does not block.

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

**Look at the real window, not only the offscreen one.** Offscreen grabs are 100% scaling
with another font, so they hid six faults that the owner's 125% screen showed at once (the
pill's "Up to date" cut to "p to dat", a grey box behind every label, an unreadable toast,
result rows wider than their pane). `tools/grab_ui.py` takes `QT_QPA_PLATFORM=windows` to
use the real platform and fonts. Two things it taught: **the theme's `QWidget { background:
window }` reaches labels**, so any label placed on a card needs the transparent rule that
now sits under it; and **`QListView` never lays its rows out again after a resize** unless
`ResizeMode.Adjust` is set. To read the actual taskbar, `PrintWindow` on the `Shell_TrayWnd`
handle works (a screenshot does not, when another window is full-screen).

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

**2026-09-07: the embedder, OCR and the reranker each pick a processor for themselves, and
nothing coordinated them.** `crash.log`'s last entry is a real access violation: the embedder
mid-`Run()` on the graphics card while OCR was independently constructing its `text_cls`
session on the same card, at the same moment. `compute_profile.py` is detection-only by design
and `backends.choose()` decides per subsystem in isolation - each of the three already carries
its own private lock, but every one of those only guards callers *within* that one subsystem.
Fixed with one process-wide gate, `app/core/gpu_serialize.py::gpu_exclusive()`, held only
around each subsystem's actual session-construction and inference calls, only when that
subsystem resolved to the graphics card - never around a whole pipeline stage, and never on the
processor path. This serialises GPU work across all three where before it could overlap; see
`CHANGELOG.md` `[Unreleased]` for the throughput trade-off, which this sandbox (no graphics
card) could not measure against real hardware - only the lock's own near-zero overhead was
measured directly.

**2026-09-07: `cached_profile` calls `detect()` unconditionally, every time - not only on a
cache miss.** `detect()` runs first, always, so its fingerprint can be compared against the
stored one; only *which value gets returned* depends on whether they match. On a healthy
machine the disk-kind and DXGI subprocess calls inside `detect()` return in well under a
second, so this is imperceptible - but on a cold cache, after a driver or hardware change, or
when those subprocesses simply hang, they run out to their full 10s/15s timeouts. `_start_
indexing` called `resolve_for_run` (and so `detect()`) inline, on the UI thread, so the window
froze for however long that took, at the exact moment somebody clicked Start. Fixed by
dispatching the resolve through a `CallableWorker`, with `_index_resolved` doing the rest
(building the Pipeline, handing it to `IndexingView.start`) once the result is back on the GUI
thread. Anything else that calls `resolve_for_run` or `compute_profile.cached_profile`/`detect`
directly from the UI thread has the same exposure and needs the same treatment.

**2026-09-07: a frozen pydantic model's `setattr` fails silently if the exception is only
logged at DEBUG.** `_limits_changed` tried `setattr(self._settings, key, value)` per key -
`Settings` is `frozen=True` (`app/core/config.py`), so every call raised, was caught by a bare
`except Exception`, and logged at DEBUG, where nobody would ever see it. The docstring's
promise - "applies to the next run in this session, no restart needed" - was false for every
key this function touches (worker count, memory ceiling, CPU cap, free-space floor, tuning
mode), for as long as the function has existed; `.env` persistence was the only half that ever
worked. Fixed with `self._settings = self._settings.model_copy(update=values)`, one
replacement for the whole batch - the established pattern for a live change to this model, also
used in `app.cli.cmd_index` for `--rerank-model`. **Known, checked, and not yet a live bug:**
`SettingsView.__init__` stores its own `self._settings = settings` reference, independent of
`MainWindow._settings` - replacing the window's copy cannot reach it. Today `SettingsView`
reads that reference in exactly two places, both `ollama_url`/`ollama_model`, neither a field
`_limits_changed` or anything like it currently touches - so this is a latent trap, not a
regression, but the day something routes an `ollama_*` key (or any field `SettingsView` reads)
through a `self._settings = self._settings.model_copy(...)` replacement on the window, check
whether `SettingsView` needs the same live reference rather than assuming it already has it.
More generally: **a caught exception logged below INFO is a bug wearing a disguise** - grep this
codebase for `except Exception` next to a bare `_log.debug` before trusting that a "successful"
save actually did anything live.

**2026-09-08: the governor's "own load" was the main process only, so every converter
subprocess counted as "other programs".** `logs/runs/run-20260908-055844-window.log`: ~25
pause/resume cycles between 06:06 and 06:28, each "the machine is busy (81-95% CPU used by
other programs)", with `index_cpu_percent 80`. LibreOffice, the DWG converters and the RTF
converter run as children (`app/extract/converter.py`, `rtf.py`, `diagrams.py`), and
`Snapshot.own_cpu_percent` was `Process.cpu_percent()/cores` for the main process alone - so
the indexer paused because of its own converter, waited it out, spawned the next and paused
again. `SystemProbe._children_cpu_percent` now adds `children(recursive=True)` (0.28ms for the
walk with six real children in the sandbox; per-child guarded, a child exiting mid-read
contributes nothing). Nobody could say *which* programs were busy, because nothing logged
it - three candidate diagnoses (antivirus, Ollama, Windows Search) and no way to choose. So
`busiest_processes()` samples the process table for half a second on the way into a CPU pause
(`resource governor: busiest right now - X 41%, Y 22%, Z 9%`), rate-limited to once per 30s,
own pid and children excluded. **Read that line before guessing next time.** The same run
said OCR was on the processor "because no display adapter was detected" while the embedder
was on the GPU: `_dxgi_adapters()`'s 15s PowerShell probe had timed out under that load, and
a timeout returned `()` - the same value as "no graphics card" - which `why_unavailable()`
then stated as a hardware fact, and which `cached_profile()` treated as a different
fingerprint and **overwrote the stored profile with**. Now `_dxgi_adapters` returns
`(adapters, failure)`, `ComputeProfile.gpu_probe_failed` records that the check did not run
(defaults False for profiles written by older code), a module-level `_last_known_adapters`
serves later `detect()` calls in the same process (the embedder, OCR and the image model each
call `detect()` for themselves - that is why one could be right and another wrong twenty
minutes apart), `cached_profile` keeps the stored adapters rather than deleting them, and the
sentence with nothing to fall back on is "the graphics card check could not run". The timeout
was deliberately not lengthened. Still open: `detect()` is called per subsystem and each call
spawns PowerShell twice; a process-wide detect-once would remove the exposure altogether, but
that is a structural change and "working version first" applies.

**2026-09-08: a transient DXGI device-removed event (driver reset, or the GPU briefly dropping
out of the system) was being treated as a permanent, silent failure in all three GPU consumers.**
`logs/runs/run-20260908-050751-window.log` (line 121-123): the embedder's ONNX call failed with
DirectML's own text for HRESULT `887A0005` (`DXGI_ERROR_DEVICE_REMOVED`) - "The GPU device
instance has been suspended" - and the same log's line 95 shows this had already happened once,
more softly, with OCR falling back to the processor moments later at line 99-100 because "no
display adapter was detected". Three separate bugs followed from the same gap: `embedder.py`'s
`embed()` gave every exception the same "delete the model cache" suggestion, which is wrong for
a driver hiccup, and never cleared `self._encoder`, so every later call kept hitting the
identical dead session for the rest of the run; `ocr.py`'s `ocr_image()` logged a per-image
inference failure at DEBUG only (invisible) and never cleared the module-global `_engine`, so a
scanned corpus silently lost OCR for the rest of the run with nothing in the logs anyone would
see; `rerank.py`'s scoring failure had a "budget, not a latch" pattern (`RERANK_FAILURE_BUDGET`)
but kept retrying the same cached `self._scorer`, so if the scorer itself was the broken thing,
every retry inside the budget was doomed identically and the budget counted down to zero without
recovery ever getting a chance. Fixed with one classifier,
`app/core/gpu_serialize.py::is_transient_gpu_error()` - a best-effort match on the DXGI
device-removed text and HRESULT codes (`887a0005`/`887a0006`/`887a0007`), documented as guidance
and recovery only, never a correctness gate - and, in each of the three, invalidating the cached
session/engine/scorer (and `choice`, where the module tracks one) on a classified failure so the
*next* attempt rebuilds fresh through the existing `backends.with_fallback` machinery, which may
land back on the graphics card or fall back to the processor. OCR's invalidation is guarded by
its existing `_engine_lock`, since `ocr_image()` runs on extraction worker threads concurrently;
OCR's construction-retry-budget (`_engine_failed`/`_engine_attempts`) is untouched by this - it is
a different, already-correct mechanism for "the package genuinely is not installed". This
sandbox has no graphics card, so the classifier and the recovery paths are proved with injected
fake sessions that fail once and then succeed, not against real DirectML hardware.

**2026-09-08, later the same day: the fix above did not fire, and a single embed failure ended
the whole index run.** `logs/runs/run-20260908-055844-window.log` at 06:29:49: `887A0020`
(`DXGI_ERROR_DRIVER_INTERNAL_ERROR`, "the driver's state is probably suspect, and the application
should not continue") escaped `Embedder.embed()`, `pipeline._feed_worker` recorded it and
`_raise_if_feeder_failed` ended the run - by design, and correctly - thirty minutes in; the log is
then silent for 3h40m until the window was closed, which the user experienced as "indexing is
extremely slow". Three gaps: the classifier matched only `887a0005/6/7`, so `887A0020` was
"generic"; even when classified, `embed()` only invalidated and re-raised, so the batch was lost
and the run ended anyway; and `backends.choose()` had no memory that the driver had failed, so the
rebuild would have asked for it again. Fixed: (1) `is_transient_gpu_error` matches the whole DXGI
facility via regex `887a00[0-9a-f]{2}` plus `dmlexecutionprovider`/`dmlcommandrecorder`/the driver
phrases - one facility, not a hand-picked list; (2) `gpu_serialize.mark_gpu_unreliable(reason)` /
`gpu_unreliable()` is a **process-wide sticky latch** (a plain string replaced whole - assignment
of an immutable is atomic under the GIL, so no lock; first reason wins; never cleared by the app,
`_reset_for_tests()` and an autouse fixture in `tests/conftest.py` clear it between tests) that
`backends.choose()` reads after `blocked`: when set and the card would otherwise have been chosen,
`auto` **and explicit `gpu`** return the CPU `Choice` with `fell_back_from=GPU` and the sentence
"the processor, because the graphics driver failed earlier in this session (...)"; `cpu` and a
machine whose card is `blocked` anyway keep their own sentences; (3) `Embedder.embed()` on a
classified failure now calls `_retry_on_processor`: mark the latch, warn once, append `problems`,
drop the session, `_ensure_encoder()` (lands on CPU because of the latch - `test_embedder.py`
proves it against a fake `TextEmbedding` that records its providers), then **one straight second
call** to the encoder on the same batch, never back through `embed()`'s except path; a second
failure raises `ERR_MODEL_LOAD` with the transient-GPU words. Non-transient exceptions take
exactly the old path (asserted: encoder called once, latch not set). OCR's `ocr_image` and the
reranker's `rerank` also mark the latch where they already invalidate, so their next
`_load_engine()`/`_ensure_scorer()` lands on the CPU with no per-subsystem code; OCR's
`_engine_attempts` counts only failed constructions, so a CPU reload that works costs nothing from
that budget (tested); the reranker's `RERANK_FAILURE_BUDGET` is untouched. **The pipeline's loud
end is deliberately unchanged** - what reaches `_feed_worker` now is a batch that failed on the
card *and* on the processor, which is genuine breakage; a dated paragraph in its docstring says
so. Trap for the next reader: **the latch is process-wide and sticky** - any test that simulates
a transient GPU error in any subsystem will silently send every later `choose()` in the same
pytest process to the CPU unless the autouse fixture is in scope; and `clip_embedder.py` gets
the CPU fallback for free through `choose()` but has no retry of its own - a driver failure
mid-CLIP-batch still raises out of `ClipImageEmbedder.embed` as before (decide whether it needs
the same one-retry treatment; not done here because no log shows it happening).

**2026-09-15: a single bare-word query silently ignores `/newest` and
`/oldest`.** Found while testing work order 0's "Relevance" item, not caused
by it. `definitions.looks_like_symbol` fires on any lone word that looks like
an identifier - which is most single words, including plain ones like
"pump" - and `definitions.boost` then runs *after* the date-sort branch in
`engine.py::_retrieve`, unconditionally, re-sorting the whole list by
`rrf_score` regardless of whether anything actually declares the symbol. A
search for `pump /oldest` comes back in relevance order, not date order, with
no notice that the sort was dropped - the same "degraded result
indistinguishable from a good one" failure §6's standing rule exists to
catch, just not one this session's scope covered. Two or more words are
unaffected; `looks_like_symbol` requires exactly one. Not fixed here -
`app/search/filename_match.py` and its engine.py wiring were kept scoped to
the "Relevance" item alone, and this is a pre-existing fault in
`definitions.boost`'s own placement, not in anything this order touched.

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
6. **Decisions - all taken on 2026-09-20 on the owner's delegation, none waiting.** Recorded in
   the orders and summarised at the top of section 3. Still the owner's to reconsider:
   - **Rail entry "Offline"** stays (the owner's own decision, same day).
   - **Chat is RELEASED and conversational**, with an optional off-by-default web scope
     exception; the 85% extractive floor is unmet with the real models measured (see the chat
     order) - choose a stronger model or a lower floor.
   - **Video/audio** promoted but off by default; the x264/x265 licence question waits for
     packaging.
   - **The released "Choose a drive or folder to catalogue" label** contradicts the refusal of a
     folder as a source; it is a released label, so it was not reworded.
   - **Whether the twelve UI goldens count as "read by a human"** (9i): a session read all twelve
     on 2026-09-20 and fixed what it found; the owner has not looked.

## 8. Where to look

| Document | For |
|---|---|
| `CLAUDE.md` | The front door. Loaded automatically at the start of a session |
| `docs/PROJECT_INSTRUCTIONS.md` | The rules. Read before writing code |
| `docs/ORDER_REGISTER.md` | Every work order, its status and queue position |
| `docs/GLOSSARY.md` | What a term means here |
| `BUILD_SPEC_V2.md` | What each layer delivers and its acceptance tests |
| `LOCAL_KNOWLEDGE_GRAPH_V2.md` | Architecture, error contract, email and cloud strategy |
| `docs/TROUBLESHOOTING.md` | When something breaks |
| `docs/VSCODE.md` | Editor setup |
| `docs/VERSIONING.md` | Version scheme, git conventions, release checklist |
| `docs/PARKED-IDEAS.md` | Approved in discussion, not ordered. Nothing here may be started |
| `CHANGELOG.md` | What changed, when, and why |

**`docs/_superseded/`** holds four modules quarantined on 2026-08-30:
`drag_out.py`, `pinned.py`, `timeline.py` and `working_set.py`. All four were written
on 27 August at 16:33-16:38, were never committed, and were never imported by anything
tracked — `working_set` imported `pinned` and nothing imported either. The pop-out
feature they were drafts for shipped as `app/ui/widgets/preview_window.py` in
`ec40c40`. They are moved rather than deleted because they were untracked, so git
holds no copy. Delete the folder once you have confirmed you want nothing from them.

## 9. Keeping this document true

This file is updated **at every release**, alongside `VERSION` and `CHANGELOG.md`. The
release checklist in `docs/VERSIONING.md` requires it, and
`tests/unit/test_handoff_current.py` fails the suite if its **Applies to** version falls
behind `VERSION`.

A handoff document that has quietly gone stale is worse than none: it is confidently wrong,
and someone will act on it.
