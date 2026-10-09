r"""How many processes the suite runner starts, and why it is not simply four.

Layer: L0 - repo tooling, no app imports.

**The failure this arithmetic exists for.** On 2026-09-20 the last block of test files
died twice in a row, in the same Qt test, with `0xC0000005` (access violation) and then
`0xC0000374` (heap corruption) - and passed when the same files ran in one process, and
again when they ran in three. The machine had 8.7 GB free of 31.7 GB, with 11 GB in
memory compression and a language model resident. Four test processes, each holding Qt,
onnxruntime, pyarrow and LanceDB (measured at 1.2 GB and 2.0 GB resident), do not fit in
that; and a native allocator that cannot get memory on Windows does not raise
`MemoryError` - it corrupts or faults in whichever test allocates next, which is why the
crash looked like a bug in a rail-pill test that passes perfectly well on its own.

So `choose_processes` is deliberately a pure function of (what was asked, what is free),
and these are its cases. An explicit `-j` is always obeyed - the person asking may know
something the arithmetic does not - but it says plainly when the memory is not there.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location("run_suite", ROOT / "scripts" / "run_suite.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


run_suite = _module()


def test_a_roomy_machine_gets_the_full_four():
    chosen, note = run_suite.choose_processes(None, 32.0)
    assert chosen == run_suite.DEFAULT_MAX_PROCESSES
    assert note == "", "nothing to explain when the default is what happens"


def test_the_machine_that_crashed_twice_would_now_get_three():
    """8.7 GB free: the exact condition of the 2026-09-20 double crash."""
    chosen, note = run_suite.choose_processes(None, 8.7)
    assert chosen == 3
    assert "8.7 GB free" in note and "3 processes" in note


@pytest.mark.parametrize("free_gb, expected", [(32.0, 4), (10.1, 4), (8.7, 3), (6.0, 2),
                                               (4.0, 1), (0.5, 1), (0.0, 1)])
def test_the_count_never_drops_below_one_and_never_rises_above_the_cap(free_gb, expected):
    chosen, _ = run_suite.choose_processes(None, free_gb)
    assert chosen == expected
    assert 1 <= chosen <= run_suite.DEFAULT_MAX_PROCESSES


def test_an_explicit_request_is_obeyed_even_when_it_will_not_fit():
    """The person asking may know something: a warning, never a refusal."""
    chosen, note = run_suite.choose_processes(4, 8.7)
    assert chosen == 4, "an explicit -j must not be silently reduced"
    assert note.startswith("WARNING")
    assert "die natively" in note and "3 would be safe" in note


def test_an_explicit_request_that_fits_says_nothing():
    assert run_suite.choose_processes(2, 32.0) == (2, "")


def test_more_processes_than_the_cap_are_allowed_when_asked_for():
    """`-j 6` on a big machine is a legitimate thing to want."""
    assert run_suite.choose_processes(6, 64.0) == (6, "")


def test_memory_that_cannot_be_read_changes_nothing():
    """`psutil` missing or failing must never stop a run or second-guess it."""
    assert run_suite.choose_processes(None, None) == (run_suite.DEFAULT_MAX_PROCESSES, "")
    assert run_suite.choose_processes(6, None) == (6, "")


def test_the_real_machine_reports_a_number_or_honestly_nothing():
    free = run_suite._free_gb()                                          # noqa: SLF001
    assert free is None or free > 0


def test_the_docstring_names_the_crash_codes_so_the_next_reader_connects_them():
    """A number like 0xC0000374 is what somebody will paste into a search box."""
    text = (ROOT / "scripts" / "run_suite.py").read_text(encoding="utf-8")
    assert "0xC0000374" in text and "0xC0000005" in text


def test_a_crashed_part_can_name_the_file_it_died_in(tmp_path, monkeypatch, capsys):
    """2026-10-05: the project's `addopts = -q` took the file names out of every
    part's log, so a part that died of an access violation reported "Last file
    it started: (none started)" and the crash could not be traced. The runner
    asks for `-v`, and a crashed part names its file."""
    import subprocess
    import sys

    seen = []

    class Died:
        def __init__(self, command, cwd, stdout, stderr, **kwargs):
            seen.append(command)
            stdout.write("tests/unit/test_first.py::test_one PASSED [ 50%]\n"
                         "tests/unit/test_crash.py::test_boom ")
            stdout.flush()

        def wait(self, timeout=None):
            return 0xC0000005

        poll = wait

    monkeypatch.setattr(subprocess, "Popen", Died)
    monkeypatch.setattr(run_suite, "find_files", lambda explicit: ["tests/unit/test_x.py"])
    monkeypatch.setattr(run_suite.tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    assert run_suite.main(["-j", "1"]) == 1
    assert "-v" in seen[0] and seen[0][0] == sys.executable
    assert "Last file it started: tests/unit/test_crash.py" in capsys.readouterr().out


def test_a_process_that_ended_without_a_summary_did_not_finish():
    """2026-10-05: pytest-timeout ends the process with exit 1, which is also
    "some tests failed". With no summary line that was reported as "finished
    (no summary)" and the run as "0 failed, 0 process(es) crashed"."""
    ended = "=" * 20 + " 3 failed, 4000 passed in 812.40s (0:13:32) " + "=" * 20
    cut_short = "tests/unit/test_x.py ....\n+++++++++++ Timeout +++++++++++\n"
    assert run_suite.part_died(1, cut_short) is True
    assert run_suite.part_died(0, "") is True
    assert run_suite.part_died(1, "collected 9 items\n" + ended + "\n") is False
    assert run_suite.part_died(0, "==== 5 skipped in 0.10s ====") is False
    assert run_suite.part_died(5, "==== no tests ran in 0.01s ====") is False
    assert run_suite.part_died(-1073741819, ended) is True              # a native crash
    source = (ROOT / "scripts" / "run_suite.py").read_text(encoding="utf-8")
    assert "STOPPED EARLY" in source and "'no summary'" not in source
    assert run_suite.TIMEOUT_MARK in "+++++++++++ Timeout +++++++++++"


