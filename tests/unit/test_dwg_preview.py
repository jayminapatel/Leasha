r"""Workspace §5c: a DWG drawing as a simplified view.

Layer: L5

`dwg2SVG` (LibreDWG) turns the drawing into SVG, the SVG is cached under the
app's own cache directory keyed by a hash of the drawing's bytes, and the
pop-out draws it as an image. Line-work and text, not a plot - enough to
answer "is this the right drawing?", which is what the item asks for.

**Both halves are exercised here**: the machine running this has no LibreDWG
on it, so the *absent* path is real, and the *present* path is driven by
pointing the allow-listed name at a program that does exist and prints its
input to standard output. That is not a stub of the conversion - it is a real
subprocess, really captured, really cached.

**The licence rule is a test, not a comment.** Running a GPL program is mere
aggregation; importing LibreDWG's Python bindings would put this MIT
application under the GPL. `test_cad.test_no_module_imports_libredwgs_python_
bindings` walks every module under `app/` for that; the two here pin the other
half of it - that this feature reaches LibreDWG through `converter.convert`,
which is the one audited place that starts a process.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import app.ui.preview_loader as module
from app.extract import converter
from app.ui.preview_loader import (
    DWG_BINARY,
    DWG_CONVERTER_MISSING_NOTE,
    DWG_PREVIEW_NOTE,
    KIND_IMAGE,
    KIND_TEXT,
    dwg_svg_cache_path,
    ensure_dwg_svg,
)

#: A drawing's first six bytes are its AutoCAD release. `AC1032` is 2018 -
#: `cad.DWG_RELEASES` is where that mapping lives, and reusing it here is the
#: point of §5c's fallback: the preview says what the index says.
DWG_2018 = b"AC1032" + b"\x00" * 64


@pytest.fixture
def drawing(tmp_path: Path) -> Path:
    path = tmp_path / "site plan.dwg"
    path.write_bytes(DWG_2018)
    return path


# ---------------------------------------------------------------------------
# The allow-list, and the licence rule as code
# ---------------------------------------------------------------------------

def test_dwg2svg_is_allow_listed_by_its_own_name():
    """The allow-list names programs. Having `dwg2dxf` on it must not silently
    permit everything else that ships in the same folder."""
    assert DWG_BINARY == "dwg2SVG"
    assert DWG_BINARY in converter.ALLOWED_BINARIES


def test_dwg2svg_is_findable_where_libredwg_actually_installs():
    """LibreDWG does not put itself on PATH on Windows any more than
    LibreOffice does - the same reason `_WINDOWS_LOCATIONS` exists at all."""
    folders, executable = converter._WINDOWS_LOCATIONS[DWG_BINARY]
    assert executable == "dwg2SVG.exe"
    assert "LibreDWG" in folders or "libredwg" in folders


def test_the_preview_reaches_libredwg_only_by_running_it():
    """**Subprocess only, never the Python bindings.** Asserted on what the
    code would do (`import`), never on the name its own justification
    contains - the recurring mistake this repo has now made three times.
    """
    source = Path(module.__file__).read_text(encoding="utf-8")
    for binding in ("import libredwg", "from libredwg", "import LibreDWG",
                    "ctypes.CDLL", "cdll."):
        assert binding not in source, f"preview_loader.py has {binding}"
    # And it does reach the one audited place that starts a process.
    assert "from app.extract.converter import convert" in source


def test_a_blocked_name_is_still_refused():
    """The allow-list gained a name; it did not stop being an allow-list."""
    assert converter.resolve_binary("dwg2svg") is None       # not the real name
    assert converter.resolve_binary("curl") is None


# ---------------------------------------------------------------------------
# `convert(stdout_to=...)` - the reason a second parameter exists at all
# ---------------------------------------------------------------------------

def test_stdout_is_kept_when_the_converter_writes_to_standard_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    r"""`dwg2SVG DRAWING.dwg >DRAWING.svg` - there is no `-o` option, so
    without this the conversion produces nothing and every drawing reports
    ERR_CONVERTER_FAILED.

    A real subprocess: the allow-listed name is pointed at `cat`, which prints
    its input to standard output exactly as `dwg2SVG` prints its SVG.
    """
    cat = shutil.which("cat")
    if not cat:
        pytest.skip("no `cat` on this machine to stand in for a stdout converter")

    from app.core.formats import ConverterRule

    source = tmp_path / "plan.dwg"
    source.write_bytes(b"<svg><line/></svg>")
    monkeypatch.setattr(converter.shutil, "which", lambda _name: cat)

    rule = ConverterRule(extension=".dwg", command=(DWG_BINARY, "{input}"),
                         produces="{stem}.svg", then="", enabled=True)
    with converter.convert(source, rule, stdout_to="{stem}.svg") as result:
        assert result.path.name == "plan.svg"
        assert result.path.read_bytes() == b"<svg><line/></svg>"


def test_a_stdout_filename_cannot_escape_the_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The output name is a template this module chooses, but it is stripped
    to a bare filename anyway - a converter's temporary directory is the only
    place `convert` is allowed to write."""
    cat = shutil.which("cat")
    if not cat:
        pytest.skip("no `cat` on this machine to stand in for a stdout converter")

    from app.core.formats import ConverterRule

    source = tmp_path / "plan.dwg"
    source.write_bytes(b"x")
    escaped = tmp_path / "escaped.svg"
    monkeypatch.setattr(converter.shutil, "which", lambda _name: cat)

    rule = ConverterRule(extension=".dwg", command=(DWG_BINARY, "{input}"),
                         produces="escaped.svg", then="", enabled=True)
    with converter.convert(source, rule, stdout_to=str(escaped)) as result:
        assert result.path.parent != tmp_path

    assert not escaped.exists()


