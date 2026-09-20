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
