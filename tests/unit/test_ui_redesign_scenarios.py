r"""UI Redesign (work order 202626160950) - the scenario half of section 9.

Order 0m's harness (`tests/unit/conftest.py`'s `gui_mainwindow`): the real
`MainWindow`, a real store, keyword-only search - driven with real keystrokes
and real mouse clicks, never by emitting a signal or calling a slot.

    9b  every shortcut lands on its page; the first run opens Search; the
        last page survives a relaunch
    9g  `sizeHint` and `paint` agree for every kind of row, in both densities
        and at three text sizes
    9h  the Search page, start to finish, without the mouse

**Two windows live in this module**: the shared one from `gui_mainwindow`, and
the pair `relaunch` builds over one store (a relaunch is exactly that). Neither
is closed - see `tests/unit/conftest.py` for why.

Bugs these scenarios found, each with its regression test below:
- the text-size preference reached the Files/Mail/Code tables and never the
  Search results (`test_the_text_size_preference_reaches_the_results_list`);
- a skeleton row was a line taller than a real row in compact density
  (`test_a_skeleton_row_is_as_tall_as_the_real_row_it_stands_in_for`).
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
np = pytest.importorskip("numpy")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QStandardItem, QStandardItemModel  # noqa: E402
from PySide6.QtWidgets import QStyleOptionViewItem  # noqa: E402

from tests.unit.conftest import gui_pump  # noqa: E402

pytestmark = pytest.mark.gui

MOD = Qt.KeyboardModifier
KEY = Qt.Key


@pytest.fixture(scope="module", autouse=True)
def _leave_no_window_showing(gui_mainwindow):
    """These scenarios `show()` their windows (shortcuts and focus need an
    active one) and never close them. A window left showing at (0, 0) takes the
    mouse clicks `QTest` aims at the next module's hidden one, so hide them."""
    yield
    gui_mainwindow[1].hide()


def _key(qtbot, app, key, mod=MOD.NoModifier, *, text: str = "") -> None:
    """A keystroke to whatever has focus, as a person's would go."""
    target = app.focusWidget() or app.activeWindow()
    if text:
        qtbot.keyClicks(target, text)
    else:
        qtbot.keyClick(target, key, mod)
    gui_pump(app, 2)


def _front(app, window, qtbot) -> None:
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    window.raise_()
    gui_pump(app)


def _page(window) -> str:
    return window.rail.tabText(window.rail.currentIndex())


def _goto(window, title: str) -> None:
    for index in range(window.rail.count()):
        if window.rail.tabText(index) == title:
            window.rail.setCurrentIndex(index)
            return
    raise AssertionError(f"no page called {title!r}")


# ---------------------------------------------------------------------------
# 9b - first run, and a relaunch
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def relaunch(tmp_path_factory):
    """`(app, launch, store)`: `launch()` builds a window over the one store."""
    from PySide6.QtWidgets import QApplication

    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from app.ui.shell import MainWindow
    from tests.unit.test_window_opens import ENV, _Engine

    root = tmp_path_factory.mktemp("relaunch")
    env = root / ".env"
    env.write_text(ENV.format(d=root.as_posix()), encoding="utf-8")
    settings = load_settings(env)
    app = QApplication.instance() or QApplication([])
    store = SqliteStore(settings.fts_db).connect()
    vectors = VectorStore(settings.vector_path, dim=settings.embed_dim).connect()
    kept: list = []

    def launch():
        window = MainWindow(settings, store, vectors, _Engine(store), debug=False)
        kept.append(window)                       # never torn down - see above
        window.show()
        return window

    yield app, launch, store
    for window in kept:
        window.hide()
    store.close()
    vectors.close()


def _settled(qtbot, window) -> None:
    """Construction has finished: the deferred views exist and the
    post-construction work (`_start_background_work`) has run."""
    qtbot.waitUntil(lambda: hasattr(window, "mail_view") and hasattr(window, "code_view"),
                    timeout=5000)
    qtbot.wait(150)


def test_the_first_run_opens_on_search_with_the_box_ready(relaunch, qtbot):
    app, launch, store = relaunch
    assert store.get_state("ui:page", "") == "", "this scenario needs a store nobody has used"
    window = launch()
    _settled(qtbot, window)
    assert _page(window) == "Search"
    assert window.rail.currentIndex() == 0
    assert window.search_view.home.isVisibleTo(window.search_view)


def test_the_last_page_survives_a_relaunch(relaunch, qtbot):
    from PySide6.QtWidgets import QApplication
    app, launch, store = relaunch
    first = launch()
    _settled(qtbot, first)
    first.activateWindow()
    button = first.rail._buttons[first._tab_index[first.files_view]]
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    gui_pump(app)
    assert _page(first) == "Files"
    # The save is queued on the ordered state writer since the page-switch fix
    # (bug 3a) - wait for it, as the window's own close does before its store goes.
    from app.ui.state_writes import pool
    assert pool().waitForDone(5000)
    assert store.get_state("ui:page", "") == "Files"

    second = launch()                     # the same store: a second launch
    _settled(qtbot, second)
    assert _page(second) == "Files"
    assert QApplication.instance() is app


# ---------------------------------------------------------------------------
# 9b - the rail, by mouse, once per switch
# ---------------------------------------------------------------------------

