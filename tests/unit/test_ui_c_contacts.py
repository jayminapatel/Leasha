r"""UI phase B, Contacts (2026-10-13): one app, the Contacts page.

Layer: L5

The owner: every screen should "look and feel like" one app, not different apps. The UI review
found four things on Contacts, each tested here:

1. **Reading is not editing.** A pick opened the editor - every field a box - to read one phone
   number. Now a pick shows a read view (`app/ui/contacts/reader.py`) and **Edit** (Enter, a
   double-click, F2) switches to the editor; Save and Cancel come back to reading.
2. **Theme tokens and the button system only**: no colour literal in Contacts, the read view's
   Edit is a system button.
3. **A labelled bar**: each toolbar button shows its icon and its words, and gives words up in a
   fixed order when the window is narrow.
4. **The one menu builder and the one list behaviour**: the right-click menu is built by
   `widgets/menus.py` (Edit first and bold, the bin line last), and both lists answer
   `widgets/list_keys.py` (Enter, F2, Delete, Ctrl+C).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt                                   # noqa: E402
from PySide6.QtGui import QGuiApplication, QKeyEvent                    # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QToolButton    # noqa: E402

from tests.unit.test_contacts_view_world import card, settle, world     # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[2]


def _key(widget, key, mods=Qt.KeyboardModifier.NoModifier) -> None:
    for kind in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
        QApplication.sendEvent(widget, QKeyEvent(kind, key, mods))


def _picked(world, page, cid):
    page.select_ids([cid])
    settle(page)
    return page


# -- 1. the read view ---------------------------------------------------------------------

@pytest.mark.slow
def test_a_pick_shows_the_read_view_not_the_editor(world):
    cid = world.add(card("Ann", "Lee", email="ann@x.example", phone="07700900123", company="Acme Ltd",
                         title="Buyer", notes="Met at the fair"))
    page = _picked(world, world.page(), cid)
    assert page.right.currentWidget() is page.reader
    assert page.reader.cid == cid and page.editor.cid == cid
    text = page.reader.text()
    for words in ("Ann Lee", "Buyer · Acme Ltd", "ann@x.example", "07700900123", "Met at the fair", "work",
                  "mobile"):
        assert words in text, words
    titles = page.reader.section_titles()
    assert titles[:2] == ["Email", "Phone"] and "Notes" in titles and "Kept in" in titles
    assert "Home Google" in text
    # Nothing in the read view is a box to type in.
    from PySide6.QtWidgets import QLineEdit, QPlainTextEdit

    assert not page.reader.details.findChildren(QLineEdit)
    assert not page.reader.details.findChildren(QPlainTextEdit)


@pytest.mark.slow
def test_edit_switches_to_the_editor_and_save_or_cancel_come_back(world):
    cid = world.add(card("Ann", "Lee", email="ann@x.example"))
    page = _picked(world, world.page(), cid)
    page.reader.edit_button.click()
    assert page.right.currentWidget() is page.editor
    page.editor.edits["company"].setText("Acme Ltd")
    page.editor._save()
    settle(page)
    assert page.right.currentWidget() is page.reader
    assert "Acme Ltd" in page.reader.text()
    page.edit_current()
    assert page.is_editing()
    page.editor.cancel_button.click()
    settle(page)
    assert page.right.currentWidget() is page.reader


def test_the_index_and_enrichment_panels_sit_under_the_read_view(world):
    page = world.page()
    assert page.reader.isAncestorOf(page.editor.index_panel)
    assert page.reader.isAncestorOf(page.editor.enrich_panel)
    assert not page.editor.isAncestorOf(page.editor.index_panel)


@pytest.mark.slow
def test_a_new_contact_still_opens_the_editor(world):
    world.add(card("Ann", "Lee"))
    page = world.page()
    settle(page)
    page.new_contact()
    assert page.right.currentWidget() is page.editor and page.editor.cid is None


@pytest.mark.slow
def test_in_the_bin_the_read_view_has_no_edit(world):
    cid = world.add(card("Ann", "Lee"))
    world.hub.delete(cid)
    page = world.page()
    settle(page)
    page.sidebar.select_key("bin")
    settle(page)
    _picked(world, page, cid)
    assert page.right.currentWidget() is page.reader
    assert not page.reader.edit_button.isEnabled()
    page.edit_current()
    assert page.right.currentWidget() is page.reader


# -- 2. tokens and the button system ---------------------------------------------------

@pytest.mark.slow
def test_the_edit_button_is_a_system_button_with_a_tooltip(world):
    page = world.page()
    edit = page.reader.edit_button
    assert isinstance(edit, QPushButton) and edit.text() == "Edit"
    assert edit.property("buttonRole") == "primary" and edit.property("buttonIcon") == "pencil"
    assert edit.toolTip() and not edit.icon().isNull()


#: Colours that are not the window's, with why. Everything else in Contacts is a theme token.
_NOT_THE_WINDOWS = {
    "app/ui/contacts_tools/qr_dialog.py": "a QR code is drawn on white so a camera can read it",
}
_NAMED = re.compile(r"GlobalColor\.(white|black|red|green|blue|gray|darkGray|lightGray)\b"
                    r"|[\"'](white|black|red|grey|gray)[\"']|rgba?\(")


def test_contacts_names_no_colour_of_its_own():
    """The hex guard is `test_ui_one_app_colours`; this adds named colours, for Contacts' files."""
    from tests.unit.test_ui_one_app_colours import hex_literals

    found = []
    files = [ROOT / "app/ui/contacts_view.py", *sorted((ROOT / "app/ui/contacts").glob("*.py")),
             *sorted((ROOT / "app/ui/contacts_tools").glob("*.py"))]
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel != "app/ui/contacts_tools/print_dialog.py":       # paper: allowed by the hex guard
            found += hex_literals(text, rel)
        if rel not in _NOT_THE_WINDOWS:
            found += [f"{rel}: {m.group(0)}" for m in _NAMED.finditer(text)]
    assert not found, found


