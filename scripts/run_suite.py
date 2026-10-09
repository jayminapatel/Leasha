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
    python scripts/run_suite.py --affected             # the tests that can see what is uncommitted
    python scripts/run_suite.py --affected origin/main # ... or everything since a ref
    python scripts/run_suite.py --quick                # no Qt, no slow tests: a sanity pass
    python scripts/run_suite.py --audit-markers        # tests over a second not marked `slow`

**How many processes.** Each one loads Qt, onnxruntime, pyarrow and LanceDB, and the
heaviest were measured at 1.2-2.0 GB resident, so the default is derived from what is
actually free rather than being a flat four; an explicit `-j` is obeyed, and warned about
when the memory is not there for it. **This is prudence, not a cure**: the native crashes
of 2026-09-20 were first blamed on memory and that was wrong - the same crash happened at
`-j 3` with 14.8 GB free. Their cause was a leaked widget (see below), and it is fixed.

**How the files are shared out (order `suite-speed`, 2026-10-09).** Each part records how
long every test file took (`scripts/suite_durations.py`, a pytest plugin) and the runner
merges the parts into `logs/suite/durations.json` after the run. The next run places files
longest-first into the emptiest part, so the parts finish together instead of the wall time
being whatever the slowest alphabetical third needs - on 2026-10-08 that was 23 minutes
against a 17-minute fastest part. With no measurements yet the files are cut into contiguous
alphabetical groups as before. `--affected` runs only the test files that can see a change
(`scripts/suite_affected.py` says how that is decided, and the runner prints what it chose);
the whole suite stays the release gate.

**A part that hangs is killed.** `pytest-timeout` cannot interrupt a thread inside a C
library, and on 2026-10-08 one part sat for seven hours in a benchmark test until it was
killed by hand. `--part-timeout` (an hour by default) kills a part and its process tree -
reader processes, LibreOffice - and reports it as **TIMED OUT**, which is red.

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
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import suite_affected  # noqa: E402 - beside this file, found through the line above

#: Windows reports an access violation as a negative exit code; POSIX reports a
#: signal the same way. Ordinary pytest exits are 0..5.
NORMAL_EXITS = {0, 1, 5}          # passed, tests failed, nothing collected

FAILED_LINE = re.compile(r"^(FAILED|ERROR) (\S+)")
FILE_LINE = re.compile(r"^(tests[\\/][\w./\\-]+\.py)")
SUMMARY = re.compile(r"(\d+) (passed|failed|skipped|error|errors|deselected|xfailed|xpassed)")

#: Where the merged measurements live. Under `logs/`, which git ignores: the numbers are
#: this machine's, and a fresh clone falls back to the contiguous split.
DURATIONS_FILE = ROOT / "logs" / "suite" / "durations.json"

#: The project's own deselection (`addopts` in `pyproject.toml`). A second `-m` on the
#: command line *replaces* it rather than adding to it, so `--quick` must repeat it.
PROJECT_DESELECT = "not jvm and not e2e"
QUICK_DESELECT = "not gui and not slow and not qt"

#: How long one part may run before it is killed with its process tree. Twice the
#: slowest part ever recorded with the machine free (23 minutes, 2026-10-08).
DEFAULT_PART_TIMEOUT = 3600

#: How often the runner looks at its parts. A module constant so a test can shorten it.
POLL_SECONDS = 1.0

#: Tests at or over this are listed by `--audit-markers` when not marked `slow`.
SLOW_SECONDS = 1.0


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


def split_balanced(files: list[str], parts: int, durations: dict[str, float]) -> list[list[str]]:
    """Groups whose measured totals are as equal as the files allow.

    Longest file first into the emptiest group (the LPT rule: the totals end up within
    one file's length of each other). A file never measured counts as the median of
    those that were, so a new test file lands somewhere sensible rather than at zero.
    Within a group the files stay alphabetical, so a crashed group still names the file
    it died in. Without any measurements this is `split()`, unchanged.
    """
    if not durations:
        return split(files, parts)
    parts = max(1, min(parts, len(files)))
    known = [durations[f] for f in files if f in durations]
    fill = statistics.median(known) if known else 1.0
    weight = {f: float(durations.get(f, fill)) for f in files}
    totals = [0.0] * parts
    groups: list[list[str]] = [[] for _ in range(parts)]
    for name in sorted(files, key=lambda f: (-weight[f], f)):
        emptiest = min(range(parts), key=lambda i: (totals[i], i))
        totals[emptiest] += weight[name]
        groups[emptiest].append(name)
    return [sorted(group) for group in groups if group]


