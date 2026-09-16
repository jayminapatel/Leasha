r"""Work order 202626130120 (0t) section 7, item 1: the two onnxruntime pins.

onnxruntime and onnxruntime-directml unpack into the same venv\Lib\site-packages\onnxruntime directory and silently overwrite each
other. The fix is that both are pinned, in the same place, to the same
version - so this test reads requirements.txt itself rather than trusting
memory, and fails the day somebody bumps one without the other, which is
the day this recurs.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "requirements.txt"

#: fastembed 0.8.0's own exclusion set - see requirements.txt's comment and
#: the work order's section 0 evidence. A version in here must never be the
#: pin, or a plain pip install -r requirements.txt breaks on day one.
FASTEMBED_EXCLUDES = {"1.20.0", "1.24.0", "1.24.1"}


def _text() -> str:
    return REQUIREMENTS.read_text(encoding="utf-8")


def test_onnxruntime_itself_is_pinned() -> None:
    """Unpinned is how 1.30.0 arrived on a venv whose DirectML wheel was
    1.24.4 - see the work order's section 0. A bare fastembed== requirement
    is not enough; the transitive package must be pinned directly."""
    text = _text()
    match = re.search(r"(?m)^onnxruntime==([0-9][0-9A-Za-z.]*)\s*$", text)
    assert match, "onnxruntime==<version> not found as its own pinned line"


def test_the_directml_pin_is_documented_and_matches() -> None:
    """Not installed unconditionally (see the comment - no Windows wheel
    exists for this project's Linux test sandboxes), but documented in the
    same file, at the same version, so the two cannot drift apart silently."""
    text = _text()
    onnx = re.search(r"(?m)^onnxruntime==([0-9][0-9A-Za-z.]*)\s*$", text)
    directml = re.search(
        r"(?m)^#\s*onnxruntime-directml==([0-9][0-9A-Za-z.]*)\s*$", text)
    assert onnx, "onnxruntime pin missing"
    assert directml, "onnxruntime-directml pin (documented) missing"
    assert onnx.group(1) == directml.group(1), (
        f"onnxruntime=={onnx.group(1)} but onnxruntime-directml=="
        f"{directml.group(1)} - the work order requires these two to always match"
    )


def test_the_pin_satisfies_fastembeds_exclusions() -> None:
    text = _text()
    onnx = re.search(r"(?m)^onnxruntime==([0-9][0-9A-Za-z.]*)\s*$", text)
    assert onnx
    assert onnx.group(1) not in FASTEMBED_EXCLUDES


def test_the_python_constant_matches_the_requirements_pin() -> None:
    """app.index.backends.DIRECTML_PIN is what install.ps1 and doctor.py
    quote in their fix messages - it must be the same number as the file a
    fresh pip install -r requirements.txt actually reads."""
    from app.index.backends import DIRECTML_PIN

    text = _text()
    directml = re.search(
        r"(?m)^#\s*onnxruntime-directml==([0-9][0-9A-Za-z.]*)\s*$", text)
    assert directml
    assert DIRECTML_PIN == directml.group(1)


def test_installps1_installs_the_same_directml_version() -> None:
    """The version install.ps1 force-reinstalls must be the one pinned in
    requirements.txt - a mismatch here is exactly how a machine ends up with
    two different DirectML builds fighting over one directory."""
    from app.index.backends import DIRECTML_PIN

    installer = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert f"onnxruntime-directml=={DIRECTML_PIN}" in installer
