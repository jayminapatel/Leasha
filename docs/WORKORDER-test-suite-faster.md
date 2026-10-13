# Work order (Parallel threads): the test suite runs faster - the slowest 20 files made lighter, every check kept

**Doc version:** 1.1 · **Updated:** 2026-10-13 · **Applies to:** app v1.0.3
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

- [ ] **1a** Each thread records its four files' times on `main` alone (the "before"), with the machine idle.

## 2. The files, in five groups

**Group A - indexing tuning and runs** (764 s)
- [ ] **2a** `tests/unit/test_index_tuning_acceptance.py` (436 s)
- [ ] **2b** `tests/unit/test_speed_work.py` (214 s)
- [ ] **2c** `tests/unit/test_run_setup.py` (114 s)
- [ ] **2d** `tests/unit/test_read_process.py` (69 s)

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
- [ ] **2i** `tests/unit/test_diagnostics.py` (144 s)
- [ ] **2j** `tests/unit/test_index_child_process.py` (137 s)
- [ ] **2k** `tests/unit/test_timed_out_retry.py` (84 s)
- [ ] **2l** `tests/unit/test_pst_folder_resume.py` (72 s)

**Group D - staging, the GPU and pictures** (407 s)
- [ ] **2m** `tests/unit/test_staging.py` (120 s)
- [ ] **2n** `tests/unit/test_resolve_gpu_regression.py` (119 s)
- [ ] **2o** `tests/unit/test_clip_lane_wiring.py` (110 s)
- [ ] **2p** `tests/unit/test_ui_never_blocks.py` (58 s)

**Group E - search, conversion and the acceptance layers** (358 s)
- [ ] **2q** `tests/unit/test_query_plans.py` (107 s)
- [ ] **2r** `tests/unit/test_converter.py` (97 s)
- [ ] **2s** `tests/integration/test_layer4_acceptance.py` (91 s)
- [ ] **2t** `tests/integration/test_layer3_acceptance.py` (63 s)

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
- [ ] **4a** **Tiers, each with one job.**
  - *Quick* (`--quick`, local, under 2 minutes): Qt-free logic and presenters only.
  - *Affected* (`--affected`, local, after each change): the files that can see the change.
  - *PR CI* (every push): everything except `e2e` and `jvm`. **`slow` is included**, because at 377 s it is too
    small to be worth a blind spot.
  - *Weekly CI*: the Windows job as well as macOS, with `jvm`.
  - *Before a release* (the owner's laptop): `e2e` against the packaged app.

  The workflow, `run_suite.py`'s tiers and `docs/VERSIONING.md`'s release checklist are changed to say exactly
  this. A guard test checks that the CI command and the tiers agree.
- [ ] **4b** **Markers that tell the truth.** After §2's speed work, every test still over 1 s is either sped up or
  marked `slow`, so `--audit-markers` lists nothing. A guard test fails on a new unmarked test over the threshold
  (from `logs/suite/durations.json`), so the markers cannot drift again.
- [ ] **4c** **A coverage map, measured once.** Run the full suite once under `pytest-cov` (branch coverage),
  commit the summary as `docs/TEST_COVERAGE.md`, and list:
  - every module under `app/` below 60% branch coverage;
  - every module that no test imports at all.
- [ ] **4d** **The gaps that matter, filled.** From 4c, the business-critical paths get tests where they are thin,
  each test first shown to fail when its behaviour breaks:
  - indexing (`app/index/`);
  - search (`app/search/`);
  - storage and migrations (`app/storage/`);
  - account sync writes and the delete brake (`app/accounts/`);
  - contact merges and enrichment (`app/contacts/`);
  - the error contract (`app/core/errors`).

  Target: **80% branch coverage** on these, and everything else **at least where it is today**, never lower.
  Trivial code (getters, `__repr__`, pure re-exports) and generated or vendored files are left out and listed.
- [ ] **4e** **Logic out of Qt where a test is slow because of Qt.** Where a slow Qt test checks a decision rather
  than a widget, the decision moves to its presenter (the existing 250-line view rule, `test_presenter.py`) and
  is tested without Qt. The Qt test keeps only the wiring. Done file by file inside §2, and counted here.
  - *2026-10-13, group B (2e-2h):* **one decision moved.** Whether an opened group stays open on a
    redraw is `presenter.expansions_kept`, tested without Qt in `test_presenter.py`; `ResultsView`
    only calls it. The rest of these four files' slow tests check widgets - pictures against goldens,
    contrast read off grabbed pixels, the window's own construction and deferral, focus and keys - so
    there was no decision to move.
- [ ] **4f** **No more order-dependent failures.** Two found on 2026-10-13: a Ctrl key left held by
  `QTest.keyClick` (#80), and a held mouse button from `mouseDClick`. An autouse fixture in
  `tests/unit/conftest.py` asserts that no key or mouse button is still held after a Qt test. A guard that the
  fixture exists keeps it from being removed.

## 3. Done when

- [ ] **3a** Every file in §2 has its before and after times written under its item, and every changed test was
  shown to go red when its behaviour breaks.
- [ ] **3b** The full suite passes once on the combined result (`scripts/run_suite.py`), and its total time is
  written here against the 5,043 s baseline. Target: the 20 files at **half their time or less** (1,260 s from
  2,520 s); a file that cannot get there says why.
- [ ] **3c** `logs/suite/durations.json` is refreshed by that run, so the runner balances its parts on the new
  times.
- [ ] **3d** The records: `CHANGELOG.md`, `HANDOFF.md`, and this order's row in `docs/ORDER_REGISTER.md`.
