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
