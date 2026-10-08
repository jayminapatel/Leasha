r"""The section list on Settings and Indexing stays put; only the right scrolls.

Layer: L5

Owner, 2026-10-08: *"The scrolling is inconsistent between indexing and
settings .. the left section headers scroll with the right ... the left
sections should not scroll with the right sections"*.

**What was wrong, measured rather than guessed.** `shell.py` wraps the whole
Settings page in one `QScrollArea` (`wrap_if_needed(view, scroll=True)`), so
the category sidebar was *inside* the thing that scrolled: on Models & AI at
1280x800 the list's top moved from y=49 to y=-510, off the window. And the
list, which has nothing of its own to scroll, passed every wheel turn up to
that same area - so turning the wheel over the section names scrolled the
settings. Indexing never did either, because each of its shelves carries its
own scroll area beside the sidebar.

Now `CategoryNav` holds the rule: the content side is one scroll area
(`nav.scroll`) and the sidebar is its sibling. Settings uses it; Indexing's
shelves keep their own areas (`CategoryNav(scroll=False)`), which is the same
shape - one scroll area per page, the sidebar outside it.

The two pages are built once per module, as `shell.py` places them: Settings
inside the wrapper the shell still gives it, Indexing bare. Building and
tearing down whole pages repeatedly in one process is a harness problem, not
an application one (see `test_settings_layout.py`).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea, QWidget  # noqa: E402

pytestmark = pytest.mark.gui

ENV = """\
DATA_PATH={d}
PROJECT_PATH={d}
LOG_PATH={d}/logs

EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
RERANK_ENABLED=false

OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mistral

MIN_FREE_GB=1
REQUIRED_FREE_GB=1
"""

#: The size the owner's report was reproduced at, and the smallest window this
#: project treats as sensible (`test_pages_reorg.SIZES`).
SIZES = ((1280, 800), (1024, 600))

#: The Settings category that is tallest with an empty index - the one the
#: fault was measured on.
TALL_SETTINGS = "Models & AI"
TALL_INDEXING = "Tuning"


def _process(app, n: int = 6) -> None:
    for _ in range(n):
        app.processEvents()


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    """`(app, settings_view, settings_top, indexing_view, indexing_top)`.

    `*_top` is what `shell.py` puts in the rail: the Settings page inside the
    scroll area the shell wraps it in, the Indexing page as it is.
    """
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.ui.indexing_view import IndexingView
    from app.ui.settings_view import SettingsView
    from app.ui.theme import stylesheet
    from app.ui.widgets.scroll import wrap_if_needed

    root = tmp_path_factory.mktemp("fixed_nav")
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)
    app = QApplication.instance() or QApplication([])
    store = SqliteStore(settings.fts_db).connect()

    settings_view = SettingsView(settings, store)
    settings_view.setStyleSheet(stylesheet("light"))
    settings_top = wrap_if_needed(settings_view, scroll=True)     # as shell.py does
    indexing_view = IndexingView()
    indexing_view.setStyleSheet(stylesheet("light"))
    indexing_top = wrap_if_needed(indexing_view, scroll=False)    # as shell.py does
    for top in (settings_top, indexing_top):
        top.resize(*SIZES[0])
        top.show()
    _process(app)

    yield app, settings_view, settings_top, indexing_view, indexing_top

    for top in (settings_top, indexing_top):
        top.hide()
    store.close()


def _both(pages):
    app, settings_view, settings_top, indexing_view, indexing_top = pages
    return app, ((settings_view, settings_top, TALL_SETTINGS),
                 (indexing_view, indexing_top, TALL_INDEXING))


def _scroll_areas_holding(widget: QWidget, stop: QWidget) -> list[QScrollArea]:
    """Every `QScrollArea` from `widget` up to and including `stop`'s parent
    chain end - the areas whose scrolling would move `widget`."""
    found = []
    w = widget
    while w is not None:
        if isinstance(w, QScrollArea) and w is not widget:
            found.append(w)
        if w is stop:
            break
        w = w.parentWidget()
    return found


def _content_area(view, category: str) -> QScrollArea:
    """The one scroll area that moves `category`'s page."""
    nav = view._nav
    page = nav.page(category)
    if isinstance(page, QScrollArea):
        return page
    assert nav.scroll is not None, f"{category}: no scroll area moves this page"
    return nav.scroll