def test_a_click_on_every_rail_button_switches_once_and_only_once(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    rail = window.rail
    _goto(window, "Search")
    seen: list[int] = []
    rail.currentChanged.connect(seen.append)
    try:
        for index, button in sorted(rail._buttons.items()):
            if index == rail.currentIndex():
                continue
            seen.clear()
            qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
            gui_pump(app)
            assert seen == [index], f"{rail.tabText(index)}: {seen}"
            assert rail.currentIndex() == index
            assert button.isChecked()
        # Pressing the button of the page already showing switches nothing.
        seen.clear()
        current = rail._buttons[rail.currentIndex()]
        qtbot.mouseClick(current, Qt.MouseButton.LeftButton)
        gui_pump(app)
        assert seen == []
    finally:
        rail.currentChanged.disconnect(seen.append)
        _goto(window, "Search")


def test_the_pill_opens_indexing_when_clicked_with_the_mouse(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    qtbot.mouseClick(window.rail.pill, Qt.MouseButton.LeftButton)
    gui_pump(app)
    assert _page(window) == "Indexing"
    _goto(window, "Search")


def test_the_pill_opens_indexing_from_the_keyboard(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    window.rail.pill.setFocus()
    gui_pump(app)
    assert app.focusWidget() is window.rail.pill
    _key(qtbot, app, KEY.Key_Return)
    assert _page(window) == "Indexing"
    _goto(window, "Search")


def test_the_rail_is_walked_with_the_arrow_keys_and_stops_at_its_ends(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    rail = window.rail
    _goto(window, "Search")
    rail.column.setFocus()
    gui_pump(app)
    order = sorted(rail._buttons)
    visited = [rail.currentIndex()]
    for _ in range(len(order) + 2):                       # past the end on purpose
        _key(qtbot, app, KEY.Key_Down)
        visited.append(rail.currentIndex())
    walked = [v for n, v in enumerate(visited) if n == 0 or v != visited[n - 1]]
    assert walked == order, f"Down visited {walked}, the rail has {order}"
    for _ in range(len(order) + 2):
        _key(qtbot, app, KEY.Key_Up)
    assert rail.currentIndex() == order[0]


def test_tab_reaches_the_rail_from_the_page_and_the_pill(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    window.search_view.focus()
    gui_pump(app)
    reached = set()
    for _ in range(80):
        _key(qtbot, app, KEY.Key_Tab)
        focus = app.focusWidget()
        if focus is window.rail.column:
            reached.add("column")
        if focus is window.rail.pill:
            reached.add("pill")
        if reached == {"column", "pill"}:
            break
    assert reached == {"column", "pill"}, f"Tab only ever reached {reached}"


# ---------------------------------------------------------------------------
# 9b - every existing shortcut, pressed, lands on its page
# ---------------------------------------------------------------------------

SHORTCUTS = [
    ("Ctrl+,", KEY.Key_Comma, MOD.ControlModifier, "Settings"),
    ("Ctrl+I", KEY.Key_I, MOD.ControlModifier, "Indexing"),
    ("Ctrl+P", KEY.Key_P, MOD.ControlModifier, "Files"),
    ("Ctrl+M", KEY.Key_M, MOD.ControlModifier, "Mail"),
    ("Ctrl+E", KEY.Key_E, MOD.ControlModifier, "Code"),
    ("Ctrl+K", KEY.Key_K, MOD.ControlModifier, "Search"),
    ("Ctrl+F", KEY.Key_F, MOD.ControlModifier, "Search"),
]


@pytest.mark.parametrize("label,key,mod,page", SHORTCUTS, ids=[s[0] for s in SHORTCUTS])
def test_each_shortcut_lands_on_its_page(gui_mainwindow, qtbot, label, key, mod, page):
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    # From somewhere else, so "already there" cannot pass it.
    _goto(window, "Reports" if page != "Reports" else "Search")
    assert _page(window) != page
    _key(qtbot, app, key, mod)
    assert _page(window) == page, f"{label} landed on {_page(window)}"
    _goto(window, "Search")


def test_the_shortcuts_that_jump_to_a_box_leave_the_cursor_in_it(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    for key, page, view in ((KEY.Key_K, "Search", window.search_view),
                            (KEY.Key_P, "Files", window.files_view),
                            (KEY.Key_M, "Mail", window.mail_view),
                            (KEY.Key_E, "Code", window.code_view)):
        _goto(window, "Reports")
        _key(qtbot, app, key, MOD.ControlModifier)
        assert _page(window) == page
        focus = app.focusWidget()
        assert focus is not None and (view is focus or view.isAncestorOf(focus)), \
            f"{page}: focus is {focus!r}"
    _goto(window, "Search")


def test_ctrl_shift_p_shows_and_hides_the_preview_on_the_page_in_front(gui_mainwindow, quokkas, qtbot):
    """**Regression, and the first half is the bug**: pressed twice on the home
    state (where the whole results pane is out of sight) the second press found
    `isVisible()` already False, decided there was nothing to hide, and left the
    pane to appear beside the first results with the preference off."""
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    view = window.search_view
    view.input.clear()
    _preview_off(qtbot, app, view)
    gui_pump(app)
    assert view.preview.isHidden() and not view.view_button.prefs.preview
    _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)
    assert not view.preview.isHidden() and view.view_button.prefs.preview
    _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)
    assert view.preview.isHidden() and not view.view_button.prefs.preview
    _key(qtbot, app, None, text="quokka")
    qtbot.waitUntil(lambda: _rows(view) >= 3, timeout=4000)
    assert not view.preview.isVisible(), "the preview came back with the results"

    # With results on screen the same key shows and hides it for real.
    _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)
    assert view.preview.isVisible()
    _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)
    assert not view.preview.isVisible()
    view.input.clear()
    # On a page with no list it does nothing, and says nothing.
    _goto(window, "Settings")
    _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)
    assert _page(window) == "Settings" and not view.view_button.prefs.preview
    _goto(window, "Search")


def test_f5_with_nothing_to_index_goes_to_settings_and_says_why(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    assert window.settings_view.current_roots() == [], "this scenario needs no folders chosen"
    _goto(window, "Search")
    window.toast.clear()
    _key(qtbot, app, KEY.Key_F5)
    assert _page(window) == "Settings"
    assert window.toast.current_text(), "F5 with no folders must say why nothing started"
    window.toast.clear()
    _goto(window, "Search")


def test_escape_from_anywhere_empties_the_search_box(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    window.search_view.input.setText("barnsley")
    window.rail.column.setFocus()                     # focus is not in the box
    gui_pump(app)
    _key(qtbot, app, KEY.Key_Escape)
    assert window.search_view.input.text() == ""


# ---------------------------------------------------------------------------
# 9h - the whole Search page, keyboard only
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def quokkas(gui_mainwindow):
    """Three documents that all say "quokka": one matched twice (a multi-match
    group), one code file, one long passage - so a search returns every kind of
    row the delegate paints."""
    app, window, store, engine = gui_mainwindow
    filler = ("the colony was counted at dusk along the north shore while the "
              "survey team logged every sighting and noted the weather for each one ")
    docs = (
        ("quokka-notes.txt", "txt", [f"first pass quokka {filler * 2}", f"second pass quokka {filler * 2}"]),
        ("quokka_tracker.py", "py", [f"# quokka tracker {filler * 2}"]),
        ("quokka-brief.pdf", "pdf", [f"a quokka briefing {filler * 3}"]),
    )
    for name, ext, chunks in docs:
        file_id = store.upsert_file(f"C:/work/{name}", parent_dir="C:/work", ext=ext,
                                    size_bytes=1, mtime_ns=1_700_000_000_000_000_000,
                                    status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": i, "text": t} for i, t in enumerate(chunks)])
    return docs


def _rows(view) -> int:
    """Rows of results on screen - **not** the grey skeleton bars.

    A search that has not answered within 300ms puts four `Skeleton` rows into
    the same model (§6d). Counting them made "wait for three rows" pass while
    the list was still only placeholders on a slow machine (Windows CI), so the
    next Down selected nothing and `next(...)` over the rows found no result.
    """
    from app.ui.result_delegate import ROLE_PAYLOAD, Skeleton

    model = view.results._model
    return sum(1 for n in range(model.rowCount())
               if not isinstance(model.item(n).data(ROLE_PAYLOAD), Skeleton))


def test_the_search_page_start_to_finish_with_the_keyboard_alone(
        gui_mainwindow, quokkas, qtbot, monkeypatch):
    import app.ui.shell as shell
    opened: list[tuple] = []
    monkeypatch.setattr(shell, "open_row_async",      # the one route (2026-10-04)
                        lambda store, row, reveal=False, **_k: opened.append((row.path, reveal)))

    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    view = window.search_view
    view.input.clear()
    _preview_off(qtbot, app, view)
    _goto(window, "Reports")

    # 1. Ctrl+K: from anywhere to the box, cursor ready.
    _key(qtbot, app, KEY.Key_K, MOD.ControlModifier)
    assert _page(window) == "Search" and app.focusWidget() is view.input

    # 2. Type. The home state gives way to results; no Enter needed.
    _key(qtbot, app, None, text="quokka")
    qtbot.waitUntil(lambda: _rows(view) >= 3, timeout=4000)
    assert not view.home.isVisibleTo(view)

    # 3. Down from the box selects the first row without the box losing focus.
    _key(qtbot, app, KEY.Key_Down)
    assert app.focusWidget() is view.input
    assert view.results.current_row() is not None
    first = view.results._list.currentIndex().row()

    # 4. Up/Down walk the rows; the last Down stays put rather than wrapping.
    for _ in range(_rows(view) + 2):
        _key(qtbot, app, KEY.Key_Down)
    last = view.results._list.currentIndex().row()
    _key(qtbot, app, KEY.Key_Down)
    assert view.results._list.currentIndex().row() == last > first, "the last row is a stop"
    # (The "That's all" line after it is not selectable, so the last row is not the last item.)
    for _ in range(_rows(view) + 2):
        _key(qtbot, app, KEY.Key_Up)
    assert view.results._list.currentIndex().row() == 0

    # 5. Ctrl+Shift+P shows the preview of the selected row.
    _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)
    assert not view.preview.isHidden() and view.view_button.prefs.preview

    # 6. Tab from the box to the list; Enter on a row that matched twice expands
    #    it instead of opening it, and Enter again closes it.
    multi = next(i for i in range(_rows(view))
                 if getattr(view.results._model.index(i, 0).data(_payload_role()),
                            "match_count", 1) > 1)
    while view.results._list.currentIndex().row() != multi:
        _key(qtbot, app, KEY.Key_Down)
    for _ in range(30):
        _key(qtbot, app, KEY.Key_Tab)
        if app.focusWidget() is view.results._list:
            break
    assert app.focusWidget() is view.results._list, "Tab never reached the results list"
    before = _rows(view)
    _key(qtbot, app, KEY.Key_Return)
    # 2026-10-05: waited for. The rows were counted the instant after the key,
    # which failed on a slow machine (macOS on GitHub, and this laptop under
    # four test processes) and passed alone.
    try:
        qtbot.waitUntil(lambda: _rows(view) > before, timeout=5000)
    except Exception:                                # noqa: BLE001 - the assert below says it
        pass
    assert _rows(view) > before, "Enter on a two-match group must show its matches"
    assert opened == []
    _key(qtbot, app, KEY.Key_Return)
    assert _rows(view) == before
    for _ in range(30):                              # and Shift+Tab finds the box again
        _key(qtbot, app, KEY.Key_Backtab, MOD.ShiftModifier)
        if app.focusWidget() is view.input:
            break
    assert app.focusWidget() is view.input

    # 7. Enter on a single-match row opens the file; Ctrl+Enter reveals it.
    single = next(i for i in range(_rows(view))
                  if getattr(view.results._model.index(i, 0).data(_payload_role()),
                             "match_count", 1) == 1
                  and view.results._model.index(i, 0).data(_payload_role()).__class__.__name__ == "ResultGroup")
    view.results._list.setCurrentIndex(view.results._model.index(single, 0))
    _key(qtbot, app, KEY.Key_Return)
    assert len(opened) == 1 and opened[0][1] is False
    _key(qtbot, app, KEY.Key_Return, MOD.ControlModifier)
    assert len(opened) == 2 and opened[1][1] is True and opened[0][0] == opened[1][0]

    # 8. Esc empties the box, the results and the status, and the home state returns.
    _key(qtbot, app, KEY.Key_Escape)
    assert view.input.text() == "" and _rows(view) == 0 and view.status.text() == ""
    assert view.home.isVisibleTo(view)
    assert app.focusWidget() is view.input

    # 9. A filter typed with the keyboard becomes a chip, and Tab + Space removes it.
    # (The operator goes first: Tab in a box that ends in one accepts the
    # completion popup's suggestion, which is what it is for, and never leaves.)
    _key(qtbot, app, None, text="quokka /type pdf")
    qtbot.waitUntil(lambda: view.chips.labels() == ["type: pdf"], timeout=2000)
    # The completion popup that opened while the operator was typed took the
    # window's activation with it, and the offscreen platform never gives it
    # back; a person's window manager does. Without this Tab goes nowhere.
    window.activateWindow()
    qtbot.wait(50)                    # a spun loop: the chips of earlier keystrokes are deleteLater'd
    for _ in range(40):
        _key(qtbot, app, KEY.Key_Tab)
        focus = app.focusWidget()
        if focus is not None and focus.objectName() == "chip":
            break
    assert app.focusWidget().objectName() == "chip", "no Tab ever reached the chip"
    _key(qtbot, app, KEY.Key_Space)
    assert view.chips.labels() == [] and view.input.text().strip() == "quokka"

    # 10. Shift+Tab out of the page and Ctrl+I / Ctrl+K: leave and come back.
    _key(qtbot, app, KEY.Key_I, MOD.ControlModifier)
    assert _page(window) == "Indexing"
    _key(qtbot, app, KEY.Key_K, MOD.ControlModifier)
    assert _page(window) == "Search" and app.focusWidget() is view.input
    assert view.input.text().strip() == "quokka", "coming back to Search must not lose the query"

    _key(qtbot, app, KEY.Key_Escape)
    _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)
    assert view.preview.isHidden()


def test_escape_still_clears_the_box_while_the_preview_is_open(gui_mainwindow, quokkas, qtbot):
    """**Regression.** The preview pane's find bar armed its own window-level
    Escape whether or not it was showing; Qt fires neither of two identical
    shortcuts, so with the pane open Escape did nothing anywhere in the window."""
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    view = window.search_view
    view.input.clear()
    _preview_off(qtbot, app, view)
    view.focus()
    _key(qtbot, app, None, text="quokka")
    qtbot.waitUntil(lambda: _rows(view) >= 3, timeout=4000)
    _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)
    assert view.preview.isVisible()
    try:
        _key(qtbot, app, KEY.Key_Escape)
        assert view.input.text() == "" and _rows(view) == 0
    finally:
        if view.preview.isVisible():
            _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)


