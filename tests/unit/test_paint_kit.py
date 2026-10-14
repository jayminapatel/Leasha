r"""The one row look, the one focus ring and the design tokens behind them (review 2026-10-14).

Layer: L5. DS-2 and DS-11 (`widgets/paint_kit.py`: every painted list's hover, selection and
keyboard ring), DS-3 (the sheet names the view classes), DS-4 (one focus width, a ring only the
keyboard asked for), DS-7 (one card), DS-8 (a `title` step, no pixel font sizes), DS-9 (three
weights), DS-10 (radii from one table), DS-12 (a press is seen).

Offscreen Qt where the rule is about what is drawn; a scan of `app/ui` where it is about what
the code may write.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "app" / "ui"


def _near(found, wanted, slack: int = 40) -> bool:
    return all(abs(a - b) <= slack for a, b in zip(found.getRgb()[:3], wanted.getRgb()[:3]))


class _Option:
    """The three things a delegate reads from its option."""

    def __init__(self, state, width: int = 240, height: int = 44):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QFont

        self.rect = QRect(0, 0, width, height)
        self.font = QFont()
        self.state = state


def _canvas(width: int = 240, height: int = 44):
    from PySide6.QtGui import QColor, QImage

    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#ffffff"))
    return image


def _state(*flags):
    from PySide6.QtWidgets import QStyle

    value = QStyle.StateFlag.State_Enabled
    for flag in flags:
        value |= getattr(QStyle.StateFlag, flag)
    return value


def _ring_colour():
    from app.ui.widgets.paint_kit import tok

    return tok(None, "focus_ring")


# ---------------------------------------------------------------------------
# DS-2 / DS-11: the ring is drawn when the row has the keyboard, and only then
# ---------------------------------------------------------------------------

def _paint_kit_pixel(*flags, ring=None):
    from PySide6.QtGui import QPainter

    from app.ui.widgets.paint_kit import paint_row_state

    image = _canvas()
    painter = QPainter(image)
    try:
        paint_row_state(painter, _Option(_state(*flags)).rect, _Option(_state(*flags)), None, ring=ring)
    finally:
        painter.end()
    return image


def test_a_row_with_the_keyboard_is_ringed():
    image = _paint_kit_pixel("State_HasFocus")
    # The tile is inset 2px; the 2px ring sits inside it, so x=3 is on the ring.
    assert _near(image.pixelColor(3, 22), _ring_colour())


def test_a_row_without_the_keyboard_is_not_ringed():
    from PySide6.QtGui import QColor

    image = _paint_kit_pixel()
    assert image.pixelColor(3, 22) == QColor("#ffffff")


def test_a_selected_row_is_filled_and_selection_beats_hover():
    from app.ui.widgets.paint_kit import tok

    selected = _paint_kit_pixel("State_Selected", "State_MouseOver").pixelColor(120, 22)
    assert _near(selected, tok(None, "accent_soft"), slack=2)
    hovered = _paint_kit_pixel("State_MouseOver").pixelColor(120, 22)
    assert _near(hovered, tok(None, "surface_hover"), slack=2)


def test_ring_true_rings_a_row_qt_did_not_mark():
    """The quick search box's list: its keyboard is in the text box, so it asks for the ring."""
    assert _near(_paint_kit_pixel("State_Selected", ring=True).pixelColor(3, 22), _ring_colour())


def test_the_results_list_rings_its_current_row():
    """The Search list is the one people walk with the arrow keys; it drew no focus at all."""
    from PySide6.QtGui import QColor, QPainter

    from app.ui.presenter import ResultGroup, ResultRow, Snippet
    from app.ui.result_delegate import ROLE_EXPANDED, ROLE_PAYLOAD, ResultDelegate

    best = ResultRow(rank=0, chunk_id=1, file_id=1, path=r"D:\a\report.pdf",
                     display_path="report.pdf", snippet=Snippet("the pump station"),
                     explain="", location="page 1", score=0.9, ext="pdf",
                     mtime_ns=1_700_000_000_000_000_000)
    payload = ResultGroup(file_id=1, name="report.pdf", folder="Archive", kind="pdf",
                          when="12 Mar 2019", path=r"D:\a\report.pdf", rows=[best])

    class Index:
        def data(self, role):
            return payload if role == ROLE_PAYLOAD else (False if role == ROLE_EXPANDED else None)

    def pixel(*flags):
        image = _canvas(300, 80)
        painter = QPainter(image)
        try:
            ResultDelegate().paint(painter, _Option(_state(*flags), 300, 80), Index())
        finally:
            painter.end()
        return image.pixelColor(3, 40)

    assert _near(pixel("State_HasFocus"), _ring_colour())
    assert pixel() == QColor("#ffffff")