def test_nothing_printed_is_a_reported_failure_not_an_empty_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A drawing `dwg2SVG` cannot read prints nothing. That must be an error
    with a fix line, not a zero-byte SVG cached forever."""
    from app.core.errors import AppErrorException
    from app.core.formats import ConverterRule

    true_program = shutil.which("true")
    if not true_program:
        pytest.skip("no `true` on this machine to stand in for a silent converter")

    source = tmp_path / "plan.dwg"
    source.write_bytes(b"x")
    monkeypatch.setattr(converter.shutil, "which", lambda _name: true_program)

    rule = ConverterRule(extension=".dwg", command=(DWG_BINARY, "{input}"),
                         produces="{stem}.svg", then="", enabled=True)
    with pytest.raises(AppErrorException) as caught:
        converter.convert(source, rule, stdout_to="{stem}.svg")
    assert caught.value.error.code == "ERR_CONVERTER_FAILED"


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

def test_the_cache_is_keyed_by_content_and_lives_under_the_app_root(
    tmp_path: Path
):
    root = tmp_path / "cache"
    one = tmp_path / "plan.dwg"
    two = tmp_path / "copy of plan.dwg"
    three = tmp_path / "other.dwg"
    one.write_bytes(DWG_2018)
    two.write_bytes(DWG_2018)
    three.write_bytes(b"AC1024" + b"\x01" * 64)

    assert dwg_svg_cache_path(one, cache_root=root) == \
           dwg_svg_cache_path(two, cache_root=root)
    assert dwg_svg_cache_path(one, cache_root=root) != \
           dwg_svg_cache_path(three, cache_root=root)

    cached = dwg_svg_cache_path(one, cache_root=root)
    assert cached.parent == root
    assert cached.suffix == ".svg"
    # Never beside the user's file, per §6's opening rule.
    assert root not in one.parents or cached.parent != one.parent


# ---------------------------------------------------------------------------
# The fallback: no converter on this machine
# ---------------------------------------------------------------------------

def test_a_drawing_previews_as_its_release_line_when_nothing_can_draw_it(
    drawing: Path, monkeypatch: pytest.MonkeyPatch
):
    """§5c's fallback, and §5a's: the `dwg_release` header line. Not a card
    saying "no preview for this type", which is what it used to be."""
    monkeypatch.setattr(module, "_dwg_converter_default", lambda: False)

    preview = module.load_preview(str(drawing))

    assert preview.kind == KIND_TEXT
    assert preview.body == "AutoCAD drawing, AutoCAD 2018"
    assert preview.meta["drawing"] is True
    assert preview.meta["dwg_preview_available"] is False
    assert preview.error is None                 # never a traceback
    assert "LibreDWG" in preview.notice


def test_the_pane_points_at_the_window_where_the_picture_is(
    drawing: Path, monkeypatch: pytest.MonkeyPatch
):
    """The in-app pane has no button of its own - §4e's reasoning, reused -
    so it says where the picture is, quoting the pop-out button's own label
    rather than describing it."""
    monkeypatch.setattr(module, "_dwg_converter_default", lambda: True)

    preview = module.load_preview(str(drawing))

    assert preview.meta["dwg_preview_available"] is True
    assert preview.notice == module.DWG_PIN_TO_SEE_NOTE
    pane = (Path(__file__).resolve().parents[2] / "app" / "ui" / "widgets"
           / "preview.py").read_text(encoding="utf-8")
    assert '"Pin in a window"' in pane, (
        "the sentence names a control that has to exist under that exact name")


def test_the_missing_note_names_libredwg_alone_and_says_where_to_get_it():
    """§5b names the ODA File Converter too, because either produces the DXF
    the *index* reads. Only LibreDWG produces an SVG, so naming the other one
    here would send somebody to install software that would not help."""
    assert "LibreDWG" in DWG_CONVERTER_MISSING_NOTE
    assert "libredwg" in DWG_CONVERTER_MISSING_NOTE      # the download link
    assert "ODA" not in DWG_CONVERTER_MISSING_NOTE


def test_a_drawing_with_an_unreadable_header_still_previews(tmp_path: Path,
                                                            monkeypatch):
    """A truncated or misnamed `.dwg` says what it can rather than nothing."""
    monkeypatch.setattr(module, "_dwg_converter_default", lambda: False)
    path = tmp_path / "broken.dwg"
    path.write_bytes(b"not a drawing at all")

    preview = module.load_preview(str(path))

    assert preview.kind == KIND_TEXT
    assert preview.body == "AutoCAD drawing."


def test_ensure_dwg_svg_falls_back_rather_than_erroring_with_no_converter(
    drawing: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(module, "dwg_svg_cache_path",
                        lambda _p, cache_root=None: tmp_path / "cache" / "x.svg")
    monkeypatch.setattr(converter.shutil, "which", lambda _name: None)
    monkeypatch.setattr(converter, "_is_windows", lambda: False)
    monkeypatch.setattr(module, "_dwg_converter_default", lambda: False)

    preview = ensure_dwg_svg(str(drawing))

    assert preview.kind == KIND_TEXT
    assert preview.body == "AutoCAD drawing, AutoCAD 2018"
    assert "LibreDWG" in preview.notice
    # A fix line, not a traceback, and not silence.
    assert "winget" not in preview.notice        # that is LibreOffice's, not this


def test_a_conversion_failure_falls_back_and_says_why(
    drawing: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.core.errors import AppErrorException, make_error

    monkeypatch.setattr(module, "dwg_svg_cache_path",
                        lambda _p, cache_root=None: tmp_path / "cache" / "x.svg")
    monkeypatch.setattr(converter.shutil, "which", lambda _name: "/usr/bin/dwg2SVG")

    def refuse(*_a, **_kw):
        raise AppErrorException(make_error(
            "ERR_CONVERTER_FAILED", "extract.converter", path=str(drawing)))

    monkeypatch.setattr(converter, "convert", refuse)

    preview = ensure_dwg_svg(str(drawing))

    assert preview.kind == KIND_TEXT
    assert preview.body == "AutoCAD drawing, AutoCAD 2018"
    assert "ERR_CONVERTER_FAILED" in preview.notice


def test_ensure_dwg_svg_never_raises_for_nonsense_input():
    for path in ("", "   ", "Z:/not/mounted/plan.dwg"):
        assert ensure_dwg_svg(path) is not None


# ---------------------------------------------------------------------------
# The success path, driven by a real subprocess
# ---------------------------------------------------------------------------

def _point_dwg2svg_at_cat(monkeypatch: pytest.MonkeyPatch) -> str:
    cat = shutil.which("cat")
    if not cat:
        pytest.skip("no `cat` on this machine to stand in for a stdout converter")
    monkeypatch.setattr(converter.shutil, "which", lambda _name: cat)
    return cat


def test_a_fresh_conversion_is_cached_and_shown_as_a_simplified_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _point_dwg2svg_at_cat(monkeypatch)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><line x1="0"/></svg>'
    drawing_path = tmp_path / "plan.dwg"
    drawing_path.write_bytes(svg)
    cache_file = tmp_path / "cache" / "plan.svg"
    monkeypatch.setattr(module, "dwg_svg_cache_path",
                        lambda _p, cache_root=None: cache_file)

    preview = ensure_dwg_svg(str(drawing_path))

    assert preview.kind == KIND_IMAGE
    assert preview.path == str(cache_file)
    assert cache_file.read_bytes() == svg
    assert preview.notice == DWG_PREVIEW_NOTE
    assert "Simplified view" in DWG_PREVIEW_NOTE


def test_the_second_open_never_runs_the_converter_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The whole point of caching: paid once per drawing somebody opens."""
    drawing_path = tmp_path / "plan.dwg"
    drawing_path.write_bytes(DWG_2018)
    cache_file = tmp_path / "cache" / "plan.svg"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"<svg/>")
    monkeypatch.setattr(module, "dwg_svg_cache_path",
                        lambda _p, cache_root=None: cache_file)

    def explode(*_a, **_kw):
        raise AssertionError("the converter must not run on a cache hit")

    monkeypatch.setattr(converter, "convert", explode)

    preview = ensure_dwg_svg(str(drawing_path))

    assert preview.kind == KIND_IMAGE
    assert preview.path == str(cache_file)