def _part_that_writes(log_text: str, exit_code: int):
    """A stand-in for `subprocess.Popen` whose part writes `log_text` and exits
    with `exit_code`; records every command it was started with."""
    seen = []

    class Part:
        def __init__(self, command, cwd, stdout, stderr, **kwargs):
            seen.append(command)
            stdout.write(log_text)
            stdout.flush()

        def wait(self, timeout=None):
            return exit_code

        poll = wait

    return Part, seen


def test_a_fixture_error_is_not_a_green_run(tmp_path, monkeypatch, capsys):
    """2026-10-08, found in review: `-rf` lists FAILED tests only, so a test
    whose fixture raised (an ERROR) was absent from the short summary, matched
    nothing, and the run exited 0 while its own tally said "1 error". This is
    the exact failure the runner exists to prevent: a green exit code that
    cannot be trusted."""
    import subprocess

    log = ("tests/unit/test_err.py::test_fine PASSED [ 50%]\n"
           "tests/unit/test_err.py::test_uses_broken ERROR [100%]\n"
           "=========================== short test summary info ===========================\n"
           "ERROR tests/unit/test_err.py::test_uses_broken - RuntimeError: boom\n"
           "========================= 1 passed, 1 error in 0.42s ==========================\n")
    Part, seen = _part_that_writes(log, 1)
    monkeypatch.setattr(subprocess, "Popen", Part)
    monkeypatch.setattr(run_suite, "find_files", lambda explicit: ["tests/unit/test_err.py"])
    monkeypatch.setattr(run_suite.tempfile, "mkdtemp", lambda prefix: str(tmp_path))

    assert run_suite.main(["-j", "1"]) == 1
    assert "-rfE" in seen[0], "errors must be listed in the short summary, not only failures"
    out = capsys.readouterr().out
    assert "FAILED tests/unit/test_err.py::test_uses_broken" in out
    assert "1 errored" in out


def test_the_summary_line_error_count_alone_is_enough(tmp_path, monkeypatch, capsys):
    """Belt and braces: even if pytest's short summary listed nothing (an older
    pytest, a plugin eating the section), a non-zero error count on the summary
    line is never reported as exit 0."""
    import subprocess

    log = ("tests/unit/test_err.py::test_fine PASSED [ 50%]\n"
           "tests/unit/test_err.py::test_uses_broken ERROR [100%]\n"
           "========================= 1 passed, 2 errors in 0.42s =========================\n")
    Part, _ = _part_that_writes(log, 1)
    monkeypatch.setattr(subprocess, "Popen", Part)
    monkeypatch.setattr(run_suite, "find_files", lambda explicit: ["tests/unit/test_err.py"])
    monkeypatch.setattr(run_suite.tempfile, "mkdtemp", lambda prefix: str(tmp_path))

    assert run_suite.main(["-j", "1"]) == 1
    assert "2 errored" in capsys.readouterr().out


def test_a_clean_part_is_still_green(tmp_path, monkeypatch, capsys):
    """The guard above must not turn an ordinary pass into a failure."""
    import subprocess

    log = ("tests/unit/test_ok.py::test_fine PASSED [100%]\n"
           "============================== 1 passed in 0.10s ==============================\n")
    Part, _ = _part_that_writes(log, 0)
    monkeypatch.setattr(subprocess, "Popen", Part)
    monkeypatch.setattr(run_suite, "find_files", lambda explicit: ["tests/unit/test_ok.py"])
    monkeypatch.setattr(run_suite.tempfile, "mkdtemp", lambda prefix: str(tmp_path))

    assert run_suite.main(["-j", "1"]) == 0
    assert "0 failed, 0 errored, 0 process(es) crashed" in capsys.readouterr().out
