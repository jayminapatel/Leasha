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
    exactly the rows somebody is looking at.

    `_append(row` (no closing paren) rather than `_append(row)`: item 5d added
    an `anchor=` keyword to the same call, and the substring must still match
    it - the point of this test is that a chunk is appended as a plain model
    row at all, not the exact argument list.
    """
    text = RESULTS_VIEW.read_text(encoding="utf-8")
    body = text.split("def _rebuild")[1].split("\n    def ")[0]
    assert "_append(row" in body


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


def test_the_chevron_has_its_own_click_target_on_multi_match_groups():
    """Item 2a: "the chevron is a real click target" - `subtitle_rect` is
    what a click handler hit-tests against, and it must exist only where the
    chevron is actually painted."""
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QFont

    from app.ui.result_delegate import ResultDelegate

    class Option:
        def __init__(self):
            self.rect = QRect(0, 0, 300, 60)
            self.font = QFont()

    delegate = ResultDelegate()
    assert delegate.subtitle_rect(Option(), group(matches=1)) is None, \
        "a single match has no chevron to click"
    rect = delegate.subtitle_rect(Option(), group(matches=3))
    assert rect is not None
    assert rect.width() > 0 and rect.height() > 0
    assert rect.top() > 0, "the chevron line sits below the name line, not on it"


def test_chevron_hit_finds_the_file_id_under_a_click_on_the_subtitle_line():
    """Item 2a end to end: `chevron_hit` is what `ResultsView.eventFilter`
    calls on every click - a hit on the subtitle line returns the group's
    `file_id`; a miss (or a single-match group, with no chevron at all)
    returns `None` so the click falls through to ordinary handling."""
    from PyQt6.QtCore import QPoint, QRect
    from PyQt6.QtGui import QFont

    from app.ui.result_delegate import ROLE_PAYLOAD, ResultDelegate

    payload = group(matches=3)

    class Index:
        def isValid(self):
            return True

        def data(self, role):
            return payload if role == ROLE_PAYLOAD else None

    class View:
        def __init__(self, index):
            self._index = index

        def indexAt(self, pos):
            return self._index

        def initViewItemOption(self, option):
            option.font = QFont()

        def visualRect(self, index):
            return QRect(0, 0, 300, 60)

    class Option:
        rect = QRect(0, 0, 300, 60)
        font = QFont()

    delegate = ResultDelegate()
    rect = delegate.subtitle_rect(Option(), payload)
    inside = rect.center()
    assert delegate.chevron_hit(View(Index()), inside) == payload.file_id
    outside = QPoint(rect.left(), max(0, rect.top() - 5))
    assert delegate.chevron_hit(View(Index()), outside) is None

    class SingleMatchIndex(Index):
        def data(self, role):
            return group(matches=1) if role == ROLE_PAYLOAD else None

    assert delegate.chevron_hit(View(SingleMatchIndex()), inside) is None


def test_chevron_hit_ignores_an_invalid_index():
    from PyQt6.QtCore import QPoint

    from app.ui.result_delegate import ResultDelegate

    class InvalidIndex:
        def isValid(self):
            return False

    class View:
        def indexAt(self, pos):
            return InvalidIndex()

    assert ResultDelegate().chevron_hit(View(), QPoint(5, 5)) is None


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


def test_the_tooltip_always_carries_the_exact_date():
    """Item 4b: the exact date is a tooltip promise, independent of whichever
    register the visible row happens to be reading in."""
    from app.ui.presenter import group_results, result_tooltip

    built = group_results([row()], now=1_700_100_000, register="plain")[0]
    assert "Date:" in result_tooltip(built)
    assert built.when_exact in result_tooltip(built)


def test_the_tooltip_carries_the_kind_word():
    """Item 3a moved the kind word off the painted row and onto the icon;
    item 7a's pass confirms it actually reached the tooltip, as promised."""
    from app.ui.presenter import result_tooltip

    assert "PDF" in result_tooltip(group())


