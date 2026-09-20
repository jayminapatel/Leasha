# Work order (One thread): test automation — the GUI clicked for real, the system proven nightly

**Doc version:** 1.5 · **Updated:** 2026-09-20 · **Applies to:** app v0.3.3
**Thread:** One thread (tests + tooling; app code changes only where a test
exposes a bug)
**Status: IN PROGRESS 2026-09-16** - the owner authorized starting this order
directly, alongside the rest of the open backlog ("do all the coding and
engineering... remove all blocks and defers"). (Was: HELD by the owner
2026-08-28, was-LAST after 0l - see the dated note below for what shipped
this pass and what is still genuinely open.) `pytest-qt`, `pywinauto`,
`hypothesis`, `pytest-timeout` were already in the venv and are now pinned
in `requirements-dev.txt` too, so a fresh venv matches.

**Why this order exists**: the presenter split means logic tests without a
display, and the wiring tests assert connections exist — but nothing in the
suite has ever *pressed a key in a real widget* or driven the assembled app.
The rerank-toggle class of bug (emitted, connected to nothing, looks fine)
lives exactly in that gap, and this project has met it repeatedly. This order
closes the gap in layers, cheapest and most valuable first.

## 0. ADDED by owner 2026-08-28 — the grab script: eyes for the AI (FIRST item when this order starts)

- [x] **0a** `tools/grab_ui.py`: constructs the real `MainWindow` offscreen
  (`QT_QPA_PLATFORM=offscreen`) against a temp/fixture store, walks every
  page and named surface (each tab, Settings, Indexing, dialogs where
  constructible), calls `widget.grab().save(...)` per surface into
  `outputs/screenshots/<surface>.png`, and exits. Optional args: one
  surface only; a `--size WxH` to reproduce layout bugs at a given window
  size. No new dependencies — `grab()` renders offscreen with what is
  already installed.
- [x] **0b** why it is first: it gives the coding agent EYES. The workflow
  it unlocks: "the Files tab looks wrong" → run the script → the agent
  reads the PNG (it is multimodal) → hypothesis → pytest-qt regression →
  fix → re-grab → visually confirm. Every later UI order benefits, and
  §5's visual goldens are this script plus a stored baseline and a diff —
  so 0a is the seed of §5, not a separate machine.
- [x] **0c** a smoke test: the script runs headless in CI, produces a
  non-empty PNG per expected surface, and no surface list drift (a new
  tab without a grab entry fails the test — the walker-test shape).

## 1. pytest-qt — real widgets, in-process (the core of the order)

- [x] **1a** harness: a `qtbot` fixture constructing the real `MainWindow`
  against a temp store + fixture index (reuse the integration fixtures);
  offscreen platform (`QT_QPA_PLATFORM=offscreen`) so it runs headless; a
  `gui` pytest marker so the subset is selectable like `jvm` is.
- [ ] **1b** scenario tests for every user journey the orders promised, each
  as keystrokes-and-assertions, not signal introspection: type → interim →
  full results render; Esc clears results AND status (the M9 regression, now
  end-to-end); tab switching; `/` popup opens, offers, inserts, scoped
  values appear; settings toggle → engine state changes (the anti-rerank-bug
  test, done properly); pop-out opens/finds/rotates/closes (workspace
  order); Offline Media Scan/Rescan/Delete against a fixture volume; the
  8-year-old scenarios from the search-experience order re-expressed as real
  keystrokes.
- [x] **1c** the rule that keeps this suite alive: every future work order's
  "acceptance sentence" gets its pytest-qt scenario **in that order** — this
  order seeds the harness and the backlog of existing journeys; the
  convention is recorded in WORKORDER-CONVENTIONS §5 as a dated note.
- [x] **1d** teach the load-bearing-tests table the new guard: a wiring test
  that a control changes observable behaviour is now expressible as a real
  interaction — migrate the weakest wiring assertions to interactions where
  cheap; never delete a passing guard to do it.

## 2. hypothesis — property tests for the parser-shaped code