def load_durations(path: Path = DURATIONS_FILE) -> dict:
    """The merged measurements, or `{}` when there are none yet or the file is unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_durations(previous: dict, parts: list[dict]) -> dict:
    """The previous measurements updated by this run's parts.

    A file that ran this time takes its new number and drops its old slow tests; a file
    that did not run (a crashed part, an `--affected` run) keeps what it had.
    """
    files = dict(previous.get("files", {}))
    slow = dict(previous.get("slow_tests", {}))
    for part in parts:
        ran = set(part.get("files", {}))
        files.update(part.get("files", {}))
        slow = {name: entry for name, entry in slow.items() if name.split("::", 1)[0] not in ran}
        slow.update(part.get("slow_tests", {}))
    return {"written": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": files, "slow_tests": slow}


def unmarked_slow(measurements: dict) -> list[tuple[str, float]]:
    """Tests at or over `SLOW_SECONDS` without the `slow` mark, slowest first."""
    found = [(name, float(entry.get("seconds", 0.0)))
             for name, entry in measurements.get("slow_tests", {}).items()
             if not entry.get("marked") and float(entry.get("seconds", 0.0)) >= SLOW_SECONDS]
    return sorted(found, key=lambda pair: (-pair[1], pair[0]))


def describe_measurements(measurements: dict, limit: int = 10) -> str:
    """The slowest files and the unmarked slow tests, for the end of a run."""
    files = measurements.get("files", {})
    if not files:
        return "no measurements yet"
    lines = [f"slowest {min(limit, len(files))} of {len(files)} test files "
             f"({sum(files.values()):.0f}s measured in all):"]
    for name, seconds in sorted(files.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]:
        lines.append(f"  {seconds:7.1f}s  {name}")
    slow = unmarked_slow(measurements)
    if slow:
        lines.append(f"{len(slow)} test(s) over {SLOW_SECONDS:.0f}s not marked `slow` "
                     f"(the marker's own definition); the slowest:")
        for name, seconds in slow[:limit]:
            lines.append(f"  {seconds:7.1f}s  {name}")
    return "\n".join(lines)


#: Resident memory one test process needs before the native allocators start failing.
#: Measured on 2026-09-20: the heaviest processes sat at 1.2 GB and 2.0 GB.
MEMORY_PER_PROCESS_GB = 2.5

#: Never more than this without being asked: beyond it the processes compete for the
#: machine rather than finishing sooner.
DEFAULT_MAX_PROCESSES = 4


def choose_processes(requested: int | None, free_gb: float | None) -> tuple[int, str]:
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


def _free_gb() -> float | None:
    try:
        import psutil
    except Exception:                                      # noqa: BLE001 - never block a run
        return None
    try:
        return float(psutil.virtual_memory().available) / (1024 ** 3)
    except Exception:                                      # noqa: BLE001
        return None


def kill_tree(process) -> None:
    """End a part and everything it started: reader processes, LibreOffice, a shell.

    On Windows `taskkill /T` walks the tree; elsewhere the part was started as its own
    session so the whole group can be signalled. Either failing falls back to killing
    the one process, which is still better than waiting forever.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           capture_output=True, check=False, timeout=30)
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
    except Exception:                                      # noqa: BLE001 - fall back below
        pass
    try:
        process.kill()
    except Exception:                                      # noqa: BLE001 - already gone
        pass