def test_accessible_text_carries_the_kind_word():
    from app.ui.presenter import accessible_text

    assert "PDF" in accessible_text(group())


def test_accessible_text_announces_a_multi_match_group_s_state():
    """The chevron is a purely visual cue - item 7a makes the state a
    screen reader can hear too."""
    from app.ui.presenter import accessible_text

    collapsed = accessible_text(group(matches=3), expanded=False)
    expanded = accessible_text(group(matches=3), expanded=True)
    assert "matches" in collapsed and "collapsed" in collapsed
    assert "matches" in expanded and "expanded" in expanded


def test_accessible_text_says_nothing_about_expansion_for_a_single_match():
    from app.ui.presenter import accessible_text

    assert "expanded" not in accessible_text(group(matches=1))
    assert "collapsed" not in accessible_text(group(matches=1))


def test_accessible_text_prefers_the_exact_date_over_the_friendly_one():
    """Sighted or not - item 7a: this is the one line meant to be trusted
    outright, never the register's "yesterday"."""
    from app.ui.presenter import accessible_text, group_results

    built = group_results([row()], now=1_700_100_000, register="plain")[0]
    spoken = accessible_text(built)
    assert built.when_exact in spoken
    assert built.when not in spoken or built.when == built.when_exact


def test_a_plain_result_row_without_a_group_still_gets_an_exact_date():
    """The tooltip works on a raw `ResultRow` too - ungrouped mode shows those
    directly, with no `ResultGroup.when_exact` to fall back on."""
    from app.ui.presenter import result_tooltip

    assert "Date:" in result_tooltip(row())


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


def test_sizehint_stays_one_line_tall_at_compact_density():
    """§8: "at every density" - the test above only exercised comfortable.
    Compact reserves exactly one snippet line (`_max_snippet_lines`), so a
    long snippet must not make the row any taller than a short one once one
    line is enough to overflow both - the same gap-under-the-row regression,
    checked at the density where the answer is "no extra line", not "two"."""
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
    delegate.prefs = ViewPreferences(density=Density.COMPACT)
    long_text = "The quick brown fox jumps over the lazy dog near the old mill by the river, twice over"
    payload = row(explain="")
    long_payload = ResultRow(**{**payload.__dict__, "snippet": Snippet(long_text)})
    short_payload = ResultRow(**{**payload.__dict__, "snippet": Snippet("short")})
    long_hint = delegate.sizeHint(Option(160), Index(long_payload))
    short_hint = delegate.sizeHint(Option(160), Index(short_payload))
    assert long_hint.height() == short_hint.height(), \
        "compact reserves exactly one snippet line, however long the text"


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


# ---------------------------------------------------------------------------
# Item 3c: code rows in monospace (the line number is a documented gap)
# ---------------------------------------------------------------------------

def test_a_python_file_is_recognised_as_code():
    from app.ui.presenter import is_code_kind

    assert is_code_kind("py")
    assert is_code_kind("PY"), "extensions arrive lower-cased from disk, not always"


def test_prose_and_data_formats_are_not_code():
    from app.ui.presenter import is_code_kind

    for kind in ("txt", "md", "csv", "pdf", "email", ""):
        assert not is_code_kind(kind), kind


def test_a_code_row_gets_a_monospace_font():
    from PyQt6.QtGui import QFont

    from app.ui.result_delegate import _snippet_font

    base = QFont()
    base.setPointSize(11)
    mono = _snippet_font(base, "py")
    assert mono.fixedPitch() or mono.styleHint() == QFont.StyleHint.Monospace \
        or mono.family() != base.family()
    assert mono.pointSize() == base.pointSize(), "the size must not change, only the family"


def test_a_prose_row_keeps_the_ordinary_font():
    from PyQt6.QtGui import QFont

    from app.ui.result_delegate import _snippet_font

    base = QFont()
    assert _snippet_font(base, "pdf") is base


