r"""The button system (`app/ui/widgets/buttons.py`), owner's request 2026-09-27.

*"this buttons across the whole screen look odd and out of place all buttons
should be consistent throughout the app and should have icons. perfect spacing
and visually world class, the indexing pill looks big and out of place"*

**Before:** on Settings › Storage & maintenance, "Run doctor", "Check that
search works", "Save a support bundle…" and "Open the recordings folder" were
stretched across the whole page while "Clear logs" and "Open the logs folder"
beside them were their natural width, and not one of them had an icon.

The first half of this file checks the helper and the rules on plain widgets.
The second half walks every page of the real window
(`tests/unit/conftest.py`'s `gui_mainwindow`, per
`docs/WORKORDER-CONVENTIONS.md` section 5b) and holds every action button on
it to the rules.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QPushButton, QVBoxLayout, QWidget  # noqa: E402

from app.ui import theme  # noqa: E402

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# The helper and the rules, on plain widgets
# ---------------------------------------------------------------------------

def test_every_icon_the_table_names_is_shipped() -> None:
    from app.ui.widgets.buttons import BUTTONS, PREFIXES, ROLES
    from app.ui.widgets.icons import ICON_NAMES, icon_file

    for label, (glyph, role) in list(BUTTONS.items()) + list(PREFIXES.items()):
        assert role in ROLES, label
        assert glyph in ICON_NAMES, f"{label}: {glyph} is not in ICON_NAMES"
        assert icon_file(glyph) is not None, f"{label}: assets/icons/{glyph}.svg is missing"


def test_the_sheet_gives_every_kind_one_height_and_one_padding() -> None:
    sheet = theme.stylesheet("light", detected="light", base_pt=9.0)
    for role in ("primary", "secondary", "danger"):
        assert f'QPushButton[buttonRole="{role}"]' in sheet
    assert "min-height: 18px" in sheet
    # Disabled primary and danger buttons must not keep their colours.
    assert 'QPushButton[buttonRole="primary"]:disabled' in sheet
    assert 'QPushButton[buttonRole="danger"]:disabled' in sheet


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_column_of_buttons_is_not_stretched_and_every_one_is_one_height(qapp, scheme) -> None:
    """The picture's bug, on a plain column: a `QVBoxLayout` gives a plain
    `QPushButton` the whole width. Through the helper each is its own width,
    at the left, and all are the same height whatever their kind."""
    from app.ui.widgets.buttons import action_button

    page = QWidget()
    page.setStyleSheet(theme.stylesheet(scheme, detected=scheme, base_pt=9.0))
    column = QVBoxLayout(page)
    labels = ("Run doctor", "Check that search works", "Save a support bundle…",
              "Clear logs", "Start indexing")
    buttons = [action_button(label) for label in labels]
    for button in buttons:
        column.addWidget(button)
    page.resize(900, 400)
    page.show()
    qapp.processEvents()
    try:
        assert len({b.height() for b in buttons}) == 1, [b.height() for b in buttons]
        for button in buttons:
            assert button.width() <= button.sizeHint().width(), button.text()
            assert button.x() == buttons[0].x(), f"{button.text()} is not at the left"
            assert not button.icon().isNull(), f"{button.text()} has no icon"
            assert button.iconSize().height() == 16
        kinds = {b.text(): b.property("buttonRole") for b in buttons}
        assert kinds["Start indexing"] == "primary"
        assert kinds["Clear logs"] == "danger"
        assert kinds["Run doctor"] == "secondary"
    finally:
        page.close()


def test_labels_are_never_changed_and_shortcut_markers_are_ignored(qapp) -> None:
    from app.ui.widgets.buttons import lookup, style_button

    button = QPushButton("&OK")
    style_button(button, *lookup(button.text()))
    assert button.text() == "&OK"
    assert button.property("buttonRole") == "primary"
    assert lookup("Yes — same as 'Holiday disk'") == ("check", "primary")
    assert lookup("Nothing like this is in the table") is None


def test_a_button_not_in_the_table_is_left_alone_by_the_sweep(qapp) -> None:
    from app.ui.widgets.buttons import style_all

    page = QWidget()
    column = QVBoxLayout(page)
    known, unknown, flat = QPushButton("Run doctor"), QPushButton("A new idea"), QPushButton("Copy")
    flat.setFlat(True)
    for button in (known, unknown, flat):
        column.addWidget(button)
    assert style_all(page) == 1
    assert known.property("buttonRole") == "secondary"
    assert unknown.property("buttonRole") is None
    assert flat.property("buttonRole") is None


def test_a_theme_change_redraws_the_icons(qapp) -> None:
    """Icons are pictures and never see the stylesheet, so a theme change has
    to repaint them - a dark red danger icon on a dark page is invisible."""
    from app.ui.widgets.buttons import action_button, retint_all

    holder = QWidget()
    button = action_button("Clear logs", parent=holder)
    retint_all(theme.PALETTES["light"], roots=[holder])
    light = button.icon().cacheKey()
    retint_all(theme.PALETTES["dark"], roots=[holder])
    assert button.icon().cacheKey() != light
    assert not button.icon().isNull()


def test_an_unknown_kind_is_refused(qapp) -> None:
    from app.ui.widgets.buttons import action_button

    with pytest.raises(ValueError):
        action_button("Run doctor", "stethoscope", "loud")


# ---------------------------------------------------------------------------
# The real window (`gui_mainwindow`)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shown(gui_mainwindow):
    """The real window, shown at 1100x760, and hidden again afterwards - a
    window left showing takes the clicks aimed at the next module's."""
    from tests.unit.conftest import gui_pump

    app, window, *_ = gui_mainwindow
    window.resize(1100, 760)
    window.show()
    gui_pump(app, 20)
    yield app, window
    window.hide()


