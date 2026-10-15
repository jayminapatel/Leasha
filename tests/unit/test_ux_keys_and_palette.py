"""Getting around by keyboard, on the real window (the UX review, 2026-10-14).

Layer: L5.

IA-1 / MS-11 the command palette, IA-2 / MS-3 Esc on the page in front, IA-3
Ctrl+1..9 and the Go menu, IA-7 People in photos on Photos' own page, IA-9 Ctrl+F
on the page's own box, IA-10 back and forward, MS-4 type-then-arrow on four
pages, MS-10 the pop-out closes on Esc.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from tests.unit.conftest import gui_pump, release_held_input  # noqa: E402

MOD = Qt.KeyboardModifier
KEY = Qt.Key


def _key(qtbot, app, key, mod=MOD.NoModifier, target=None) -> None:
    target = target or app.focusWidget() or app.activeWindow()
    qtbot.keyClick(target, key, mod)
    gui_pump(app, 2)
    release_held_input()


def _front(app, window, qtbot) -> None:
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    window.raise_()
    gui_pump(app)


def _page(window) -> str:
    return window.rail.tabText(window.rail.currentIndex())


def _goto(window, title: str) -> None:
    index = window.nav.index_of(title)
    assert index >= 0, f"no page called {title!r}"
    window.rail.setCurrentIndex(index)


@pytest.fixture
def win(gui_mainwindow, qtbot):
    app, window, store, engine = gui_mainwindow
    _front(app, window, qtbot)
    yield app, window
    palette = window.nav.palette
    if palette is not None and palette.isVisible():
        palette.close()
    _goto(window, "Search")
    gui_pump(app)


# ---------------------------------------------------------------------------
# IA-1 / MS-11 - the command palette
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_ctrl_k_opens_the_palette_and_enter_goes_to_the_page_chosen(win, qtbot):
    app, window = win
    _goto(window, "Tidy")
    gui_pump(app)
    _key(qtbot, app, KEY.Key_K, MOD.ControlModifier)
    palette = window.nav.palette
    assert palette is not None and palette.isVisible(), "Ctrl+K opens the palette"
    assert _page(window) == "Tidy", "Ctrl+K no longer jumps to Search"
    qtbot.keyClicks(palette.box, "mail")
    assert palette.shown()[0].text == "Go to Mail"
    _key(qtbot, app, KEY.Key_Return, target=palette.box)
    gui_pump(app, 4)
    assert not palette.isVisible()
    assert _page(window) == "Mail"


def test_arrows_move_the_choice_and_esc_closes(win, qtbot):
    app, window = win
    palette = window.nav.open_palette()
    gui_pump(app)
    assert palette.list.currentRow() == 0
    _key(qtbot, app, KEY.Key_Down, target=palette.box)
    _key(qtbot, app, KEY.Key_Down, target=palette.box)
    assert palette.list.currentRow() == 2
    _key(qtbot, app, KEY.Key_Up, target=palette.box)
    assert palette.list.currentRow() == 1
    _key(qtbot, app, KEY.Key_Escape, target=palette.box)
    assert not palette.isVisible()


def test_the_palette_has_every_page_every_setting_the_menu_verbs_and_sync(win):
    app, window = win
    entries = window.nav.palette_entries()
    texts = [e.text for e in entries]
    rail = window.rail
    for index in range(rail.count()):
        assert f"Go to {rail.tabText(index)}" in texts
    for category in window.settings_view._nav.category_names():
        assert f"Settings: {category}" in texts
    for verb in ("Preview pane", "Start indexing", "Save this search…", "Keyboard shortcuts",
                 "People in photos", "Back", "Sync now"):
        assert verb in texts, verb
    assert "Command palette…" not in texts, "the palette does not list itself"
    assert texts.count("Go to Mail") == 1 and "Mail" not in texts, "a page once, as a page"
    order = rail.page_order()
    pages = [t.removeprefix("Go to ") for t in texts if t.startswith("Go to ")]
    assert pages[:len(order)] == order, "pages in the rail's order"


def test_a_line_starting_with_the_marker_shows_actions_only(win, qtbot):
    app, window = win
    palette = window.nav.open_palette()
    qtbot.keyClicks(palette.box, ">")
    kinds = {e.kind for e in palette.shown()}
    assert kinds == {"action"}
    palette.close()


def test_words_that_match_nothing_are_searched_for_on_the_search_page(win, qtbot):
    app, window = win
    _goto(window, "Tidy")
    palette = window.nav.open_palette()
    qtbot.keyClicks(palette.box, "zqxv harbour")
    assert palette.shown()[-1].text == "Search for 'zqxv harbour'"
    palette.list.setCurrentRow(palette.list.count() - 1)
    _key(qtbot, app, KEY.Key_Return, target=palette.box)
    gui_pump(app, 4)
    assert _page(window) == "Search"
    assert window.search_view.input.text() == "zqxv harbour"
    window.search_view.input.clear()


@pytest.mark.slow
def test_a_settings_line_opens_that_category(win):
    app, window = win
    nav = window.settings_view._nav
    wanted = nav.category_names()[-1]
    entry = next(e for e in window.nav.palette_entries() if e.text == f"Settings: {wanted}")
    entry.run()
    gui_pump(app)
    assert _page(window) == "Settings" and nav.current_category() == wanted
    nav.show_category(nav.category_names()[0])


def test_sync_now_opens_the_door_the_contacts_toolbar_uses(win, monkeypatch):
    app, window = win
    page = window.contacts_view.ensure_built()
    asked = []
    monkeypatch.setattr(page, "open_tool", lambda name: asked.append(name))
    entry = next(e for e in window.nav.palette_entries() if e.text == "Sync now")
    entry.run()
    assert asked == ["sync"]


def test_open_sync_status_opens_the_sync_window_without_a_sync(win, monkeypatch):
    """The palette reaches the Sync window the way Settings > Accounts' button does:
    `AccountRuntime.open_sync`, which starts nothing."""
    app, window = win
    runtime = getattr(window, "account_runtime", None)
    if runtime is None:
        pytest.skip("no account runtime in this window")
    opened = []
    monkeypatch.setattr(runtime, "open_sync", lambda *a, **k: opened.append(True))
    entry = next(e for e in window.nav.palette_entries() if e.text == "Open sync status")
    entry.run()
    assert opened == [True]


# ---------------------------------------------------------------------------
# IA-2 / MS-3 - Esc clears the page in front, and only that page
# ---------------------------------------------------------------------------

def test_esc_clears_the_box_of_the_page_in_front_and_leaves_search_alone(win, qtbot):
    app, window = win
    window.search_view.input.setText("barnsley")
    _goto(window, "Mail")
    window.mail_view.input.setText("dave")
    window.rail.column.setFocus()
    gui_pump(app)
    _key(qtbot, app, KEY.Key_Escape)
    assert window.mail_view.input.text() == ""
    assert window.search_view.input.text() == "barnsley", "a page out of sight keeps its query"
    _goto(window, "Tidy")
    window.rail.column.setFocus()
    _key(qtbot, app, KEY.Key_Escape)
    assert window.search_view.input.text() == "barnsley", "Tidy has no box: nothing cleared"
    _goto(window, "Search")
    window.rail.column.setFocus()
    _key(qtbot, app, KEY.Key_Escape)
    assert window.search_view.input.text() == ""


# ---------------------------------------------------------------------------
# IA-9 - Ctrl+F on the page's own box
# ---------------------------------------------------------------------------

def test_ctrl_f_puts_the_cursor_in_the_pages_own_box_else_search(win, qtbot):
    app, window = win
    for title, box in (("Mail", window.mail_view.input), ("Files", window.files_view.input),
                       ("Photos", window.photos_view.input)):
        _goto(window, title)
        window.rail.column.setFocus()
        gui_pump(app)
        _key(qtbot, app, KEY.Key_F, MOD.ControlModifier)
        assert _page(window) == title, f"Ctrl+F left {title}"
        assert app.focusWidget() is box, f"{title}: focus is {app.focusWidget()!r}"
    _goto(window, "Tidy")
    window.rail.column.setFocus()
    _key(qtbot, app, KEY.Key_F, MOD.ControlModifier)
    assert _page(window) == "Search" and app.focusWidget() is window.search_view.input


# ---------------------------------------------------------------------------
# IA-3 - Ctrl+1..9 and the Go menu
# ---------------------------------------------------------------------------

def test_ctrl_and_a_number_opens_the_page_at_that_place_on_the_rail(win, qtbot):
    app, window = win
    order = window.rail.page_order()
    keys = [KEY.Key_1, KEY.Key_2, KEY.Key_3, KEY.Key_4, KEY.Key_5, KEY.Key_6, KEY.Key_7,
            KEY.Key_8, KEY.Key_9]
    for number, title in enumerate(order[:9], start=1):
        window.rail.column.setFocus()
        _key(qtbot, app, keys[number - 1], MOD.ControlModifier)
        assert _page(window) == title, f"Ctrl+{number} went to {_page(window)}, not {title}"
    assert not window.nav.go_to_position(len(order) + 1)


def test_ctrl_and_a_number_follows_the_rail_when_a_page_is_moved(win):
    app, window = win
    rail = window.rail
    movable = [i for i in range(rail.count()) if rail.movable(i)]
    first, second = movable[0], movable[1]
    before = rail.page_order()
    assert rail.moveTab(first, second)
    try:
        assert window.nav.go_to_position(1)
        assert _page(window) == before[1]
    finally:
        rail.moveTab(second, first)
    assert rail.page_order() == before


def test_the_go_menu_lists_every_rail_page_in_rail_order(win):
    app, window = win
    menu = window.nav.go_menu
    assert menu is not None and menu.title().replace("&", "") == "Go"
    menu.aboutToShow.emit()
    texts = [a.text() for a in menu.actions() if a.text()]
    order = [t for t in window.rail.page_order() if t in window.nav.go_pages]
    assert texts[:len(order)] == order
    for title in ("Search", "Chat", "Mail", "Photos", "Files", "Contacts", "Code", "Browse",
                  "Tidy", "Indexing", "Offline", "People in photos", "Back", "Forward"):
        assert title in texts, title
    assert set(window.rail.page_order()) <= set(texts)


# ---------------------------------------------------------------------------
# IA-7 - People in photos opens Photos' own naming page
# ---------------------------------------------------------------------------

def test_people_in_photos_opens_the_naming_page_inside_photos(win):
    app, window = win
    before = window._photo_tagger
    action = next(a for a in window.nav.go_extras if a.text() == "People in photos")
    action.trigger()
    gui_pump(app, 4)
    try:
        assert _page(window) == "Photos"
        assert window.photos_view.centre.currentIndex() == 1, "the naming page is in front"
        assert window._photo_tagger is before, "no second window holding the same page"
    finally:
        window.photos_view.show_photos()
        gui_pump(app)


# ---------------------------------------------------------------------------
# IA-10 - back and forward
# ---------------------------------------------------------------------------

def test_alt_left_and_alt_right_walk_back_and_forward(win, qtbot):
    app, window = win
    window.nav.reset()
    for title in ("Mail", "Files", "Code"):
        _goto(window, title)
        gui_pump(app)
    window.rail.column.setFocus()
    _key(qtbot, app, KEY.Key_Left, MOD.AltModifier)
    assert _page(window) == "Files"
    _key(qtbot, app, KEY.Key_Left, MOD.AltModifier)
    assert _page(window) == "Mail"
    _key(qtbot, app, KEY.Key_Right, MOD.AltModifier)
    assert _page(window) == "Files"
    _goto(window, "Tidy")
    assert not window.nav.forward(), "somewhere new drops the forward places"
    assert window.nav.back() and _page(window) == "Files"


def test_the_mouse_back_and_forward_buttons_do_the_same(win, qtbot):
    app, window = win
    window.nav.reset()
    _goto(window, "Mail")
    _goto(window, "Files")
    gui_pump(app)
    qtbot.mouseClick(window.files_view, Qt.MouseButton.BackButton)
    gui_pump(app)
    assert _page(window) == "Mail"
    qtbot.mouseClick(window.mail_view, Qt.MouseButton.ForwardButton)
    gui_pump(app)
    assert _page(window) == "Files"


def test_back_returns_to_the_photos_naming_page_and_the_settings_category(win):
    app, window = win
    nav, settings = window.nav, window.settings_view._nav
    nav.reset()
    window._show_photos(naming=True)
    gui_pump(app, 4)
    _goto(window, "Mail")
    window.photos_view.show_photos()            # changed while out of sight
    assert nav.back() and _page(window) == "Photos"
    assert window.photos_view.centre.currentIndex() == 1, "back on the naming page"
    window.photos_view.show_photos()
    _goto(window, "Settings")
    wanted = settings.category_names()[1]
    settings.show_category(wanted)
    _goto(window, "Mail")
    settings.show_category(settings.category_names()[0])
    assert nav.back() and _page(window) == "Settings"
    assert settings.current_category() == wanted


# ---------------------------------------------------------------------------
# MS-4 - type, then arrow, on Files, Mail, Code and Photos
# ---------------------------------------------------------------------------

def test_the_four_page_boxes_hand_their_arrows_to_their_lists(win):
    app, window = win
    for view in (window.files_view, window.mail_view, window.code_view, window.photos_view):
        keys = getattr(view.input, "_arrow_keys", None)
        assert keys is not None, type(view).__name__
        assert keys.view() is not None, type(view).__name__
    assert window.code_view.input._arrow_keys.view() is window.code_view.results.table
    assert window.photos_view.input._arrow_keys.view() is window.photos_view.browser.active_view()


# ---------------------------------------------------------------------------
# MS-10 - the pop-out closes on Esc, after its find bar
# ---------------------------------------------------------------------------

def test_the_pop_out_closes_on_esc_once_its_find_bar_has_closed(qapp, qtbot, tmp_path):
    from app.ui.presenter.opening import Place
    from app.ui.widgets.preview_window import PreviewWindow

    note = tmp_path / "harbour.txt"
    note.write_text("the harbour survey quay", encoding="utf-8")
    popped = PreviewWindow(Place(str(note)))
    popped.show()
    qtbot.waitExposed(popped)
    popped.activateWindow()
    gui_pump(qapp)
    try:
        popped.find.focus()
        gui_pump(qapp)
        assert popped.find.isVisible()
        qtbot.keyClick(popped.find.box, KEY.Key_Escape)
        gui_pump(qapp)
        assert not popped.find.isVisible() and popped.isVisible(), "the find bar goes first"
        qtbot.keyClick(popped, KEY.Key_Escape)
        gui_pump(qapp)
        assert not popped.isVisible(), "the second Esc closes the window"
    finally:
        popped.close()
        gui_pump(qapp)


def test_the_palette_is_drawn_only_in_tokens_both_themes_have(win, qtbot):
    from app.ui import theme
    from app.ui.widgets.command_palette import TOKENS

    for name, palette in theme.PALETTES.items():
        missing = [t for t in TOKENS if t not in palette]
        assert not missing, f"{name} lacks {missing}"
    app, window = win
    palette = window.nav.open_palette()
    gui_pump(app)
    _key(qtbot, app, KEY.Key_K, MOD.ControlModifier, target=palette.box)
    assert not palette.isVisible(), "Ctrl+K again puts it away"
