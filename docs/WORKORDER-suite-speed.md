# Work order (One thread): the suite runs in the time its slowest third needs, and a change runs only the tests that can see it

**Doc version:** 1.0 · **Updated:** 2026-10-09 · **Applies to:** app v1.0.3
**Thread:** One thread (`scripts/run_suite.py`, a pytest plugin beside it, `tests/unit/`,
`.vscode/tasks.json`, `scripts/README.md`, `docs/VSCODE.md`)
**Status:** SHIPPED 2026-10-09, the same session. RELEASED by the owner 2026-10-09 ("As a testing expert can the full suite test be
optimised so it runs faster and tests only relevant parts" ... "Do it, make it efficient and
comprehensive"), to be built in the same session that wrote it.

**Where this came from.** The whole suite is the release gate and the hand-off proof: about
13,700 tests in 525 files, run by `scripts/run_suite.py` in three processes so that a native
crash in one is reported rather than hidden. What it costs was measured from the runner's own
logs before this order was written (the laptop, Leasha closed unless said):

| Run | part0 | part1 | part2 | Wall time |
|---|---|---|---|---|
| 2026-10-08, night | 17 min | 23 min | 22 min | 23 min |
| 2026-10-08, with an index run going | 35 min | not recorded | 47 min | 47 min |
| 2026-10-07 | 15 min | not recorded | not recorded | about 20 min |

Where the time goes, from the code rather than a guess:

1. **The parts are cut by file count, not by cost.** `split()` hands each process a contiguous
   alphabetical third of the files. The wall time is whatever the slowest third takes, and on the
   night run that was six minutes more than the fastest. Nothing records how long a file takes, so
   nothing could cut better.
2. **Every change runs everything.** 274 of the 525 files build Qt widgets; the other 250 are
   pure Python and cheap. A change to one reader runs all of them. There is no mapping from a
   changed file to the tests that can see it, so the only honest run is the whole suite - twenty
   minutes for a one-line fix, and so skipped more often than it should be.
3. **A test that hangs in native code hangs the run.** `pytest-timeout` cannot interrupt a
   thread that is inside a C library, so on 2026-10-08 one part sat for seven hours on a
   benchmark test until it was killed by hand (HANDOFF 7.119). The runner trusts pytest's limit
   and has none of its own.

What was ruled out, and why, so nobody re-derives it:

- **`pytest-xdist`.** One Python process per worker, shared across many files: a leaked widget
  or a native fault takes down every test queued behind it, which is the exact failure the
  runner was written to survive (its docstring). The runner's separate processes per part stay.
- **`pytest-testmon`.** Selects tests from coverage traces of the last run. The traces do not
  follow the reader subprocesses or Qt's event loop, so it would quietly drop tests that the
  change does reach. The mapping below is by import, which is static and checkable.
- **Marking more tests `slow` by hand.** The marker's own definition is "takes more than a
  second"; until something measures, a mark is an opinion. Item 1a measures first.

## 1. The runner measures, balances and does not hang

> **2026-10-09:** built. `scripts/suite_durations.py`; `Recorder` registered only when `LEASHA_SUITE_DURATIONS` is set; the runner merges the parts (`merge_durations`).

- [x] **1a** Each part records how long every test file took. `scripts/suite_durations.py` is a
      pytest plugin the runner loads with `-p scripts.suite_durations`; it sums the setup, call
      and teardown durations by file and writes them, with every test over a second and whether
      it was marked `slow`, to the path in `LEASHA_SUITE_DURATIONS`. After a run the runner
      merges the parts into `logs/suite/durations.json` (under `logs/`, which git ignores: the
      numbers are this machine's). A part that crashes writes nothing and the previous numbers
      stand.
      *Acceptance:* a pytest run with the plugin on a small file writes a JSON file whose
      `files` total matches the run and whose `slow_tests` carry a `marked` flag; the runner's
      merge keeps a file that did not run this time and overwrites one that did.
> **2026-10-09:** built. `split_balanced` (LPT, median for the unmeasured, alphabetical inside a part); `split` unchanged as the fallback.

- [x] **1b** The parts are balanced by measured cost. With a durations file, `split_balanced`
      places files longest-first into the emptiest part (the LPT rule), a file never measured
      counting as the median of those that were; within a part the files stay alphabetical so a
      crashed part still names the file it died in. Without a durations file the old contiguous
      split is used unchanged, so a fresh clone behaves as it did.
      *Acceptance:* for any durations the parts' totals differ by no more than the longest
      single file; every file appears exactly once; the same inputs give the same parts.
> **2026-10-09:** built. `--part-timeout`, `_wait_all`, `kill_tree` (`taskkill /T /F` on Windows, the process group elsewhere); tested on a real parent and child.

- [x] **1c** The runner has a wall-clock ceiling per part, `--part-timeout` (default one hour,
      twice the slowest part ever recorded when the machine was free). A part past it is killed
      with its whole process tree (reader processes, LibreOffice), reported as **TIMED OUT** with
      the last file it started, and counts as a crash, so the exit code is red.
      *Acceptance:* a part that never ends is killed and reported, and `main()` returns 1.
> **2026-10-09:** built. `describe_measurements` after every run; `--audit-markers` prints forty from the stored file and runs nothing.

- [x] **1d** The runner reports what the measurement found: after a run, the ten slowest files,
      and every test over a second that is not marked `slow` (the marker's own definition).
      `--audit-markers` prints the full list from the stored file without running anything. The
      list is information for the person; this order marks nothing.

## 2. A change runs the tests that can see it

> **2026-10-09:** built. `scripts/suite_affected.py`. Measured on this repository: `app/ui/search_view.py` -> 46 of 523, `docs/TROUBLESHOOTING.md` -> 13, `app/index/read_process.py` -> 308, `app/extract/archive.py` or anything in `app/core` -> the whole suite. A backend change honestly reaches most tests; the balanced split is the lever there.

- [x] **2a** `run_suite.py --affected [REF]` runs the tests a change can reach, in the same
      processes with the same crash reporting. The changed files are `git diff --name-only REF`
      (default `HEAD`: everything uncommitted) plus untracked files. The mapping is static:
      - A changed `app/` module, and every `app/` module that imports it, transitively (the
        import graph is read with `ast`, relative imports resolved): every test file that
        imports any module in that set, and every test file whose text names the changed file
        (the guard tests that read source do that).
      - A changed test file runs itself. A changed `tests/conftest.py`, `tests/fixtures/`,
        `pyproject.toml` or `requirements*.txt` runs everything.
      - A changed document, script, PowerShell file, packaging file or `Leasha.pyproj` runs
        the docs, hand-off and project-file tests and every test whose text names it.
      - **Always:** the load-bearing tests in `WORKORDER-CONVENTIONS.md` §0 (parsed from the
        same table `test_docs_versioned.py` checks), the docs, hand-off, project-file and
        layering tests.
      - When the affected set is more than six files in ten, the whole suite runs and the
        runner says why: a change to `app/core` reaches everything, and pretending otherwise
        is the vacuous-skip lesson (order 0m S1a) again.
      The runner prints what it chose and why before starting, so the person can see what the
      selection left out.
      *Acceptance:* on a fixture tree, a changed leaf module selects the tests that import it
      and the module that imports it, and nothing else beyond the always-run set; a changed
      conftest selects everything; a changed document selects the docs tests and the test that
      names it. On this repository, the 1.0.3 change set resolves to a list that includes
      `test_reader_process_isolation.py`, `test_read_process.py` and `test_files_left_in_hand.py`.
> **2026-10-09:** built. `--quick`; `PROJECT_DESELECT` is checked against `pyproject.toml` by a test so the two cannot drift.

- [x] **2b** `run_suite.py --quick` deselects `gui`, `slow` and `qt` tests for a sanity pass,
      combining with the project's own `-m` (a second `-m` would replace it, not add to it). It
      is stated in the output and the docs as a sanity pass, never a gate.
> **2026-10-09:** built. two tasks in `.vscode/tasks.json`, `docs/VSCODE.md` 1.5, `scripts/README.md` 1.3; a test reads all three.

- [x] **2c** The VS Code tasks and the docs: **Run the tests affected by your changes** and
      **Quick check (no Qt, no slow tests)** in `.vscode/tasks.json` and `docs/VSCODE.md`;
      `scripts/README.md` describes the runner as it now is. The release checklist and the
      hand-off proof stay the whole suite.

## 3. What this order does not do

- It does not change which tests exist, what they assert or which are marked `slow`. Item 1d
  lists candidates; marking them is a decision for the owner, with the numbers in front of them.
- It does not make `--affected` or `--quick` a release gate. The whole suite is the gate; the
  numbers above are what the whole suite costs, and 1b is what brings them down.
- It does not add `pytest-xdist` or `pytest-testmon` (above).

## 4. Records

> **2026-10-09:** done, in the 1.0.3 release records.

- [x] **4a** `HANDOFF.md` entry; `CHANGELOG.md` in the product voice under the release this
      ships in; `docs/ORDER_REGISTER.md` row; this order's checkboxes ticked with dated notes,
      never reworded.