def test_the_drawing_itself_is_never_written_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """§6, asserted on the bytes rather than on the intention - the same
    check §2e's rotation test makes for the same reason."""
    _point_dwg2svg_at_cat(monkeypatch)
    drawing_path = tmp_path / "plan.dwg"
    drawing_path.write_bytes(DWG_2018)
    before = drawing_path.read_bytes()
    monkeypatch.setattr(module, "dwg_svg_cache_path",
                        lambda _p, cache_root=None: tmp_path / "cache" / "plan.svg")

    ensure_dwg_svg(str(drawing_path))

    assert drawing_path.read_bytes() == before


def test_the_cached_svg_really_draws_through_the_path_the_pop_out_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    r"""The one thing §5c's own note said could not be assumed: that the route
    §4a left behind actually draws an SVG.

    §4a added `.svg` to the image suffixes on the finding that this build's Qt
    already carries the `qsvg` image-format plugin, so `QImage(path)` decodes
    one like any raster file - it built no `QtSvg` path of its own. That is a
    narrower thing to hang a *cached render* on, so it is measured here rather
    than trusted: the whole chain, from `.dwg` to a `QImage` with real pixel
    dimensions, through `render_page.render()` - which is what the pop-out's
    rotate and zoom call - and through `decode_image`, which is what the
    in-app pane calls.
    """
    _qapp()
    from PyQt6.QtGui import QImageReader

    from app.ui.render_page import render
    from app.ui.view_of_file import View

    if b"svg" not in QImageReader.supportedImageFormats():
        pytest.skip("this Qt build has no svg image-format plugin")

    _point_dwg2svg_at_cat(monkeypatch)
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">'
           b'<line x1="0" y1="0" x2="120" y2="60" stroke="black"/>'
           b'<text x="5" y="20">TITLE BLOCK</text></svg>')
    drawing_path = tmp_path / "plan.dwg"
    drawing_path.write_bytes(svg)
    cache_file = tmp_path / "cache" / "plan.svg"
    monkeypatch.setattr(module, "dwg_svg_cache_path",
                        lambda _p, cache_root=None: cache_file)

    preview = ensure_dwg_svg(str(drawing_path))
    assert preview.kind == KIND_IMAGE

    image = render(preview.path, kind=preview.kind, view=View(),
                   fit_to=(400, 300))
    assert image is not None and not image.isNull()
    assert (image.width(), image.height()) == (120, 60)
    assert module.decode_image(preview.path) is not None


