# Work order (Parallel threads): the test suite runs faster - the slowest 20 files made lighter, every check kept

**Doc version:** 1.4 · **Updated:** 2026-10-13 · **Applies to:** app v1.0.3
**Thread:** Seven parallel threads - one per group in §2, one for §4a/4b/4f, one for §4c/4d (`tests/unit/`, `tests/integration/`, test fixtures and
`tests/unit/conftest.py` only where a group needs it; app code only for a fault a test finds)
**Status:** RELEASED by the owner 2026-10-13 ("yes do it, use parallel agents"), after asking "we have lots of
testing files .py can they be optimised or they are all needed".

## Why

Measured from `logs/suite/durations.json` on `main` (2026-10-13): 564 timed test files, **5,043 s** of test time
across the suite's four processes, median 1.9 s a file. The time is concentrated: the slowest **21 files are half
of it** and the slowest 97 are 80%. The 289 files under 2 s total 172 s, so deleting small files would save under
three minutes. The slowest 20 files take **2,520 s**. Several of them start a real index run or a real child
process to check something a lighter fixture could prove (UNCONFIRMED per file - each group measures its own).

## Rules (every thread)

- **Nothing a test checks is lost.** A test is made faster by a lighter fixture, a smaller corpus, a fake where the
  real thing is not what is being tested, a shared module-scoped setup, or removing a wait that polls - never by
  weakening or deleting an assertion. Where a slow test is the *only* proof of a real end-to-end behaviour, it
  stays as it is and is said so.
- **A test is deleted only with proof** it checks nothing another test does not (name both, and show the other
  one fails when the behaviour breaks). None is expected.
- **Measured, before and after**, each file alone on the same machine: `venv\Scripts\python.exe -m pytest
  <file> --durations=0`. Both numbers go under the file's item. A change that does not save at least 20% of a
  file's time is not made.
- **Each changed test still fails when the behaviour breaks**: for every fixture swapped, break the behaviour once
  by hand, confirm the test goes red, and undo.
- Do all the coding first, then test once per group; the combined verification is the full suite once at the end
  (`CLAUDE.md`, "Parallel, then test together").

## 1. Before starting

- [x] **1a** Each thread records its four files' times on `main` alone (the "before"), with the machine idle.

  *Done 2026-10-13.* Each §2 item carries its file's before time, measured on `main` alone by its thread.

## 2. The files, in five groups

**Group A - indexing tuning and runs** (764 s)
- [x] **2a** `tests/unit/test_index_tuning_acceptance.py` (436 s)
  - *2026-10-13, Group A:* **before 205.4 s, after 2.6 s** (back to back, each file alone,
    `-p no:cacheprovider --durations=0`, lower of the runs; under the busier load earlier the
    same day the before was 509-595 s and the after 4.8-7.1 s). **Cause, from a stack dump:**
    `resolve_for_run` calls `test_this_machine_if_new(settings)` first, and a stand-in settings
    class with no `embed_device` reads as "auto", so on this DirectML laptop each of the five
    `resolve_for_run` calls ran the real device test (every model, processor and card) and,
    with no `state_path`, kept nothing. **Change:** a new opt-in fixture `no_hardware_test` in
    `tests/conftest.py` makes `device_test.gpu_usable()` say no, as it already does on CI
    (honours `EMBED_DEVICE=auto` to test with the card on purpose), plus `quiet_machine` (the
    governor sees an idle CPU); both applied by `pytestmark`. No assertion changed. The
    device test itself stays proved by `test_model_devices.py`. **Red check:** `resolve_for_run`
    forced to Manual - `test_the_next_run_uses_the_automatic_values` red; dedup forced off -
    `test_the_dedup_switch_changes_what_the_model_is_asked` red. §4e: the Qt tests here are not
    slow (the `screen` fixture is under 0.5 s), so nothing moved.
- [x] **2b** `tests/unit/test_speed_work.py` (214 s)
  - *2026-10-13, Group A:* **before 156.2 s, after 4.5 s** (back to back; earlier, busier: before
    531-1,543 s, after 10.4-18.3 s). Same cause as 2a: `test_the_tuning_mode_reaches_the_run`
    (two calls) and `test_a_machine_that_cannot_be_examined_...` ran the real device test, 146-967 s
    a test. Same change (`pytestmark` with `quiet_machine`, `no_hardware_test`). **Red check:**
    Manual forced - `test_the_tuning_mode_reaches_the_run` red; dedup off -
    `test_repeated_text_is_embedded_once` red. §4e: no Qt here.
- [x] **2c** `tests/unit/test_run_setup.py` (114 s)
  - *2026-10-13, Group A:* **before 57.6 s, after 8.4 s** (back to back; earlier, busier: before
    359-524 s, after 19.5-20.1 s). Two causes. `test_an_offline_media_scan_is_built_like_every_other_run`
    (300-350 s when busy) ran the real device test through its stand-in settings - fixed by the
    same two fixtures. And each command-line run started and stopped a reader child process
    (about 10 s a run, three runs in one test): `after_run_env` now also writes
    `INDEX_READ_PROCESSES=false`, since these tests check the pass and its records, and reading
    in a child is proved in `test_read_process.py`. **Red check:** `images_due_after` returning
    None - `test_a_command_line_text_pass_makes_its_next_run_the_images_pass` red; every run
    counted as the saved set - `test_a_run_over_other_folders_...` red; saved cloud opt-ins never
    read - `test_a_command_line_run_over_the_saved_folders_...` red; reader processes never on -
    the Offline Media test red. §4e: no Qt here.
