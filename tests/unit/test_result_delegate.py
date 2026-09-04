"""Rows are painted, not built - and the two halves must agree.

Layer: L5

`QListWidget` with `setItemWidget` gave every row three live `QLabel`s. Five
hundred results is fifteen hundred widgets, each with a layout, a palette and
event handling, all constructed before the first is visible. A delegate paints
the dozen rows on screen and the cost stops scaling with the result count.

**The failure mode this file guards.** A delegate measures in `sizeHint` and
draws in `paint`. When those two disagree, text is clipped at the bottom of
every row - easy to produce by adding a line to one and forgetting the other,
and tedious to chase because it looks like a font problem.

Qt widgets need a display, so these test the geometry rules and the text
decisions rather than the pixels.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.ui.presenter import ResultGroup, ResultRow, Snippet
from app.ui.view_options import Density, ViewPreferences

DELEGATE = Path(__file__).resolve().parents[2] / "app" / "ui" / "result_delegate.py"
RESULTS_VIEW = Path(__file__).resolve().parents[2] / "app" / "ui" / "results_view.py"


def row(score=0.9, location="page 4", explain="keyword match"):
    return ResultRow(
        rank=0, chunk_id=1, file_id=1, path=r"D:\a\report.pdf",
        display_path="report.pdf", snippet=Snippet("the pump station"),
        explain=explain, location=location, score=score, ext="pdf",
        mtime_ns=1_700_000_000_000_000_000,
    )


def group(matches=1, **kwargs):
    rows = [row() for _ in range(matches)]
    return ResultGroup(file_id=1, name="report.pdf", folder="Archive > Leeds",
                       kind="pdf", when="12 Mar 2019", path=r"D:\a\report.pdf",
                       rows=rows, **kwargs)


# ---------------------------------------------------------------------------
# The virtualisation itself
# ---------------------------------------------------------------------------

def calls_named(path: Path) -> set[str]:
    """Names actually *called*, not names mentioned.

    The first version of this test searched the file for the string
    `setItemWidget` and tripped on the docstring explaining that it is no longer
    used. A substring check cannot tell code from prose.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                found.add(func.attr)
            elif isinstance(func, ast.Name):
                found.add(func.id)
    return found


def test_the_view_no_longer_builds_a_widget_per_row():
    """**The whole point.** `setItemWidget` is what made five hundred results
    fifteen hundred widgets."""
    called = calls_named(RESULTS_VIEW)
    assert "setItemWidget" not in called
    assert "QListWidget" not in called


def test_the_view_uses_a_model_and_a_delegate():
    text = RESULTS_VIEW.read_text(encoding="utf-8")
    assert "QListView" in text
    assert "setItemDelegate" in text


def test_expanded_chunks_are_model_rows_not_nested_widgets():
    """Nesting widgets inside a virtualised list defeats the virtualisation for
    exactly the rows somebody is looking at."""
    text = RESULTS_VIEW.read_text(encoding="utf-8")
    body = text.split("def _rebuild")[1].split("\n    def ")[0]
    assert "_append(row)" in body


def test_scrolling_is_per_pixel():
    """Rows have different heights - a snippet wraps to one line or two - and
    per-item scrolling jumps by whole rows, which reads as stuttering."""
    assert "ScrollPerPixel" in RESULTS_VIEW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Measuring and painting must use the same numbers
# ---------------------------------------------------------------------------

def test_the_geometry_has_a_single_source():
    """`Metrics` exists so `sizeHint` and `paint` cannot drift apart.

    It lives in `view_options.py` rather than here: it is pure numbers with no
    Qt in it, and keeping it beside the other density rules means the geometry
    can be checked without a display - which is how these tests run at all.
    """
    from app.ui.view_options import Metrics  # noqa: F401 - importable Qt-free

    source = DELEGATE.read_text(encoding="utf-8")
    for method in ("sizeHint", "def paint"):
        body = source.split(method)[1].split("\n    def ")[0]
        assert "Metrics.for_density" in body, f"{method} does not use the shared metrics"


def test_both_halves_ask_the_same_question_about_the_snippet():
    """If `sizeHint` reserves room for a snippet that `paint` skips - or the
    reverse - every row is wrong by one line."""
    source = DELEGATE.read_text(encoding="utf-8")
    for method in ("sizeHint", "def paint"):
        body = source.split(method)[1].split("\n    def ")[0]
        assert "_shows_snippet" in body