def test_ctrl_enter_in_the_box_reveals_the_result_when_interpret_is_off(
        gui_mainwindow, quokkas, qtbot, monkeypatch):
    """**Regression.** Ctrl+Enter is also the Interpret shortcut, and that one
    fired and did nothing while the feature was hidden - still consuming the
    key, so "reveal this result" (item 6a) never reached the box."""
    import app.ui.shell as shell
    seen: list[tuple] = []
    monkeypatch.setattr(shell, "open_row_async",      # the one route (2026-10-04)
                        lambda store, row, reveal=False, **_k: seen.append((row.path, reveal)))
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    view = window.search_view
    assert not view.interpret_button.isVisible(), "this scenario needs Interpret off"
    view.input.clear()
    view.focus()
    _key(qtbot, app, None, text="brief")
    qtbot.waitUntil(lambda: _rows(view) >= 1, timeout=4000)
    _key(qtbot, app, KEY.Key_Down)
    _key(qtbot, app, KEY.Key_Return, MOD.ControlModifier)
    assert seen and seen[-1][1] is True
    view.input.clear()


def _preview_off(qtbot, app, view) -> None:
    """Leave the pane hidden whatever an earlier scenario left it as."""
    if view.view_button.prefs.preview:
        _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)


def _payload_role() -> int:
    from app.ui.result_delegate import ROLE_PAYLOAD
    return ROLE_PAYLOAD