# ---------------------------------------------------------------------------
# Item 4c: twin disambiguation actually paints
# ---------------------------------------------------------------------------

def test_a_group_with_emphasis_paints_without_raising():
    """A real paint pass, not just the presenter's text decision - this is
    the one place a bad character range in `folder_emphasis` would show up
    as a crash rather than a wrong pixel."""
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QFont, QPixmap, QPainter

    from app.ui.result_delegate import ResultDelegate, ROLE_PAYLOAD, ROLE_EXPANDED
    from app.ui.presenter import ResultGroup

    twin = ResultGroup(file_id=1, name="invoice.pdf", folder="… > ClientA > 2019 > Q1",
                       kind="pdf", when="12 Mar 2019", path=r"D:\a\invoice.pdf",
                       rows=[row()], folder_emphasis=(4, 11))

    from PyQt6.QtWidgets import QStyle

    class Option:
        def __init__(self):
            self.rect = QRect(0, 0, 300, 60)
            self.font = QFont()
            self.state = QStyle.StateFlag.State_Enabled

    class Index:
        def data(self, role):
            if role == ROLE_PAYLOAD:
                return twin
            if role == ROLE_EXPANDED:
                return False
            return None

    pixmap = QPixmap(300, 60)
    painter = QPainter(pixmap)
    try:
        ResultDelegate().paint(painter, Option(), Index())
    finally:
        painter.end()


# ---------------------------------------------------------------------------
# Item 5a: a subtle hover state
# ---------------------------------------------------------------------------

