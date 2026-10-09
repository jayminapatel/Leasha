# scripts

**Doc version:** 1.3 · **Updated:** 2026-10-09 · **Applies to:** app v0.3.5

Helper scripts that are not part of the application. Run them from the project folder with
the venv's Python (`venv\Scripts\python.exe` on Windows, `venv/bin/python` elsewhere).

- `run_suite.py` - runs the whole test suite in several processes (three or four, fewer when
  memory is short) and says when one stops before the end: **CRASHED** for a native crash,
  **STOPPED EARLY** when a test overran its time limit and pytest-timeout ended the process,
  **TIMED OUT** when a part ran past `--part-timeout` (an hour) and was killed with its
  process tree. Either way it names the last file that process started. Use it rather than
  one `pytest tests` process, which can die part-way through. Each part records what every
  test file cost (`suite_durations.py`, a pytest plugin) and the runner merges that into
  `logs/suite/durations.json`, so the next run shares the files out by measured cost and the
  parts finish together; with no measurements yet the files are cut into alphabetical
  groups. `-j N` sets the processes, `--timeout S` the limit for one test; name test files to
  run only those. `--affected [REF]` runs only the test files that can see what changed
  against REF (default `HEAD`: everything uncommitted and untracked) - `suite_affected.py`
  maps a change through the import graph and the texts of the tests, always adds the
  load-bearing tests, and runs everything when most of the suite can see the change, saying
  why. `--quick` deselects `gui`, `slow` and `qt` tests for a sanity pass; neither is the
  release gate, which stays the whole suite. `--audit-markers` lists the slowest files and
  every test over a second not marked `slow`, from the stored measurements.
- `suite_durations.py` and `suite_affected.py` - the runner's two helpers above; not run on
  their own.
- `regen_vs_project.py` - rewrites `Leasha.pyproj` from the files git tracks. Run it after
  adding or removing a Python file (`git add` it first); `tests/unit/test_vs_project.py` fails
  until you do.
- `stage.py` - builds the tree an installed copy of Leasha has and runs from it, to catch a file
  the application needs that an install would not carry.
- `pst_field_test.py` - reads your own `.pst` files with the libpff reader, read-only, one
  process per archive, and writes a report of speed, status counts and any archive that stalled
  or failed. `--outlook` adds the archives open in Outlook; `--stall` sets how long a reader may
  stand still.
- `parse-check.ps1` - checks every PowerShell script with PowerShell's own parser, including the
  missing-BOM fault that once stopped the installer without a word. `run-install.cmd` runs it
  first.
- `install-nightly.ps1` - registers (or, with `-Uninstall`, removes) `tools\nightly.py` as a
  Windows scheduled task. Run only by the owner: it is a standing change to the machine.

The Windows installer is built by `packaging\build.ps1`, not from here.