def test_the_results_list_and_the_scope_are_reachable_by_tab(gui_mainwindow, quokkas, qtbot):
    """Everything on the page that can be pressed is a tab stop: the scope's
    four segments and the results list itself (which arrows then walk)."""
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    view = window.search_view
    view.input.clear()
    _goto(window, "Search")
    view.focus()
    _key(qtbot, app, None, text="quokka")
    qtbot.waitUntil(lambda: _rows(view) >= 3, timeout=4000)
    view.focus()
    hit = set()
    for _ in range(60):
        _key(qtbot, app, KEY.Key_Tab)
        focus = app.focusWidget()
        if focus is view.results._list:
            hit.add("list")
        for i in range(view.scope.count()):
            if focus is view.scope.button(i):
                hit.add(f"scope{i}")
    assert "list" in hit, f"Tab never reached the results list; reached {sorted(hit)}"
    assert any(h.startswith("scope") for h in hit), "Tab never reached the scope control"
    view.input.clear()


# ---------------------------------------------------------------------------
# 9g - sizeHint == paint
# ---------------------------------------------------------------------------

TEXT_SIZES = {"small": 8, "system": 0, "large": 16}


def _set_prefs(window, *, density: str, font_pt: int):
    """What the View menu does: the prefs go through the search page."""
    from app.ui.view_options import ViewPreferences
    window.search_view._view_changed(ViewPreferences(density=density, font_pt=font_pt))
    gui_pump(window_app(window))