- [x] **2d** `tests/unit/test_read_process.py` (69 s)
  - *2026-10-13, Group A:* **before 20.3 s, after 13.9 s** (alternated before/after twice in the
    same minutes; under load the before was 68-80 s, and one before run was killed after more than
    twenty minutes parked in `resources.wait_while_throttled` under the card pass - the governor
    waiting for a machine at 100% CPU, which `quiet_machine` now prevents). Change: the
    3,000-message mbox is built once per module (`big_mbox`) and written directly rather than
    through 3,000 `EmailMessage`s (6-10 s a build); its bytes are `_mbox`'s own bar the From_
    line's clock, held by a new test, `test_the_quick_mailbox_is_the_same_mailbox`.
    `test_the_benchmark_corpus_indexes_the_same_with_reader_processes` (8-40 s) is left as it is:
    it is the only end-to-end proof that the switch changes nothing. **Red check:** wrong error
    code - the crash test red; the abandoned child not ended - the abandoned-file test red; the
    child sending one extra chunk - the benchmark test red; the quick mailbox wrapped at 76 - the
    new test red. §4e: no Qt here.

**Group B - the window and the screen** (484 s)
- [x] **2e** `tests/unit/test_grab_ui.py` (229 s)
  - *2026-10-13, done.* **Before 96.7 s, after 44.6 s (-54%).** Measured 2026-10-13 on the laptop, each file alone, `--durations=0`, `main` and this branch run back to back in turn, twice; the lower of each pair. Six other agents were running, so the absolute numbers drift by 2x across the day (an earlier pair of runs gave 213/59, 181/118, 52/44, 95/44); the ratio is what held. The golden
    comparison was six `tools/grab_ui.py` processes one after another (76 s of the 97); they now run
    at once (`GRAB_PROCESSES`, half the cores), each with a `TEMP` of its own ending in `Temp`. They
    could not overlap before because every grab keeps its store in the one fixed
    `<temp>/leasha-grab-snapshot`; the pictures draw that path as "... > Temp > leasha-grab-snapshot
    > documents" at every size, so the glyphs are unchanged. Same twelve goldens, same tolerance, no
    golden regenerated. **Red check:** with the tolerance forced to -1, `main` (one at a time) and
    this branch (at once) both list all twelve at **distance 0**; with the window colour changed in
    `app/ui/theme.py`, all twelve drift (30 to 48) and the test goes red. Left slow: the in-process
    grab of all 19 surfaces (`grabbed`, 19 s), the only check that every surface can be reached.
- [x] **2f** `tests/unit/test_window_opens.py` (93 s)
  - *2026-10-13, done.* **Before 64.0 s, after 34.4 s (-46%).** Measured 2026-10-13 on the laptop, each file alone, `--durations=0`, `main` and this branch run back to back in turn, twice; the lower of each pair. Six other agents were running, so the absolute numbers drift by 2x across the day (an earlier pair of runs gave 213/59, 181/118, 52/44, 95/44); the ratio is what held. 27 windows were
    built, at 3-7 s each. Eleven tests now use the shared module window: what a window is "the instant
    `MainWindow()` returns" is read once by the `window` fixture (`_first_look`) before any loop turn,
    and the three deferral tests assert on that; the tab-order, rail-place, themed/wheel-guarded,
    history, clip-progress, maximised and minimised tests use the window itself; the restore test
    builds only its second window. The sixteen that act in the gap before the deferred pages, close a
    window or need state stored before construction still build their own, and must. `shutil.which`
    is cached for the module (the PATH does not change in a run; about 1 s of every build). **Red
    checks:** deferred pages built inside `__init__` - the three `_first_look` tests red; Code
    inserted one place early - tab order and rail place red; the clip callback not wired, the
    minimised `showNormal` removed, an empty geometry blob, the history signal not connected and the
    second `protect_all` skipped - restore, maximised, minimised, clip, themed/wheel-guarded and
    history red (8 failed). All undone.
