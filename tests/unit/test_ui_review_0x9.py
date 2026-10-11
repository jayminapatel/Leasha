r"""Order 0x section 9 - the UI review, one scenario per improvement.

Every surface of the real window was grabbed to PNG offscreen on 2026-09-27,
light and dark, at 1100x760 and at 760x560, and looked at. Each improvement
that came out of that review is pinned here by a scenario that drives the real
`MainWindow` (`tests/unit/conftest.py`'s `gui_mainwindow`) with real key
presses and clicks, per `docs/WORKORDER-CONVENTIONS.md` section 5b - not by
calling a slot or asserting that one function mentions another.

Each test says what the picture showed before the fix, so whoever reads a
failure knows what a person would be looking at.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402

from tests.unit.conftest import gui_pump  # noqa: E402

pytestmark = pytest.mark.gui

KEY = Qt.Key


@pytest.fixture(scope="module", autouse=True)
def _leave_no_window_showing(gui_mainwindow):
    """These scenarios `show()` the window (sizes and focus need a real one)
    and never close it - closing a `MainWindow` mid-process is what crashes.
    A window left showing takes the clicks `QTest` aims at the next module's
    hidden one, so it is hidden again afterwards."""
    yield
    gui_mainwindow[1].hide()


def _front(app, window, qtbot, width: int, height: int) -> None:
    """Show the window at a size, and let the layouts settle."""
    window.resize(width, height)
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    gui_pump(app, 20)


def _rail_labels_fit(window) -> list[str]:
    """Each rail button whose label is showing but is squeezed below the height
    that label needs. Empty means nothing is cut off."""
    squeezed = []
    for button in window.rail._buttons.values():
        showing_label = button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
        if showing_label and button.height() < button.sizeHint().height():
            squeezed.append(f"{button.text()}: {button.height()} < {button.sizeHint().height()}")
    return squeezed


def test_a_short_window_shows_rail_icons_rather_than_labels_cut_in_half(gui_mainwindow, qtbot):
    r"""**Before:** at 560 pixels tall every rail label was cut in half -
    "Searcn", "Files" with its lower half missing - because the rail measured
    its own needs while its buttons were still hidden, decided it needed 114
    pixels, and never switched to icons. Grab: `before-760x560/*/search-home.png`.

    Now: short, it shows icons alone (the words stay in each tooltip and
    accessible name); tall again, the labels come back. The arrow keys still
    walk the rail in both forms."""
    app, window, *_ = gui_mainwindow
    rail = window.rail

    _front(app, window, qtbot, 760, 560)
    # The real window is on screen before Mail, Code, Chat, Indexing and
    # Settings are added - each `insertTab` re-lays the rail out while it is
    # showing. This fixture builds every page before it shows the window, so
    # re-lay it out once here, the same call a late `insertTab` makes, from the
    # state the real window is in at that moment: labels showing, because when
    # it was first measured it had only four pages and they fitted.
    rail._set_compact(False)
    rail._relayout()
    gui_pump(app, 20)
    assert _rail_labels_fit(window) == [], "a label is showing but cut off"
    assert all(b.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
               for b in rail._buttons.values()), "560 tall is too short for labels"
    assert all(b.accessibleName() or b.text() for b in rail._buttons.values())

    # The rail still works from the keyboard while it is icons only.
    rail.column.setFocus()
    before = rail.currentIndex()
    qtbot.keyClick(rail.column, KEY.Key_Down)
    gui_pump(app, 4)
    assert rail.currentIndex() != before
    qtbot.keyClick(rail.column, KEY.Key_Up)
    gui_pump(app, 4)
    assert rail.currentIndex() == before

    _front(app, window, qtbot, 1100, 760)
    assert _rail_labels_fit(window) == []
    assert all(b.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
               for b in rail._buttons.values()), "760 tall has room for every label"


# ---------------------------------------------------------------------------
# Helpers for the colour checks below
# ---------------------------------------------------------------------------

def _luminance(colour) -> float:
    """WCAG relative luminance of a `QColor` (0 black .. 1 white)."""
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(colour.red()) + 0.7152 * channel(colour.green()) + 0.0722 * channel(colour.blue())


def _contrast(a, b) -> float:
    """WCAG contrast ratio of two `QColor`s: 1 (none) .. 21 (black on white)."""
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


class _Theme:
    """Switch the real window to one theme for a test, and back afterwards -
    the same path the Appearance setting takes, stylesheet and pixmaps both."""

    def __init__(self, window, scheme: str) -> None:
        self.window, self.scheme = window, scheme

    def __enter__(self):
        self.before = self.window._theme_preference
        self.window._theme_preference = self.scheme
        self.window._apply_theme()
        return self

    def __exit__(self, *_exc) -> None:
        self.window._theme_preference = self.before
        self.window._apply_theme()


def _most_contrasting(image, rect, ground) -> float:
    """The strongest contrast any pixel inside `rect` has against `ground` -
    for a thin icon, that is the icon's own stroke."""
    from PySide6.QtGui import QColor
    best = 1.0
    for x in range(rect.left(), rect.right() + 1):
        for y in range(rect.top(), rect.bottom() + 1):
            # **Laid over the ground first.** A widget with a transparent
            # background grabs as transparent pixels, and read without their
            # alpha those are black - which against a light window is a
            # perfect 19 to 1 that nobody sees. Blending by alpha gives the
            # colour a person would actually be looking at.
            pixel = QColor.fromRgba(image.pixel(x, y))
            a = pixel.alphaF()
            seen = QColor(round(pixel.red() * a + ground.red() * (1 - a)),
                          round(pixel.green() * a + ground.green() * (1 - a)),
                          round(pixel.blue() * a + ground.blue() * (1 - a)))
            best = max(best, _contrast(seen, ground))
    return best


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_chosen_page_icon_can_be_seen_on_its_own_highlight(gui_mainwindow, qtbot, scheme):
    r"""**Before:** in the light theme the chosen page's icon was drawn white on
    the pale lavender highlight - 1.2 to 1, all but invisible - so the one icon
    that says "you are here" was the one you could not see. Grab:
    `before-1100x760/light/search-home.png`, the Search icon.

    Now it is clicked with the mouse, grabbed, and its stroke must reach 3 to 1
    against the highlight (WCAG's floor for a meaningful graphic) in both themes."""
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QColor
    from app.ui import theme

    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, 1100, 760)
    with _Theme(window, scheme):
        button = next(b for b in window.rail._buttons.values() if b.text() == "Files")
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        gui_pump(app, 6)
        assert window.rail.tabText(window.rail.currentIndex()) == "Files"
        image = button.grab().toImage()
        ground = QColor(theme.theme_colours()["rail_on_bg"])
        # The icon sits centred near the top of the button, above its label.
        size = button.iconSize()
        left = (button.width() - size.width()) // 2
        icon_box = QRect(QPoint(left, 4), size + size / 2).intersected(image.rect())
        assert _most_contrasting(image, icon_box, ground) >= 3.0, scheme
    window.rail.setCurrentIndex(0)