def test_a_drawing_too_detailed_to_draw_says_so_rather_than_hanging_the_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    drawing_path = tmp_path / "plan.dwg"
    drawing_path.write_bytes(DWG_2018)
    cache_file = tmp_path / "cache" / "plan.svg"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"<svg/>" + b"x" * module.DWG_SVG_MAX_BYTES)
    monkeypatch.setattr(module, "dwg_svg_cache_path",
                        lambda _p, cache_root=None: cache_file)

    preview = ensure_dwg_svg(str(drawing_path))

    assert preview.kind == KIND_TEXT
    assert "too detailed" in preview.notice
    assert preview.body == "AutoCAD drawing, AutoCAD 2018"


# ---------------------------------------------------------------------------
# The pop-out window
# ---------------------------------------------------------------------------

def _qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _Row:
    def __init__(self, path: str) -> None:
        self.path = path
        self.name = Path(path).name
        self.page = 0


def _drawing_preview(path: Path, *, available: bool, notice: str = ""):
    from app.ui.preview_loader import Preview

    return Preview(
        kind=KIND_TEXT, path=str(path), title=path.name,
        body="AutoCAD drawing, AutoCAD 2018", notice=notice,
        meta={"drawing": True, "dwg_preview_available": available},
    )


def test_the_button_appears_for_a_drawing_when_libredwg_is_there(drawing: Path):
    _qapp()
    from app.ui.widgets.preview_window import PreviewWindow

    window = PreviewWindow(_Row(str(drawing)), state={})
    window._loaded(
        _drawing_preview(drawing, available=True,
                         notice=module.DWG_PIN_TO_SEE_NOTE),
        window._generation)

    assert not window.simplified_button.isHidden()
    assert window.full_layout_button.isHidden()
    # The pane's "pin it to see it" sentence is an instruction to do what has
    # already been done by the time this window exists.
    assert window.note.isHidden()