- [x] **2g** `tests/unit/test_ui_redesign_scenarios.py` (83 s)
  - *2026-10-13, done.* **Before 32.7 s, after 24.5 s (-25%), with one test more.** Measured 2026-10-13 on the laptop, each file alone, `--durations=0`, `main` and this branch run back to back in turn, twice; the lower of each pair. Six other agents were running, so the absolute numbers drift by 2x across the day (an earlier pair of runs gave 213/59, 181/118, 52/44, 95/44); the ratio is what held. The
    two theme tests undid `theme.stylesheet(...)` with `window._apply_theme()`, which re-polishes the
    whole window (about 2 s, four times) and put back nothing they had changed; `_theme_back` puts
    back the palette and the pane's tint, and asserts the window's own palette is back. The relaunch
    scenario's first window is the first-run scenario's (already a launch over that store), so only
    the second is built. `shutil.which` cached for the module, as in 2f. **Red checks:** `_theme_back`
    without its `stylesheet` call - its own guard goes red; the last page not restored - the relaunch
    test red.
  - **The flaky keyboard scenario** (`test_the_search_page_start_to_finish_with_the_keyboard_alone`,
    "4 rows where more were expected"). **Cause:** typing answers several times - the keyword tier,
    the full tier for the same words, then the mail subtitles for each - and every redraw ran
    `ResultsView.show_results`, which cleared the set of opened groups. The test waited only for
    three rows (the keyword tier), so under load the full tier landed on the turn of its Enter and
    closed the group again. The same happens to a person: on the owner's index the reranked answer
    lands up to 2.7 s after the first. **Reproduced** on `main` by holding the full tier's answer
    until the Enter: red, "assert 4 > 4". **Fixed** in the app: the same search redrawn keeps the
    groups that were opened, while their files are still there (`presenter.expansions_kept`, used by
    `show_results` with its `keep_scroll`); a new search still starts closed. The same hold is green
    with it. The test now also waits for the full tier and its workers before pressing keys
    (`_type_and_let_it_answer`). New: `test_a_group_opened_with_enter_stays_open_when_its_search_answers_again`
    and three Qt-free tests in `test_presenter.py`; with `expansions_kept` broken, it and two of the
    three go red.
- [x] **2h** `tests/unit/test_ui_review_0x9.py` (79 s)
  - *2026-10-13, done.* **Before 41.2 s, after 18.2 s (-56%).** Measured 2026-10-13 on the laptop, each file alone, `--durations=0`, `main` and this branch run back to back in turn, twice; the lower of each pair. Six other agents were running, so the absolute numbers drift by 2x across the day (an earlier pair of runs gave 213/59, 181/118, 52/44, 95/44); the ratio is what held. `_apply_theme` on the
    whole window costs about 3 s, and the eight themed tests switched in and out, 16 of the 21 calls in one
    run, which took 62 s in all (measured with a wrapper round `_apply_theme`). The themed tests are now grouped
    (`parametrize("scheme", ..., scope="module")`: every light one, then every dark one), `_Theme`
    switches only when the window is in another theme, and the window's own theme is put back before
    any test that asks for none and at the end of the module: three switches. **Red check:** the
    `window` colour changed in both palettes - `test_quiet_text_still_reaches_wcag_aa` red in
    `[light]` and `[dark]`, so each group really runs in its theme.

**Group C - the index process and its recovery** (437 s)

Measured 2026-10-13, each file alone with `--durations=0`, before (`origin/main`) and after run back to back, twice, lower of the two, wall seconds of the pytest run. The laptop was shared: other threads' suites had it at 100% CPU throughout, so both numbers are well above the idle figure in brackets; the ratio is the point.

- [x] **2i** `tests/unit/test_diagnostics.py` (144 s)
  *2026-10-13: **205 s -> 46 s** (-78%).* The time was `build_bundle` running `doctor.py --json --quick` as a child (20-60 s each) in all six bundle tests. Five of them (no nesting, a broken section, an explicit path, `.env` redaction, log truncation) now take a `no_doctor` fixture that stubs `diagnostics._doctor`; `test_bundle_contains_the_expected_parts` keeps the real doctor child, and now also asserts the `doctor` section is in the report. Red-checked: emptying `SKIP_FOLDERS`, re-raising in `_guarded`, ignoring `out_path`, making `redact_env` return its input and switching off `_tail_bytes`'s cut each turned its test red. Left slow: the one real-doctor bundle (about 30 s under load), on purpose. §4e: no Qt in this file.
- [x] **2j** `tests/unit/test_index_child_process.py` (137 s)
  *2026-10-13: **201 s -> 105 s** (-48%).* Every test still starts the real `app.cli index --events jsonl` child - that is this file's point (crash, kill, orphan, pipes) - but each child is lighter: `INDEX_READ_PROCESSES=false` (reader processes, on by default since 2026-10-09, meant one more Python per worker per child; on the busy laptop they missed their 60 s start and `test_a_whole_run_arrives_as_the_stats_the_page_draws` ran out its 300 s), `INDEX_LOW_PRIORITY=false`, and `COMPUTE_PROFILE_OVERRIDE` to a fixed profile so no child runs the two PowerShell machine probes (`light_children`, `LIGHT_CHILD_ENV`). Red-checked: a different code for a dead child, no `window_gone` stop, and a child that ignores pause each turned their test red. Found on the way: breaking only the stdin reader's call to `window_gone` did not turn the orphan test red - a second path still stops the child - so the check was made on `window_gone` itself. Left slow: about 15 s a child (interpreter and pipeline import), which is the real process this file exists to prove. §4e: no Qt in this file.
- [x] **2k** `tests/unit/test_timed_out_retry.py` (84 s)
  *2026-10-13: **147 s -> 81 s** (-45%).* The time was `test_a_retry_through_the_indexing_process`: two real children at below-normal priority, each probing the machine (one sat 240 s without reading a file and failed). It now uses `light_children` and `LIGHT_CHILD_ENV` from `test_index_child_process.py`, and `cli_env` adds the profile override and `INDEX_LOW_PRIORITY=false` for the in-process command-line runs. The child test stays - it is the only proof the retry crosses the pipe. Real-time waits (the 1 s minimum limit, 2.5 s and 3 s slow reads) were left: shortening them would cut the margin that makes the timeout land. Red-checked: `child_arguments` dropping the retry flags turned the child test red; `clamp_factor` returning 1 turned both command-line retry tests red. §4e: no Qt in this file.