def _show_preview_with_a_result(app, window, qtbot) -> object:
    """Type a real search with the keyboard, walk into the results, and have
    the preview showing with its "Open" button ready. Returns the pane."""
    from PySide6.QtCore import Qt as _Qt

    window.rail.setCurrentIndex(window.rail.indexOf(window.search_view))
    view = window.search_view
    view.input.clear()
    view.input.setFocus()
    qtbot.keyClicks(view.input, "barnsley")
    qtbot.waitUntil(lambda: view.results._model.rowCount() > 0, timeout=4000)
    if view.preview.isHidden():
        qtbot.keyClick(view.input, KEY.Key_P,
                       _Qt.KeyboardModifier.ControlModifier | _Qt.KeyboardModifier.ShiftModifier)
        gui_pump(app, 4)
    view.results._list.setFocus()
    qtbot.keyClick(view.results._list, KEY.Key_Home)
    qtbot.waitUntil(lambda: view.preview.open_button.isEnabled(), timeout=4000)
    gui_pump(app, 4)
    return view.preview


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_open_button_label_is_readable_on_its_own_fill(gui_mainwindow, qtbot, scheme):
    r"""**Before:** in the dark theme the filled "Open" button was white text
    on the light lavender accent - 2.8 to 1, under the 4.5 WCAG asks of body
    text. Grab: `before-1100x760/dark/search-results.png`, bottom of the
    preview.

    Now a search is typed, a result chosen from the keyboard, the preview's
    button grabbed, and its label must reach 4.5 to 1 against the fill."""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor
    from app.ui import theme

    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, 1100, 760)
    with _Theme(window, scheme):
        pane = _show_preview_with_a_result(app, window, qtbot)
        button = pane.open_button
        assert button.isVisible() and button.text() == "Open"
        image = button.grab().toImage()
        fill = QColor(theme.theme_colours()["accent"])
        # The middle band of the button: the icon and the word, clear of the
        # rounded corners and the border.
        inside = QRect(6, image.height() // 3, image.width() - 12, image.height() // 3)
        assert _most_contrasting(image, inside, fill) >= 4.5, scheme
    window.search_view.input.clear()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_quiet_text_still_reaches_wcag_aa(gui_mainwindow, qtbot, scheme):
    r"""**Before:** the faint text colour - result counts, hints, table headers,
    "Nothing logged yet" - measured 3.2 to 3.9 to 1 on its grounds, under the
    4.5 WCAG AA asks of body text. Grab: `before-1100x760/light/files.png`,
    "15 file names indexed." under the box.

    Now the Files page is opened with the mouse, a search typed into its box,
    and the line under the box - painted in that colour - is grabbed. Its text
    must reach 4.5 to 1 against the window, in both themes."""
    from PySide6.QtGui import QColor
    from app.ui import theme

    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, 1100, 760)
    with _Theme(window, scheme):
        button = next(b for b in window.rail._buttons.values() if b.text() == "Files")
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        view = window.files_view
        qtbot.waitUntil(lambda: bool(view.summary.text()), timeout=4000)
        view.input.setFocus()
        qtbot.keyClicks(view.input, "leeds")
        gui_pump(app, 10)
        label = view.summary
        assert label.isVisible() and label.text()
        colours = theme.theme_colours()
        assert label.palette().color(label.foregroundRole()).name() == colours["text_faint"]
        image = label.grab().toImage()
        ground = QColor(colours["window"])
        assert _most_contrasting(image, image.rect(), ground) >= 4.5, (scheme, label.text())
        view.input.clear()
    window.rail.setCurrentIndex(0)


