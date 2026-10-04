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


def test_every_caption_is_exactly_one_picture_in_the_guide():
    html = (ROOT / "docs" / "USER_GUIDE.html").read_text(encoding="utf-8")
    for name, caption in guide_pictures.CAPTIONS.items():
        found = re.findall(r'<img[^>]*alt="' + re.escape(caption) + '"', html)
        assert len(found) == 1, f"{name}: {caption!r} appears {len(found)} times in the guide"


def test_every_caption_names_a_surface_the_grab_tool_can_reach():
    from tools import grab_ui

    for name in guide_pictures.CAPTIONS:
        assert name in grab_ui.SURFACES, name


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