def test_the_quick_search_rows_ring_the_chosen_row_in_the_one_width():
    from PySide6.QtGui import QColor, QPainter

    from app.ui.widgets.mini_search_rows import ROLE_LINES, MiniRowDelegate

    class Index:
        def data(self, role):
            return {"kind": "recent", "text": "invoices"} if role == ROLE_LINES else None

    def pixel(*flags):
        image = _canvas()
        painter = QPainter(image)
        try:
            MiniRowDelegate().paint(painter, _Option(_state(*flags)), Index())
        finally:
            painter.end()
        return image.pixelColor(3, 22)

    assert _near(pixel("State_Selected"), _ring_colour())
    assert pixel() == QColor("#ffffff")


def test_a_side_list_rings_its_current_line_when_it_has_the_keyboard(monkeypatch):
    """`outline: 0` took the native box away and nothing replaced it (DS-2)."""
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem

    from app.ui.presenter.side_entry import SideEntry
    from app.ui.widgets.side_list import SideList

    side = SideList("testSide", accessible="Test")
    side.resize(220, 200)
    side.set_entries([SideEntry(key="a", label="Alpha"), SideEntry(key="b", label="Beta")])
    side.setCurrentItem(side.topLevelItem(0))
    index = side.currentIndex()

    def ring(focused: bool):
        monkeypatch.setattr(side, "hasFocus", lambda: focused)
        option = QStyleOptionViewItem()
        option.initFrom(side)
        option.rect = side.visualRect(index).adjusted(-side.visualRect(index).left(), 0, 0, 0)
        option.rect.setWidth(220)
        image = _canvas(220, max(20, option.rect.bottom() + 4))
        painter = QPainter(image)
        try:
            side.drawRow(painter, option, index)
        finally:
            painter.end()
        return image.pixelColor(2, option.rect.center().y())

    assert _near(ring(True), _ring_colour())
    assert not _near(ring(False), _ring_colour(), slack=10)


# The guard: a delegate that paints its own selection uses the kit.
#: Painters that draw a selection some other way, and why. Never grows.
_OWN_SELECTION_OK = {
    # The photo grid's tiles are drawn by the style, from the sheet's QListView::item rules.
    "widgets/photo_browser.py",
}


def _paints_selection_without_the_kit(source: str) -> list[int]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("paint"):
            body = ast.get_source_segment(source, node) or ""
            if "State_Selected" in body and ("accent_soft" in body or "fillRect" in body
                                             or "fillPath" in body) and "paint_row_state" not in body:
                found.append(node.lineno)
    return found


def test_every_painted_selection_goes_through_the_kit():
    loose = []
    for path in sorted(UI.rglob("*.py")):
        rel = path.relative_to(UI).as_posix()
        if rel in _OWN_SELECTION_OK or rel == "widgets/paint_kit.py":
            continue
        loose += [f"{rel}:{line}" for line in _paints_selection_without_the_kit(path.read_text(encoding="utf-8"))]
    assert not loose, "a painter fills its own selection - use paint_kit.paint_row_state: " + ", ".join(loose)


def test_the_selection_guard_would_notice_a_hand_fill():
    source = ("def paint(self, p, option, index):\n"
              "    if option.state & QStyle.StateFlag.State_Selected:\n"
              "        p.fillRect(option.rect, self.tok('accent_soft'))\n")
    assert _paints_selection_without_the_kit(source) == [1]


# ---------------------------------------------------------------------------
# DS-3: tables and trees are named
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_sheet_names_table_and_tree_views_for_hover_and_selection(scheme):
    from app.ui.theme import stylesheet

    sheet = stylesheet(scheme, base_pt=9.0)
    for state in ("hover", "selected"):
        selector = re.search(r"([^{}]*QListWidget::item:" + state + r"[^{]*)\{", sheet).group(1)
        for name in ("QTableView", "QTreeView", "QTableWidget", "QTreeWidget", "QListView"):
            assert f"{name}::item:{state}" in selector, (name, state)
    assert "QTableView::item, QTableWidget::item {" in sheet        # the row's hairline too
    frame = re.search(r"([^{}]*QListWidget, QListView[^{]*)\{", sheet).group(1)
    assert "QTableView" in frame