def window_app(window):
    from PySide6.QtWidgets import QApplication
    return QApplication.instance()


def _bg() -> QColor:
    from app.ui.theme import theme_colours
    return QColor(theme_colours().get("surface", "#ffffff"))


def _canvas(width: int, height: int):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(_bg())
    return image


def _ink(image) -> "np.ndarray":
    """Boolean HxW: which pixels differ from the background."""
    bpl = image.bytesPerLine()
    # PySide6 hands back a plain buffer; PyQt6's needed `.asarray(size)`.
    raw = np.frombuffer(image.constBits(), dtype=np.uint8, count=image.height() * bpl)
    pixels = raw.reshape(image.height(), bpl // 4, 4)[:, :image.width(), :3]
    bg = _bg()
    return (pixels != np.array([bg.blue(), bg.green(), bg.red()], dtype=np.uint8)).any(axis=2)


def _payload_rows(view):
    """`(payload, expanded)` for every row a search for "quokka" produces,
    with the two-match group both closed and open."""
    from app.ui.result_delegate import ROLE_EXPANDED, ROLE_PAYLOAD
    out = []
    model = view.results._model
    for i in range(model.rowCount()):
        index = model.index(i, 0)
        out.append((index.data(ROLE_PAYLOAD), bool(index.data(ROLE_EXPANDED))))
    return out


@pytest.fixture(scope="module")
def painted_payloads(gui_mainwindow, quokkas):
    """Every kind of row a search for "quokka" paints, plus a skeleton and the
    end-of-list line. Module-scoped, so it drives Qt directly (`QTest`) rather
    than through the function-scoped `qtbot`."""
    import time

    from PySide6.QtTest import QTest

    from app.ui.presenter import Terminator
    from app.ui.result_delegate import Skeleton

    app, window, store, engine = gui_mainwindow
    window.show()
    window.activateWindow()
    view = window.search_view
    _goto(window, "Search")
    view.input.clear()
    view.focus()
    QTest.keyClicks(view.input, "quokka")
    end = time.monotonic() + 4
    while _rows(view) < 3 and time.monotonic() < end:
        QTest.qWait(20)
    closed = _payload_rows(view)
    multi = next(i for i, (p, _e) in enumerate(closed) if getattr(p, "match_count", 1) > 1)
    view.results._toggle(closed[multi][0].file_id)
    opened = _payload_rows(view)
    assert len(opened) > len(closed)
    payloads = closed + [row for row in opened if row not in closed]
    payloads += [(Skeleton(0), False), (Terminator("That's all - 3 results."), False)]
    kinds = {type(p).__name__ for p, _e in payloads}
    assert {"ResultGroup", "ResultRow", "Skeleton", "Terminator"} <= kinds, kinds
    view.input.clear()
    return payloads


def _paint(window, payload, expanded: bool, width: int):
    """Hint, and an image painted into a row exactly that tall, on a canvas
    with room to spare either side so overdraw would show."""
    from app.ui.result_delegate import ROLE_EXPANDED, ROLE_PAYLOAD
    delegate = window.search_view.results._delegate
    model = QStandardItemModel()
    item = QStandardItem()
    item.setData(payload, ROLE_PAYLOAD)
    item.setData(expanded, ROLE_EXPANDED)
    model.appendRow(item)
    index = model.index(0, 0)
    option = QStyleOptionViewItem()
    window.search_view.results._list.initViewItemOption(option)
    option.rect = QRect(0, 0, width, 999)
    hint = delegate.sizeHint(option, index)
    assert hint.width() == width
    option.rect = QRect(0, 0, width, hint.height())
    image = _canvas(width + 60, hint.height() + 120)
    painter = QPainter(image)
    delegate.paint(painter, option, index)
    painter.end()
    return hint.height(), image, option


@pytest.mark.parametrize("width", [340, 760])
@pytest.mark.parametrize("size", list(TEXT_SIZES))
@pytest.mark.parametrize("density", ["compact", "normal"])
def test_paint_stays_inside_size_hint_and_leaves_no_gap(
        gui_mainwindow, painted_payloads, density, size, width):
    """The gap-under-every-row regression, over the whole matrix: nothing is
    drawn below the height `sizeHint` reserved or right of the width it was
    given, and the last thing drawn sits within a half line of the bottom pad -
    a reserved-but-empty line would be a full one."""
    from PySide6.QtGui import QFontMetrics
    from app.ui.presenter import Terminator
    from app.ui.result_delegate import Skeleton
    from app.ui.view_options import Metrics

    app, window, store, engine = gui_mainwindow
    _set_prefs(window, density=density, font_pt=TEXT_SIZES[size])
    try:
        metrics = Metrics.for_density(density)
        failures = []
        for payload, expanded in painted_payloads:
            height, image, option = _paint(window, payload, expanded, width)
            ink = _ink(image)
            rows = np.nonzero(ink.any(axis=1))[0]
            cols = np.nonzero(ink.any(axis=0))[0]
            label = f"{type(payload).__name__} {getattr(payload, 'name', getattr(payload, 'location', ''))!s}"
            if rows.size == 0:
                failures.append(f"{label}: nothing painted")
                continue
            if rows.max() >= height:
                failures.append(f"{label}: ink at y={rows.max()} but the hint is {height}")
            if cols.max() >= width:
                failures.append(f"{label}: ink at x={cols.max()} but the width is {width}")
            if isinstance(payload, Terminator):
                continue                      # centred in its rect: no bottom pad to measure
            line = QFontMetrics(option.font).height()
            slack = metrics.pad_y + line * 0.6
            if rows.max() < height - slack:
                failures.append(f"{label}: last ink at y={rows.max()}, hint {height} - "
                                f"a gap of {height - rows.max()}px under it")
        assert not failures, f"{density}/{size}/{width}px:\n  " + "\n  ".join(failures)
    finally:
        _set_prefs(window, density="normal", font_pt=0)


def test_the_text_size_preference_reaches_the_results_list(gui_mainwindow, painted_payloads, qtbot):
    """**Regression.** The View menu's text size was applied to the Files, Mail
    and Code tables and never to the Search results: the painted-rows change
    left nothing setting the list's font, so the spin box did nothing there."""
    app, window, store, engine = gui_mainwindow
    view = window.search_view
    try:
        heights = {}
        for name, pt in TEXT_SIZES.items():
            _set_prefs(window, density="normal", font_pt=pt)
            if pt:
                assert view.results._list.font().pointSize() == pt, name
            heights[name] = _paint(window, painted_payloads[0][0], False, 500)[0]
        assert heights["small"] < heights["system"] <= heights["large"], heights
        assert heights["small"] < heights["large"], heights
        # And it is the saved preference, so it is there next launch.
        _set_prefs(window, density="compact", font_pt=16)
        # Saved on the ordered state writer since bug 3a - wait for it.
        from app.ui.state_writes import pool
        assert pool().waitForDone(5000)
        state = store.all_state()
        assert state["ui:results:font_pt"] == "16" and state["ui:results:density"] == "compact"
    finally:
        _set_prefs(window, density="normal", font_pt=0)


def test_a_skeleton_row_is_as_tall_as_the_real_row_it_stands_in_for(gui_mainwindow, painted_payloads):
    """§6d: the list must not jump when the real rows replace the skeleton.
    The stand-in is a group row with a one-line snippet in comfortable
    density - and in compact, which drops the snippet from group rows, one
    with none. **Regression:** compact used to draw the skeleton's third bar
    anyway, a line taller than the row that replaced it."""
    from app.ui.presenter import ResultGroup, ResultRow, Snippet
    from app.ui.result_delegate import Skeleton

    app, window, store, engine = gui_mainwindow
    row = ResultRow(rank=0, chunk_id=1, file_id=1, path=r"D:\a\r.pdf", display_path="r.pdf",
                    snippet=Snippet("a short passage"), explain="", location="page 1",
                    score=0.5, ext="pdf", mtime_ns=1_700_000_000_000_000_000)
    real = ResultGroup(file_id=1, name="r.pdf", folder="A > B", kind="pdf", when="1 Jan 2019",
                       path=r"D:\a\r.pdf", rows=[row])
    try:
        for density in ("compact", "normal"):
            for size, pt in TEXT_SIZES.items():
                _set_prefs(window, density=density, font_pt=pt)
                skeleton = _paint(window, Skeleton(0), False, 600)[0]
                actual = _paint(window, real, False, 600)[0]
                assert skeleton == actual, f"{density}/{size}: skeleton {skeleton}px, real row {actual}px"
    finally:
        _set_prefs(window, density="normal", font_pt=0)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_each_kind_badge_is_filled_with_its_own_token_in_both_themes(gui_mainwindow, scheme):
    from app.ui import theme
    from app.ui.kind_badge import badge_token
    from app.ui.presenter import ResultGroup, ResultRow, Snippet
    from app.ui.result_delegate import BADGE_SIZE  # noqa: F401 - the badge is what is sampled
    from app.ui.view_options import Metrics

    app, window, store, engine = gui_mainwindow
    theme.stylesheet(scheme, detected=scheme)             # sets the palette painters read
    try:
        colours = theme.theme_colours()
        pad = Metrics.for_density("normal")
        tokens = set()
        for kind in ("pdf", "email", "py", "jpg"):
            row = ResultRow(rank=0, chunk_id=1, file_id=1, path=f"D:/a/x.{kind}",
                            display_path=f"x.{kind}", snippet=Snippet("x"), explain="",
                            location="", score=0.5, ext=kind, mtime_ns=1_700_000_000_000_000_000)
            group = ResultGroup(file_id=1, name=f"x.{kind}", folder="A", kind=kind,
                                when="1 Jan 2019", path=f"D:/a/x.{kind}", rows=[row])
            height, image, option = _paint(window, group, False, 500)
            wanted = QColor(colours[badge_token(kind)])
            probe = image.pixelColor(pad.pad_x + 14, pad.pad_y + 3)      # top edge, clear of the word
            assert probe.rgb() == wanted.rgb(), (scheme, kind, probe.name(), wanted.name())
            tokens.add(badge_token(kind))
        assert len(tokens) == 4, "documents, mail, code and the rest must not share a colour"
    finally:
        window._apply_theme()                              # back to the window's own


# ---------------------------------------------------------------------------
# 9i - what looking at the goldens found
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme,dark_ground", [("dark", True), ("light", False)])
def test_code_in_the_preview_is_coloured_for_the_theme_not_for_the_operating_system(
        gui_mainwindow, scheme, dark_ground):
    """**Regression.** The code highlighter mixed its colours from the widget's
    `QPalette` - the OS's - so with the theme forced dark on a light-mode
    machine the keywords and numbers were dark ink on a dark page (the dark
    golden showed `0.1` all but invisible). It now follows the sheet's tokens,
    at construction and on every theme change."""
    from app.ui import theme

    app, window, store, engine = gui_mainwindow
    pane = window.search_view.preview
    theme.stylesheet(scheme, detected="light")            # the OS says light, always
    try:
        pane.retint(theme.theme_colours())
        formats = pane._highlighter._formats
        for role in ("keyword", "number", "string", "constant"):
            lightness = formats[role].foreground().color().lightness()
            assert (lightness > 128) is dark_ground, (scheme, role, lightness)
    finally:
        window._apply_theme()


@pytest.mark.parametrize("width", [1024, 1100, 1400])
def test_the_home_box_is_640_wide_however_small_the_window_when_there_is_room(
        gui_mainwindow, qtbot, width):
    """**Regression, from reading the 1100x760 golden.** The box shared the row
    with two equal flanks and so got a third of the page: 316px at 1100, its
    placeholder cut off mid-word. 640 is a ceiling, not a target."""
    from app.ui.widgets.search_home import BOX_WIDTH

    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    window.search_view.input.clear()
    window.resize(width, 700)
    qtbot.wait(50)
    box = window.search_view.input
    assert box.width() == min(BOX_WIDTH, window.search_view.width() - 48), (width, box.width())
    window.resize(1100, 760)


def test_a_search_still_out_when_the_box_is_cleared_does_not_come_back(
        gui_mainwindow, quokkas, qtbot, monkeypatch):
    """**Regression, found because `test_gui_scenarios.py::
    test_escape_clears_results_and_status` began failing under load.** The
    empty-box branch of `_dispatch` cleared the list but did not retire the
    searches already dispatched, so one that finished a moment later drew its
    rows under an empty box and a blank status line."""
    import time

    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    view = window.search_view
    view.input.clear()
    gui_pump(app)

    real_search, real_interim = engine.search, engine.interim

    def slow(fn):
        def wrapped(*a, **k):
            time.sleep(0.4)
            return fn(*a, **k)
        return wrapped

    monkeypatch.setattr(engine, "search", slow(real_search))
    monkeypatch.setattr(engine, "interim", slow(real_interim))
    before = view._generation
    _key(qtbot, app, None, text="quokka")
    qtbot.waitUntil(lambda: view._generation > before, timeout=3000)      # it is out
    _key(qtbot, app, KEY.Key_Escape)
    assert view.input.text() == ""
    qtbot.wait(1200)                                                      # and comes back
    assert _rows(view) == 0 and view.status.text() == ""
    assert view.home.isVisibleTo(view)


# ---------------------------------------------------------------------------
# 2026-09-20 - the four small UI questions the redesign scenarios raised, each
# decided (see the dated note in WORKORDER-202626160950-ui-redesign.md).
# ---------------------------------------------------------------------------

def _first_multi_match_row(view) -> int:
    for i in range(_rows(view)):
        payload = view.results._model.index(i, 0).data(_payload_role())
        if getattr(payload, "match_count", 1) > 1:
            return i
    raise AssertionError("no multi-match group in the results")


def test_enter_in_the_box_on_a_group_of_two_matches_opens_its_best_hit(
        gui_mainwindow, quokkas, qtbot, monkeypatch):
    """Decision (a), kept: in the *box*, Enter opens - the group's best hit, not
    an expansion. Expanding is what Enter does with the *list* focused (the
    keyboard-alone scenario above), where the person is exploring the rows; the
    box is where they are asking for the answer."""
    import app.ui.shell as shell
    opened: list[tuple] = []
    monkeypatch.setattr(shell, "open_row_async",      # the one route (2026-10-04)
                        lambda store, row, reveal=False, **_k: opened.append((row.path, reveal)))
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    view = window.search_view
    view.input.clear()
    _preview_off(qtbot, app, view)
    view.focus()
    _key(qtbot, app, None, text="quokka")
    qtbot.waitUntil(lambda: _rows(view) >= 3, timeout=4000)
    multi = _first_multi_match_row(view)
    _key(qtbot, app, KEY.Key_Down)
    while view.results._list.currentIndex().row() != multi:
        _key(qtbot, app, KEY.Key_Down)
    group = view.results._model.index(multi, 0).data(_payload_role())
    rows_before = _rows(view)

    _key(qtbot, app, KEY.Key_Return)

    assert app.focusWidget() is view.input
    assert opened == [(group.best.path, False)], "Enter in the box must open the best hit"
    assert _rows(view) == rows_before, "and must not expand the group"
    view.input.clear()


def test_ctrl_enter_is_interpret_when_it_is_on_and_reveal_when_it_is_off(
        gui_mainwindow, quokkas, qtbot, monkeypatch):
    """Decision (a), kept: one chord, two meanings, decided by whether Interpret
    is offered - never both at once (the shortcut is disabled with the button
    hidden, so it cannot swallow the reveal; see `build_controls`)."""
    import app.ui.search_view as search_view_module
    import app.ui.shell as shell
    interpreted: list[int] = []
    monkeypatch.setattr(search_view_module, "interpret_into",
                        lambda _view: interpreted.append(1))
    revealed: list[tuple] = []
    monkeypatch.setattr(shell, "open_row_async",      # the one route (2026-10-04)
                        lambda store, row, reveal=False, **_k: revealed.append((row.path, reveal)))
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    view = window.search_view
    view.input.clear()
    _preview_off(qtbot, app, view)
    view.focus()
    _key(qtbot, app, None, text="quokka")
    qtbot.waitUntil(lambda: _rows(view) >= 3, timeout=4000)
    _key(qtbot, app, KEY.Key_Down)
    was_on = view.interpret_button.isVisible()
    try:
        view.set_interpret_enabled(True)
        gui_pump(app, 3)
        _key(qtbot, app, KEY.Key_Return, MOD.ControlModifier)
        assert interpreted == [1], "Ctrl+Enter must interpret while Interpret is on"
        assert revealed == [], "and must not also reveal"

        view.set_interpret_enabled(False)
        gui_pump(app, 3)
        _key(qtbot, app, KEY.Key_Return, MOD.ControlModifier)
        assert interpreted == [1], "with Interpret off it must not interpret"
        assert revealed and revealed[-1][1] is True, "it reveals instead"
    finally:
        view.set_interpret_enabled(was_on)
        view.input.clear()


def test_the_pinned_panel_takes_no_room_until_something_is_pinned(
        gui_mainwindow, quokkas, qtbot):
    """Decision (b): an empty 'Pinned working set' was a titled box, a blank list
    and four greyed buttons taking a fifth of the results page. It now appears
    with the first pin and leaves with the last - and the switch still rules."""
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    view = window.search_view
    view.input.clear()
    view.focus()
    _key(qtbot, app, None, text="quokka")
    qtbot.waitUntil(lambda: _rows(view) >= 3, timeout=4000)
    outer = view.split
    panel = outer.widget(1)
    switch = outer.switches.boxes["pinned"]
    assert switch.isChecked(), "this scenario needs the panel switched on"
    panel.clear()
    gui_pump(app, 3)

    def first_row():
        return view.results._row_for(view.results._model.index(0, 0).data(_payload_role()))

    assert panel.pins == () and panel.isHidden(), "empty: not drawn"
    assert outer.widget(0).width() >= outer.width() - 4, (
        "the results side must have the whole page while nothing is pinned")

    panel.pin(first_row())
    gui_pump(app, 3)
    assert panel.isVisible() and panel.width() > 100, "the first pin brings it in"

    panel.clear()
    gui_pump(app, 3)
    assert panel.isHidden(), "clearing the list takes it away again"

    panel.pin(first_row())
    switch.setChecked(False)
    gui_pump(app, 3)
    assert panel.isHidden(), "switched off stays off, pins or not"
    switch.setChecked(True)
    gui_pump(app, 3)
    assert panel.isVisible(), "and switching on again shows what was pinned"
    panel.clear()
    view.input.clear()


def test_escape_closes_the_find_bar_first_and_the_box_only_on_the_second_press(
        gui_mainwindow, quokkas, qtbot):
    """Decision (c): with the preview's find bar open, Escape is "close the find
    bar" whichever of the two has focus, and the search-box behaviour (empty the
    box) happens only on a second press - closing a little bar must not throw
    away the search that produced the document being read."""
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    _goto(window, "Search")
    view = window.search_view
    view.input.clear()
    _preview_off(qtbot, app, view)
    view.focus()
    _key(qtbot, app, None, text="quokka")
    qtbot.waitUntil(lambda: _rows(view) >= 3, timeout=4000)
    _key(qtbot, app, KEY.Key_Down)
    _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)
    assert view.preview.isVisible()
    try:
        for focus_in in ("box", "bar"):
            view.preview.find.focus()
            gui_pump(app, 3)
            assert view.preview.find.isVisible()
            if focus_in == "box":
                view.input.setFocus()
                gui_pump(app, 2)
                assert app.focusWidget() is view.input
            _key(qtbot, app, KEY.Key_Escape)
            assert not view.preview.find.isVisible(), (
                f"Escape with focus in the {focus_in} must close the find bar first")
            assert view.input.text() == "quokka", (
                f"and must leave the search alone (focus in the {focus_in})")
            view.input.setFocus()
            gui_pump(app, 2)
            _key(qtbot, app, KEY.Key_Escape)
            assert view.input.text() == "", "the second Escape empties the box"
            # Back to a searched state for the second pass.
            view.focus()
            _key(qtbot, app, None, text="quokka")
            qtbot.waitUntil(lambda: _rows(view) >= 3, timeout=4000)
            _key(qtbot, app, KEY.Key_Down)
    finally:
        if view.preview.isVisible():
            _key(qtbot, app, KEY.Key_P, MOD.ControlModifier | MOD.ShiftModifier)
        view.input.clear()


