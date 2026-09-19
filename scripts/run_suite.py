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

    python scripts/run_suite.py            # four processes
    python scripts/run_suite.py -j 6       # six
    python scripts/run_suite.py tests/unit/test_ocr.py tests/unit/test_backends.py

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="test files (default: all)")
    parser.add_argument("-j", "--processes", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=900,
                        help="seconds one test may take (pytest-timeout)")
    args = parser.parse_args(argv)

    files = find_files(args.files)
    groups = split(files, args.processes)
    work = Path(tempfile.mkdtemp(prefix="leasha-suite-"))
    started = time.time()
    print(f"{len(files)} test files, {len(groups)} processes, logs in {work}", flush=True)

    running = []
    for number, group in enumerate(groups):
        log = work / f"part{number}.txt"
        handle = open(log, "w", encoding="utf-8", errors="replace")
        command = [sys.executable, "-m", "pytest", *group, "-rf",
                   "-p", "no:cacheprovider", f"--timeout={args.timeout}",
                   f"--basetemp={work / f'tmp{number}'}"]
        process = subprocess.Popen(command, cwd=ROOT, stdout=handle,
                                   stderr=subprocess.STDOUT)
        running.append((number, group, log, handle, process))

    failures: set[str] = set()
    crashed = 0
    for number, group, log, handle, process in running:
        code = process.wait()
        handle.close()
        text = log.read_text(encoding="utf-8", errors="replace")
        last_file = next((m.group(1) for m in map(FILE_LINE.match, reversed(text.splitlines()))
                          if m), "(none started)")
        last_summary = next((line for line in reversed(text.splitlines())
                             if re.search(r"\d+ (passed|failed)", line)), "")
        tally = ", ".join(f"{count} {word}" for count, word in SUMMARY.findall(last_summary))
        failures.update(m.group(2) for m in map(FAILED_LINE.match, text.splitlines()) if m)
        if code in NORMAL_EXITS:
            print(f"  part {number}: finished ({tally or 'no summary'}) - {len(group)} files", flush=True)
        else:
            crashed += 1
            print(f"  part {number}: CRASHED, exit {code} (0x{code & 0xFFFFFFFF:X}) - the process "
                  f"died. Last file it started: {last_file}. Its later tests did NOT run.", flush=True)

    print()
    for name in sorted(failures):
        print(f"FAILED {name}")
    print(f"\n{len(failures)} failed, {crashed} process(es) crashed, "
          f"{int(time.time() - started)}s.  Full logs: {work}")
    return 1 if failures or crashed else 0


if __name__ == "__main__":
    sys.exit(main())