def _open(app, window, page: str, category: str = "") -> None:
    """Bring a rail page, and a category on it, forward - the same route
    `tools/grab_ui.py` takes."""
    from tests.unit.conftest import gui_pump

    rail = window.rail
    for index in range(rail.count()):
        if rail.tabText(index) == page:
            rail.setCurrentIndex(index)
            break
    gui_pump(app, 3)
    if category:
        for view in (window.settings_view, window.indexing_view):
            here = rail.widget(rail.currentIndex())
            if view is here or view.isAncestorOf(here) or here.isAncestorOf(view):
                view._nav.show_category(category, persist=False)
    gui_pump(app, 5)


def test_storage_page_buttons_are_their_own_width_with_an_icon(shown) -> None:
    """**Before:** "Run doctor", "Check that search works", "Save a support
    bundle…" and "Open the recordings folder" ran the whole width of the page,
    "Clear logs" beside them did not, and none had an icon.

    Now each is as wide as its words and icon, the three checks share one row
    at the left, and "Clear logs" is marked as the one that deletes."""
    app, window = shown
    _open(app, window, "Settings", "Storage & maintenance")
    box = window.settings_view.environment
    buttons = [box.run_doctor_button, box.check_button, box.bundle_button,
               box.clear_logs_button]
    for button in buttons:
        assert button.isVisible(), button.text()
        assert button.width() <= button.sizeHint().width(), (
            f"{button.text()} is stretched to {button.width()}px")
        assert button.width() < box.width() // 2, button.text()
        assert not button.icon().isNull(), f"{button.text()} has no icon"
    row = {b.y() for b in buttons[:3]}
    assert len(row) == 1, "the three checks share one row"
    assert box.run_doctor_button.x() < box.check_button.x() < box.bundle_button.x()
    assert box.clear_logs_button.property("buttonRole") == "danger"
    assert len({b.height() for b in buttons}) == 1


def test_indexing_row_is_one_system_start_primary_reset_danger(shown) -> None:
    """**Before:** the five Indexing buttons had icons (3ddb128) but all in one
    grey, Start no different from Scan, and Reset - the one that deletes the
    index - looked like the rest.

    Now Start is the page's filled primary, Reset reads as danger, each has
    its icon at one height, and while a run is held the Pause button's icon
    turns to play along with its word."""
    app, window = shown
    _open(app, window, "Indexing", "Status")
    view = window.indexing_view
    buttons = [view.start_button, view.pause_button, view.stop_button,
               view.scan_button, view.reset_button]
    for button in buttons:
        assert not button.icon().isNull(), button.text()
        assert button.width() <= button.sizeHint().width(), button.text()
    assert len({b.height() for b in buttons}) == 1
    assert view.start_button.property("buttonRole") == "primary"
    assert view.reset_button.property("buttonRole") == "danger"
    assert {b.property("buttonRole") for b in buttons[1:4]} == {"secondary"}
    try:
        view.controls.show_held(True)
        assert view.pause_button.text() == "Resume"
        assert view.pause_button.property("buttonIcon") == "play"
    finally:
        view.controls.show_held(False)
    assert view.pause_button.property("buttonIcon") == "pause"


