r"""Order 0m S5 - `check_nightly_status` (doctor.py) and the pure parts of
`tools/nightly.py`.

Layer: n/a (both are root-level scripts, `pyproject.toml` puts the repo root
on `pythonpath` - the same reason `test_doctor.py` imports `doctor` directly).

The nightly script's own `run()` launches real subprocesses over a real
index and genuinely takes tens of seconds - it was verified live, once,
against the real CLI while this was built, not re-run here on every suite
pass. What is tested here is the logic that does not need a subprocess:
the floor comparison, and the log line `doctor.py` reads back.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import doctor                                     # noqa: E402
from tools import nightly                          # noqa: E402


# ---------------------------------------------------------------------------
# doctor.py's check_nightly_status
# ---------------------------------------------------------------------------

def test_no_log_file_is_optional_not_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    result = doctor.check_nightly_status()
    assert result.optional
    assert not result.ok
    assert "never run" in result.detail


def test_a_passing_last_line_reports_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    log = tmp_path / "logs" / "nightly.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "2026-09-15T02:00:00+00:00 FAIL index=1.0s recall=0.1 breaches=1\n"
        "2026-09-16T02:00:00+00:00 PASS index=40.0s recall=0.7 breaches=0\n",
        encoding="utf-8")

    result = doctor.check_nightly_status()
    assert result.ok
    assert "last passed" in result.name
    assert "2026-09-16" in result.detail
    # The most recent line, not the first - an old failure must not shadow
    # a later pass.
    assert "FAIL" not in result.detail.split(" ", 1)[0]


def test_a_failing_last_line_reports_not_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    log = tmp_path / "logs" / "nightly.log"
    log.parent.mkdir(parents=True)
    log.write_text("2026-09-16T02:00:00+00:00 FAIL index=1.0s recall=0.1 breaches=1\n",
                   encoding="utf-8")

    result = doctor.check_nightly_status()
    assert not result.ok
    assert "last failed" in result.name
    assert result.optional


# ---------------------------------------------------------------------------
# tools/nightly.py's pure logic
# ---------------------------------------------------------------------------

def test_an_unmeasured_floor_never_breaches(monkeypatch):
    monkeypatch.setitem(nightly.PERF_FLOORS, "recall_at_10", None)
    assert nightly._check_floor("recall_at_10", 0.0, higher_is_better=True) is None


def test_a_value_below_a_higher_is_better_floor_breaches(monkeypatch):
    monkeypatch.setitem(nightly.PERF_FLOORS, "recall_at_10", 0.6)
    message = nightly._check_floor("recall_at_10", 0.4, higher_is_better=True)
    assert message is not None
    assert "0.4" in message and "0.6" in message


def test_a_value_above_a_higher_is_better_floor_does_not_breach(monkeypatch):
    monkeypatch.setitem(nightly.PERF_FLOORS, "recall_at_10", 0.6)
    assert nightly._check_floor("recall_at_10", 0.8, higher_is_better=True) is None


def test_a_value_above_a_lower_is_better_floor_breaches(monkeypatch):
    monkeypatch.setitem(nightly.PERF_FLOORS, "search_p95_ms", 500.0)
    message = nightly._check_floor("search_p95_ms", 900.0, higher_is_better=False)
    assert message is not None


def test_the_log_line_names_pass_or_fail_and_the_headline_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LOG_PATH", tmp_path / "nightly.log")
    report = {
        "ok": True, "finished_at": "2026-09-16T02:00:00+00:00",
        "evaluate": {"recall": 0.7}, "index": {"seconds": 12.345}, "breaches": [],
    }
    nightly._append_log_line(report)
    line = (tmp_path / "nightly.log").read_text(encoding="utf-8").strip()
    assert line.startswith("2026-09-16T02:00:00+00:00 PASS")
    assert "recall=0.7" in line
    assert "index=12.3s" in line


def test_a_failing_report_still_gets_a_log_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(nightly, "LOG_PATH", tmp_path / "nightly.log")
    report = {
        "ok": False, "finished_at": "2026-09-16T02:00:00+00:00",
        "evaluate": {"recall": None}, "index": {"seconds": None}, "breaches": ["recall too low"],
    }
    nightly._append_log_line(report)
    line = (tmp_path / "nightly.log").read_text(encoding="utf-8").strip()
    assert line.startswith("2026-09-16T02:00:00+00:00 FAIL")
    # Loud only on failure - the module docstring's own promise.
    assert "FAIL" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Order 0m 5a: the measured floors and the probe that feeds them
# ---------------------------------------------------------------------------

#: The worst of the six real runs recorded around pinning the floors
#: (2026-09-19, owner's machine, busy). Not a target - see `PERF_FLOORS`.
_WORST_OBSERVED = {
    "index_files_per_second": 0.62,
    "chunker_chunks_per_second": 757.0,
    "embed_chunks_per_second": 6.9,
    "search_p95_ms": 396.0,
    "recall_at_10": 0.7,
}
_LOWER_IS_BETTER = {"search_p95_ms"}


def test_every_measured_metric_has_a_pinned_floor_or_says_none():
    assert set(nightly.PERF_FLOORS) == set(_WORST_OBSERVED)
    for name, floor in nightly.PERF_FLOORS.items():
        assert floor is None or floor > 0, name


def test_the_worst_run_ever_observed_does_not_trip_its_own_floor():
    """A floor that the measurements it was set from would breach is a
    tripwire wired to the wrong wall."""
    for name, value in _WORST_OBSERVED.items():
        assert nightly._check_floor(
            name, value, higher_is_better=name not in _LOWER_IS_BETTER) is None, name


def test_a_several_fold_regression_does_trip_every_pinned_floor():
    for name, value in _WORST_OBSERVED.items():
        if nightly.PERF_FLOORS[name] is None:
            continue
        worse = value * 4 if name in _LOWER_IS_BETTER else value / 4
        assert nightly._check_floor(
            name, worse, higher_is_better=name not in _LOWER_IS_BETTER) is not None, name


def test_the_log_line_carries_every_measurement_after_the_fields_doctor_reads(
        tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LOG_PATH", tmp_path / "nightly.log")
    nightly._append_log_line({
        "ok": True, "finished_at": "2026-09-19T02:00:00+00:00",
        "evaluate": {"recall": 0.7},
        "index": {"seconds": 40.0, "files_per_second": 0.875},
        "probe": {"chunker_chunks_per_second": 1000.1, "embed_chunks_per_second": 7.5,
                  "search_p95_ms": 250.0, "search_p50_ms": 190.0},
        "breaches": [],
    })
    line = (tmp_path / "nightly.log").read_text(encoding="utf-8").strip()
    # doctor.py reads the head of the line; the new fields ride after it.
    assert line.startswith("2026-09-19T02:00:00+00:00 PASS index=40.0s recall=0.7 breaches=0")
    for field in ("files_per_s=0.88", "chunker_per_s=1000.1", "embed_per_s=7.5",
                  "search_p95_ms=250.0", "search_p50_ms=190.0"):
        assert field in line


def test_leasha_python_overrides_the_venv_path(monkeypatch, tmp_path):
    """A git worktree has no `venv/` of its own."""
    monkeypatch.setenv("LEASHA_PYTHON", str(tmp_path / "python.exe"))
    assert nightly._find_python() == tmp_path / "python.exe"
    monkeypatch.delenv("LEASHA_PYTHON")
    assert nightly._find_python().parts[-3:] == ("venv", "Scripts", "python.exe")


def test_the_probe_measures_the_chunker_without_a_model():
    from tools import nightly_probe

    out = nightly_probe.measure_chunker()
    assert out["chunker_chunks_per_second"] > 0
    assert out["chunker_chars_per_second"] > 0
    assert len(out["_chunks"]) > nightly_probe.DOCUMENTS   # several chunks per document

