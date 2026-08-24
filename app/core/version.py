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
def git_describe() -> str | None:
    """Short git description, or None when git or the repo is unavailable.

    Never raises: the packaged app may ship without a .git directory.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


def build_info() -> dict[str, str]:
    """Version details for the About dialog, logs, and doctor.py output."""
    info = {"version": version()}
    described = git_describe()
    if described:
        info["git"] = described
    return info


__version__ = version()