def _page_list(window) -> list:
    """Every page and category of the real window, as (page, category)."""
    pages = [(window.rail.tabText(i), "") for i in range(window.rail.count())
             if window.rail.tabText(i) not in ("Indexing", "Settings")]
    pages += [("Indexing", name) for name in window.indexing_view._nav.category_names()]
    pages += [("Settings", name) for name in window.settings_view._nav.category_names()]
    return pages


def _action_buttons(root) -> list:
    from app.ui.widgets.buttons import is_exempt

    return [b for b in root.findChildren(QPushButton) if not is_exempt(b)]


def _rule_breaks(buttons, *, visible_to) -> list[str]:
    """What is wrong with each button, in words, per the rules in
    `widgets/buttons.py`'s docstring. Empty means every one follows them."""
    from app.ui.widgets.buttons import ICON_PX, ROLES

    broken = []
    for button in buttons:
        name = button.text() or button.accessibleName() or button.objectName()
        if button.property("buttonRole") not in ROLES:
            broken.append(f"{name!r} is not in the button system (no kind)")
            continue
        if button.icon().isNull() or button.iconSize().height() != ICON_PX:
            broken.append(f"{name!r} has no {ICON_PX}px icon")
        if not (button.text() or button.accessibleName()):
            broken.append(f"{name!r} has no words for a screen reader")
        if button.isVisibleTo(visible_to) and button.width() > button.sizeHint().width():
            broken.append(f"{name!r} is stretched to {button.width()}px "
                          f"(its natural width is {button.sizeHint().width()}px)")
    return broken


def test_every_page_every_action_button_follows_the_rules(shown) -> None:
    """**The owner's sentence, as a walk:** every page and every category of
    the real window, and on each one every action button has a kind, a 16px
    icon, its words, and its natural width; and every one of them in the whole
    window is the same height."""
    app, window = shown
    broken: list[str] = []
    heights: dict[int, list[str]] = {}
    for page, category in _page_list(window):
        _open(app, window, page, category)
        here = window.rail.widget(window.rail.currentIndex())
        buttons = _action_buttons(here)
        where = f"{page}{' > ' + category if category else ''}"
        broken += [f"{where}: {line}" for line in _rule_breaks(buttons, visible_to=window)]
        for button in buttons:
            if button.isVisibleTo(window):
                heights.setdefault(button.height(), []).append(f"{where}: {button.text()}")
    assert not broken, "\n".join(broken)
    assert len(heights) == 1, {h: names[:4] for h, names in heights.items()}
    seen = sum(len(names) for names in heights.values())
    assert seen >= 40, f"only {seen} buttons were on screen - the walk missed pages"


def test_the_dialogs_and_pop_outs_follow_the_rules_too(qapp, shown) -> None:
    """Dialogs style themselves as they are built. OK is the dialog's primary,
    Cancel secondary, "Forget this source" danger - and their row stays where
    the operating system puts it."""
    from app.ui.widgets.offline_media_dialogs import (
        DeleteVolumeDialog, RenameSuggestionDialog, ScanNameDialog,
    )
    from app.ui.widgets.pinned_panel import PinnedPanel
    from app.ui.widgets.report_export_dialog import SourceSelectionDialog

    built = [ScanNameDialog("/media/usb"), RenameSuggestionDialog("Holiday disk"),
             DeleteVolumeDialog("Holiday disk", 12), SourceSelectionDialog([]),
             PinnedPanel()]
    try:
        for dialog in built:
            dialog.show()                   # laid out, as a person sees it
            qapp.processEvents()
            broken = _rule_breaks(_action_buttons(dialog), visible_to=dialog)
            dialog.hide()
            assert not broken, f"{type(dialog).__name__}: {broken}"
        kinds = {b.text(): b.property("buttonRole") for b in _action_buttons(built[2])}
        assert "danger" in kinds.values(), kinds
        assert {b.property("buttonRole") for b in _action_buttons(built[0])} == {
            "primary", "secondary"}
    finally:
        for dialog in built:
            dialog.deleteLater()


# ---------------------------------------------------------------------------
# The indexing pill
# ---------------------------------------------------------------------------