- [x] **2l** `tests/unit/test_pst_folder_resume.py` (72 s)
  *2026-10-13: **64 s -> 31 s** (-52%).* Its ~30 pipeline runs used the default `ResourceLimits` (pause over 80% CPU, 10 s quiet to resume, below-normal priority, a real system probe). They now go through one `_pipeline` with `test_file_watch._pipeline`'s limits and a governor on a fixed `Snapshot`; the fake `pypff` tree, the stops and every comparison are unchanged. Red-checked: never loading the archive cursor turned five tests red (`[5]`, `[6]`, `[8]`, `[9]`, the killed run); stamping each document one folder on turned seven red. §4e: no Qt in this file.

**Group D - staging, the GPU and pictures** (407 s)
- [x] **2m** `tests/unit/test_staging.py` (120 s)
  - *2026-10-13, Group D:* **53.1 s -> 17.0 s** (each alone, `--durations=0`, before on `main` and after
    alternated, twice, lower taken; before 60.2/53.1, after 21.4/17.0). The two AST checks parsed all of `app/`
    (666 files, ~8 s a parse) once each - now parsed once and shared. The read-only checks share one
    module-scoped staging (and one `--with-tests` staging) instead of staging the whole tree eleven and four
    times. The re-staging tests (staging twice, a foreign folder, a corrupt manifest, a missing name) stage a
    small source tree with every shipped name in it; the real tree's re-stage, where the read-only file once
    broke it, still runs in `test_re_staging_does_not_delete_an_index_living_in_the_destination`. **Found:**
    with `PYTHONDONTWRITEBYTECODE=1` (set on this laptop) the checkout has no `__pycache__`, so the bytecode
    checks passed with `__pycache__` taken out of `EXCLUDED`; `test_bytecode_in_the_source_is_not_copied`
    added, red with that break. Every changed test shown red by a break in `scripts/stage.py`.
- [x] **2n** `tests/unit/test_resolve_gpu_regression.py` (119 s)
  - *2026-10-13, Group D:* **131.0 s -> 3.0 s** (before 131.0/153.6, after 3.0/4.0). The cause was not the
    GPU check: `resolve_for_run` first calls `test_this_machine_if_new`, and this file's `_Settings` has no
    `state_path`, so no stored result is ever found and every call is an untested machine - on a laptop with a
    usable graphics card two of the four tests ran the real device test (every model on the processor and the
    card) and saved the result nowhere. An autouse fixture stubs `test_this_machine_if_new`; that behaviour is
    pinned in `test_model_devices.py`. Red: `_gpu_regression` returning `""` fails the lost-provider test.
    **For Group A:** `test_index_tuning_acceptance.py` calls `resolve_for_run` with invented settings too and
    may pay the same cost (UNCONFIRMED - not measured here).
- [x] **2o** `tests/unit/test_clip_lane_wiring.py` (110 s)
  - *2026-10-13, Group D:* **112.4 s -> 23.7 s** (before 112.4/130.8, after 23.7/26.2, cache already
    filled). The real-model test (`slow`, kept, the only proof that CLIP finds a photo by its description) spent
    ~90 s downloading both CLIP towers (580 MB) into a fresh temporary folder on every run. The cache is now
    `LEASHA_REAL_EMBED_CACHE` if set, else pytest's own cache (`.pytest_cache/d/clip_lane_models`, gitignored);
    the first run in a checkout, and CI, still download. Models, pipeline, LanceDB and search are all still real.
    Red: the query vector built from a fixed string instead of the typed description fails it. **Seen, not
    explained:** two of the "before" runs, under heavy load from other threads, sat idle for over 20 minutes
    after the image vectors were written and were killed; four later runs with `faulthandler_timeout` did not
    hang, so no stack was caught.
- [x] **2p** `tests/unit/test_ui_never_blocks.py` (58 s)
  - *2026-10-13, Group D:* **73.3 s -> 40.7 s** (before 84.2/73.3, after 62.8/40.7). Six parametrized
    checks over every `app/ui` module, and `enclosing_function` per call, re-parsed the same files thousands of
    times; the parse and the call list are now cached on the file's text (so a file a test writes is never
    answered from an older tree). What is left is ~1,900 tests' own overhead and
    `test_no_function_reimports_a_name_the_module_already_has` (one parse of all of `app/`, ~10 s), which stays.
    Red: a `time.sleep`, a `processEvents` and a `store.stats()` added to `files_view.py` each fail their check.
    No Qt in the file, so §4e has nothing to move.