def test_an_empty_index_does_not_call_itself_up_to_date(gui_mainwindow, qtbot):
    r"""**Before:** on a first run, with nothing indexed, the rail's pill read
    "Up to date - 0 files" while the Search page beside it said "Nothing has
    been indexed yet". Grab: `after1-rail-760x560/light/search-home.png` (an
    empty store), the pill at the rail's foot.

    Now the Indexing page's count of 0 reaches the pill as it does in the app
    (the page's own signal), the pill reads "Nothing yet", a mouse click on it
    opens the Indexing page, and a count of documents brings "Up to date" back
    word for word."""
    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, 1100, 760)
    pill = window.rail.pill
    before = window._last_document_count
    try:
        window.indexing_view.totals_shown.emit(0)
        gui_pump(app, 4)
        assert pill.headline.text() == "Nothing yet"
        assert "Up to date" not in pill.accessibleName()
        qtbot.mouseClick(pill, Qt.MouseButton.LeftButton)
        gui_pump(app, 4)
        assert window.rail.tabText(window.rail.currentIndex()) == "Indexing"

        window.indexing_view.totals_shown.emit(3)
        gui_pump(app, 4)
        assert pill.headline.text() == "Up to date"
        assert pill.detail.text() == "3 files"
    finally:
        window.indexing_view.totals_shown.emit(before or 0)
        window.rail.setCurrentIndex(0)
        gui_pump(app, 4)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_suggested_searches_and_filter_chips_are_round_not_square(gui_mainwindow, qtbot, scheme):
    r"""**Before:** every "pill" had square corners - the four suggested
    searches under the empty box and each filter chip - because they asked for
    a 999px radius and Qt draws no rounding at all for a radius above half the
    height. Grab: `before-1100x760/light/search-home.png`, the four boxes
    under the search box.

    Now Escape empties the box (the home page, suggestions showing), and a
    typed `/type pdf` makes a chip; the top-left corner pixel of each must be
    the page behind it, not the pill's own fill."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor, QImage, QPainter, QRegion
    from PySide6.QtWidgets import QWidget
    from app.ui import theme

    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, 1100, 760)
    view = window.search_view

    def corner_is_rounded(widget) -> bool:
        # Render it over a colour that is in neither theme, so a corner that
        # is not painted shows that colour and nothing else.
        image = QImage(widget.size(), QImage.Format.Format_ARGB32)
        image.fill(QColor("#ff00ff"))
        painter = QPainter(image)
        # Children only - without this flag Qt first fills the whole rectangle
        # with the window colour, and every corner looks painted.
        widget.render(painter, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
        painter.end()
        return image.pixelColor(0, 0).name() == "#ff00ff"

    with _Theme(window, scheme):
        window.rail.setCurrentIndex(window.rail.indexOf(view))
        view.input.setFocus()
        qtbot.keyClicks(view.input, "leeds")
        qtbot.keyClick(view.input, KEY.Key_Escape)
        gui_pump(app, 6)
        assert view.input.text() == ""
        suggestions = [w for w in view.findChildren(QWidget)
                       if w.objectName() == "suggestion" and w.isVisible()]
        assert suggestions, "the home page shows suggested searches"
        assert all(corner_is_rounded(s) for s in suggestions), scheme

        qtbot.keyClicks(view.input, "leeds /type pdf")
        qtbot.waitUntil(lambda: any(w.objectName() == "chip" and w.isVisible()
                                    for w in view.findChildren(QWidget)), timeout=4000)
        chips = [w for w in view.findChildren(QWidget) if w.objectName() == "chip" and w.isVisible()]
        assert all(corner_is_rounded(c) for c in chips), scheme
        assert theme.RADIUS["radius_pill"] != "999px"
        qtbot.keyClick(view.input, KEY.Key_Escape)
        gui_pump(app, 4)


@pytest.mark.parametrize("size", [(1100, 760), (760, 560)])
def test_every_settings_category_name_is_shown_whole(gui_mainwindow, qtbot, size):
    r"""**Before:** the Settings sidebar was squeezed to its 120-pixel floor by
    the wide Search page and read "What's index" and "Storage & m"; on
    Appearance, at its 190 ceiling, "Storage & maintenance" was a few pixels
    too wide and a sideways scrollbar sat under the list. Grabs:
    `before-1100x760/light/settings-search.png` and `settings-appearance.png`.

    Now Ctrl+, opens Settings, each category is clicked with the mouse, and on
    every one the list is at least as wide as its widest name, with no
    sideways scrollbar - at a normal and a narrow window."""
    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, *size)
    window.activateWindow()
    qtbot.keyClick(window, KEY.Key_Comma, Qt.KeyboardModifier.ControlModifier)
    gui_pump(app, 6)
    assert window.rail.tabText(window.rail.currentIndex()) == "Settings"
    nav = window.settings_view._nav
    sidebar = nav.sidebar
    try:
        for row in range(sidebar.count()):
            item = sidebar.item(row)
            rect = sidebar.visualItemRect(item)
            qtbot.mouseClick(sidebar.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
            gui_pump(app, 6)
            assert nav.current_category() == item.text()
            widest = sidebar.sizeHintForColumn(0)
            assert sidebar.viewport().width() >= widest, (item.text(), sidebar.viewport().width(), widest)
            assert not sidebar.horizontalScrollBar().isVisible(), item.text()
    finally:
        nav.show_category("What's indexed", persist=False)
        window.rail.setCurrentIndex(0)


@pytest.mark.parametrize("size", [(1100, 760), (760, 560)])
def test_the_timeline_controls_wrap_instead_of_being_cut_or_piled_up(gui_mainwindow, qtbot, size):
    r"""**Before**, on Reports -> Browse your timeline: the report list was
    squeezed to about sixty pixels ("Digi", "The", "Brow"); the year box read
    "2015   (1"; "Whole year" read "Who...ear"; and on a narrow window every
    month button was squeezed to nothing and the two date boxes were drawn on
    top of each other. Grabs: `before-1100x760/light/timeline.png` and
    `before-760x560/light/timeline.png`.

    Now the timeline is opened with the mouse - since order 1i (2026-10-11)
    its own rail page, "Browse", no longer a row in Reports - and at both
    sizes: the year box is as wide as its longest year, every month button as
    wide as its word, and no two controls overlap. A month is then chosen with
    the mouse, as a person would."""
    from PySide6.QtCore import QRect
    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, *size)
    button = next(b for b in window.rail._buttons.values() if b.text() == "Browse")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    gui_pump(app, 4)
    timeline = window.timeline_view
    picker = timeline.picker
    timeline.refresh()
    qtbot.waitUntil(lambda: timeline._overview is not None and picker.year_box.count() > 0,
                    timeout=20000)
    picker.year_box.setCurrentIndex(0)
    gui_pump(app, 10)
    try:
        assert timeline.isVisible()
        assert picker.year_box.width() >= picker.year_box.sizeHint().width(), "the year is cut"
        for month in picker.month_buttons:
            assert month.isVisible() and month.width() >= month.sizeHint().width(), month.text()

        def box(widget) -> QRect:
            return QRect(widget.mapTo(picker, widget.rect().topLeft()), widget.size())

        placed = [*picker.month_buttons, picker.range_from, picker.range_to, picker.range_go]
        for i, first in enumerate(placed):
            for second in placed[i + 1:]:
                assert not box(first).intersects(box(second)), (first.objectName() or
                                                                getattr(first, "text", str)(),
                                                                getattr(second, "text", str)())
        # Every control is inside the picker, none pushed off its right edge.
        for widget in placed:
            assert picker.rect().contains(box(widget)), getattr(widget, "text", str)()

        enabled = next(b for b in picker.month_buttons[1:] if b.isEnabled())
        qtbot.mouseClick(enabled, Qt.MouseButton.LeftButton)
        gui_pump(app, 4)
        assert enabled.isChecked()
    finally:
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(5000)
        window.rail.setCurrentIndex(0)
        gui_pump(app, 4)


def test_the_fast_or_thoughtful_box_stays_the_size_of_its_words(gui_mainwindow, qtbot):
    r"""**Before:** once Chat was ready (its "Ollama is not running" notice
    hidden) the Fast/Thoughtful box stretched across the whole pane - one word,
    "Fast", in a bar as wide as the conversation, reading like a title rather
    than a choice. Grab: `before-1100x760/light/chat.png`, the top bar.

    Now Chat is opened with the mouse and told it is ready (what the window
    does when Ollama answers); the box stays about as wide as its longest
    choice, and the keyboard still changes it."""
    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, 1100, 760)
    button = next(b for b in window.rail._buttons.values() if b.text() == "Chat")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    gui_pump(app, 4)
    chat = window.chat_view
    chosen: list = []
    chat.speed_changed.connect(chosen.append)
    try:
        # Opening Chat asks, on a worker, whether Ollama is there; on this
        # machine it is not, and that answer must land before this test says
        # "ready", or it lands after and hides the box's new state again.
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(10_000)
        gui_pump(app, 10)
        chat.show_available(True)
        gui_pump(app, 6)
        speed = chat.speed
        assert not chat.notice.isVisible()
        assert speed.isVisible() and speed.isEnabled()
        assert speed.width() <= speed.sizeHint().width() + 8, (speed.width(), speed.sizeHint().width())
        speed.setFocus()
        qtbot.keyClick(speed, KEY.Key_Down)
        gui_pump(app, 2)
        assert chosen and chosen[-1] == "thoughtful"
        qtbot.keyClick(speed, KEY.Key_Up)
        gui_pump(app, 2)
        assert chosen[-1] == "fast"
        # With the notice back, the notice takes the room, as before.
        chat.show_available(False, "not running")
        gui_pump(app, 6)
        assert chat.notice.isVisible() and chat.notice.width() > speed.width()
    finally:
        chat.speed_changed.disconnect(chosen.append)
        window.rail.setCurrentIndex(0)


# ---------------------------------------------------------------------------
# Items the review proposed and did not build, built afterwards the same day.
# ---------------------------------------------------------------------------

def test_the_indexing_headline_agrees_with_the_document_count(gui_mainwindow, qtbot):
    r"""**Before** (review finding 10): with no run this session, Indexing ›
    Status read "Nothing indexed yet." directly above "Documents 17" - the
    starting headline, which only a run ever replaced. Grab:
    `after-final-1100x760/light/indexing-status.png`.

    Now a mouse click on the rail's pill opens the page, its totals are read as
    they always were, and the headline says the Search page's own sentence for
    that count. A run's own headline is never overwritten by a later count."""
    from tests.unit.conftest import GUI_DOCUMENTS
    from app.ui.widgets.indexing_layout import paint_totals

    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, 1100, 760)
    view = window.indexing_view
    count = len(GUI_DOCUMENTS)
    try:
        qtbot.mouseClick(window.rail.pill, Qt.MouseButton.LeftButton)
        gui_pump(app, 4)
        assert window.rail.tabText(window.rail.currentIndex()) == "Indexing"
        qtbot.waitUntil(
            lambda: view.headline.text() == f"{count:,} documents ready to search.",
            timeout=4000)
        assert "Nothing indexed yet" not in view.headline.text()

        # Something a run said is more specific than a count, and stays.
        view.headline.setText("Finished: 3 indexed, 0 skipped, 0 removed")
        paint_totals(view, dict(view._totals_payload))
        assert view.headline.text() == "Finished: 3 indexed, 0 skipped, 0 removed"
    finally:
        view.headline.setText(getattr(view, "_resting_headline", "Nothing indexed yet."))
        window.rail.setCurrentIndex(0)
        gui_pump(app, 4)