# ---------------------------------------------------------------------------
# DS-4: one focus width, and a ring only the keyboard asked for
# ---------------------------------------------------------------------------

def _rules(sheet: str):
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", sheet):
        yield match.group(1).strip(), match.group(2)


def test_every_focus_border_is_the_one_width():
    from app.ui.theme import FOCUS_WIDTH, stylesheet

    sheet = re.sub(r"/\*.*?\*/", "", stylesheet("light", base_pt=9.0), flags=re.S)
    focus_rules = [(s, body) for s, body in _rules(sheet) if ":focus" in s]
    assert len(focus_rules) > 15
    for selector, body in focus_rules:
        for width in re.findall(r"border:\s*(\d+)px", body):
            assert int(width) == FOCUS_WIDTH, selector
    # Tabs and the rail take the ring colour, not the accent or the label ink.
    tab = dict(_rules(sheet))["QTabBar::tab:focus"]
    assert "#4a37b0" in tab                                   # light `focus_ring`
    rail = dict(_rules(sheet))["#rail QToolButton:focus"]
    assert "#4a37b0" in rail


def test_a_focused_button_keeps_its_size():
    """The 2px ring takes its extra pixel from the padding, so a word never moves."""
    from app.ui import theme

    focus = theme._button_focus()
    for name in ("button_pad_y", "button_pad_left", "button_pad_right"):
        assert int(focus[f"{name}_focus"][:-2]) == int(theme.BUTTON[name][:-2]) - (theme.FOCUS_WIDTH - 1)


def test_a_click_marks_pointer_focus_and_the_keyboard_does_not():
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QFocusEvent
    from PySide6.QtWidgets import QPushButton

    from app.ui.widgets.focus_visible import PROPERTY, FocusVisible

    button = QPushButton("Open")
    watcher = FocusVisible()
    watcher.eventFilter(button, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.MouseFocusReason))
    assert button.property(PROPERTY) is True
    watcher.eventFilter(button, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
    assert button.property(PROPERTY) is False


def test_the_sheet_quiets_a_ring_the_pointer_put_there():
    from app.ui.theme import stylesheet

    sheet = stylesheet("dark", base_pt=9.0)
    for selector in ('QPushButton[pointerFocus="true"]:focus', 'QToolButton[pointerFocus="true"]:focus',
                     'QTabBar[pointerFocus="true"]::tab:focus', '#rail QToolButton[pointerFocus="true"]:focus'):
        assert selector in sheet


# ---------------------------------------------------------------------------
# DS-12: a press is seen
# ---------------------------------------------------------------------------

def test_both_palettes_have_a_pressed_accent_unlike_rest_and_hover():
    from app.ui.theme import PALETTES

    for palette in PALETTES.values():
        assert palette["accent_pressed"] not in (palette["accent"], palette["focus_ring"])


def test_the_missing_states_have_rules():
    from app.ui.theme import PALETTES, stylesheet

    sheet = re.sub(r"/\*.*?\*/", "", stylesheet("light", base_pt=9.0), flags=re.S)
    rules = dict(_rules(sheet))
    assert PALETTES["light"]["accent_pressed"] in rules['QPushButton[buttonRole="primary"]:pressed']
    assert 'QPushButton[buttonRole="danger"]:pressed' in rules
    assert 'QPushButton[buttonRole="danger"]:focus' in rules
    assert "#segmented QToolButton:hover:!checked" in rules
    assert "#rail QToolButton:pressed" in rules


# ---------------------------------------------------------------------------
# DS-10: radii from one table
# ---------------------------------------------------------------------------

def test_the_radius_table_has_the_menu_and_window_steps():
    from app.ui.theme import RADIUS

    assert RADIUS["radius_menu"] == "6px" and RADIUS["radius_window"] == "14px"


def test_the_painters_read_their_radii_from_the_table():
    from app.ui.result_delegate import ROW_RADIUS
    from app.ui.theme import radius_px
    from app.ui.widgets.mini_search_frame import CARD_RADIUS
    from app.ui.widgets.paint_kit import badge_radius, row_radius

    assert ROW_RADIUS == row_radius() == badge_radius() == radius_px("radius_control")
    assert CARD_RADIUS == radius_px("radius_window")
    source = (UI / "widgets" / "mini_search_rows.py").read_text(encoding="utf-8")
    assert "_RADIUS =" not in source and "7.0, 7.0" not in source


def test_the_sheet_writes_no_step_radius_as_a_number():
    from app.ui import theme

    for literal in ("radius: 5px", "radius: 6px", "radius: 8px", "radius: 10px", "radius: 14px"):
        assert literal not in theme._TEMPLATE, literal


# ---------------------------------------------------------------------------
# DS-9: three weights
# ---------------------------------------------------------------------------

def test_the_weight_table():
    from app.ui import theme

    assert theme.WEIGHT == {"regular": 400, "semibold": 600, "bold": 700}
    assert not re.search(r"font-weight:\s*\d", theme._TEMPLATE)


def test_a_result_name_is_semibold_and_bold_when_chosen():
    from PySide6.QtGui import QFont

    from app.ui.result_delegate import ResultDelegate

    name, meta, body = ResultDelegate()._fonts(QFont())
    assert name.weight() == QFont.Weight.DemiBold and meta.weight() == QFont.Weight.Normal
    source = (UI / "result_delegate.py").read_text(encoding="utf-8")
    assert "ExtraBold" not in source


# ---------------------------------------------------------------------------
# DS-8: a title step, and no font sized in pixels
# ---------------------------------------------------------------------------

def test_the_title_step_is_the_page_titles():
    from app.ui.theme import SCALE, font_sizes
    from app.ui.widgets.page_frame import TITLE_SCALE

    assert SCALE["title"] == TITLE_SCALE == 1.25
    assert font_sizes(9.0)["title"] == "11.2pt" or font_sizes(9.0)["title"] == "11.3pt"


def test_scaled_font_turns_a_pixel_font_into_points():
    from PySide6.QtGui import QFont

    from app.ui.widgets.paint_kit import scaled_font

    base = QFont()
    base.setPixelSize(16)
    font = scaled_font(base, "body")
    assert font.pixelSize() == -1 and font.pointSizeF() == pytest.approx(12.0)
    assert scaled_font(base, "title").pointSizeF() == pytest.approx(15.0)


def _pixel_sizes(source: str) -> list[int]:
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "setPixelSize"]