def _wheel(widget: QWidget, dy: int = -120) -> QWidget | None:
    """A wheel turn over `widget`, delivered the way `QApplication::notify`
    delivers a real one: to the widget under the pointer, then to each parent
    until one accepts it. A synthetic `sendEvent` is never propagated by Qt -
    a real wheel is - so the walk is done here. Returns whoever took it."""
    target = widget.viewport() if hasattr(widget, "viewport") else widget
    w = target
    while w is not None:
        pos = QPointF(10, 10)
        event = QWheelEvent(pos, QPointF(w.mapToGlobal(pos.toPoint())), QPoint(0, 0),
                            QPoint(0, dy), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                            False)
        event.ignore()
        QApplication.sendEvent(w, event)
        if event.isAccepted():
            return w
        if w.isWindow():
            return None
        w = w.parentWidget()
    return None


# ---------------------------------------------------------------------------
# Structure: the sidebar is never inside what scrolls the content
# ---------------------------------------------------------------------------

def test_no_scroll_area_that_moves_the_content_holds_the_sidebar(pages):
    app, rows = _both(pages)
    for view, top, _tall in rows:
        nav = view._nav
        for name in nav.category_names():
            page = nav.page(name)
            areas = _scroll_areas_holding(page, top)
            if isinstance(page, QScrollArea):
                areas.insert(0, page)
            for area in areas:
                if area.verticalScrollBar().maximum() == 0 and not nav.isAncestorOf(area):
                    continue        # the shell's inert wrapper - see the test below
                assert not area.isAncestorOf(nav.sidebar), (
                    f"{type(view).__name__}/{name}: the sidebar is inside a scroll "
                    "area that moves the page, so it scrolls away with it")


def test_each_page_is_moved_by_exactly_one_scroll_area_inside_the_nav(pages):
    """One on the right, never two nested. Indexing's Status shelf is the one
    exception, and has none: its skipped-files list scrolls its own rows, and
    wrapping the shelf again would be the two-scrollbars fault
    `widgets/scroll.py` warns about."""
    app, rows = _both(pages)
    for view, top, _tall in rows:
        nav = view._nav
        for name in nav.category_names():
            page = nav.page(name)
            inside = _scroll_areas_holding(page, nav)
            if isinstance(page, QScrollArea):
                inside.insert(0, page)
            expected = 0 if name == "Status" else 1
            assert len(inside) == expected, (
                f"{type(view).__name__}/{name}: {len(inside)} scroll areas move "
                f"this page inside the nav, expected {expected}")


def test_settings_puts_its_scroll_area_in_the_nav_and_indexing_on_its_shelves(pages):
    app, settings_view, _st, indexing_view, _it = pages
    assert isinstance(settings_view._nav.scroll, QScrollArea)
    assert settings_view._nav.scroll.objectName() == "categoryScroll"
    assert indexing_view._nav.scroll is None, (
        "Indexing's shelves scroll themselves; a nav scroll area round them "
        "would put a page inside two")


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_nothing_that_holds_the_sidebar_can_scroll(pages, size):
    """The wrapper `shell.py` still puts round Settings is inert: the page now
    asks for no more height than a window gives it, so that area has nothing
    to scroll and the list it holds cannot move."""
    app, rows = _both(pages)
    for view, top, tall in rows:
        top.resize(*size)
        view._nav.show_category(tall)
        _process(app)
        for area in _scroll_areas_holding(view._nav.sidebar, top):
            if area is view._nav.sidebar:
                continue
            assert area.verticalScrollBar().maximum() == 0, (
                f"{type(view).__name__} at {size}: a scroll area holding the "
                f"sidebar can scroll {area.verticalScrollBar().maximum()} pixels")
    for _view, top, _tall in rows:
        top.resize(*SIZES[0])
    _process(app)


# ---------------------------------------------------------------------------
# Behaviour: scroll the right, the left does not move
# ---------------------------------------------------------------------------

def test_scrolling_the_content_leaves_the_sidebar_where_it_was(pages):
    app, rows = _both(pages)
    for view, top, tall in rows:
        nav = view._nav
        nav.show_category(tall)
        _process(app)
        area = _content_area(view, tall)
        bar = area.verticalScrollBar()
        assert bar.maximum() > 0, (
            f"{type(view).__name__}/{tall} fits the window; this test would prove nothing")
        before = (nav.sidebar.mapTo(top, QPoint(0, 0)), nav.sidebar.size())
        bar.setValue(bar.maximum())
        _process(app)
        after = (nav.sidebar.mapTo(top, QPoint(0, 0)), nav.sidebar.size())
        assert after == before, (
            f"{type(view).__name__}: the sidebar moved from {before} to {after} "
            "when only the content was scrolled")
        bar.setValue(0)
    _process(app)