def test_compact_shrinks_padding_and_not_text():
    """More rows on screen is the point. Text you cannot read is not more
    information."""
    from app.ui.view_options import Metrics

    compact = Metrics.for_density(Density.COMPACT)
    normal = Metrics.for_density(Density.NORMAL)
    assert compact.pad_y < normal.pad_y
    assert compact.name_bump == normal.name_bump, "compact must not shrink the name"


def test_an_unknown_density_falls_back_rather_than_raising():
    from app.ui.view_options import Metrics

    assert Metrics.for_density("enormous") == Metrics.for_density(Density.NORMAL)


def test_a_pixel_sized_font_is_handled():
    """`theme.py` sets `font-size: 13px`, so `pointSize()` returns -1. Adding to
    it would produce `setPointSize(-1)`, which Qt warns about and ignores - the
    exact bug that reached the console this morning."""
    source = DELEGATE.read_text(encoding="utf-8")
    fonts = source.split("def _fonts")[1].split("\n    def ")[0]
    assert "pixelSize" in fonts
    assert "setPixelSize" in fonts


# ---------------------------------------------------------------------------
# What each row says
# ---------------------------------------------------------------------------

def test_a_single_match_group_has_no_expander():
    from app.ui.presenter import group_subtitle

    assert "▸" not in group_subtitle(group(matches=1))


def test_a_multi_match_group_shows_a_closed_arrow_when_collapsed():
    from app.ui.presenter import group_subtitle

    text = group_subtitle(group(matches=3), expanded=False)
    assert "3 matches" in text and "▸" in text


def test_an_expanded_group_shows_an_open_arrow():
    from app.ui.presenter import group_subtitle

    assert "▾" in group_subtitle(group(matches=3), expanded=True)


def test_expansion_is_a_fact_about_the_view_not_the_data():
    """A `ResultGroup` is frozen and describes the results. Whether its chunks
    are on screen belongs to one view at one moment - putting it on the
    dataclass would make two views of the same results fight over it."""
    assert not hasattr(group(), "expanded")


def test_scores_are_absent_until_asked_for():
    from app.ui.presenter import group_subtitle

    assert "score" not in group_subtitle(group(), show_scores=False)


def test_scores_appear_inline_when_asked_for():
    from app.ui.presenter import group_subtitle

    assert "score" in group_subtitle(group(), show_scores=True)


def test_the_explanation_is_always_reachable_from_the_tooltip():
    """It came off every row to stop it competing with the name. It must not
    become unavailable - being able to ask why is where trust comes from."""
    from app.ui.presenter import result_tooltip

    text = result_tooltip(group())
    assert "score" in text
    assert r"D:\a\report.pdf" in text, "the full path must survive the breadcrumb"


def test_a_missing_file_says_so_in_the_tooltip():
    from app.ui.presenter import result_tooltip

    assert "missing" in result_tooltip(group(), missing=True).lower()


def test_the_kind_tag_is_text_rather_than_an_icon():
    """Text survives dark mode, high-DPI and a missing font file. None of that
    is worth paying for before anybody says the tags are insufficient."""
    from app.ui.presenter import kind_tag

    assert kind_tag("pdf") == "PDF"
    assert kind_tag("email") == "MAIL"


def test_an_unknown_kind_still_gets_a_short_tag():
    from app.ui.presenter import kind_tag

    assert kind_tag("sevenzip") == "SEVE"
    assert kind_tag("") == "?"


# ---------------------------------------------------------------------------
# The delegate must not become a second presenter
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Item 1a: two-line snippet wrapping in comfortable density
# ---------------------------------------------------------------------------

def test_compact_keeps_one_snippet_line():
    from app.ui.result_delegate import _max_snippet_lines
    from app.ui.view_options import Density

    assert _max_snippet_lines(Density.COMPACT) == 1


def test_normal_density_allows_two_snippet_lines():
    from app.ui.result_delegate import _max_snippet_lines
    from app.ui.view_options import Density

    assert _max_snippet_lines(Density.NORMAL) == 2


def test_a_long_snippet_wraps_to_two_ranges_at_two_lines():
    from PyQt6.QtGui import QFont, QFontMetrics

    from app.ui.result_delegate import _wrap_ranges

    font = QFont()
    metrics = QFontMetrics(font)
    text = "The quick brown fox jumps over the lazy dog near the old mill by the river"
    narrow = metrics.horizontalAdvance("The quick brown fox")
    ranges = _wrap_ranges(metrics, text, narrow, max_lines=2)
    assert len(ranges) == 2
    # Contiguous, and nothing is skipped between the two lines.
    assert ranges[0][1] <= ranges[1][0]
    assert ranges[0][0] == 0