@pytest.mark.parametrize("size", [(1100, 760), (760, 560)])
def test_the_suggested_searches_are_shown_whole_at_any_width(gui_mainwindow, qtbot, size):
    r"""**Before** (review finding 14): at 760 wide the four suggested searches
    under the empty box were squeezed into one row and cut in the middle -
    "the pdf …e boiler", "photos fr… District". Grab:
    `after-final-760x560/light/search-home.png`.

    Now Escape empties the box, and every suggestion is at least as wide as
    its own words, none overlaps another or runs off the page, and a click
    on one still searches for it."""
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QWidget

    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, *size)
    view = window.search_view
    try:
        window.rail.setCurrentIndex(window.rail.indexOf(view))
        view.input.setFocus()
        qtbot.keyClicks(view.input, "leeds")
        qtbot.keyClick(view.input, KEY.Key_Escape)
        gui_pump(app, 8)
        pills = [w for w in view.findChildren(QWidget)
                 if w.objectName() == "suggestion" and w.isVisible()]
        assert len(pills) == 4, "the home page shows its four suggested searches"
        cut = [f"{p.text()}: {p.width()} < {p.sizeHint().width()}"
               for p in pills if p.width() < p.sizeHint().width()]
        assert cut == [], size
        page = QRect(view.mapToGlobal(view.rect().topLeft()), view.size())
        boxes = [QRect(p.mapToGlobal(p.rect().topLeft()), p.size()) for p in pills]
        assert all(page.contains(b) for b in boxes), size
        assert not any(a.intersects(b) for i, a in enumerate(boxes) for b in boxes[i + 1:]), size

        qtbot.mouseClick(pills[0], Qt.MouseButton.LeftButton)
        gui_pump(app, 4)
        assert view.input.text().strip() != ""
    finally:
        view.input.clear()
        qtbot.keyClick(view.input, KEY.Key_Escape)
        gui_pump(app, 4)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_report_names_and_the_activity_card_sit_at_the_same_inset_as_the_rest(
        gui_mainwindow, qtbot, scheme):
    r"""**Before** (review finding 13): each report name on Reports sat behind
    a ~36px blank indent, because its key was stored as `setData(1, key)` and
    role 1 is the item's *icon* - the list reserved an icon's width for a
    string it could not draw. And on Settings › Storage & maintenance, the
    "Recent activity" card's caption and log box ran hard against the card's
    edge. Grabs: `after-final-1100x760/light/reports.png` and
    `settings-storage.png`.

    Now the Reports list is opened with the mouse, no entry carries an icon,
    the first painted pixel of an unselected name is within a few pixels of the
    row's left edge, and a click on "The Space Report" still opens it (the key
    is read back from its new role). Then Settings is opened with Ctrl+, and
    Storage & maintenance clicked: the card's caption and log box are inset
    from its edge like every other card's contents."""
    from PySide6.QtGui import QColor
    from app.ui.widgets.timeline_host import REPORT_KEY

    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, 1100, 760)
    reports = window.reports_view
    with _Theme(window, scheme):
        try:
            button = next(b for b in window.rail._buttons.values() if b.text() == "Reports")
            qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
            gui_pump(app, 4)
            names = reports.list
            assert REPORT_KEY == Qt.ItemDataRole.UserRole
            # *Corrected 4 October 2026:* the owner asked for an icon on each
            # report ("there are no icons for the reports on the report page"),
            # so each entry now carries one on purpose - the fault this
            # guarded was a blank icon slot holding a string, which stays fixed
            # (the key is still read back from `UserRole`, asserted above).
            assert all(names.item(r).data(Qt.ItemDataRole.DecorationRole) is not None
                       for r in range(names.count()))
            names.setCurrentRow(0)
            gui_pump(app, 4)
            space = next(r for r in range(names.count())
                         if names.item(r).data(REPORT_KEY) == "space")
            rect = names.visualItemRect(names.item(space))
            image = names.viewport().grab().toImage()
            ground = image.pixelColor(rect.left() + 1, rect.top() + 1)

            def differs(c: QColor) -> bool:
                return max(abs(c.red() - ground.red()), abs(c.green() - ground.green()),
                           abs(c.blue() - ground.blue())) > 60

            first = next(x for x in range(rect.left(), rect.right())
                         if any(differs(image.pixelColor(x, y))
                                for y in range(rect.top(), rect.bottom())))
            assert first - rect.left() <= 16, (scheme, first - rect.left())

            qtbot.mouseClick(names.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
            gui_pump(app, 6)
            assert reports._selected_key() == "space"

            qtbot.keyClick(window, KEY.Key_Comma, Qt.KeyboardModifier.ControlModifier)
            gui_pump(app, 6)
            nav = window.settings_view._nav
            sidebar = nav.sidebar
            item = next(sidebar.item(r) for r in range(sidebar.count())
                        if sidebar.item(r).text() == "Storage & maintenance")
            qtbot.mouseClick(sidebar.viewport(), Qt.MouseButton.LeftButton,
                             pos=sidebar.visualItemRect(item).center())
            gui_pump(app, 6)
            card = window.settings_view.debug_pane
            assert card.isVisible()
            caption = card.layout().itemAt(0).widget()
            for inside in (caption, card.view):
                assert inside.geometry().left() >= 6, (scheme, inside.geometry())
                assert card.width() - inside.geometry().right() >= 6, (scheme, inside.geometry())
        finally:
            window.settings_view._nav.show_category("What's indexed", persist=False)
            window.rail.setCurrentIndex(0)
            gui_pump(app, 4)


def test_files_opens_with_names_at_their_own_width_and_a_drag_is_still_the_one_kept(
        gui_mainwindow, qtbot):
    r"""**Before** (review finding 3): the Files table opened with every column
    at its heading's width - Name 63px over names three times that, "12 Mar
    ..." cut short - and Folder taking the rest, because the table's one fit
    ran in `__init__`, before its rows existed. Grab:
    `after-final-1100x760/light/files.png` (Mail the same, `mail.png`).

    Now the Files page is opened with the mouse; with nothing saved, Name is
    as wide as its names (up to the 40% cap) and **nothing was written** - the
    fit is not a choice. Then a column dragged the way a person does is the
    width that is saved, as always."""
    from app.ui.view_options import _available_width, column_cap

    app, window, *_ = gui_mainwindow
    _front(app, window, qtbot, 1100, 760)
    files = window.files_view
    table = files.results
    try:
        button = next(b for b in window.rail._buttons.values() if b.text() == "Files")
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: table.rowCount() > 0, timeout=4000)
        gui_pump(app, 6)
        assert dict(files.view_button.prefs.widths) == {}, "nothing saved going in"
        content = table.sizeHintForColumn(0)
        cap = column_cap(_available_width(table))
        heading = table.horizontalHeader().sectionSizeHint(0)
        assert content > heading, "the fixture's names are wider than 'Name'"
        assert table.columnWidth(0) >= min(content, cap) - 1, (
            table.columnWidth(0), content, cap)

        qtbot.wait(1400)                         # two of the watcher's looks
        assert dict(files.view_button.prefs.widths) == {}, (
            "a fitted width was saved as though somebody had dragged it")

        table.horizontalHeader().resizeSection(0, 222)
        qtbot.waitUntil(lambda: dict(files.view_button.prefs.widths).get("name") == 222,
                        timeout=4000)
    finally:
        files.view_button.refit()
        window.rail.setCurrentIndex(0)
        gui_pump(app, 4)