def test_the_button_is_hidden_and_the_note_says_what_to_install(drawing: Path):
    _qapp()
    from app.ui.widgets.preview_window import PreviewWindow

    window = PreviewWindow(_Row(str(drawing)), state={})
    window._loaded(
        _drawing_preview(drawing, available=False,
                         notice=DWG_CONVERTER_MISSING_NOTE),
        window._generation)

    assert window.simplified_button.isHidden()
    assert "LibreDWG" in window.note.text()


def test_the_off_switch_hides_the_button_even_with_libredwg_installed(
    drawing: Path
):
    """§6: on by default, individually off-able."""
    from app.ui.widgets.preview_window import DWG_PREVIEW_ENABLED_KEY, PreviewWindow

    _qapp()
    off = PreviewWindow(_Row(str(drawing)),
                        state={DWG_PREVIEW_ENABLED_KEY: "off"})
    off._loaded(_drawing_preview(drawing, available=True), off._generation)
    assert off.simplified_button.isHidden()

    on = PreviewWindow(_Row(str(drawing)), state={})     # nothing stored: on
    on._loaded(_drawing_preview(drawing, available=True), on._generation)
    assert not on.simplified_button.isHidden()


def test_the_switch_reads_and_writes_the_one_key():
    _qapp()
    from app.ui.widgets import preview_window as window_module

    class _Store:
        def __init__(self, value):
            self.value = value

        def get_state(self, _key, _default=None):
            return self.value

    seen: list = []
    default_on = window_module.enabled_checkbox(_Store(None),
                                                on_toggle=seen.append)
    turned_off = window_module.enabled_checkbox(_Store("off"),
                                                on_toggle=seen.append)
    broken = window_module.enabled_checkbox(object(), on_toggle=seen.append)

    assert default_on.isChecked()
    assert not turned_off.isChecked()
    assert broken.isChecked(), "a store that raises must not lose the default"
    assert default_on.toolTip()


