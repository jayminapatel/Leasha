"""Quick actions on a contact: the right-click menu (6r), and the list's Ctrl+C.

Layer: L5

Edit, copy an email address or a phone number, copy the contact as a vCard, show its QR code (and
open its address in the maps site the person chose - the QR tool does that, nothing is sent
anywhere until they click), favourite, export, enrich, delete. Built by the one menu builder
(`app/ui/widgets/menus.py`, UI phase B): an icon on every line, **Edit** first and bold (it is what
Enter and a double-click do), the copy lines together, Move to the bin last with the danger icon;
each line shows its key where the list answers one (Enter, Ctrl+C, Del). The words are the ones
this menu always had. Copying a vCard reads the contact on a worker; the clipboard is set when it
lands.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu

from app.contacts import vcard
from app.ui.contacts import loader
from app.ui.contacts.toolbar import enrich_words
from app.ui.qtsip import open_menu
from app.ui.widgets.menus import Item, build

__all__ = ["show_menu", "copy_text", "menu_items", "copy_picked"]


def copy_text(text: str) -> None:
    clip = QGuiApplication.clipboard()
    if clip is not None:
        clip.setText(text)


def copy_picked(page: Any) -> str:
    """Ctrl+C on the list: the email address of every picked contact that has one, "; " between them
    (ready for an email's To box). Returns what was copied ("" when none had an address)."""
    rows = [r for r in (page.model.row_for_id(i) for i in page.selected_ids()) if r is not None]
    text = "; ".join(r.email for r in rows if r.email)
    if text:
        copy_text(text)
    return text


def menu_items(page: Any) -> list[Item]:
    """The lines for the page's picked contacts, in the builder's terms."""
    ids = page.selected_ids()
    rows = [r for r in (page.model.row_for_id(i) for i in ids) if r is not None]
    if not rows:
        return []
    if page._entry.kind == "bin":
        return [Item("Restore", lambda: page._bulk("restore", None), group="open", icon="archive-restore",
                     default=True)]
    one = rows[0] if len(rows) == 1 else None
    items: list[Item] = []
    if one is not None:
        items.append(Item("Edit", page.edit_current, group="open", icon="pencil", default=True,
                          shortcut="Return"))
        if one.email:
            items.append(Item("Copy email address", lambda: copy_text(one.email), group="copy", icon="mail",
                              shortcut="Ctrl+C"))
        if one.phone:
            items.append(Item("Copy phone number", lambda: copy_text(one.phone), group="copy", icon="phone"))
        items.append(Item("Copy as vCard", lambda: copy_vcard(page, one.id), group="copy", icon="copy"))
        items.append(Item("Show QR code or map…", lambda: page.open_tool("qr"), group="act", icon="qr-code"))
    items += [
        Item("Favourite (or not)", lambda: page._bulk("favourite", None), group="act", icon="star"),
        Item("Export…", lambda: page.open_tool("export"), group="act", icon="download"),
        Item(enrich_words(len(rows)), lambda: page.open_tool("enrich"), group="act", icon="wand-sparkles"),
        Item("Move to the bin", lambda: page._bulk("delete", None), group="danger", icon="trash-2",
             shortcut="Del"),
    ]
    return items


def show_menu(page: Any, global_pos: Any) -> QMenu:
    """Build (and pop up) the menu for the page's picked contacts."""
    items = menu_items(page)
    menu = build(page, items)
    if items:
        open_menu(menu, global_pos)
    return menu


def copy_vcard(page: Any, cid: int) -> None:
    hub = page.hub

    def work() -> str:
        card = hub.get_card(cid)
        return vcard.dumps(card) if card is not None else ""

    loader.run_task(page, work, lambda text: (copy_text(text), page.notice.emit("Copied the contact as a vCard."))
                    if text else None)
