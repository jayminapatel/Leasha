r"""A name that does not exist is a dead process, not an error message.

On 2026-08-27 the new exception hook caught this, in `logs/crash/crash.log`:

    File "app/ui/shell.py", line 637, in <lambda>
      self.indexing_view.finished.connect(
          lambda _stats: self.scheduler.notify_finished())
    File "app/ui/scheduler.py", line 114, in notify_finished
      run_worker(QThreadPool.globalInstance(), worker)
    NameError: name 'QThreadPool' is not defined

`QThreadPool` was used but never imported, so **`notify_finished` had never
once worked** - and it runs every time an index run ends. Under PyQt6 an
exception escaping a slot calls `qFatal()`, so this was not a logged warning:
it was the window vanishing. A second one, `QPrinter` in
`preview_window._print_picture`, would have done the same to anybody printing
a picture from a pop-out preview.

**Both were already detectable.** `pyproject.toml` selects rule family `F`,
which includes F821, and ruff finds both in under a second. Nobody was
running it over `app/`. A check that exists and is not run is not a check, so
it runs here, in the suite, on every commit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _ruff(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", *arguments],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )


@pytest.fixture(scope="module")
def ruff_available() -> None:
    probe = _ruff("--version")
    if probe.returncode not in (0, 1, 2) or "No module named" in probe.stderr:
        pytest.skip("ruff is not installed in this environment")


def test_no_undefined_names_anywhere_in_app(ruff_available) -> None:
    r"""F821, over the whole of `app/`.

    **The narrowest rule that would have caught both**, deliberately: a broad
    "ruff must be clean" gate would fail on formatting nobody has got to yet
    and be switched off within a week. This one only fires on a name that
    cannot resolve, which is never a style opinion and always a crash.
    """
    del ruff_available
    result = _ruff("--select", "F821", "--quiet", "app/")
    assert result.returncode == 0, (
        "a name is used that does not exist. Under PyQt6 this is not an error "
        "message, it is the process dying:\n\n" + (result.stdout or result.stderr))


def test_the_check_can_actually_fail(ruff_available, tmp_path) -> None:
    """Otherwise it passes on a misconfiguration forever."""
    del ruff_available
    bait = tmp_path / "bait.py"
    bait.write_text("def go():\n    return NoSuchName\n", encoding="utf-8")
    result = _ruff("--select", "F821", "--quiet", str(bait))
    assert result.returncode != 0, "F821 is not actually being applied"
    assert "F821" in (result.stdout + result.stderr)


def test_undefined_names_in_the_test_suite_too(ruff_available) -> None:
    """Tests get the same rule. A test that dies on a typo tests nothing."""
    del ruff_available
    result = _ruff("--select", "F821", "--quiet", "tests/")
    assert result.returncode == 0, (result.stdout or result.stderr)
