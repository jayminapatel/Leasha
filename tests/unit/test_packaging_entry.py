"""The packaged build's one entry point answers the way `python.exe` does.

Order 202626082213, 2026-10-05. `Leasha.exe` and `leasha-cli.exe` are both
`packaging/leasha_entry.py`; the window starts its own children through
`leasha-cli.exe` with `-m`, `-c` or a script path, and a terminal user types
plain `app.cli` commands at it. Tested here without building anything: the
dispatch is ordinary Python.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1].parent


@pytest.fixture()
def entry():
    spec = importlib.util.spec_from_file_location(
        "leasha_entry", ROOT / "packaging" / "leasha_entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _as(monkeypatch, program: str, *args: str) -> None:
    monkeypatch.setattr(sys, "executable", str(Path("C:/Leasha") / program))
    monkeypatch.setattr(sys, "argv", [program, *args])


def test_dash_m_runs_the_module_with_its_own_arguments(entry, monkeypatch):
    seen = []
    monkeypatch.setattr(entry.runpy, "run_module",
                        lambda name, **kw: seen.append((name, list(sys.argv), kw["run_name"])))
    _as(monkeypatch, "leasha-cli.exe", "-m", "app.cli", "index", "--events", "jsonl")
    assert entry.main() == 0
    assert seen == [("app.cli", ["app.cli", "index", "--events", "jsonl"], "__main__")]


def test_dash_c_runs_the_code_as_main(entry, monkeypatch, tmp_path):
    marker = tmp_path / "ran.txt"
    _as(monkeypatch, "leasha-cli.exe", "-c",
        f"import sys, pathlib; pathlib.Path({str(marker)!r}).write_text(__name__ + ' ' + sys.argv[1])",
        "model-x")
    assert entry.main() == 0
    assert marker.read_text() == "__main__ model-x"


def test_a_script_path_runs_that_script(entry, monkeypatch, tmp_path):
    script = tmp_path / "doctor.py"
    marker = tmp_path / "ran.txt"
    script.write_text(f"import sys, pathlib\npathlib.Path({str(marker)!r}).write_text(' '.join(sys.argv[1:]))\n")
    _as(monkeypatch, "leasha-cli.exe", str(script), "--json", "--quick")
    assert entry.main() == 0
    assert marker.read_text() == "--json --quick"


def test_a_plain_command_at_the_console_program_is_an_app_cli_command(entry, monkeypatch):
    seen = []
    monkeypatch.setattr(entry.runpy, "run_module", lambda name, **kw: seen.append((name, list(sys.argv))))
    _as(monkeypatch, "leasha-cli.exe", "search", "boiler")
    entry.main()
    assert seen == [("app.cli", ["app.cli", "search", "boiler"])]


def test_the_window_program_with_nothing_given_opens_the_window(entry, monkeypatch):
    import app.main

    opened = []
    monkeypatch.setattr(app.main, "main", lambda: opened.append(True) or 0)
    _as(monkeypatch, "Leasha.exe")
    assert entry.main() == 0
    assert opened == [True]
