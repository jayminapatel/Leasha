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
- [ ] **2e** `tests/unit/test_grab_ui.py` (229 s)
- [ ] **2f** `tests/unit/test_window_opens.py` (93 s)
- [ ] **2g** `tests/unit/test_ui_redesign_scenarios.py` (83 s)
- [ ] **2h** `tests/unit/test_ui_review_0x9.py` (79 s)

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