# ---------------------------------------------------------------------------
# 2026-09-20 - the rail must not overlap itself at the smallest window.
# ---------------------------------------------------------------------------

def _rail_rects(rail) -> list:
    """Every rail entry that is showing, as (name, rect in column coordinates)."""
    from PySide6.QtCore import QRect

    found = []
    for button in list(rail._buttons.values()) + [rail.pill]:
        if button.isVisibleTo(rail.column):
            found.append((getattr(button, "text", lambda: "pill")() or "pill",
                          QRect(button.mapTo(rail.column, button.rect().topLeft()),
                                button.size()), button))
    return found


def _assert_rail_whole(rail) -> None:
    rects = _rail_rects(rail)
    assert len(rects) >= 8, "the rail lost entries"
    for name, rect, button in rects:
        assert rect.height() >= button.minimumSizeHint().height(), (
            f"{name} was squeezed to {rect.height()}px, below the "
            f"{button.minimumSizeHint().height()}px it needs")
        assert rail.column.rect().contains(rect), f"{name} sits outside the rail"
    for i, (name, rect, _b) in enumerate(rects):
        for other, other_rect, _o in rects[i + 1:]:
            assert not rect.intersects(other_rect), f"{name} overlaps {other}"


def test_the_rail_never_squeezes_or_overlaps_its_entries_at_the_smallest_window(
        gui_mainwindow, qtbot):
    """**Found on the real window at 125% scaling, 1024x600:** the window's floor
    (480) was lower than the rail needs, so Qt squeezed seven fixed-height
    buttons to 47px where 57 are needed and the indexing pill sat over the last
    label. The invariant is independent of scale: at the smallest height the
    window allows, and at a tall one, every entry has the height it needs and
    none overlaps another."""
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    rail = window.rail
    try:
        window.resize(1100, 200)                       # Qt clamps to the floor
        gui_pump(app, 5)
        assert window.height() == window.minimumSize().height() or window.height() > 200
        _assert_rail_whole(rail)
        squeezed = rail._compact

        window.resize(1100, 900)
        gui_pump(app, 5)
        _assert_rail_whole(rail)
        assert not rail._compact, "with room to spare the labels come back"
        assert all(b.toolButtonStyle().name.endswith("TextUnderIcon")
                   for b in rail._buttons.values())
        assert squeezed or window.minimumSize().height() >= rail._natural, (
            "a window shorter than the labelled rail must have gone icon-only")
    finally:
        window.resize(1100, 760)
        gui_pump(app, 3)