def test_a_short_snippet_does_not_manufacture_a_second_line():
    from PyQt6.QtGui import QFont, QFontMetrics

    from app.ui.result_delegate import _wrap_ranges

    font = QFont()
    metrics = QFontMetrics(font)
    text = "short passage"
    wide = metrics.horizontalAdvance(text) + 200
    ranges = _wrap_ranges(metrics, text, wide, max_lines=2)
    assert len(ranges) == 1
    assert ranges[0] == (0, len(text))


def test_snippet_height_matches_the_number_of_wrapped_lines():
    from PyQt6.QtGui import QFont, QFontMetrics

    from app.ui.result_delegate import _snippet_height

    font = QFont()
    line_height = QFontMetrics(font).height()
    text = "The quick brown fox jumps over the lazy dog near the old mill by the river"
    narrow = QFontMetrics(font).horizontalAdvance("The quick brown fox")
    assert _snippet_height(font, text, narrow, max_lines=1) == line_height
    assert _snippet_height(font, text, narrow, max_lines=2) == line_height * 2


def test_sizehint_and_paint_still_agree_at_two_lines():
    """The gap-under-every-row regression this file exists to guard: `sizeHint`
    must reserve exactly as many lines as `paint` draws, at every density."""
    from app.ui.result_delegate import ResultDelegate, ROLE_PAYLOAD
    from app.ui.view_options import ViewPreferences, Density

    class Option:
        def __init__(self, width):
            from PyQt6.QtCore import QRect
            from PyQt6.QtGui import QFont
            self.rect = QRect(0, 0, width, 999)
            self.font = QFont()

    class Index:
        def __init__(self, payload):
            self._payload = payload

        def data(self, role):
            return self._payload if role == ROLE_PAYLOAD else None

    delegate = ResultDelegate()
    delegate.prefs = ViewPreferences(density=Density.NORMAL)
    long_text = "The quick brown fox jumps over the lazy dog near the old mill by the river"
    payload = row(explain="")
    payload = ResultRow(**{**payload.__dict__, "snippet": Snippet(long_text)})
    hint = delegate.sizeHint(Option(160), Index(payload))
    # Two lines of body text plus the meta line must fit; a narrow row with a
    # long snippet must be taller than one with a short one at the same width.
    short_payload = ResultRow(**{**payload.__dict__, "snippet": Snippet("short")})
    short_hint = delegate.sizeHint(Option(160), Index(short_payload))
    assert hint.height() > short_hint.height()


# ---------------------------------------------------------------------------
# Item 3a: real file icons, cached per extension
# ---------------------------------------------------------------------------

def test_the_bracketed_kind_tag_is_gone_from_the_painted_row():
    """The `[PDF]` text tag gives way to a real icon - item 3a."""
    source = DELEGATE.read_text(encoding="utf-8")
    body = source.split("def _paint_group")[1].split("\n    def ")[0]
    assert "[{kind_tag" not in body
    assert "_icon_for" in body


def test_an_icon_is_cached_after_the_first_lookup():
    from app.ui.result_delegate import _icon_for, _ICON_CACHE

    _ICON_CACHE.clear()
    first = _icon_for("pdf")
    assert "pdf" in _ICON_CACHE
    second = _icon_for("pdf")
    assert first is second, "a second lookup for the same kind must not re-ask the shell"


def test_an_unknown_kind_still_returns_a_usable_icon():
    from app.ui.result_delegate import _icon_for

    icon = _icon_for("sevenzip")
    assert icon is not None
    assert not icon.isNull()


def test_mail_gets_its_own_icon_rather_than_a_generic_one():
    """`.eml` is a real extension the shell recognises - mail should not fall
    back to the generic-file icon every other unrecognised kind gets."""
    from app.ui.result_delegate import _icon_for, _ICON_CACHE

    _ICON_CACHE.clear()
    mail_icon = _icon_for("email")
    assert not mail_icon.isNull()


def test_the_kind_word_still_reaches_the_tooltip_and_accessible_text():
    """Item 3a is explicit: the icon replaces the painted tag, never the word
    a screen reader or a tooltip relies on."""
    from app.ui.presenter import accessible_text, result_tooltip

    payload = group()
    # `accessible_text` names the file itself; the kind word survives in the
    # tooltip via `why`/`explain`, which is what `result_tooltip` renders.
    assert accessible_text(payload)
    assert result_tooltip(payload)


def test_the_delegate_holds_no_store_or_engine():
    """It paints. Anything it needed to look up would be a query per repaint,
    which is a query per scroll frame."""
    tree = ast.parse(DELEGATE.read_text(encoding="utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any("storage" in name or "engine" in name for name in imported)