def test_the_sidebar_is_the_full_height_of_the_nav(pages):
    app, rows = _both(pages)
    for view, top, tall in rows:
        nav = view._nav
        nav.show_category(tall)
        _process(app)
        assert nav.sidebar.height() == nav.height(), (
            f"{type(view).__name__}: sidebar {nav.sidebar.height()} px in a nav "
            f"{nav.height()} px tall")
        assert nav.sidebar.mapTo(top, QPoint(0, 0)).y() >= 0, "the sidebar starts off-screen"


def test_a_wheel_over_the_sidebar_never_scrolls_the_content(pages):
    """Before: on Settings this wheel reached the shell's scroll area and
    scrolled the page under the list."""
    app, rows = _both(pages)
    for view, top, tall in rows:
        nav = view._nav
        nav.show_category(tall)
        _process(app)
        watched = [_content_area(view, tall)] + [
            a for a in _scroll_areas_holding(nav.sidebar, top) if a is not nav.sidebar]
        values = [a.verticalScrollBar().value() for a in watched]
        for dy in (-120, -120, 120):
            _wheel(nav.sidebar, dy)
            _process(app)
        assert [a.verticalScrollBar().value() for a in watched] == values, (
            f"{type(view).__name__}: turning the wheel over the section list "
            "scrolled the page")


def test_choosing_a_category_opens_it_at_its_top(pages):
    """The Settings scroll area is shared by every category, so a choice
    starts at the top rather than wherever the last one had been left."""
    app, settings_view, _st, _iv, _it = pages
    nav = settings_view._nav
    nav.show_category(TALL_SETTINGS)
    _process(app)
    nav.scroll.verticalScrollBar().setValue(nav.scroll.verticalScrollBar().maximum())
    _process(app)
    other = next(n for n in nav.category_names() if n != TALL_SETTINGS)
    nav.show_category(other)
    _process(app)
    assert nav.scroll.verticalScrollBar().value() == 0
    assert nav.page(other).isVisibleTo(settings_view)


def test_the_filter_box_stays_above_the_scroll_and_still_filters(pages):
    """The filter box is outside the scroll area, so it is always in view, and
    filtering still shows every matching category inside it."""
    from app.ui.settings_view import CATEGORY_SEARCH, CATEGORY_WHATS_INDEXED

    app, settings_view, settings_top, _iv, _it = pages
    nav = settings_view._nav
    assert not nav.scroll.isAncestorOf(settings_view.filter_box)
    settings_view.filter_box.setText("rerank")
    _process(app)
    try:
        assert nav.page(CATEGORY_SEARCH).isVisibleTo(settings_view)
        assert not nav.page(CATEGORY_WHATS_INDEXED).isVisibleTo(settings_view)
        assert nav.scroll.isAncestorOf(nav.page(CATEGORY_SEARCH))
        y = settings_view.filter_box.mapTo(settings_top, QPoint(0, 0)).y()
        bar = nav.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        _process(app)
        assert settings_view.filter_box.mapTo(settings_top, QPoint(0, 0)).y() == y
    finally:
        settings_view.filter_box.setText("")
        _process(app)


def test_advanced_folds_open_inside_the_scroll_area(pages):
    """Opening "Advanced" makes the page taller; the extra height scrolls on
    the right and the sidebar does not move."""
    app, settings_view, settings_top, _iv, _it = pages
    nav = settings_view._nav
    folds = settings_view._folds
    assert folds, "no Advanced folds to check"
    nav.show_category(TALL_SETTINGS)
    _process(app)
    fold = next(f for f in folds if f.isVisibleTo(settings_view))
    assert nav.scroll.isAncestorOf(fold)
    closed = nav.scroll.verticalScrollBar().maximum()
    sidebar_at = nav.sidebar.mapTo(settings_top, QPoint(0, 0))
    try:
        for f in folds:
            f.set_open(True)
        _process(app)
        assert nav.scroll.verticalScrollBar().maximum() > closed
        assert nav.sidebar.mapTo(settings_top, QPoint(0, 0)) == sidebar_at
    finally:
        for f in folds:
            f.set_open(False)
        _process(app)