@pytest.mark.slow
def test_the_read_view_follows_a_theme_change(world):
    from app.ui.theme import PALETTES, Theme

    cid = world.add(card("Ann", "Lee", email="ann@x.example"))
    page = _picked(world, world.page(), cid)
    page.retint(PALETTES[Theme.LIGHT])
    light = page.reader.photo_label.pixmap().toImage()
    page.retint(PALETTES[Theme.DARK])
    assert page.reader.photo_label.pixmap().toImage() != light


# -- 3. the labelled bar ---------------------------------------------------------------

def test_every_bar_button_shows_its_icon_and_its_words(world):
    page = world.page()
    bar = page.toolbar
    bar.fit(4000)
    buttons = [b for b in bar.findChildren(QToolButton) if b.accessibleName()
               and b.accessibleName() != "Clear the search box"]
    assert len(buttons) >= 12
    for b in buttons:
        assert b.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon, b.accessibleName()
        assert b.text() == b.accessibleName(), "the words are the button's name, unchanged"
        assert b.toolTip() and not b.icon().isNull(), b.accessibleName()


def test_a_narrow_bar_gives_up_words_in_order_and_new_contact_keeps_its_own(world):
    page = world.page()
    bar = page.toolbar
    assert bar.fit(4000) == 0
    level = bar.fit(700)
    assert level >= 1
    assert bar.list_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    assert bar.tools["import"].toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    assert bar.new_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    assert bar.tools["import"].toolTip()
    assert bar.fit(4000) == 0
    assert bar.tools["import"].toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon


# -- 4. the menu builder and the list keys ---------------------------------------------

def _menu(page, monkeypatch):
    from app.ui.contacts import quick

    shown = {}
    monkeypatch.setattr(quick, "open_menu", lambda menu, pos: shown.setdefault("menu", menu))
    quick.show_menu(page, page.mapToGlobal(page.rect().center()))
    return shown["menu"]


