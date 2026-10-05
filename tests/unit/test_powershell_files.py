"""Every PowerShell file is ASCII and saved UTF-8 with a BOM (non-negotiable 7).

2026-10-05. The rule was enforced script by script (`test_launcher.py` for
`add-to-path.ps1`, `test_install_nightly_script.py`, ...), so a new script was
covered only if somebody remembered to write its test. `packaging/build.ps1`
arrived that day without one. This walks every tracked `.ps1`, so the next one
is covered by being added. `archive/` is old code nobody runs, left as it was.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _scripts() -> list[str]:
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.ps1"],
                            capture_output=True, text=True, check=True).stdout.split()
    return [name for name in listed if not name.startswith("archive/")]


def test_every_powershell_file_is_ascii_with_a_bom() -> None:
    scripts = _scripts()
    assert "packaging/build.ps1" in scripts and "install.ps1" in scripts, scripts
    wrong = []
    for name in scripts:
        raw = (ROOT / name).read_bytes()
        if raw[:3] != b"\xef\xbb\xbf":
            wrong.append(f"{name}: no BOM")
        elif any(byte > 127 for byte in raw[3:]):
            wrong.append(f"{name}: not ASCII")
    assert not wrong, wrong
