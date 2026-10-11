"""Order 1p, Contacts onto the shared left-hand list.

Layer: L5

`ContactSidebar` keeps every signal and method the page uses; `SideList` draws it. These cover
what the move could have lost: the right-click menus (the tag colour submenu is a new thing the
shared list learned for this), the duplicates door, and the arrow keys picking. The menus are
tried on a sidebar of its own, so no page dialog opens.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt                                    # noqa: E402
from PySide6.QtTest import QTest                                          # noqa: E402

from app.ui.contacts.logic import Entry                                   # noqa: E402
from app.ui.contacts.sidebar import ContactSidebar                        # noqa: E402
from app.ui.widgets.side_list import SideList                             # noqa: E402

from tests.unit.test_contacts_view_world import card, settle, world      # noqa: E402,F401

ENTRIES = [
    Entry("all", "all", "All", "contact", 4, section="Contacts"),
    Entry("favourites", "favourites", "Favourites", "star", 1, section="Contacts"),
    Entry("group:3", "group", "Family", "users", 2, 3, section="Groups"),
    Entry("tag:5", "tag", "Clients", "tag", 1, 5, "#4C8BF5", section="Tags"),
    Entry("duplicates", "duplicates", "Possible duplicates", "merge", None, section="Needs a look"),
    Entry("smart:9", "smart", "No phone", "sliders-horizontal", None, 9, section="Smart groups"),
]


@pytest.fixture()
def side(qapp):
    s = ContactSidebar()
    s.resize(260, 500)
    s.show()
    s.set_entries(ENTRIES)
    yield s
    s.hide()
    s.deleteLater()


def _right_click(side, key):
    item = side.tree._find(key)
    assert item is not None, key
    side.tree.scrollToItem(item)
    side.tree._menu(QPoint(side.tree.visualItemRect(item).center()))
    return side.tree.last_menu


def test_the_contacts_sidebar_is_the_shared_side_list(side):
    assert isinstance(side.tree, SideList)
    assert side.tree.accessibleName() == "Contact lists"
    assert [e.key for e in side.tree.entries()] == [e.key for e in ENTRIES]
    assert side.tree.entry("tag:5").colour == "#4C8BF5" and side.tree.entry("all").kind == "all"
    assert side.tree.entry("duplicates").kind == "door"


def test_the_page_still_lists_what_the_hub_holds_in_the_shared_list(world):
    world.add(card("Ann", "Lee", groups=["Family"]))
    page = world.page()
    assert isinstance(page.sidebar.tree, SideList)
    assert [e.key for e in page.sidebar.tree.entries()][:2] == ["all", "favourites"]


def test_a_group_and_a_smart_group_have_their_own_menus(side):
    seen = []
    side.group_rename.connect(lambda gid: seen.append(("rename", gid)))
    side.smart_edit.connect(lambda i: seen.append(("edit", i)))
    side.smart_delete.connect(lambda i: seen.append(("delete", i)))
    [rename] = _right_click(side, "group:3").actions()
    assert rename.text().startswith("Rename this group")
    rename.trigger()
    edit, delete = _right_click(side, "smart:9").actions()
    assert (edit.text(), delete.text()) == ("Edit this smart group…", "Delete this smart group")
    edit.trigger()
    delete.trigger()
    assert seen == [("rename", 3), ("edit", 9), ("delete", 9)]


def test_a_tag_menu_offers_a_colour_submenu_and_delete(side):
    seen = []
    side.tag_colour.connect(lambda i, c: seen.append(("colour", i, c)))
    side.tag_delete.connect(lambda i: seen.append(("delete", i)))
    top = {a.text(): a for a in _right_click(side, "tag:5").actions()}
    assert set(top) == {"Colour", "Delete this tag"}
    sub = top["Colour"].menu()
    assert sub is not None and len(sub.actions()) >= 8
    chosen = sub.actions()[1]
    chosen.trigger()
    top["Delete this tag"].trigger()
    assert seen == [("colour", 5, chosen.text()), ("delete", 5)] and chosen.text().startswith("#")


def test_the_duplicates_door_asks_for_its_tool_and_the_list_stays(side):
    asked, picked = [], []
    side.opener_requested.connect(asked.append)
    side.picked.connect(picked.append)
    side.select_key("duplicates")
    side.tree.door.emit(side.tree.entry("duplicates"))
    assert asked == ["duplicates", "duplicates"] and picked == [] and side.current_key() == "all"


def test_selecting_by_key_picks_and_a_silent_select_does_not(side):
    picked = []
    side.picked.connect(lambda e: picked.append(e.key))
    side.select_key("favourites")
    side.select_key("group:3", emit=False)
    assert picked == ["favourites"] and side.current_key() == "group:3"
    side.set_entries(ENTRIES)                                  # a refresh keeps the choice
    assert side.current_key() == "group:3"
    side.set_entries([e for e in ENTRIES if e.key != "group:3"])
    assert side.current_key() == "all"                         # its list is gone: back to All


def test_an_arrow_key_moves_and_picks_as_the_list_always_did(side):
    picked = []
    side.picked.connect(lambda e: picked.append(e.key))
    side.tree.setFocus()
    if not side.tree.hasFocus():
        pytest.skip("this platform does not give focus to an offscreen widget")
    side.tree.setCurrentItem(side.tree._find("all"))
    picked.clear()
    QTest.keyClick(side.tree, Qt.Key.Key_Down)
    assert picked, "an arrow key picked nothing"
    assert side.current_key() == picked[-1]


def test_the_name_on_a_card_is_the_application_font_bold_at_12_points(qapp):
    """2026-10-11, the owner: the name on a card was too big - the application's own font, bold, 12."""
    from PySide6.QtGui import QFont

    from app.ui.contacts.delegates import NAME_POINTS, name_font

    base = QFont("Segoe UI")
    base.setPointSizeF(9.0)
    f = name_font(base)
    assert f.bold() and f.family() == base.family() and f.pointSizeF() == NAME_POINTS == 12.0
    bigger = QFont(base)
    bigger.setPointSizeF(11.0)                                           # "Make text bigger"
    assert name_font(bigger).pointSizeF() > 12.0 and name_font(bigger).pixelSize() == -1
