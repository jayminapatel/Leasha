"""Single source of truth for the application version.

The version lives in the top-level ``VERSION`` file so that PowerShell scripts,
CI, and the packaged app all read the same value. Nothing else should hardcode it.

Layer: L0
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

__all__ = ["__version__", "version", "build_info", "PROJECT_ROOT", "VERSION_FILE"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = PROJECT_ROOT / "VERSION"

_FALLBACK = "0.0.0"


@lru_cache(maxsize=1)
def version() -> str:
    """Return the semantic version string, e.g. ``0.1.0``."""
    try:
        text = VERSION_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return _FALLBACK
    return text.splitlines()[0].strip() if text else _FALLBACK


@lru_cache(maxsize=1)
def git_describe() -> tuple[str | None, str | None]:
    """Return (description, reason_it_failed).

    Never raises: a packaged build ships without a .git directory, and that is
    not an error. But a silent `return None` hid a real problem once - git
    refusing the repository with "dubious ownership" - so the reason is
    reported rather than swallowed.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return None, "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return None, "git did not respond within 5s"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"

    value = out.stdout.strip()
    if value:
        return value, None

    reason = out.stderr.strip() or f"git exited with code {out.returncode} and no output"
    if "dubious ownership" in reason:
        reason += (
            " | fix: git config --global --add safe.directory "
            + str(PROJECT_ROOT).replace("\\", "/")
        )
    return None, reason


def build_info() -> dict[str, str]:
    """Version details for the About dialog, logs, and doctor.py output."""
    info = {"version": version()}
    described, reason = git_describe()
    if described:
        info["git"] = described
    elif reason:
        info["git_error"] = reason
    return info


__version__ = version()