def test_no_font_is_sized_in_pixels():
    """Pixels ignore the text-size setting; the guide says points (`paint_kit.points_for_pixels`)."""
    loose = [f"{p.relative_to(UI).as_posix()}:{line}" for p in sorted(UI.rglob("*.py"))
             for line in _pixel_sizes(p.read_text(encoding="utf-8"))]
    assert not loose, "setPixelSize - use a point size: " + ", ".join(loose)


def test_the_pixel_guard_would_notice_one():
    assert _pixel_sizes("f.setPointSizeF(9)\nf.setPixelSize(12)\n") == [2]


# ---------------------------------------------------------------------------
# DS-7: one card
# ---------------------------------------------------------------------------

def test_the_four_cards_are_one_card():
    from app.ui.widgets.answer_card import AnswerCard
    from app.ui.widgets.card import CARD_MARGINS, Card
    from app.ui.widgets.machine_card import MachineCard
    from app.ui.widgets.mail_card import MailCardView
    from app.ui.widgets.person_card import PersonCard, PersonInfo

    cards = [AnswerCard(lambda: ""), MailCardView(), PersonCard(PersonInfo(1, "Zed")), MachineCard()]
    for card in cards:
        assert isinstance(card, Card) and card.property("card") is True, type(card).__name__
        margins = card.layout().contentsMargins()
        assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == CARD_MARGINS
        assert not card.styleSheet(), f"{type(card).__name__} styles itself"
    assert cards[3].title_label.text() == "This machine"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_card_rule_is_the_surface_the_edge_and_the_box_radius(scheme):
    from app.ui.theme import PALETTES, RADIUS, stylesheet

    sheet = re.sub(r"/\*.*?\*/", "", stylesheet(scheme, base_pt=9.0), flags=re.S)
    rule = dict(_rules(sheet))['*[card="true"]']
    palette = PALETTES[scheme]
    assert palette["surface"] in rule and palette["border"] in rule and RADIUS["radius_box"] in rule