**Group E - search, conversion and the acceptance layers** (358 s)
- [x] **2q** `tests/unit/test_query_plans.py` (107 s)
  - *2026-10-13:* **48.3 s -> 18.7 s (-61%)** (each file alone, `--durations=0`, before and after alternated back to back, twice each, the lower taken; the laptop was shared with other threads' runs, so read the ratio, not the seconds). The `populated` store (800 files and messages, written one row at a time) was rebuilt for each of its 15 tests at 2-4 s each; it is now built once per module, since every test that takes it only reads it (`EXPLAIN QUERY PLAN`, a `SELECT`, `keyword.search`). Red check: `PRAGMA case_sensitive_like = ON` added to `SqliteStore.connect` - 7 of the module-fixture tests went red - then undone. Left slow: `browse_table` (about 11 s, already module-scoped), because its VM-step assertions are measured at its 30,000 rows.
- [x] **2r** `tests/unit/test_converter.py` (97 s)
  - *2026-10-13:* **55.0 s -> 25.4 s (-54%)** (each file alone, `--durations=0`, before and after alternated back to back, twice each, the lower taken; the laptop was shared with other threads' runs, so read the ratio, not the seconds). `real_doc` started LibreOffice to make a `.doc` for each of five tests (8-13 s each); it is made once per module now, and each test still runs its own real conversion of it. Red check: `ConversionResult.close` made to skip the clean-up - `test_the_temporary_directory_is_removed` went red - then undone. Left slow, on purpose: the first real conversion (LibreOffice cold start) and `test_on_windows_a_doc_really_converts`, the only proof that the raw Windows invocation works.
- [x] **2s** `tests/integration/test_layer4_acceptance.py` (91 s)
  - *2026-10-13:* **134.6 s -> 33.2 s (-75%)** (each file alone, `--durations=0`, before and after alternated back to back, twice each, the lower taken; the laptop was shared with other threads' runs, so read the ratio, not the seconds). Still end to end: the same real `Pipeline.run()` over the real corpus, but once per module (`built_index`) instead of once for each of 57 tests. Each test gets its own copy of the SQLite file and the LanceDB folder, and the corpus is put back with its indexed timestamps at the same path, so the tests that write (usage log, re-index, corpus edits) cannot leak into each other. Red checks, each undone: `record_open` made a no-op - `test_opening_a_result_is_recorded` red; `has_changed` made to call a moved file unchanged - `test_the_cache_is_invalidated_by_indexing` red, which proves the re-index on the copy really runs. One timed run of the second "after" failed `test_warm_search_is_fast` (p95 617 ms against 300) while the machine was loaded (that round's "before" took 331 s); a side-by-side check in one process put a copied index and a freshly built one at the same speed (p95 26-44 ms against 32-66 ms), so the copy is not the cause. The test is wall-clock and sensitive to load as it was before.
- [x] **2t** `tests/integration/test_layer3_acceptance.py` (63 s)
  - *2026-10-13:* **Left as it is: 105.8 s, and the change tried saved 8% (97.7 s)**, under the 20% bar (each file alone, `--durations=0`, before and after alternated back to back, twice each, the lower taken; the laptop was shared with other threads' runs, so read the ratio, not the seconds). Tried: the twelve tests that start from a fully indexed corpus shared one real run per module, each with its own copy of the stores, the same as 2s; it went red when broken (pruning off, change detection off, the vector count off by one) and was then reverted. It saves little because the time is in the runs that are the behaviour under test - an interrupted run, a low disk, a corrupt file, a locked file, a small queue - about 1.5 s each, with no fixture to share. What is left inside each run is the pipeline's own 0.25 s worker polls (`pipeline.py`), which is app code and not this order's.

## 4. Optimal testing, not only faster testing

*Added 2026-10-13 at the owner's word ("also make sure it is optimal testing"). Measured on `main` the same day.*

**What was found.**
- **The mix.** `tests/unit` holds 670 files and 9,911 tests, `tests/integration` 8 files and 127 tests, and
  `tests/live` 3 tests. One e2e file (pywinauto, excluded by default) and one jvm file. 209 of the "unit" files
  build real Qt widgets and 65 drive the real window (`gui`), so a good part of "unit" is integration in practice.
- **The markers are wrong.** 698 tests take over a second, but only 93 are marked `slow` (377 s). The other
  **605 (3,687 s) are not**, so `run_suite.py --quick` (`not gui and not slow and not qt`) is not quick, and
  `--audit-markers` already lists them.
- **A gap on CI.** The Windows CI job runs `-m "not jvm and not e2e and not slow"` on every push and PR, and the
  weekly schedule runs only the macOS job. **The 93 `slow` tests never run on CI**, only in a local full run.
- **No coverage map.** `pytest-cov` is installed, but nothing records which modules no test reaches.

**The plan.**
- [x] **4a** **Tiers, each with one job.**
  - *Quick* (`--quick`, local, under 2 minutes): Qt-free logic and presenters only.
  - *Affected* (`--affected`, local, after each change): the files that can see the change.
  - *PR CI* (every push): everything except `e2e` and `jvm`. **`slow` is included**, because at 377 s it is too
    small to be worth a blind spot.
  - *Weekly CI*: the Windows job as well as macOS, with `jvm`.
  - *Before a release* (the owner's laptop): `e2e` against the packaged app.

  The workflow, `run_suite.py`'s tiers and `docs/VERSIONING.md`'s release checklist are changed to say exactly
  this. A guard test checks that the CI command and the tiers agree.

  *Done 2026-10-13.* `ci.yml`: the Windows job runs `-m "not jvm and not e2e"` (`slow` included) on every
  event, the weekly schedule included, and on the schedule only a second step runs `-m "jvm"` in a process of
  its own (JPype can take a process down). mpxj and jpype1 are not in the requirements, so on CI the jvm tests
  prove the fallback and skip the real reader. The macOS job is unchanged (still excludes `slow`; non-blocking,
  hand or weekly). `run_suite.py` holds the tiers as `TIERS`; `docs/VERSIONING.md`'s release checklist gains
  step 10, `e2e` on the owner's laptop against the packaged app. Guard: `tests/unit/test_suite_tiers.py`.
- [x] **4b** **Markers that tell the truth.** After §2's speed work, every test still over 1 s is either sped up or
  marked `slow`, so `--audit-markers` lists nothing. A guard test fails on a new unmarked test over the threshold
  (from `logs/suite/durations.json`), so the markers cannot drift again.

  *Done 2026-10-13, except the 20 §2 files.* Of the 605 unmarked tests over `run_suite.SLOW_SECONDS` (1 s),
  156 are in the 20 files §2 is speeding up and were left alone; the other **449 (428 test functions, the rest
  parametrized cases) in 186 files are marked `slow`** - per test in 180 files, `pytestmark` in the 6 where
  every test is slow. Guard: `tests/unit/test_slow_markers_truthful.py`, reading the source rather than the
  measurement's stale `marked` flag, with the 20 files exempt until their PRs merge (it skips where no
  `durations.json` exists, CI included). `--audit-markers` still lists them until the next full run rewrites
  the flags. **Found and fixed on the way:** `tests/conftest.py`'s Florence-2 guard let every `slow` test load
  the real model, so this marking would have let 428 more tests try a 1 GB download; the opt-out is a new
  `real_florence` marker on the two real-model proofs, with a test that a merely slow test is still guarded.
- [x] **4c** **A coverage map, measured once.** Run the full suite once under `pytest-cov` (branch coverage),
  commit the summary as `docs/TEST_COVERAGE.md`, and list:
  - every module under `app/` below 60% branch coverage;
  - every module that no test imports at all.

  *Done 2026-10-13.* `docs/TEST_COVERAGE.md`: the full suite on `main` 08aadaa under branch coverage in four
  parts, then the §4d files on top. 667 modules; 3 that no test runs; 91 under 60% branch. Two limits it
  states: code in a child process and code on Qt's thread pool are not traced, so those modules read lower
  than they are tested (the Contacts merge, conflicts and import paths among them).
- [x] **4d** **The gaps that matter, filled.** From 4c, the business-critical paths get tests where they are thin,
  each test first shown to fail when its behaviour breaks:
  - indexing (`app/index/`);
  - search (`app/search/`);
  - storage and migrations (`app/storage/`);
  - account sync writes and the delete brake (`app/accounts/`);
  - contact merges and enrichment (`app/contacts/`);
  - the error contract (`app/core/errors`).

  Target: **80% branch coverage** on these, and everything else **at least where it is today**, never lower.
  Trivial code (getters, `__repr__`, pure re-exports) and generated or vendored files are left out and listed.

  *2026-10-13, the gaps from the static scan (never-imported modules and unpinned promises), in risk order.*
  Eight new files, 147 tests (counting parametrized cases); every test function was shown red by at least one
  hand-made break to the app code, run, then undone (79 breaks, listed per file). **Not done here:** indexing and search (`app/index/`, `app/search/`) were not in this
  thread's list, and the 80% target is not claimed - the coverage numbers come from 4c's separate run.
  - `test_cov_contacts_merge_safe.py` (`book_actions.plan_safe_merges`, `merge_safe`, on a real hub): two
    cards with one email are planned and folded into the fuller one; every group of one action is one batch
    and one `undo_batch` gives every card back exactly; a caller's batch id is used, nothing to merge offers
    no undo; a group marked safe with a name-only, mixed or empty reason list, a group not flagged safe, and a
    group with one live card are refused; two people sharing only a name are suggested but never bulk-merged;
    the survivor holds every value, phone, company and group of what it absorbed; the folded contact keeps
    its card, sources and a `merged` journal entry naming the survivor; an account link moves to the survivor,
    a surplus one is queued `pending_remove`, and undo hands the links back. Red: the reason re-check removed;
    a batch per group; `pick_survivor` taking the first; the merged card not written; a one-card group
    planned; both safe layers removed (`dupes.safe_groups` and the re-check - either alone is covered by the
    other); the scan cache served stale (`book_version` ignored and `forget_scan` emptied, likewise).
  - `test_cov_storage_migration_steps.py` (`_apply_step`, `_roll_back`): a failure in the second of two steps
    keeps the first and leaves the version after it, `ERR_MIGRATION_FAILED` naming the step; an `AppError`
    raised by a step is rolled back and passes through as itself; another connection sees the old version
    while a step runs; a second run applies nothing; a caller's open transaction is committed first, not
    rolled back with the step; an own-transaction step moves the version once, and a failing one leaves it
    unchanged; a newer index and a gap in the steps are refused; `_roll_back` never raises, and a failure
    whose rollback also fails reports the first error. Red: no rollback; the version committed before the
    step; no second look under the lock; the `AppError` branch removed; the own-transaction first look
    removed; `_roll_back` unguarded; the caller's transaction not committed; a failure committed; a newer
    index accepted; a missing step skipped.
  - `test_cov_accounts_delete_brake.py` (`_too_many`, `_push`, `_apply_master_deletes`, with the `World` of
    `test_contact_sync_engine.py`): "more than" the count and the share, exactly at each and one past; one
    delete is never a mass delete; deletes exactly at the limit go through; one past, they wait while a new
    contact is still written, stay held on the next sync, and Allow carries them out; the master deleting
    exactly the limit is carried out to the Bin; past it, the contacts are only flagged, what was pulled is
    kept, the run is recorded `held` with its count, a flag counts again until Allow, and Allow deletes them.
    Red: `>=` for the count; `>=` for the share; the single-delete exception removed; the push holding all
    work; the push brake off; the master brake skipped; Allow ignored at the master; the earlier flags not
    counted again.
  - `test_cov_core_errors_guard.py` (`guard`, `to_app_error`): an unexpected exception becomes
    `ERR_UNEXPECTED` with its component, its context and its traceback, the cause chained; a chosen code
    fills its message from the context; an `AppErrorException` passes through as the same object whatever
    code is asked; an interrupt is not converted; caller details are kept beside the traceback. Red: the
    fix below reverted; `guard` wrapping app errors; no pass-through in `to_app_error`; `BaseException`
    caught; `from None`; the traceback dropped; the context dropped; a caller's suggestion dropped.
  - `test_cov_contacts_enrich_quoted.py`, `..._sigplaces.py`, `..._suggest.py`,
    `test_cov_accounts_providers_common.py` (`_common` and `_mailcommon`): the Sent line in seven shapes; a
    quoted block credited only to its own From address, none for a From with no address; quote marks off;
    `mail_quoting` to-and-not-from, newest first, window and limit, through the trigram index and the scan;
    countries whole and longest first, cities as written, domain endings, the region's order of evidence;
    automatic senders and people who are not; suggestions skip the book (Bin included), the owner and robots,
    keep the minimum, the limit and the order, and accepting is one sourced batch that never runs twice; each
    HTTP status and body to its registered error, short error text, times, tokens, refused DOCTYPEs and
    entities, base64url, a transport close that never fails. Red: 12 breaks in `quoted`, 8 in `sigplaces`
    (the fix below reverted among them), 9 in `suggest`, 17 in the two provider modules. Three breaks stayed
    green and are not claimed: the fraction-cutting in `parse_time` and in `epoch_from_iso` cannot be
    falsified on Python 3.12, whose `fromisoformat` reads seven digits itself, and `b64url_decode`'s padding
    arithmetic cannot either, because `base64` ignores surplus padding (no padding at all does go red).
  - **Two faults found and fixed.** `to_app_error(exc, c, details=...)` - and so `guard(c, details=...)` -
    passed `details` to `make_error` twice and raised a `TypeError` in place of the error it was reporting
    (`app/core/errors.py`; caller details are now kept above the traceback). `sigplaces.city_in` never gave
    a city of two or more words back as written ("ABU DHABI" came back "Abu Dhabi"): `re.escape` escapes the
    space, so replacing the space afterwards left a pattern that matched nothing (`app/contacts/enrich/sigplaces.py`;
    its only caller, `sigparse`, uses the answer as a yes or no, so no address changes).
  - **One question for the owner, not changed:** a merge does not carry the folded contact's sources
    (`contact_sources`) to the survivor - they stay on the folded contact in the Bin, so a value it brought
    shows no source on the survivor until the merge is undone. Whether the survivor should inherit them is
    a product decision.
  - *2026-10-13, measured (4c):* branch coverage with §4d - indexing 82.5%, search 91.3%, storage 81.0%,
    contacts 80.6%, the error contract (`app/core/errors.py`) 91.7%; accounts 79.1%, then 86.2% with the note
    below (measured on the accounts tests). Every path at 80% or more; indexing and search were there
    without new tests. The one fault fixed here besides the two above: a Graph update retried one property at
    a time counted an expired sign-in as that property refused, so every contact was reported refused
    instead of the account needing sign-in (`graph_contacts._update_piecewise`).
  - *2026-10-13, accounts to 80%:* `test_cov_accounts_carddav.py` (22 tests, on the loopback CardDAV server),
    `test_cov_accounts_graph_contacts.py` (25) and `test_cov_accounts_google_people.py` (21): the three contact
    connectors' batches answering each contact on its own (conflict, refusal, already gone, a sign-in failure
    stopping the batch), a refused batch sent one at a time, each CardDAV group card written once a batch and
    retried once when it moved, `$batch` refused or throttled past its rounds, folders and labels over pages or
    gone mid-walk, and single reads and writes whose refusal must not pass for success. Every test was red under
    at least one of 107 hand breaks (26 CardDAV, 45 Graph, 36 Google), each undone; one more stayed green and is
    not claimed (a second Graph web page cannot reach `_to_body`, which `project` cuts to one). **One fault
    found and fixed:** Graph's one-property-at-a-time retry counted a 401 as that property refused, so an
    expired sign-in sent every property of every contact and reported each as refused by Outlook; it now stops
    the batch as the account's error (`app/accounts/providers/graph_contacts.py`). Branch coverage of
    `app/accounts/`, the same 63-file selection (every `tests/unit` file that imports it) before and 66 after:
    **78.8% -> 86.2%** (1,776 -> 1,944 of 2,254 -> 2,256 branches); `carddav.py` 68.0% -> 88.9%,
    `graph_contacts.py` 68.3% -> 94.3%, `google_people.py` 74.6% -> 94.1%; nothing lower.
- [ ] **4e** **Logic out of Qt where a test is slow because of Qt.** Where a slow Qt test checks a decision rather
  than a widget, the decision moves to its presenter (the existing 250-line view rule, `test_presenter.py`) and
  is tested without Qt. The Qt test keeps only the wiring. Done file by file inside §2, and counted here.
  - *2026-10-13, group B (2e-2h):* **one decision moved.** Whether an opened group stays open on a
    redraw is `presenter.expansions_kept`, tested without Qt in `test_presenter.py`; `ResultsView`
    only calls it. The rest of these four files' slow tests check widgets - pictures against goldens,
    contrast read off grabbed pixels, the window's own construction and deferral, focus and keys - so
    there was no decision to move.
  - *2026-10-13, Group D:* none of `test_staging.py`, `test_resolve_gpu_regression.py`,
    `test_clip_lane_wiring.py` or `test_ui_never_blocks.py` builds a Qt widget; their time was a device test, a
    model download, repeated staging and repeated parsing. Nothing to move.
- [x] **4f** **No more order-dependent failures.** Two found on 2026-10-13: a Ctrl key left held by
  `QTest.keyClick` (#80), and a held mouse button from `mouseDClick`. An autouse fixture in
  `tests/unit/conftest.py` asserts that no key or mouse button is still held after a Qt test. A guard that the
  fixture exists keeps it from being removed.

  *Done 2026-10-13.* `no_input_left_held` (autouse, `tests/unit/conftest.py`): after any test once a
  `QApplication` exists, it reads `keyboardModifiers()` and `mouseButtons()`, releases whatever is held through
  a hidden scratch widget, and **fails the test at teardown, by name**. Run over the 22 files that press
  modifiers or double-click: 20 tests tripped it. The 7 in `test_list_keys.py` are fixed (its `_press` and
  `_double_click` helpers now let go). The 13 in `test_ui_redesign_scenarios.py` (Ctrl or Shift left held by
  its shortcut presses) are released and warned about through `KNOWN_INPUT_LEAKERS`, because §2g is rewriting
  that file; it comes off the list when §2g lands. Guards in `tests/unit/test_suite_tiers.py`: the fixture
  exists, is autouse and active, sees and releases both leaks, and excuses only §2 files.

## 3. Done when

- [x] **3a** Every file in §2 has its before and after times written under its item, and every changed test was
  shown to go red when its behaviour breaks.

  *Done 2026-10-13.* Every §2 item has its before and after times and the breaks that turned its
  changed tests red, written under it by its thread (#92).
- [x] **3b** The full suite passes once on the combined result (`scripts/run_suite.py`), and its total time is
  written here against the 5,043 s baseline. Target: the 20 files at **half their time or less** (1,260 s from
  2,520 s); a file that cannot get there says why.

  *Done 2026-10-13, on `main` 08aadaa (#92) with `scripts/run_suite.py`, four parts, on the laptop.*
  17,344 passed, 164 skipped, 1 xfailed, 3 failed, 0 crashed; wall 947 s. The three failures were fixed in #93 and
  pass (144 tests in their files): the verbatim-labels guard could not read the error box's title, which
  had moved into `widgets/error_box.py` unchanged; `test_converter_warm` counted `test_lo_session`'s
  sleeping children in another part as its own; `test_walker` counted another thread's `os.scandir`.
  **Test time: 2,901 s against the 5,043 s baseline (-42%)**, with 684 timed files against 564.
  **The 20 files: 535 s against 2,520 s (-79%; target 1,260 s, met).** Per file, before and after:
  2a 436 -> 1.1, 2b 214 -> 3.7, 2c 114 -> 6.5, 2d 69 -> 12.8, 2e 229 -> 51.7, 2f 94 -> 56.6,
  2g 83 -> 13.4, 2h 79 -> 21.1, 2i 144 -> 14.0, 2j 137 -> 73.9, 2k 84 -> 42.3, 2l 72 -> 20.6,
  2m 120 -> 19.1, 2n 119 -> 0.1, 2o 111 -> 75.5, 2p 58 -> 20.5, 2q 108 -> 38.3, 2r 97 -> 23.9,
  2s 91 -> 13.3, 2t 63 -> 26.5 s. Measured with four parts at once, so a single file's number carries
  the load of the other three.
- [x] **3c** `logs/suite/durations.json` is refreshed by that run, so the runner balances its parts on the new
  times.

  *Done 2026-10-13.* That run rewrote `logs/suite/durations.json` (684 files). Read against the source,
  229 cases (223 test functions in 96 files) took 1 s or more without a `slow` mark now that §2's
  exemptions end; all are marked (#93), and `test_ui_redesign_scenarios` lets go of its keys, so
  `KNOWN_INPUT_LEAKERS` is empty (4b, 4f).
- [x] **3d** The records: `CHANGELOG.md`, `HANDOFF.md`, and this order's row in `docs/ORDER_REGISTER.md`.

  *Done 2026-10-13.* `CHANGELOG.md`, `HANDOFF.md`, the register row (#93). **Left open: 4e** - groups B and
  D wrote what they found (one decision moved, `presenter.expansions_kept`; nothing to move in D); groups A,
  C and E did not say whether they looked, so it is not ticked on their behalf.