@pytest.mark.slow
def test_the_right_click_menu_is_the_builders(world, monkeypatch):
    from app.ui.widgets import menus

    cid = world.add(card("Ann", "Lee", email="ann@x.example", phone="07700900123"))
    page = _picked(world, world.page(), cid)
    menu = _menu(page, monkeypatch)
    assert menu.property(menus.BUILT_PROPERTY) is True
    lines = [a for a in menu.actions() if not a.isSeparator()]
    assert lines[0].text() == "Edit" and menu.defaultAction() is lines[0] and lines[0].font().bold()
    assert lines[-1].text() == "Move to the bin" and lines[-1].shortcut().toString() == "Del"
    assert all(not a.icon().isNull() for a in lines)
    by = {a.text(): a for a in lines}
    assert by["Copy email address"].shortcut().toString() == "Ctrl+C"
    lines[0].trigger()
    assert page.right.currentWidget() is page.editor


@pytest.mark.slow
def test_several_picked_have_no_edit_line_and_the_bin_offers_restore(world, monkeypatch):
    a, b = world.add(card("Ann", "Lee")), world.add(card("Bob", "Bell"))
    page = world.page()
    page.select_ids([a, b])
    settle(page)
    words = [x.text() for x in _menu(page, monkeypatch).actions() if not x.isSeparator()]
    assert "Edit" not in words and words[-1] == "Move to the bin"
    world.hub.delete(a)
    page.sidebar.select_key("bin")
    settle(page)
    _picked(world, page, a)
    words = [x.text() for x in _menu(page, monkeypatch).actions() if not x.isSeparator()]
    assert words == ["Restore"]


def test_contacts_builds_no_right_click_menu_by_hand():
    """`test_menu_system`'s guard leaves Contacts to its own branch; this is that guard for Contacts.
    The drop-downs that are not a right-click are counted, as there."""
    hand = re.compile(r"QMenu\(|\.addMenu\(|\.addSeparator\(|\.addAction\(")
    drop_downs = {
        "app/ui/contacts/bulkbar.py": "the bar's Add to group, tag and account drop-downs",
        "app/ui/contacts/toolbar.py": "the More tools drop-down",
    }
    offenders = []
    for path in sorted((ROOT / "app/ui/contacts").glob("*.py")) + [ROOT / "app/ui/contacts_view.py"]:
        rel = path.relative_to(ROOT).as_posix()
        code = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                         if not line.strip().startswith("#"))
        if hand.search(code) and rel not in drop_downs:
            offenders.append(rel)
    assert not offenders, offenders


@pytest.mark.slow
def test_the_lists_answer_the_one_list_behaviour(world):
    a = world.add(card("Ann", "Lee", email="ann@x.example"))
    b = world.add(card("Bob", "Bell", email="bob@x.example"))
    page = world.page()
    settle(page)
    for view in (page.table, page.cards):
        assert view.property("listKeys") is True
    _picked(world, page, a)
    page.table.setCurrentIndex(page.model.index(page.model.position_of(a), 0))
    _key(page.table, Qt.Key.Key_F2)
    assert page.right.currentWidget() is page.editor
    page.editor.cancel_button.click()
    settle(page)
    page.select_ids([a, b])
    settle(page)
    QGuiApplication.clipboard().clear()
    _key(page.table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert sorted(QGuiApplication.clipboard().text().split("; ")) == ["ann@x.example", "bob@x.example"]
    asked = []
    page._bulk = lambda name, arg: asked.append(name)
    _key(page.cards, Qt.Key.Key_Delete)
    assert asked == ["delete"]


@pytest.mark.slow
def test_enter_edits_once(world):
    cid = world.add(card("Ann", "Lee"))
    page = _picked(world, world.page(), cid)
    opened = []
    page.editor.focus_first = lambda: opened.append(1)
    page.table.setCurrentIndex(page.model.index(0, 0))
    _key(page.table, Qt.Key.Key_Return)
    assert opened == [1] and page.right.currentWidget() is page.editor
