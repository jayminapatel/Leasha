r"""A log you can read at a glance and act on. Workspace §1.

Layer: L5.

Four things the order asks for, and one rule under all of them.

**Colour is never the only signal.** The level word stays in the line, so a
person who cannot tell the warn colour from the normal one — a common enough
thing, and true of every screenshot ever pasted into a message — still reads
`WARNING`. The same rule the focus ring and the meaning-match marker follow.

**Theme tokens, never hex.** A hardcoded red is invisible on one of the two
themes, and which one depends on the red. Both palettes define `danger`, with
different values, which is the entire reason the palette is two dictionaries.

**A copy, not a move.** Popping the log out leaves the pane in Settings
exactly where it was, so closing the window returns nothing to re-wire.

**Only the lines worth clicking are clickable.** An underlined `INFO` that
goes nowhere teaches people that none of them go anywhere.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                     # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

from app.ui.log_lines import (                                  # noqa: E402
    ERROR, LEVELS, NORMAL, TOKENS, WARN, colour_token, is_actionable,
    level_of, path_in,
)
from app.ui.theme import PALETTES, palette_for                  # noqa: E402
from app.ui.widgets.log_window import (                         # noqa: E402
    GEOMETRY_KEY, MIN_HEIGHT, MIN_WIDTH, ON_TOP_KEY, LogWindow, geometry_from,
    geometry_text,
)

_INFO = "15:09:34 INFO     index indexed 12 files"
_WARN = "15:09:34 WARNING  index Sheet Q3 truncated at 5000 rows"
_ERROR = "15:09:34 ERROR    index could not read 'C:/work/report.pdf': locked"


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# §1a — reading a line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("line", "level"), [
    (_INFO, NORMAL), (_WARN, WARN), (_ERROR, ERROR),
    ("15:09:34 DEBUG    x nothing to do", NORMAL),
    ("15:09:34 CRITICAL x the disk is gone", ERROR),
])
def test_a_line_is_read_for_its_level(line, level):
    assert level_of(line) == level


@pytest.mark.parametrize("line", [
    "", None, "Traceback (most recent call last):",
    "    File \"x.py\", line 4, in <module>", "not a log line at all",
])
def test_anything_unparseable_is_an_ordinary_line(line):
    r"""**Not an error, and that matters.** The log holds tracebacks,
    continuation lines and whatever a subprocess printed; colouring those by
    accident would put red through a wall of text that is merely detail."""
    assert level_of(line) == NORMAL


def test_there_are_three_buckets_and_not_five():
    r"""A log rendering five severities in five colours is harder to scan than
    one rendered in none. The eye is looking for the lines that are *not*
    fine, and two colours answer that."""
    assert set(LEVELS.values()) == {NORMAL, WARN, ERROR}


# ---------------------------------------------------------------------------
# §1a — tokens, never hex
# ---------------------------------------------------------------------------

def test_a_line_names_a_token_and_never_a_colour():
    for line in (_INFO, _WARN, _ERROR):
        token = colour_token(line)
        assert not token.startswith("#"), "a hex value escaped into the code"
        assert token in TOKENS.values()


def test_both_themes_define_every_token_the_log_uses():
    r"""**The failure this guards is silent.** A token missing from one
    palette renders as an empty colour string, which Qt ignores - so the
    warning line would simply look ordinary on that theme, with nothing
    anywhere saying why."""
    for theme, palette in PALETTES.items():
        for token in TOKENS.values():
            assert token in palette, f"{theme} has no {token!r}"


def test_the_two_themes_disagree_about_danger():
    """Which is the point of two palettes: the same token needs a different
    value on a white ground, exactly as `highlight` already did."""
    values = {palette["danger"] for palette in PALETTES.values()}
    assert len(values) == len(PALETTES)


# ---------------------------------------------------------------------------
# §1b — a log you can act on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("line", "found"), [
    (_ERROR, "C:/work/report.pdf"),
    (r"15:09:34 ERROR  x skipped C:\Users\jay\My Docs\a.docx (locked)",
     r"C:\Users\jay\My Docs\a.docx"),
    ("15:09:34 WARNING x //server/share/site plan.dwg is too big",
     "//server/share/site plan.dwg"),
    ("15:09:34 ERROR  x failed on /home/x/a b/c.txt, moving on",
     "/home/x/a b/c.txt"),
])
def test_a_path_is_found_however_it_is_written(line, found):
    r"""**Spaces included, because that is where people keep things.** The
    first version of this stopped at the first space, so
    `C:\Users\jay\My Docs\a.docx` - the shape of most real paths - was found
    by nothing at all."""
    assert path_in(line) == found


@pytest.mark.parametrize("line", [
    _INFO, _WARN,
    "15:09:34 ERROR    x could not read C:/work  (a folder, not a file)",
    "15:09:34 ERROR    x something went wrong",
])
def test_a_line_with_no_file_in_it_offers_nothing(line):
    """A folder is not a file: an extension is required, because a click that
    opened the wrong thing is worse than a line that does not respond."""
    assert not is_actionable(line)


def test_only_warnings_and_errors_are_clickable():
    r"""**An underlined `INFO` that goes nowhere teaches people that none of
    them go anywhere.** §1b asks for a log you can act on, and the way to fail
    at that is to make every line look actionable."""
    ordinary = "15:09:34 INFO     x wrote C:/work/report.pdf"
    assert path_in(ordinary) == "C:/work/report.pdf"
    assert not is_actionable(ordinary)
    assert is_actionable(_ERROR)


def test_reading_a_line_never_raises():
    for rubbish in (None, "", 12, object(), b"bytes"):
        assert level_of(rubbish) in (NORMAL, WARN, ERROR)
        assert path_in(rubbish) is None or isinstance(path_in(rubbish), str)


# ---------------------------------------------------------------------------
# The pane, painted
# ---------------------------------------------------------------------------

def _painted(qapp, palette=None):
    from app.ui.widgets.debug_pane import DebugPane

    pane = DebugPane()
    if palette is not None:
        pane.set_palette(palette)
    pane._draw([_INFO, _WARN, _ERROR])
    return pane


def test_the_warning_and_the_error_are_painted_in_the_themes_own_colours(qapp):
    palette = palette_for("dark")
    html = _painted(qapp, palette).view.document().toHtml().lower()
    assert palette["warning"].lower() in html
    assert palette["danger"].lower() in html


def test_the_same_lines_are_painted_differently_on_the_other_theme(qapp):
    r"""The regression the token rule exists to prevent, as a test: a
    hardcoded value would give both themes the same html."""
    dark = _painted(qapp, palette_for("dark")).view.document().toHtml().lower()
    light = _painted(qapp, palette_for("light")).view.document().toHtml().lower()
    assert palette_for("light")["danger"].lower() in light
    assert palette_for("light")["danger"].lower() not in dark


def test_the_level_word_is_still_in_the_line(qapp):
    r"""**Colour is never the only signal.** The accessibility rule this
    codebase applies to the focus ring and the meaning-match marker, applied
    to a log."""
    text = _painted(qapp, palette_for("dark")).view.toPlainText()
    for word in ("INFO", "WARNING", "ERROR"):
        assert word in text


def test_an_actionable_line_is_underlined_as_well_as_coloured(qapp):
    """Underline alone reads as a hyperlink; colour alone is not a signal."""
    html = _painted(qapp, palette_for("dark")).view.document().toHtml()
    assert "underline" in html


def test_a_pane_with_no_palette_draws_plain_text_rather_than_nothing(qapp):
    """Which is what a widget built in a test sees, and what a widget built
    before the first theme pass sees. No colour beats a wrong one."""
    pane = _painted(qapp, None)
    assert "WARNING" in pane.view.toPlainText()


def test_the_pane_reports_the_file_a_line_names(qapp):
    from app.ui.widgets.debug_pane import DebugPane

    pane = DebugPane()
    chosen: list = []
    pane.file_chosen.connect(chosen.append)
    pane.file_chosen.emit(path_in(_ERROR) or "")
    assert chosen == ["C:/work/report.pdf"]


# ---------------------------------------------------------------------------
# §1c / §1d — the window
# ---------------------------------------------------------------------------

def test_the_window_remembers_where_it_was(qapp):
    window = LogWindow()
    window.restore({GEOMETRY_KEY: "40,60,700,400"})
    assert window.geometry().width() == 700
    assert window.geometry().height() == 400


@pytest.mark.parametrize("text", [
    "", None, "nonsense", "1,2,3", "1,2,3,4,5", "a,b,c,d",
    f"0,0,{MIN_WIDTH - 1},{MIN_HEIGHT}", f"0,0,{MIN_WIDTH},{MIN_HEIGHT - 1}",
])
def test_an_unusable_remembered_size_is_forgotten_rather_than_used(text):
    r"""**A geometry from a screen that is no longer attached**, or a window
    somebody dragged to one pixel. Either must cost the memory rather than
    produce a window that cannot be found or cannot be read."""
    assert geometry_from(text) is None


def test_a_geometry_round_trips(qapp):
    window = LogWindow()
    window.restore({GEOMETRY_KEY: "40,60,700,400"})
    assert geometry_from(geometry_text(window.geometry())) is not None


def test_the_geometry_is_readable_by_a_person(qapp):
    r"""Four integers rather than `saveGeometry()`'s base64 blob: somebody
    looking at `index_state` to work out why a window opened off-screen has to
    be able to see where it thinks it is."""
    window = LogWindow()
    window.restore({GEOMETRY_KEY: "40,60,700,400"})
    assert geometry_text(window.geometry()).count(",") == 3


def test_stay_on_top_is_applied_and_remembered(qapp):
    remembered: dict = {}
    window = LogWindow()
    window.remember.connect(remembered.update)

    window.on_top.setChecked(True)
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert remembered[ON_TOP_KEY] == "true"

    window.on_top.setChecked(False)
    assert not (window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    assert remembered[ON_TOP_KEY] == "false"


def test_stay_on_top_is_restored(qapp):
    window = LogWindow()
    window.restore({ON_TOP_KEY: "true"})
    assert window.on_top.isChecked()
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint


def test_restoring_rubbish_leaves_a_usable_window(qapp):
    """Anything can be in `index_state`. A window that will not open is a
    worse outcome than one that opens in the default place."""
    window = LogWindow()
    window.restore({GEOMETRY_KEY: object(), ON_TOP_KEY: object()})
    window.restore(None)
    assert window.width() >= MIN_WIDTH


def test_the_window_is_not_parented_to_the_main_one(qapp):
    r"""**§1c's load-bearing detail.** A parented `QWidget` with a window flag
    is still owned by its parent, so minimising the main window would take the
    log with it - which is the one thing the pop-out exists to prevent."""
    assert LogWindow().parent() is None


def test_the_popped_out_window_does_not_offer_to_pop_out_again(qapp):
    assert not LogWindow().pane.pop_button.isVisible()


def test_the_pane_in_settings_does_offer_it(qapp):
    from app.ui.widgets.debug_pane import DebugPane

    assert DebugPane().pop_button.toolTip()


def test_closing_the_window_says_so(qapp):
    """So the window can forget it and the next pop-out builds a fresh one
    rather than showing a dead widget."""
    window = LogWindow()
    closed: list = []
    window.closed.connect(lambda: closed.append(True))
    window.close()
    assert closed == [True]


def test_popping_out_is_a_copy_and_never_a_move():
    r"""**The design the order sets out**, and the reason closing the window
    returns nothing to re-wire: `LogWindow` builds its own `DebugPane` rather
    than re-parenting the one in Settings, so Settings is untouched.
    """
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[2]
              / "app" / "ui" / "widgets" / "log_window.py").read_text(
                  encoding="utf-8")
    assert "DebugPane(self" in source
    for stealing in ("setParent(", "takeWidget(", "removeWidget("):
        assert stealing not in source, f"log_window.py calls {stealing}"


def test_the_window_never_writes_to_a_user_file():
    r"""§6's first rule for this whole order: *no code path in this order
    writes to a user file.* The log window reads a ring in memory and writes
    two preferences; a guard is cheaper than remembering.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "app" / "ui"
    for name in ("widgets/log_window.py", "log_lines.py"):
        source = (root / name).read_text(encoding="utf-8")
        for writing in ("open(", "write_text(", "shutil.", "os.remove",
                        "unlink("):
            assert writing not in source, f"{name} calls {writing}"
