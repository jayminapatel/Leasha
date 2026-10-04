"""`tools/guide_pictures.py` - the guide's pictures, retaken and put back. 2026-10-04.

Layer: L5

The grab itself needs the Windows platform and the demonstration store, so it
is not run here. What is: every caption the tool knows is one picture in the
guide (a caption that drifts would swap nothing, or the wrong one), and the
swap replaces exactly the named picture's bytes and nothing else, keeping the
file's own line endings (the repo normalises; a tool that changed them made a
whole-file diff).
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from tools import guide_pictures  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PNG = base64.b64decode(  # a 1x1 PNG
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def test_importing_the_tool_does_not_change_the_platform_the_tests_run_on():
    """It did: `QT_QPA_PLATFORM=windows` at import put every Qt test collected
    after it on the real desktop - the real clipboard, tray and fonts."""
    import os
    import subprocess
    import sys

    code = ("import os; os.environ['QT_QPA_PLATFORM']='offscreen'; "
            "from tools import guide_pictures; print(os.environ['QT_QPA_PLATFORM'])")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=120)
    assert done.stdout.strip().splitlines()[-1] == "offscreen", done.stderr[-400:]
    assert os.environ.get("QT_QPA_PLATFORM") != "windows"


def test_every_caption_is_exactly_one_picture_in_the_guide():
    html = (ROOT / "docs" / "USER_GUIDE.html").read_text(encoding="utf-8")
    for name, caption in guide_pictures.CAPTIONS.items():
        found = re.findall(r'<img[^>]*alt="' + re.escape(caption) + '"', html)
        assert len(found) == 1, f"{name}: {caption!r} appears {len(found)} times in the guide"


def test_every_caption_names_a_surface_the_grab_tool_can_reach():
    from tools import grab_ui

    for name in guide_pictures.CAPTIONS:
        assert (name in grab_ui.SURFACES or name in guide_pictures.MENUS
                or name in guide_pictures.WINDOWS), name


@pytest.mark.gui
def test_every_menu_picture_names_a_menu_on_the_bar_and_help_has_about(gui_mainwindow):
    """The Help picture was the one left "by hand" on 4 October, the day
    About Leasha was added to it."""
    _app, window, _store, _engine = gui_mainwindow
    for title in guide_pictures.MENUS.values():
        assert guide_pictures.menu_of(window, title) is not None
    texts = [a.text().replace("&", "") for a in guide_pictures.menu_of(window, "Help").actions()]
    assert texts == ["Keyboard shortcuts", "About Leasha"]


@pytest.mark.gui
def test_the_three_windows_the_tool_takes_can_be_taken_offscreen(gui_mainwindow):
    """The More menu and the Photo Tagger, each a picture with something in
    it - here offscreen, through the same code the tool runs. Not the mini
    box: its taker waits for a real search's results, and this fixture's
    engine is a stub (the tool itself runs the keyword engine over the demo
    store)."""
    app, window, store, _engine = gui_mainwindow
    for name in ("more-menu", "photo-tagger"):
        image = guide_pictures.grab_window(app, window, store, name)
        assert not image.isNull() and image.width() > 100, name


def test_the_swap_replaces_only_the_named_picture_and_keeps_the_line_ends(tmp_path):
    old = base64.b64encode(b"old").decode()
    page = ("<h1>Guide</h1>\r\n"
            f'<p><img src="data:image/png;base64,{old}" alt="The Files page"></p>\r\n'
            f'<p><img src="data:image/png;base64,{old}" alt="The Mail page"></p>\r\n')
    guide = tmp_path / "guide.html"
    guide.write_bytes(page.encode())
    picture = tmp_path / "files.png"
    picture.write_bytes(PNG)

    assert guide_pictures.swap({"files": picture}, guide) == 1

    raw = guide.read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b""), "the line endings were changed"
    html = raw.decode()
    new = base64.b64encode(PNG).decode()
    assert f'src="data:image/png;base64,{new}" alt="The Files page"' in html
    assert f'src="data:image/png;base64,{old}" alt="The Mail page"' in html, "the other was touched"
    assert html.startswith("<h1>Guide</h1>")


def test_a_caption_that_is_not_in_the_guide_is_refused_before_anything_is_written(tmp_path):
    guide = tmp_path / "guide.html"
    guide.write_text("<p>no pictures</p>", encoding="utf-8")
    picture = tmp_path / "files.png"
    picture.write_bytes(PNG)
    with pytest.raises(SystemExit, match="exactly one picture"):
        guide_pictures.swap({"files": picture}, guide)
    assert guide.read_text(encoding="utf-8") == "<p>no pictures</p>"
