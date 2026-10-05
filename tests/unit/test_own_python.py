"""A packaged build starts its own children with the console program beside it.

2026-10-05, the first Windows installer. In a PyInstaller build `sys.executable`
is `Leasha.exe`, which opens the window whatever it is given; every child the
app starts as `<python> -m app.cli ...` would have opened a second window.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.core.osbridge import stdio


def test_from_source_it_is_the_python_running_now(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert stdio.own_python() == sys.executable


def test_in_a_packaged_build_it_is_the_console_program_beside_the_window(monkeypatch, tmp_path):
    window = tmp_path / "Leasha" / "Leasha.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(window))
    assert stdio.own_python() == str(window.with_name(stdio.CLI_PROGRAM))
    assert stdio.console_python(str(window)) == str(window.with_name(stdio.CLI_PROGRAM))


def test_every_child_of_leasha_s_own_code_asks_for_it():
    """`sys.executable` named directly as a child's program is the bug this
    replaces; the only ones left are reports, `pip` advice and the
    desktop-shortcut fall-backs, which are not children of Leasha's own code."""
    root = Path(__file__).resolve().parents[2]
    allowed = {"app/core/diagnostics.py": 1, "app/core/runlog.py": 1,
               "app/ui/tasks.py": 1, "app/cli/desktop.py": 1, "app/core/deeplink.py": 1,
               "app/core/osbridge/stdio.py": 1}
    import re

    for path in (root / "app").rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        code = [line for line in path.read_text(encoding="utf-8").splitlines()
                if "sys.executable" in line and not line.lstrip().startswith("#")]
        launches = [line for line in code
                    if (re.search(r"\[\s*(python or )?sys\.executable\s*,", line)
                        or "python or sys.executable" in line)
                    and '"-m", "pip"' not in line]      # source-only; see the next test
        assert not launches, f"{rel} starts a child with sys.executable: {launches}"


def test_installing_a_package_in_a_packaged_build_says_so_and_starts_nothing(monkeypatch):
    """`pip` under `sys.executable` is right from source (it installs into the
    venv); a packaged build has no pip, and that line would open a window."""
    import subprocess

    from app.ui import tasks

    def nothing(*_a, **_k):
        raise AssertionError("nothing may be started")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(subprocess, "run", nothing)
    result = tasks.install_package("python-pptx")
    assert result["ok"] is False and "cannot add packages" in result["detail"]
