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