- [x] **2a** `parse_query`: any generated string parses without raising;
  round-trip properties (rendering a parsed query re-parses to the same
  structure); the negation/phrase/colon corner cases (M4's family) as
  properties, not examples.
- [x] **2b** `sanitise.py`: output is always well-formed for arbitrary input
  (it is an allow-list rebuild — assert the allow-list holds under fuzz).
- [x] **2c** chunker: offsets invariant (`text[start:end] == segment`) for
  generated documents of arbitrary paragraph shapes; the perf floor guards
  stay separate.
- [x] **2d** filters/`file_filter_sql`: generated filter combinations always
  produce parameterised SQL (no injection shape possible under fuzz), and
  LIKE-escaping properties hold.
- [x] **2e** deadline/health-check settings tuned so hypothesis runs in the
  default suite without flaking CI (profile: fewer examples default, many
  under a `slow` marker).

## 3. pywinauto — five black-box journeys, no more

- [x] **3a** UIA-driven smoke against the *packaged/launched* app (leasha.cmd
  path, real window): launch → window appears; search → result row exists
  (found via accessible names — the accessibility discipline pays here);
  open a result; pop-out + stay-on-top actually stays on top; clean close
  mid-search (the shutdown-race classic, black-box). Marked `e2e`, excluded
  from the default run, executed before releases and after the PySide6
  migration.
- [x] **3b** flake discipline: each journey retries once, artifacts a
  screenshot on failure, and a journey that flakes twice in a month is fixed
  or deleted — a flaky e2e suite is worse than none (recorded as the rule).

## 4. Visual regression, lightly

- [x] **4a** `qtbot`-grabbed goldens for the key views (search with results,
  files, mail, indexing, settings, pop-out) in BOTH themes; tolerance-based
  diff (imagehash distance is fine); goldens updated only deliberately, in
  the commit that changes the look, named in its message. This is the only
  automated catch for the theme-token bug class (the unreadable-QListView
  incident).

## 5. The nightly system loop (on the owner's machine — the target hardware)

- [x] **5a** one script: build/refresh the scale fixture → full index run via
  CLI → perf floors asserted (chunker, embed rate, ladder, search p95 vs
  pinned numbers) → `evaluate --builtin` recall floors → kill-resume spot
  check → one-line report appended to a log the owner can glance at
  (`logs/nightly.log`), loud only on failure.
- [ ] **5b** registered as a Windows scheduled task by an opt-in installer
  step (never silently — the owner enables it once); `doctor` shows when the
  nightly last passed.
- [x] **5c** GitHub Actions `windows-latest`: unit + pytest-qt(offscreen) +
  hypothesis(default profile) on every push; the `e2e`/`jvm`/`slow` markers
  excluded. This is the contributor gate for the open-source future.

> **2026-09-20 (later) - the three findings below, worked; 1b's Offline Media half is now
> pressed for real.** (1) *Offline Media Scan / Rescan / Delete*:
> `test_gui_scenarios_orders.py::test_offline_media_scan_rescan_and_delete_pressed_for_real`
> drives the real button, the real name dialog, the worker, the store, the pipeline, the
> tree and the confirmation dialog (faked: the OS volume API, the native chooser, the
> embedder and the machine-wide run-lock mutex). **The hang did NOT reproduce in-process** -
> Scan and Rescan each finished in under a second. What the code did allow: connection
> hand-out waited on `_conns_lock` with no limit and let a locked database escape from
> inside it; both are now bounded by the store's timeout and end in the plain-words
> `ERR_DB_BUSY` (`test_sqlite_bounded_connection.py`). The launched-app cause is
> UNCONFIRMED. (2) *An offline file's text in the preview body*: a real bug, found by
> asserting it - `presenter/results.py::to_row` never copied `volume_id`/`relative_path`,
> so a drive-backed row previewed as an ordinary missing file (and Open/Reveal could not
> resolve it). Fixed; the 0k scenario now asserts the body. (3) *Folder as a source*: stays
> refused (DECISION: a folder has no stable volume identity); the message is now
> `ERR_SOURCE_NOT_A_DRIVE` - "Choose the drive itself, not a folder on it". The chooser
> text and the tab intro still say "drive or folder" (released wording, not edited - the
> owner may want it reconsidered). (4) *"Nothing selected"*: a plausible cause, not seen
> in the launched app - `ResultsView._rebuild` kept the selection only when the query text
> was unchanged, so an interim tier for "barn" replaced by the full tier for "barnsley"
> dropped the click and the pane, opened a moment later, said "Nothing selected". Fixed
> (the selection now survives whenever the same document is still in the results) with a
> scenario that fails without it.
>
> **2026-09-20 - 3a, 3b and 5a ticked, each proven by a real run on this machine; 5b's
> script is built and dry-run tested but never registered; 1b is still open and the
> table below is why.** (Correction to the 2026-09-19 note under this one, which put 3a,
> 3b and 5b with the owner because they need an interactive desktop: this machine has
> one, and they were run here.)
>
> **3a - the launched app, driven by UIA (`tests/unit/test_e2e_pywinauto.py`,
> `tests/unit/e2e_support.py`).** Run: `venv\Scripts\python.exe -m pytest
> tests/unit/test_e2e_pywinauto.py -m e2e --timeout=180 --timeout-method=thread -v` -
> **7 passed in 67 s** (the last run); the window appeared in 22-48 s across runs (the
> machine was busy); an earlier run of the same file had one pop-out flake, retried and
> logged. Journeys: launch (title `Leasha`, all eight rail pages, the search box); search
> (a result row named for the file; Escape empties the box and the row); selecting a row
> and opening the preview shows its text; **Pin in a window -> Keep on top really sets
> `WS_EX_TOPMOST`, clearing it clears the flag, closing the pop-out leaves the main
> window**; Settings opens; **closing after all of that ends every process in 0.5-1.7 s**
> (measured after a session, `logs/e2e.log`); closing with a search in flight ends it in
> 0.4 s. **Order 0u section 6d is therefore not reproduced by a normal close** (that is
> evidence against the simple case, not a closure - the incident needs the next hang's
> stack). "Open a result" is deliberately *not* pressed (Enter would launch the real
> file in Notepad); the preview journey stands in for it. **Isolation is by
> construction, not by promise:** `app/core/config.py` finds `.env` from the *code's own
> location*, not the working directory (the old file's docstring said otherwise, and
> its launched app would have opened the real index), so the app is launched from a
> scratch copy of `app/`, `assets/`, `config/` with its own `.env`, logs, window state
> and three seeded documents. **Two traps found:** `tests/conftest.py` sets
> `QT_QPA_PLATFORM=offscreen` for the pytest process and a child inherits it, so the
> launched app ran with no window at all (stripped in `e2e_support.LaunchedApp`); and
> `venv\Scripts\pythonw.exe` is a stub, the window belongs to its *child* process, so
> "exited" means the whole tree. **Safety:** `click_input`/`type_keys` act on the screen,
> so nothing is sent until the foreground window belongs to the launched app's own
> process tree (`FocusLost` otherwise) - a run that took the mouse while another
> application was in front would otherwise have clicked into it; each launched app also
> has a 7-minute wall-clock watchdog that kills its own tree only.
>
> **3b - enforced, not just written.** A failing journey is screenshotted to
> `outputs/e2e-failures/` and retried once; passing on the retry is logged as a *flake*
> in `logs/e2e.log`, and a **second flake of the same journey in the same month fails the
> run** ("fix it or delete it"). The live tree changes under UIA (`KeyError: None`,
> `COMError` while enumerating) - `e2e_support.descendants` retries those, which is what
> removed the one flake seen.
>
> **5a - measured, and the floors pinned from the worst of three.** `tools/nightly.py`
> now builds or refreshes a persistent *scale* corpus (`tests/fixtures/scale_corpus.py`,
> 300 unique text files, refreshed only when its manifest differs) beside the real-format
> fixtures, runs one timed index over both (344 files, 326 indexed), measures the OCR
> **ladder** (rungs 0-1 model-free; rung 2, the detection probe, on the graphics card) as
> well as chunker, embed and search p95, runs `evaluate --builtin`, and runs the
> kill-resume stage **mid-flight** in a fresh store (it waits until at least one file is
> INDEXED, kills, resumes, and requires the same INDEXED count as the uninterrupted run).
> **Three full runs, all PASS** (`logs/nightly.log`, 6.6 / 8 / 8 minutes on a loaded
> machine): index 1.62 / 2.07 / 1.02 files/s, chunker 2252 / 1404 / 524 per s, embed
> 10.95 / 9.73 / 8.13 per s, ladder rungs 0-1 56.9 / 37.9 / 18.5 images/s, rung 2 761 /
> 829 / 1252 ms per photo, search p95 171 / 252 / 298 ms, recall 0.7 each time,
> kill-resume killed at 13 / 10 / 10 files and resumed to 325 / 326 / 326 = the reference.
> **How the margins were chosen** (written beside `PERF_FLOORS`): each floor is 2.5x-3x
> beyond the worst value ever seen for that metric (rate / 3, latency x 3), the anchor
> being the worst of at least three runs - here of all eight runs across both days - and
> every run was on a machine that was busy with someone else's work, so the worst is
> already pessimistic. No floor came from one run. New floors: index 0.35 files/s (the
> metric now covers the scale corpus, replacing 0.2), chunker 175/s (was 300), ladder
> rungs 0-1 6.0 images/s, ladder rung 2 3500 ms, the rest unchanged. The rung-2 figure is
> several times the "~50-150 ms" in `ocr_ladder.py`'s docstring on this integrated
> adapter - a measurement to record, not a defect asserted.
>
> **5b - the script exists; registration is still the owner's one deliberate act.**
> `scripts/install-nightly.ps1` (ASCII, BOM, added to `parse-check.ps1`): `-WhatIf` /
> `-DryRun` print the task name, time, command and working folder and change nothing;
> a plain run asks first (`ConfirmImpact=High`); `-Time HH:MM`; `-Status` (registered?
> last run? last `nightly.log` line); `-Uninstall` (asks; with `-WhatIf` it says what it
> would remove and removes nothing); contradictory switches exit 2; a missing venv refuses
> with exit 1 before touching the scheduler. It runs under the owner's account while logged
> on (no password stored, no elevation), `StartWhenAvailable`, a 3-hour limit, `pythonw`.
> `tests/unit/test_install_nightly_script.py` (11 tests, 24 s) drives every path except
> the registering one, under a throwaway task name, and proves that name is absent from
> Task Scheduler afterwards; the dry-run path was also run against an existing task
> (`Adobe Acrobat Update Task`) under `-Uninstall -WhatIf`, which left it untouched. **Not
> ticked**: nothing has been registered (by instruction), so "registered as a Windows
> scheduled task" is unproven end to end. `install.ps1`'s own opt-in step is unchanged.
> `doctor` already shows the last nightly result.
>
> **1b - still open. Coverage table** (built by reading each shipped order's acceptance
> sentence against the scenario test names and docstrings in `tests/unit/`; the lead's
> `run_suite.py` is what proves the old ones pass - this pass re-ran only the new file):
>
> | Order | Promise | Scenario |
> |---|---|---|
> | 0m 1b | type -> interim -> full; Esc; rerank checkbox flips the engine; pop-out opens / stays on top / closes | `test_gui_scenarios.py` (9 tests) |
> | 0m 1b | pop-out find / rotate / remember rotation | `test_gui_scenarios_journeys.py` (rotate, remembered, Ctrl+F, Esc closes find) |
> | 0a | `/` popup opens, offers, inserts | `test_gui_scenarios.py::test_slash_popup_opens...` |
> | 0a | `/` **scoped values** appear and filter | **new** `test_gui_scenarios_orders.py::test_slash_type_then_a_space_offers_the_types_the_index_actually_holds` |
> | 0c | the eight-year-old journeys as keystrokes | `test_gui_scenarios_journeys.py` (misspelling, whole question, two misspellings, quoted phrase, emptied filter let go of, filter offer, unknown name, Enter opens, last search offered, lands on Search) |
> | 0e | pop-out, pinned panel, log window, spreadsheet grid, drag-out, global hotkey | pop-out and pinned panel: `test_gui_scenarios*.py`, `test_ui_redesign_scenarios.py`; log window `test_log_window.py`; grid `test_spreadsheet_preview.py`; drag-out `test_drag_out.py`; hotkey `test_mini_search.py` - **widget level only for the last three; a real OS drag and a system-wide hotkey need the desktop and are not pressed** |
> | 0g | mail findable by sender | `test_gui_scenarios_results.py::test_a_mail_result_is_labelled_by_sender...`; mbox/Takeout indexing itself is headless (`test_email_mbox.py`, integration) |
> | 0h | a photo found by typing a description | engine level with the real CLIP towers (`test_clip_lane_wiring.py`); **no window-level scenario - needs the real model, network-dependent; listed, not faked** |
> | 0i | AI-written words marked in the preview | **gap** - no window-level scenario; model-bound |
> | 0j | the Photo Tagger is reachable | `test_wired_features.py` (Settings button, Go menu) |
> | 0k | describe a file from memory -> "on Projects 2019 (offline...)" and its text without the drive | **new** `test_gui_scenarios_orders.py::test_a_file_on_an_unplugged_drive_says_which_drive_it_is_on` (first half only: the row names the drive; **the pane showing the offline file's indexed text did not appear within 15 s in the assembled window - unproven, UNCONFIRMED whether by design**) |
> | 0k / 0m 1b | Offline Media **Scan, Rescan, Delete pressed** | **NOT DONE** - see below. The old test still only asserts `rescan.isEnabled() or delete.isEnabled()` |
> | 0d | first run offers profile folders, "Add all four" | `test_privacy_defaults.py` at widget level; **no window-level scenario** |
> | 0f | picture folder indexes, ladder, dates | headless (pipeline / CLI tests) - not a keystroke promise |
> | 0p | every header sorts; Relevance restorable | `test_table_sorting.py` (27) |
> | 0q | results read right, list stable, keyboard-driven | `test_gui_scenarios_results.py`, `test_ui_redesign_scenarios.py` |
> | 0s | why-menu, saved names, chips, prefill, summon | `test_adoption_scenarios.py`; summon-around-selection is `test_mini_search.py` at widget level |
> | pages reorg | Settings filter, five shelves; Indexing three views | `test_pages_reorg.py` (page level, typed into the filter; not through the assembled window) |
> | space report / idle tune | Reports surface opens, export, idle-tune rules | `test_idle_tune_and_space_report_ui.py` |
> | UI redesign | opens on one box; rail; shortcuts; keyboard-only journey | `test_ui_redesign_scenarios.py` (28) |
> | 0m 3a | the launched app, black-box | `test_e2e_pywinauto.py` (above) |
>
> **Offline Media Scan / Rescan / Delete: attempted, not achieved, and what it found.**
> (1) *A folder cannot be catalogued, only a drive root or a share*: choosing an ordinary
> folder on `D:` fails with `ERR_CONFIG_INVALID` "could not read a volume or network
> identity" (`identify_root` answers only for a drive root). The button says "Scan a
> drive..." but its chooser text and the tab's intro say "drive or folder". Whether folders
> are meant to be sources is the owner's call; UNCONFIRMED whether this is a bug or the
> design. (2) The scenario therefore has to present the fixture folder as a volume by
> replacing the OS volume API, the chooser, the embedder and the machine-wide run-lock
> mutex (with the real lock, any other index run on the machine - the owner's, or another
> test process - makes the scan fail with `ERR_INDEX_RUNNING`, correctly). With those
> replaced the run **hung for the whole timeout** with workers blocked in
> `_read_external_run` -> `SqliteStore._new_connection`; the cause was not found in the
> time available and the scenario was removed rather than committed unproven.
>
> **Also noticed, not fixed (in files another thread is editing):** in the launched app,
> clicking a result and opening the preview pane *within about two seconds* of the rows
> appearing showed "Nothing selected" three times out of three, while the same steps a few
> seconds later, or with the pane already open, showed the file. It is **not reproduced
> offscreen** (`test_gui_scenarios_results.py::test_the_selected_row_survives_the_interim_to_full_swap`
> passes and a direct select-then-toggle probe showed the file), so it is a black-box
> observation of a timing window around the metadata redraw, UNCONFIRMED as a product bug;
> the journey waits for the search to settle rather than depend on it. And a toast
> "Downloading the picture-search model - 13%" stayed on screen for minutes in the scratch
> app (it had a model cache and network, so a stalled progress toast is the suspect).
>
> **Hangs while writing these:** three times a pytest process ran on for many minutes after
> a failing test with no output. Cause found: pytest formats the failure with
> `inspect.getmodule`, which `realpath`s every module in `sys.modules` (torch, pyarrow and
> the rest are loaded in this process) - very slow on a loaded machine. Use `--tb=short` or
> `--tb=line`, and `--timeout=180 --timeout-method=thread`.
>
> **2026-09-19 - nothing new is ticked; here is why, and what the suite learned
> about itself.** **1b stays open**: the Offline Media Scan half is out of scope
> (the `test_gui_scenarios_journeys.py` docstring says so), and the
> Rescan/Delete test only asserts `rescan.isEnabled() or delete.isEnabled()`,
> which is weaker than pressing the control. Everything else in 1b has a
> scenario. **5a stays open**: `tools/nightly.py` pins floors for index rate,
> chunker, embed rate, search p95 and recall, but the item also names a ladder
> floor and the script says outright that it is not measured or pinned.
> **3a, 3b and 5b remain the owner's** - they need the owner's interactive
> desktop and a scheduled task, and neither exists in the build session.
>
> **The full suite died silently three times in one process on 2026-09-19**:
> pytest exited with `0xC0000005` (a native access violation) a thousand-odd
> tests in, with no traceback, no Windows event and empty stderr, so every
> later test simply never reported. `-q` output looks the same whether a run
> finished or vanished. Cause: `EMBED_DEVICE` defaults to `auto`, which on a
> machine with DirectML means the graphics card, so the real-model tests
> (embedder, CLIP, reranker, OCR) were building DirectML ONNX sessions inside
> the pytest process; after enough earlier tests had loaded torch, pyarrow and
> more ONNX sessions, a later DirectML run - RapidOCR's text detector, inside
> `InferenceSession.run` - crashed. Shown by a fault stack written to a file,
> and by the same tests completing normally with the process pinned to the
> processor. **`tests/conftest.py` now pins the suite to the processor**
> (`EMBED_DEVICE=auto` in the environment opts back in on purpose); nothing
> tests the card - the device *choice* is tested with fake profiles. It also
> stops the suite competing with the running application for the same card.
> **`scripts/run_suite.py`** splits the files across several processes and
> reports a process that died as CRASHED, with the last file it started, and
> counts it as a failure; `python scripts/run_suite.py -j 4`.
>
> **A second crash is open and unfixed.** One run also died with an access
> violation in `app/ui/view_options.py` (`look`, the column-width watcher's
> timer) during a Qt event pump between tests. Its cause is not established
> (UNCONFIRMED) and there is no reproduction, so nothing was changed there; the
> code has a four-attempt history of column-width bugs and editing it blind
> risks undoing that. A related, smaller bug of the same family was found and
> fixed the same day: a late search answer painted into a results view that had
> been destroyed (`RuntimeError` inside a Qt slot).

## 6. Done means

Suite green with the new layers on Windows; the `gui` subset counted as RUN
not skipped (the vacuous-skip lesson from the PySide6 draft applies here
from day one); nightly has passed three consecutive nights on the owner's
machine; flake rule and acceptance-scenario convention recorded as dated
notes in WORKORDER-CONVENTIONS. Acceptance sentence: a stranger's pull
request cannot silently break a keystroke, a theme, a floor, or a promise —
because something automated presses the keys every day.

---

> **2026-09-16 — owner authorized starting this order directly** ("do all
> the coding and engineering... remove all blocks and defers"), alongside
> the rest of the open backlog. What shipped this pass, and what is still
> genuinely open:
>
> **Section 0 (0a/0b/0c), ticked.** Already built during the UI Redesign
> order (`202626160950` §9k/§9l) and confirmed here rather than rebuilt.
> Verifying it live caught a real bug: `tools/grab_ui.py`'s `SURFACES` dict
> still said `"page": "Offline Media"` after the rail tab was shortened to
> "Offline" - `test_every_rail_page_has_a_grab_surface` (0c's own smoke
> test) failed on it immediately. Fixed. This is the exact class of drift
> §0's own text says the tool exists to catch.
>
> **Section 1, mostly ticked.** 1a: `tests/unit/conftest.py`'s
> `gui_mainwindow` fixture - a real `SqliteStore`, a real `SearchEngine`
> (only the embedding model stubbed, so nothing downloads), a real
> `MainWindow`, driven by `qtbot`. The `gui` marker is registered in
> `pyproject.toml` and, per the item's own requirement, is **not** excluded
> from the default run - only `jvm` and the new `e2e` marker are.
> `tests/unit/test_gui_scenarios.py` has nine real keystroke-and-click
> scenarios, all passing, stable across repeated runs: type-to-results, a
> no-match query, Escape clearing the box, the rerank checkbox actually
> flipping `engine.reranker.enabled`, both rerank controls staying in step,
> the `/` popup inserting an operator by a real mouse click on its popup
> row, and the pop-out opening/staying-on-top/closing. Driving the Escape
> scenario found a second real bug: nothing had ever wired Escape to empty
> the search box - `_on_text_changed`'s own comment already said "or
> pressing Esc" as if it did, but no `keyPressEvent`/`QShortcut` existed
> anywhere on the box. Fixed in `search_view.py`'s existing `eventFilter`
> (the same one already handling Up/Down/Enter), reusing the M9 empty-box
> path rather than duplicating it. **1b left unticked**: what is built is
> real and verified, not a placeholder, but it does not yet cover Offline
> Media Scan (opens a native drive-browse dialog, not drivable headless -
> Rescan/Delete against a fixture volume are covered instead), the pop-out's
> find/rotate (needs a real photo; this file's fixtures are text/PDF), or an
> exhaustive re-expression of every "eight-year-old" scenario from the
> search-experience order. 1c: the convention is recorded in
> `docs/WORKORDER-CONVENTIONS.md` §5b, dated. 1d: the M12 rerank wiring
> tests in `test_review_section_four.py` are untouched (never delete a
> passing guard) and now have real-interaction siblings alongside them.
>
> **Section 2, ticked in full.** `tests/unit/test_query.py`,
> `test_sanitise.py`, `test_chunker.py` each gained `hypothesis` properties
> (2a/2b/2c); `tests/unit/test_filters_properties.py` is new (2d) - it runs
> `file_filter_sql`'s output against a real, migrated `SqliteStore` schema
> rather than only asserting shape, the same reasoning `test_query.py`
> already uses for `to_fts_match`. 2a's "round-trip" is driven through the
> parser's own grammar (build a query from a known filter, confirm the same
> filter parses back out) rather than through a `ParsedQuery` renderer,
> because no such renderer exists - inventing one only for this test would
> have been scope the item never asked for. 2e: a `"leasha"` hypothesis
> profile (`max_examples=25`, no deadline) is registered and loaded by
> default in `tests/conftest.py`; a `"leasha-thorough"` profile
> (`max_examples=300`) is available via `HYPOTHESIS_PROFILE=leasha-thorough`
> for a deliberate deeper run.
>
> **Section 3, left unticked.** `tests/unit/test_e2e_pywinauto.py` has the
> five journeys, written against an isolated fixture `.env` (never the real
> `D:\Leasha\Data` - non-negotiable #10), UIA lookups by `auto_id`/name, and
> 3b's retry-once-then-screenshot mechanism. **Not live-verified**: driving
> real UIA mouse/keyboard synthesis against a genuinely focused, on-screen
> window is a different thing from the offscreen Qt this session can run,
> and needs the owner's own interactive desktop - the same category of gap
> as 0l's on-tape ordering and the §4 UNC test, both already documented as
> hardware-blocked rather than code-blocked. Run it by hand:
> `pytest tests/unit/test_e2e_pywinauto.py -m e2e -v`.
>
> **Section 4, ticked.** Already built and verified during the UI Redesign
> pass; re-confirmed here (`test_fresh_grabs_match_the_goldens_within_
> tolerance`, imagehash `phash`, both themes, eight goldens on disk).
>
> **Section 5, one of three ticked.** 5a left unticked on purpose: `tools/
> nightly.py` exists and was verified live - twice, once with `--quick` and
> once running the kill-resume stage for real, both against the real CLI,
> producing a real `evaluate --builtin` recall (0.7) and a real
> `logs/nightly.log` line - but it does not yet assert the chunker/embed-
> rate/ladder/search-p95 perf floors the item names, because no real
> measurement of any of them exists yet to pin as a floor. `PERF_FLOORS` in
> the script is explicit about this (`None` until a real number replaces
> it) rather than a guessed placeholder - this project's "measure, do not
> assume" rule applies to a floor as much as to any other number. 5b left
> unticked: `doctor.py`'s half (`check_nightly_status`, "last passed"/"last
> failed" read from `logs/nightly.log`) is built and verified against a
> real log line; `install.ps1`'s opt-in scheduled-task step is written,
> ASCII-checked, BOM-preserved, and PowerShell-parses cleanly, but was
> deliberately **not run** - registering a real recurring Scheduled Task is
> a standing-configuration change on a real machine, not something to do
> without being asked. 5c ticked: `.github/workflows/ci.yml` runs unit +
> `gui` (offscreen) + hypothesis on `windows-latest`, excluding
> `e2e`/`jvm`/`slow` - YAML-validated here; an actual push is what proves it
> against real GitHub Actions.
>
> **§6's own "done means" is not fully met**, and said so above rather than
> being reworded: the suite is green with every new layer that can run
> offscreen, but "nightly passed three consecutive nights" is inherently a
> multi-day, real-hardware claim nobody can make from inside one session.
