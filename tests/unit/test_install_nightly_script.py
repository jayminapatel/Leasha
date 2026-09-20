r"""Order 0m section 5b - `scripts/install-nightly.ps1`, tested without ever
registering anything.

Layer: n/a (a PowerShell script)

**Registration is the owner's opt-in and is never done by a test.** Every call
here is `-WhatIf`, `-DryRun`, `-Status`, or an argument error, all of which
return before `Register-ScheduledTask`; each one uses a throwaway task name, and
the last test proves that name is not in Task Scheduler afterwards.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.windows

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install-nightly.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
TASK = f"Leasha nightly test {uuid.uuid4().hex[:8]}"


def _run(*args: str) -> subprocess.CompletedProcess:
    if not POWERSHELL or sys.platform != "win32":
        pytest.skip("PowerShell is not available")
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         "-TaskName", TASK, *args],
        capture_output=True, text=True, timeout=120, check=False)


def test_the_script_is_ascii_with_a_bom():
    """Non-negotiable #7: PowerShell 5.1 reads a BOM-less file as the ANSI codepage."""
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert all(byte < 128 for byte in raw[3:])


def test_it_declares_whatif_confirm_and_an_uninstall_switch():
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "SupportsShouldProcess" in text
    assert "[switch]$Uninstall" in text
    assert "[switch]$Status" in text
    assert "[switch]$DryRun" in text


@pytest.mark.parametrize("flag", ["-WhatIf", "-DryRun"])
def test_a_dry_run_says_what_it_would_do_and_changes_nothing(flag):
    result = _run(flag)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert f"Task name  : {TASK}" in out
    assert "every day at 02:00" in out
    assert "nightly.py" in out and "pythonw.exe" in out
    assert "Nothing was changed" in out
    assert "Registered '" not in out


def test_a_dry_run_takes_the_time_argument():
    result = _run("-DryRun", "-Time", "03:30")
    assert result.returncode == 0
    assert "every day at 03:30" in result.stdout


def test_a_bad_time_is_refused_not_registered():
    result = _run("-DryRun", "-Time", "25:99")
    assert result.returncode != 0
    assert "Nothing was changed" not in result.stdout


def test_uninstall_and_status_together_are_refused_with_exit_2():
    result = _run("-Uninstall", "-Status")
    assert result.returncode == 2
    assert "not both" in result.stdout


def test_uninstall_of_a_task_that_is_not_there_is_a_quiet_success():
    result = _run("-Uninstall", "-WhatIf")
    assert result.returncode == 0
    assert "Nothing to remove" in result.stdout


def test_status_says_when_it_is_not_registered():
    result = _run("-Status")
    assert result.returncode == 0
    assert "Not registered" in result.stdout


def test_a_real_install_with_no_venv_refuses_before_touching_the_scheduler(tmp_path):
    """No `venv\\Scripts\\pythonw.exe` under the project path: refuse, exit 1.
    The refusal comes before the confirmation question, so nothing waits on input."""
    result = _run("-ProjectPath", str(tmp_path))
    assert result.returncode == 1
    assert "Cannot register" in result.stdout


def test_nothing_above_ever_registered_the_throwaway_task():
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command",
         f"if (Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue) "
         "{ 'REGISTERED' } else { 'ABSENT' }"],
        capture_output=True, text=True, timeout=60, check=False) if POWERSHELL else None
    if result is None:
        pytest.skip("PowerShell is not available")
    assert result.stdout.strip() == "ABSENT"