def test_the_pill_reads_as_a_rail_button_and_still_says_state_and_count(shown, qtbot) -> None:
    """**Before:** the pill at the rail's foot was a filled card - a bold
    two-line "Up to date", a bar and "17 files" - the heaviest thing on the
    rail, taller than any page button around it.

    Now it is the rail buttons' width, no taller than two of their labels
    would make it, unfilled until chosen; its state is a word under an icon
    with a coloured dot; the count is in its tooltip and accessible name; a
    mouse click still opens the Indexing page."""
    from PyQt6.QtCore import Qt

    from app.ui.rail_state import FINISHED, RUNNING, pill_text
    from tests.unit.conftest import gui_pump

    app, window = shown
    _open(app, window, "Search")
    rail, pill = window.rail, window.rail.pill
    settings_button = next(b for b in rail._buttons.values() if b.text() == "Settings")

    rail.show_pill(pill_text(FINISHED, documents=17), None)
    gui_pump(app, 3)
    assert pill.headline.isVisible() and pill.headline.text() == "Up to date"
    assert not pill.detail.isVisible(), "the figures moved to the tooltip"
    assert "17 files" in pill.toolTip() and "Open the Indexing page" in pill.toolTip()
    assert "17 files" in pill.accessibleName()
    assert pill.glyph.pixmap() is not None and not pill.glyph.pixmap().isNull()
    assert not pill.bar.isVisible(), "the bar only shows while a run moves it"
    assert pill.width() == settings_button.width()
    line = pill.headline.fontMetrics().lineSpacing()
    assert pill.height() <= settings_button.height() + line + 2, (
        f"pill {pill.height()}px against a {settings_button.height()}px button")
    assert pill.property("selected") is not True

    rail.show_pill(pill_text(RUNNING, indexed=1234), None)
    gui_pump(app, 3)
    assert pill.headline.text() == "Indexing"
    assert "1,234 so far" in pill.toolTip()
    assert pill.bar.isVisible()

    qtbot.mouseClick(pill, Qt.MouseButton.LeftButton)
    gui_pump(app, 3)
    assert rail.widget(rail.currentIndex()) is window.indexing_view or \
        window.indexing_view.isAncestorOf(rail.widget(rail.currentIndex())) or \
        rail.widget(rail.currentIndex()).isAncestorOf(window.indexing_view)
    assert pill.property("selected") is True
    rail.show_pill(pill_text(FINISHED, documents=17), None)
    _open(app, window, "Search")


def test_a_short_window_leaves_the_pill_as_an_icon(shown) -> None:
    """0x section 9's short-window rule, for the pill too: below the height the
    labelled rail needs, the buttons go to icons alone and so does the pill -
    its word stays in the tooltip - and both come back when there is room."""
    from tests.unit.conftest import gui_pump

    app, window = shown
    rail, pill = window.rail, window.rail.pill
    try:
        window.resize(1100, 200)                 # Qt stops at the window's floor
        gui_pump(app, 8)
        assert rail._compact or window.minimumSize().height() >= rail._natural
        if rail._compact:
            assert not pill.headline.isVisible()
            assert pill.glyph.isVisible()
            assert pill.headline.text() in pill.toolTip()
        window.resize(1100, 900)
        gui_pump(app, 8)
        assert not rail._compact
        assert pill.headline.isVisible()
    finally:
        window.resize(1100, 760)
        gui_pump(app, 5)


def test_every_pill_state_has_a_dot_colour_from_the_theme() -> None:
    """The dot's colour is decided without Qt, like the words, and is always
    a real token in both palettes."""
    from app.ui.rail_state import FAILED, FINISHED, IDLE, RUNNING, TONES, pill_text

    states = {
        "Up to date": pill_text(FINISHED, documents=3),
        "Indexing": pill_text(RUNNING, indexed=5),
        "Paused": pill_text(RUNNING, indexed=5, paused=True),
        "Stopped": pill_text(FINISHED, indexed=5, stopped_early=True),
        "Needs attention": pill_text(FAILED, error="disk full"),
        "Nothing yet": pill_text(IDLE, documents=0),
        "Index": pill_text(IDLE, documents=None),
    }
    for headline, state in states.items():
        assert state.headline == headline
        assert state.tone in TONES, headline
        for palette in theme.PALETTES.values():
            assert TONES[state.tone] in palette, (headline, TONES[state.tone])
    assert states["Up to date"].tone == "done"
    assert states["Needs attention"].tone == "failed"
    assert states["Paused"].tone == states["Stopped"].tone == "held"


def test_switching_the_theme_redraws_the_button_icons_on_the_real_window(shown) -> None:
    """Icons are pictures: without a redraw, the dark theme's lavender primary
    ink would stay on the light theme's navy "Start indexing"."""
    app, window = shown
    button = window.indexing_view.start_button
    before = window._theme_preference
    try:
        window._theme_preference = "light"
        window._apply_theme()
        light = button.property("buttonPainted")
        window._theme_preference = "dark"
        window._apply_theme()
        assert button.property("buttonPainted") != light
        assert theme.PALETTES["dark"]["accent_on"] in button.property("buttonPainted")
    finally:
        window._theme_preference = before
        window._apply_theme()