def test_the_button_is_in_the_row_of_switches_above_the_results():
    """Non-negotiable 11: a tunable with no control is not tunable. The
    switch sits with the other four §6 switches, not in a settings file."""
    source = (Path(__file__).resolve().parents[2] / "app" / "ui" / "widgets"
             / "result_tools.py").read_text(encoding="utf-8")
    assert "DWG_PREVIEW_ENABLED_KEY" in source
    assert "drawings_box" in source


def test_showing_the_simplified_view_leaves_the_real_path_alone(
    drawing: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """"Open the real file" and "Show in folder" must keep pointing at the
    drawing, never at the cached SVG - §4e's `_display_path` split, reused."""
    _qapp()
    from app.ui.preview_loader import Preview
    from app.ui.widgets.preview_window import PreviewWindow

    cached = tmp_path / "cache" / "plan.svg"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"<svg/>")

    window = PreviewWindow(_Row(str(drawing)), state={})
    window._loaded(_drawing_preview(drawing, available=True), window._generation)
    original = window._path
    monkeypatch.setattr(window, "_render", lambda: None)

    window._simplified_ready(
        Preview(kind=KIND_IMAGE, path=str(cached), title=drawing.name,
                notice=DWG_PREVIEW_NOTE, meta={"drawing": True}),
        window._generation)

    assert window._path == original
    assert window._display_path == str(cached)
    assert window.simplified_button.isHidden()
    assert window.note.text() == DWG_PREVIEW_NOTE


def test_a_failed_conversion_leaves_the_release_line_on_screen(drawing: Path):
    """The fallback has to be visible, not merely returned."""
    _qapp()
    from app.ui.widgets.preview_window import PreviewWindow

    window = PreviewWindow(_Row(str(drawing)), state={})
    window._loaded(_drawing_preview(drawing, available=True), window._generation)

    window._simplified_ready(
        _drawing_preview(drawing, available=True,
                         notice="[ERR_CONVERTER_FAILED] it did not work"),
        window._generation)

    assert window.text.toPlainText() == "AutoCAD drawing, AutoCAD 2018"
    assert "ERR_CONVERTER_FAILED" in window.note.text()
    assert window.simplified_button.isEnabled()


def test_a_stale_reply_never_overwrites_a_newer_one(drawing: Path):
    """Each pop-out counts its own renders - §2a's whole point, and this new
    button bumps the same counter every other load does."""
    _qapp()
    from app.ui.widgets.preview_window import PreviewWindow

    window = PreviewWindow(_Row(str(drawing)), state={})
    window._loaded(_drawing_preview(drawing, available=True), window._generation)
    before_note = window.note.text()
    before_path = window._display_path

    from app.ui.preview_loader import Preview

    window._simplified_ready(
        Preview(kind=KIND_IMAGE, path="/somewhere/else.svg", title="stale",
                notice="stale"),
        window._generation - 5)

    assert window.note.text() == before_note
    assert window._display_path == before_path
    assert not window.simplified_button.isHidden()


def test_nothing_in_the_window_opens_a_file_for_writing():
    """Re-asserted after §5c: the cache write lives in `preview_loader.py`,
    where §4e's already does, and never in the window."""
    source = (Path(__file__).resolve().parents[2] / "app" / "ui" / "widgets"
             / "preview_window.py").read_text(encoding="utf-8")
    for writing in ("write_text(", "write_bytes(", "shutil.", "os.remove",
                    "unlink(", '"w"', "'w'"):
        assert writing not in source, f"preview_window.py has {writing}"
