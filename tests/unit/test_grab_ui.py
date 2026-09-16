r"""`tools/grab_ui.py` - 0m §0c's smoke test and 0m §4a's goldens.

Folded into the UI Redesign (`202626160950` §9k / §9l) on 2026-09-16.

Two Qt-free checks run anywhere: the surface list matches the pages the
window declares, and every golden on disk has a surface. The grab itself and
the golden comparison need PyQt6 and are skipped without it - run them on
the Windows venv:

    venv\Scripts\python.exe -m pytest tests/unit/test_grab_ui.py -v

The goldens under `tests/golden/ui-redesign/` are the §9i captures - twelve
images, both themes, produced by the script and looked at by a person before
the order was ticked. They change only in the commit that changes the look.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden" / "ui-redesign"

#: Perceptual-hash distance a fresh grab may drift from its golden before the
#: look is called changed. Font hinting and a scrollbar arriving move a few
#: bits; a new layout moves dozens.
TOLERANCE = 12


def _surfaces_from_source() -> dict:
    tree = ast.parse((ROOT / "tools" / "grab_ui.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "SURFACES":
            return ast.literal_eval(node.value)
    raise AssertionError("SURFACES not found in tools/grab_ui.py")


def _rail_titles_from_shell() -> list[str]:
    source = (ROOT / "app" / "ui" / "shell.py").read_text(encoding="utf-8")
    titles = re.findall(r'\(self\.\w+_view, "([^"]+)", (?:True|False), "[a-z-]+", "[a-z]*"\)', source)
    titles += re.findall(r'insertTab\(after_files \+ \d, \w+, "([^"]+)"', source)
    return titles


def test_every_rail_page_has_a_grab_surface():
    """0m §0c: no surface-list drift. A new page without a grab entry fails."""
    pages = {spec["page"] for spec in _surfaces_from_source().values()}
    titles = set(_rail_titles_from_shell())
    assert titles, "could not read the rail titles from shell.py"
    missing = titles - pages
    assert not missing, f"pages with no grab entry: {sorted(missing)}"


def test_every_golden_on_disk_names_a_surface():
    if not GOLDEN.is_dir():
        pytest.skip("no goldens captured yet (§9i runs on the Windows venv)")
    surfaces = _surfaces_from_source()
    stray = [p.name for p in GOLDEN.rglob("*.png") if p.stem not in surfaces]
    assert not stray, stray


# ---------------------------------------------------------------------------
# Qt: the grab, and the comparison
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def grabbed(tmp_path_factory):
    pytest.importorskip("PyQt6")
    from tools import grab_ui

    out = tmp_path_factory.mktemp("grabs")
    written = grab_ui.grab(list(grab_ui.SURFACES), out)
    return out, written


def test_the_script_produces_a_non_empty_png_per_surface(grabbed):
    from tools import grab_ui
    out, written = grabbed
    assert len(written) == len(grab_ui.SURFACES)
    for path in written:
        assert path.is_file() and path.stat().st_size > 1000, path


def test_fresh_grabs_match_the_goldens_within_tolerance(grabbed):
    """0m §4a: the only automated catch for the theme-token bug class (the
    unreadable-QListView incident). Perceptual hash, not pixel equality."""
    if not GOLDEN.is_dir() or not any(GOLDEN.rglob("*.png")):
        pytest.skip("no goldens captured yet (§9i runs on the Windows venv)")
    imagehash = pytest.importorskip("imagehash")
    from PIL import Image
    out, written = grabbed
    drifted = []
    for path in written:
        golden = GOLDEN / path.name
        if not golden.is_file():
            continue
        distance = imagehash.phash(Image.open(golden)) - imagehash.phash(Image.open(path))
        if distance > TOLERANCE:
            drifted.append(f"{path.name}: distance {distance}")
    assert not drifted, ("the look drifted from the goldens - if that was the "
                         "point of the commit, regenerate them and say so in "
                         "the message:\n  " + "\n  ".join(drifted))
