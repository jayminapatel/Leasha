"""Run the whole test suite in several processes, and say so when one dies.

Layer: L0

**Why this exists.** Run in one process, the suite ended silently three times
in a row on 2026-09-19: pytest exited with 0xC0000005 (a native access
violation) part-way through, with no traceback, no Windows event and empty
stderr, so every test after it simply never reported. `-q` output looks the
same whether the run finished or the process vanished.

Two causes were found. The suite was building real DirectML sessions on the
graphics card through the default `EMBED_DEVICE=auto` (pinned to the processor
in `tests/conftest.py` now), and a Qt timer in `app/ui/view_options.py` can
touch a table that has been torn down (open - see HANDOFF.md). Neither is a
reason to trust a single long process, so this splits the files across a few
and reports on each:

    python scripts/run_suite.py            # as many processes as memory allows, up to four
    python scripts/run_suite.py -j 6       # six, whatever the machine has free
    python scripts/run_suite.py tests/unit/test_ocr.py tests/unit/test_backends.py

**How many processes.** Each one loads Qt, onnxruntime, pyarrow and LanceDB, and the
heaviest were measured at 1.2-2.0 GB resident, so the default is derived from what is
actually free rather than being a flat four; an explicit `-j` is obeyed, and warned about
when the memory is not there for it. **This is prudence, not a cure**: the native crashes
of 2026-09-20 were first blamed on memory and that was wrong - the same crash happened at
`-j 3` with 14.8 GB free. Their cause was a leaked widget (see below), and it is fixed.

**A native crash here is usually a leaked Qt widget, not the test that died.** Three runs
died with `0xC0000005` / `0xC0000374` inside one rail test that passes perfectly well on
its own. It called `QApplication.setStyleSheet`, which makes Qt re-polish *every* widget
alive in the process; tests build real widgets and let Python drop them without
`deleteLater`, so on a long run the walk eventually reaches a C++ object that is already
gone. Reproduced deliberately with the fourteen files that precede it - and neither half
of those crashes alone, which is the tell: it is the *number* of leaked widgets, not one
culprit file. Fixed by scoping that stylesheet to the widget under test. If this happens
again, look for whatever just walked all widgets, not at the test named in the traceback.

Exit code 0 only when every process finished and nothing failed. A process
that died is reported as **CRASHED**, with the last test file it had started -
that file is the place to start looking - and counts as a failure.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]

#: Windows reports an access violation as a negative exit code; POSIX reports a
#: signal the same way. Ordinary pytest exits are 0..5.
NORMAL_EXITS = {0, 1, 5}          # passed, tests failed, nothing collected

FAILED_LINE = re.compile(r"^(FAILED|ERROR) (\S+)")
FILE_LINE = re.compile(r"^(tests[\\/][\w./\\-]+\.py)")
SUMMARY = re.compile(r"(\d+) (passed|failed|skipped|error|errors|deselected|xfailed|xpassed)")


def find_files(explicit: list[str]) -> list[str]:
    if explicit:
        return explicit
    found = sorted(p.relative_to(ROOT).as_posix()
                   for p in (ROOT / "tests").rglob("test_*.py"))
    return found


def split(files: list[str], parts: int) -> list[list[str]]:
    """Contiguous, near-equal groups - contiguous so a group is a stable range."""
    parts = max(1, min(parts, len(files)))
    size = -(-len(files) // parts)
    return [files[i:i + size] for i in range(0, len(files), size)]


#: Resident memory one test process needs before the native allocators start failing.
#: Measured on 2026-09-20: the heaviest processes sat at 1.2 GB and 2.0 GB.
MEMORY_PER_PROCESS_GB = 2.5

#: Never more than this without being asked: beyond it the processes compete for the
#: machine rather than finishing sooner.
DEFAULT_MAX_PROCESSES = 4


def choose_processes(requested: Optional[int], free_gb: Optional[float]) -> tuple[int, str]:
    """How many processes to run, and the sentence to print when that needs explaining.

    Pure, so the arithmetic is testable without a particular machine: `free_gb` is
    `None` when the amount free could not be read, and then nothing is second-guessed.
    """
    if free_gb is None:
        return (requested or DEFAULT_MAX_PROCESSES), ""
    affordable = max(1, int(free_gb // MEMORY_PER_PROCESS_GB))
    if requested is None:
        chosen = max(1, min(DEFAULT_MAX_PROCESSES, affordable))
        if chosen < DEFAULT_MAX_PROCESSES:
            return chosen, (f"{free_gb:.1f} GB free, so {chosen} process"
                            f"{'es' if chosen != 1 else ''} rather than {DEFAULT_MAX_PROCESSES} "
                            f"(about {MEMORY_PER_PROCESS_GB} GB each)")
        return chosen, ""
    if requested > affordable:
        return requested, (f"WARNING: {requested} processes asked for with only {free_gb:.1f} GB "
                           f"free - about {MEMORY_PER_PROCESS_GB} GB each is needed, so a process "
                           f"may die natively part-way through. {affordable} would be safe.")
    return requested, ""


#: What pytest-timeout prints before it ends the process.
TIMEOUT_MARK = "+ Timeout +"


#: pytest's last line, whatever the counts: "==== 3 failed, 9 passed in 12.34s ====",
#: "==== 5 skipped in 0.10s ====", "==== no tests ran in 0.01s ====".
LAST_LINE = re.compile(r"^=+ .* in [\d.]+s.*=+\s*$", re.MULTILINE)


def part_died(code: int, log_text: str) -> bool:
    """Did this process stop before it finished its files?

    Yes if its exit code is not one pytest gives, **and yes if it gave an
    ordinary code but never printed its last line** - pytest always prints it
    when it reaches the end, so without it the end was not reached.
    """
    return code not in NORMAL_EXITS or not LAST_LINE.search(log_text or "")


def _free_gb() -> Optional[float]:
    try:
        import psutil
    except Exception:                                      # noqa: BLE001 - never block a run
        return None
    try:
        return float(psutil.virtual_memory().available) / (1024 ** 3)
    except Exception:                                      # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="test files (default: all)")
    parser.add_argument("-j", "--processes", type=int, default=None,
                        help="how many processes (default: what the free memory allows, "
                             f"up to {DEFAULT_MAX_PROCESSES})")
    parser.add_argument("--timeout", type=int, default=900,
                        help="seconds one test may take (pytest-timeout)")
    args = parser.parse_args(argv)

    files = find_files(args.files)
    processes, note = choose_processes(args.processes, _free_gb())
    if note:
        print(note, flush=True)
    groups = split(files, processes)
    # Not "leasha-...": tests name fixture repositories after the project and
    # match them by name *and* root-path substring, so a temp folder with the
    # project's name in it made `repo:leasha` match a second repository.
    work = Path(tempfile.mkdtemp(prefix="pt-run-"))
    started = time.time()
    print(f"{len(files)} test files, {len(groups)} processes, logs in {work}", flush=True)

    running = []
    for number, group in enumerate(groups):
        log = work / f"part{number}.txt"
        handle = open(log, "w", encoding="utf-8", errors="replace")
        # 2026-10-05: `-v` undoes the project's `addopts = -q`, which dropped the
        # file names from the log - so a crashed part could never say which file
        # it died in (`FILE_LINE` matched nothing; "Last file it started: (none
        # started)"). One line per file, the dots after it, as pytest's default.
        # 2026-10-08, found in review: `-rf` lists FAILED tests only. A test whose
        # fixture raised is an ERROR, which `-rf` leaves out of the short summary,
        # so `FAILED_LINE` (which already accepts ERROR) matched nothing, the part
        # "finished" with a normal summary line, and the run exited 0 with the
        # tally saying "1 error". `-rfE` lists both; the error count from the
        # summary line is counted below as well, so the exit code cannot be green
        # while the tally is not.
        command = [sys.executable, "-m", "pytest", *group, "-v", "-rfE",
                   "-p", "no:cacheprovider", f"--timeout={args.timeout}",
                   f"--basetemp={work / f'tmp{number}'}"]
        process = subprocess.Popen(command, cwd=ROOT, stdout=handle,
                                   stderr=subprocess.STDOUT)
        running.append((number, group, log, handle, process))

    failures: set[str] = set()
    crashed = 0
    errored = 0
    for number, group, log, handle, process in running:
        code = process.wait()
        handle.close()
        text = log.read_text(encoding="utf-8", errors="replace")
        last_file = next((m.group(1) for m in map(FILE_LINE.match, reversed(text.splitlines()))
                          if m), "(none started)")
        last_summary = next((line for line in reversed(text.splitlines())
                             if re.search(r"\d+ (passed|failed|error)", line)), "")
        counts = SUMMARY.findall(last_summary)
        tally = ", ".join(f"{count} {word}" for count, word in counts)
        # The belt to `-rfE`'s braces: whatever the short summary lists, a
        # non-zero error count on the summary line is never a green run.
        errored += sum(int(count) for count, word in counts if word.startswith("error"))
        failures.update(m.group(2) for m in map(FAILED_LINE.match, text.splitlines()) if m)
        if not part_died(code, text):
            print(f"  part {number}: finished ({tally or 'nothing passed or failed'}) - "
                  f"{len(group)} files", flush=True)
        elif code in NORMAL_EXITS:
            # 2026-10-05: an ordinary exit code and no summary line. pytest-timeout
            # ends the whole process (`os._exit(1)`) when one test overruns, and 1
            # is also "some tests failed" - so this read "finished (no summary)"
            # and the run ended "0 failed, 0 process(es) crashed" with most of the
            # suite not run. Found on a trial branch where every process stopped
            # on a blocked pop-up and the total said all was well.
            crashed += 1
            why = ("one test ran past the time limit and pytest-timeout ended the process"
                   if TIMEOUT_MARK in text else "it ended without a summary line")
            print(f"  part {number}: STOPPED EARLY, exit {code} - {why}. Last file it "
                  f"started: {last_file}. Its later tests did NOT run.", flush=True)
        else:
            crashed += 1
            print(f"  part {number}: CRASHED, exit {code} (0x{code & 0xFFFFFFFF:X}) - the process "
                  f"died. Last file it started: {last_file}. Its later tests did NOT run.", flush=True)

    print()
    for name in sorted(failures):
        print(f"FAILED {name}")
    print(f"\n{len(failures)} failed, {errored} errored, {crashed} process(es) crashed, "
          f"{int(time.time() - started)}s.  Full logs: {work}")
    return 1 if failures or errored or crashed else 0


if __name__ == "__main__":
    sys.exit(main())