def _wait_all(running: list, deadline: float) -> dict[int, tuple[int | None, bool]]:
    """Wait for every part; a part still going at `deadline` is killed.

    Returns part number -> (exit code, timed out).
    """
    results: dict[int, tuple[int | None, bool]] = {}
    pending = list(running)
    while pending:
        for item in list(pending):
            number, _group, _log, _handle, process = item
            code = process.poll()
            if code is not None:
                results[number] = (code, False)
                pending.remove(item)
            elif time.time() >= deadline:
                kill_tree(process)
                try:
                    code = process.wait(timeout=60)
                except Exception:                          # noqa: BLE001 - report it anyway
                    code = None
                results[number] = (code, True)
                pending.remove(item)
        if pending:
            time.sleep(POLL_SECONDS)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="test files (default: all)")
    parser.add_argument("-j", "--processes", type=int, default=None,
                        help="how many processes (default: what the free memory allows, "
                             f"up to {DEFAULT_MAX_PROCESSES})")
    parser.add_argument("--timeout", type=int, default=900,
                        help="seconds one test may take (pytest-timeout)")
    parser.add_argument("--part-timeout", type=float, default=DEFAULT_PART_TIMEOUT,
                        help="seconds one part may run before it is killed with its process "
                             f"tree (default {DEFAULT_PART_TIMEOUT})")
    parser.add_argument("--affected", nargs="?", const="HEAD", default=None, metavar="REF",
                        help="only the test files that can see what changed against REF "
                             "(default HEAD: everything uncommitted, plus untracked files)")
    parser.add_argument("--quick", action="store_true",
                        help=f"deselect `{QUICK_DESELECT}`: a sanity pass, never a gate")
    parser.add_argument("--durations", type=Path, default=DURATIONS_FILE,
                        help="where the measured durations are kept (default logs/suite/)")
    parser.add_argument("--audit-markers", action="store_true",
                        help="print the slowest files and the tests over a second not marked "
                             "`slow`, from the stored measurements, and run nothing")
    args = parser.parse_args(argv)

    measurements = load_durations(args.durations)
    if args.audit_markers:
        print(describe_measurements(measurements, limit=40), flush=True)
        return 0

    files = find_files(args.files)
    if args.affected is not None and not args.files:
        changed = suite_affected.changed_paths(ROOT, args.affected)
        chosen = suite_affected.select(changed, ROOT, files,
                                       always=suite_affected.always_run(ROOT))
        print(f"{len(changed)} changed file(s) against {args.affected}", flush=True)
        print(chosen.describe(len(files)), flush=True)
        files = chosen.files
        if not files:
            print("nothing to run", flush=True)
            return 0
    processes, note = choose_processes(args.processes, _free_gb())
    if note:
        print(note, flush=True)
    groups = split_balanced(files, processes, measurements.get("files", {}))
    # Not "leasha-...": tests name fixture repositories after the project and
    # match them by name *and* root-path substring, so a temp folder with the
    # project's name in it made `repo:leasha` match a second repository.
    work = Path(tempfile.mkdtemp(prefix="pt-run-"))
    started = time.time()
    how = ("balanced by measured cost" if measurements.get("files")
           else "contiguous (no measurements yet)")
    print(f"{len(files)} test files, {len(groups)} processes ({how}), logs in {work}", flush=True)
    if args.quick:
        print(f"quick: deselecting `{QUICK_DESELECT}` - a sanity pass, not a gate", flush=True)

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
                   "-p", "no:cacheprovider", "-p", "scripts.suite_durations",
                   f"--timeout={args.timeout}",
                   f"--basetemp={work / f'tmp{number}'}"]
        if args.quick:
            command += ["-m", f"{PROJECT_DESELECT} and {QUICK_DESELECT}"]
        env = dict(os.environ)
        env["LEASHA_SUITE_DURATIONS"] = str(work / f"durations{number}.json")
        extra = {} if sys.platform == "win32" else {"start_new_session": True}
        process = subprocess.Popen(command, cwd=ROOT, stdout=handle,
                                   stderr=subprocess.STDOUT, env=env, **extra)
        running.append((number, group, log, handle, process))

    outcomes = _wait_all(running, started + args.part_timeout)

    failures: set[str] = set()
    crashed = 0
    errored = 0
    measured_parts: list[dict] = []
    for number, group, log, handle, _process in running:
        code, timed_out = outcomes[number]
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
        part_durations = load_durations(work / f"durations{number}.json")
        if part_durations.get("files"):
            measured_parts.append(part_durations)
        if timed_out:
            crashed += 1
            print(f"  part {number}: TIMED OUT after {int(args.part_timeout)}s and was killed "
                  f"with its process tree. Last file it started: {last_file}. Its later tests "
                  f"did NOT run.", flush=True)
        elif not part_died(code, text):
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
            print(f"  part {number}: CRASHED, exit {code} (0x{code & 0xFFFFFFFF:X}) - the "
                  f"process died. Last file it started: {last_file}. Its later tests did "
                  f"NOT run.", flush=True)

    if measured_parts:
        merged = merge_durations(measurements, measured_parts)
        try:
            args.durations.parent.mkdir(parents=True, exist_ok=True)
            tmp = args.durations.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(merged, indent=1), encoding="utf-8")
            tmp.replace(args.durations)
        except OSError as exc:
            print(f"(could not write {args.durations}: {exc})", flush=True)
        print()
        print(describe_measurements(merged), flush=True)

    print()
    for name in sorted(failures):
        print(f"FAILED {name}")
    print(f"\n{len(failures)} failed, {errored} errored, {crashed} process(es) crashed, "
          f"{int(time.time() - started)}s.  Full logs: {work}")
    return 1 if failures or errored or crashed else 0


if __name__ == "__main__":
    sys.exit(main())
