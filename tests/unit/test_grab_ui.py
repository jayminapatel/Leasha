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
    # Its own store folder, not the fixed one the golden subprocesses below
    # share: this grab runs in the test process, which lives on after it.
    written = grab_ui.grab(list(grab_ui.SURFACES), out,
                           workdir=tmp_path_factory.mktemp("grab-store"))
    return out, written


def test_the_script_produces_a_non_empty_png_per_surface(grabbed):
    from tools import grab_ui
    out, written = grabbed
    assert len(written) == len(grab_ui.SURFACES)
    for path in written:
        assert path.is_file() and path.stat().st_size > 1000, path


def _golden_sets() -> list[tuple[str, str, Path]]:
    """`(theme, size, folder)` for every `<theme>-<WxH>/<theme>/` on disk."""
    found = []
    for folder in sorted(GOLDEN.glob("*-*x*")):
        theme, _, size = folder.name.partition("-")
        if (folder / theme).is_dir():
            found.append((theme, size, folder / theme))
    return found


def test_the_goldens_are_the_twelve_9i_describes():
    """§9i: the empty state and results-with-inspector, light and dark, at
    1024x600, the default window (1100x760) and maximised (1920x1080)."""
    if not GOLDEN.is_dir():
        pytest.skip("no goldens captured yet (§9i runs on the Windows venv)")
    have = {(theme, size, p.stem) for theme, size, folder in _golden_sets()
            for p in folder.glob("*.png")}
    want = {(theme, size, state) for theme in ("light", "dark")
            for size in ("1024x600", "1100x760", "1920x1080")
            for state in ("search-home", "search-results")}
    assert have == want, f"missing {sorted(want - have)}, stray {sorted(have - want)}"


def test_fresh_grabs_match_the_goldens_within_tolerance(tmp_path):
    """0m §4a: the only automated catch for the theme-token bug class (the
    unreadable-QListView incident). Perceptual hash, not pixel equality.

    **Each golden is compared with a grab at its own theme and size.** This
    used to compare `grabs/<name>.png` with `GOLDEN/<name>.png`, a path that
    does not exist (the goldens live under `<theme>-<size>/<theme>/`), so every
    file was skipped and the test could not fail."""
    pytest.importorskip("PyQt6")
    if not GOLDEN.is_dir() or not any(GOLDEN.rglob("*.png")):
        pytest.skip("no goldens captured yet (§9i runs on the Windows venv)")
    imagehash = pytest.importorskip("imagehash")
    from PIL import Image

    import subprocess
    import sys

    drifted, compared = [], 0
    for theme, size, folder in _golden_sets():
        names = sorted(p.stem for p in folder.glob("*.png"))
        out = tmp_path / f"{theme}-{size}"
        # **A fresh process per set, the way the goldens were made.** The
        # offscreen platform reads its font directory once, when the first
        # `QApplication` is built (`tools/grab_ui.py` sets it on import); in a
        # test run that has already built one, an in-process grab renders every
        # glyph as a box and drifts from every golden by 20-plus bits.
        run = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "grab_ui.py"), "--theme", theme,
             "--size", size, "--out", str(out), *[f"--surface={n}" for n in names]],
            cwd=ROOT, capture_output=True, text=True, timeout=240)
        assert run.returncode == 0, run.stderr[-2000:]
        for path in sorted((out / theme).glob("*.png")):
            distance = imagehash.phash(Image.open(folder / path.name)) - imagehash.phash(Image.open(path))
            compared += 1
            if distance > TOLERANCE:
                drifted.append(f"{theme}-{size}/{path.name}: distance {distance}")
    assert compared, "no golden was compared with anything"
    assert not drifted, ("the look drifted from the goldens - if that was the "
                         "point of the commit, regenerate them and say so in "
                         "the message:\n  " + "\n  ".join(drifted))