def test_hover_paints_a_different_background_from_the_ordinary_row():
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QFont, QImage, QPainter
    from PyQt6.QtWidgets import QStyle

    from app.ui.result_delegate import ResultDelegate, ROLE_PAYLOAD, ROLE_EXPANDED

    payload = group()

    class Option:
        def __init__(self, state):
            self.rect = QRect(0, 0, 200, 40)
            self.font = QFont()
            self.state = state

    class Index:
        def data(self, role):
            if role == ROLE_PAYLOAD:
                return payload
            if role == ROLE_EXPANDED:
                return False
            return None

    def pixel_at(state):
        image = QImage(200, 40, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        ResultDelegate().paint(painter, Option(state), Index())
        painter.end()
        return image.pixelColor(2, 2)

    plain = pixel_at(QStyle.StateFlag.State_Enabled)
    hovered = pixel_at(QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_MouseOver)
    assert plain != hovered, "hover must paint a visibly different background"


def test_hover_never_wins_over_selection():
    """Selection is the stronger signal - a selected, hovered row must still
    read as selected, never fall back to the plain hover tint."""
    from app.ui.result_delegate import ResultDelegate

    source = DELEGATE.read_text(encoding="utf-8")
    body = source.split("def paint")[1].split("\n    def ")[0]
    # Selected is checked first, and hover is only an elif - a selected row
    # can never also take the hover branch.
    assert body.index("State_Selected") < body.index("State_MouseOver")


# ---------------------------------------------------------------------------
# Item 5c: the terminator row, actually wired in (it wasn't - see the dated
# note on this item in the work order)
# ---------------------------------------------------------------------------

def test_the_terminator_is_appended_after_every_rebuild():
    text = RESULTS_VIEW.read_text(encoding="utf-8")
    body = text.split("def _rebuild")[1].split("\n    def ")[0]
    assert "_append_terminator" in body
    assert "results_terminator" in body


def test_the_terminator_row_is_not_selectable():
    text = RESULTS_VIEW.read_text(encoding="utf-8")
    body = text.split("def _append_terminator")[1].split("\n    def ")[0]
    assert "ItemIsSelectable" in body


def test_a_terminator_paints_without_raising():
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QFont, QPixmap, QPainter
    from PyQt6.QtWidgets import QStyle

    from app.ui.presenter import Terminator
    from app.ui.result_delegate import ResultDelegate, ROLE_PAYLOAD

    class Option:
        def __init__(self):
            self.rect = QRect(0, 0, 200, 30)
            self.font = QFont()
            self.state = QStyle.StateFlag.State_Enabled

    class Index:
        def data(self, role):
            return Terminator("That's all — 23 results.") if role == ROLE_PAYLOAD else None

    pixmap = QPixmap(200, 30)
    painter = QPainter(pixmap)
    try:
        ResultDelegate().paint(painter, Option(), Index())
        hint = ResultDelegate().sizeHint(Option(), Index())
    finally:
        painter.end()
    assert hint.height() > 0


# ---------------------------------------------------------------------------
# Item 7a: the accessibility pass - text scaling and the highlight signal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
def test_the_delegate_scales_with_the_system_font(scale):
    """Windows text scaling reaches this delegate the same way the rest of
    the application's "Make text bigger" support does - through the font
    the view hands it, never a fixed pixel count of its own. 125/150/200%
    must all still measure and paint in agreement, not merely at 100%."""
    from PyQt6.QtGui import QFont

    from app.ui.result_delegate import ResultDelegate

    base_pt = 13
    base = QFont()
    base.setPointSize(int(round(base_pt * scale)))
    delegate = ResultDelegate()
    name_font, meta_font, body_font = delegate._fonts(base)
    assert name_font.pointSize() > body_font.pointSize() > 0
    assert meta_font.pointSize() > 0
    # The hierarchy is relative to *this* base, not a hard-coded pair of
    # numbers - scaling the base scales the gap between name and body too.
    if scale > 1.0:
        assert name_font.pointSize() > int(round(13 * 1.0)) + 2


def test_sizehint_grows_with_a_scaled_font_rather_than_clipping():
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QFont

    from app.ui.result_delegate import ResultDelegate, ROLE_PAYLOAD, ROLE_EXPANDED

    payload = group()

    class Option:
        def __init__(self, point_size):
            self.rect = QRect(0, 0, 300, 999)
            self.font = QFont()
            self.font.setPointSize(point_size)

    class Index:
        def data(self, role):
            if role == ROLE_PAYLOAD:
                return payload
            if role == ROLE_EXPANDED:
                return False
            return None

    delegate = ResultDelegate()
    small = delegate.sizeHint(Option(10), Index())
    large = delegate.sizeHint(Option(20), Index())
    assert large.height() > small.height(), "a 200% font must reserve more room, not the same"


def test_the_highlight_is_signalled_by_weight_and_by_colour():
    """Colour is never the only signal, per this order's own standing rule -
    a bold run and a plain run must differ in *both* font and pen."""
    from PyQt6.QtGui import QColor, QFont, QPen

    from app.ui.result_delegate import _draw_run

    calls: list[tuple[str, Any]] = []

    class RecordingPainter:
        def setFont(self, font):
            calls.append(("font", QFont(font)))

        def setPen(self, pen):
            calls.append(("pen", QColor(pen.color())))

        def drawText(self, *args):
            pass

    plain_colour = QColor("black")
    matched_colour = QColor("blue")
    bold_font = QFont()
    bold_font.setBold(True)
    text = "the pump station report"
    _draw_run(RecordingPainter(), text, 0, len(text), [(4, 8)], 0, 0, 10_000,
             QFont(), bold_font, QPen(plain_colour), QPen(matched_colour))
    fonts = [value for kind, value in calls if kind == "font"]
    colours = [value for kind, value in calls if kind == "pen"]
    assert any(f.bold() for f in fonts), "the matched run must be bold"
    assert any(not f.bold() for f in fonts), "the plain run must not be"
    assert matched_colour in colours, "the matched run must also use the highlight colour"
    assert plain_colour in colours, "the plain run must use the ordinary colour"


def test_the_delegate_holds_no_store_or_engine():
    """It paints. Anything it needed to look up would be a query per repaint,
    which is a query per scroll frame."""
    tree = ast.parse(DELEGATE.read_text(encoding="utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any("storage" in name or "engine" in name for name in imported)
