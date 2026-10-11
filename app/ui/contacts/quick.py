"""Quick actions on a contact: the right-click menu (6r).

Layer: L5

Copy an email address or a phone number, copy the contact as a vCard, show its QR code (and open
its address in the maps site the person chose - the QR tool does that, nothing is sent anywhere
until they click), favourite, delete. Each carries its icon (6m). Copying a vCard reads the
contact on a worker; the clipboard is set when it lands.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu

from app.contacts import vcard
from app.ui.contacts import loader
from app.ui.qtsip import open_menu
from app.ui.theme import theme_colours
from app.ui.widgets.icons import icon

__all__ = ["show_menu", "copy_text"]


def copy_text(text: str) -> None:
    clip = QGuiApplication.clipboard()
    if clip is not None:
        clip.setText(text)


def show_menu(page: Any, global_pos: Any) -> QMenu:
    """Build (and pop up) the menu for the page's picked contacts."""
    c = theme_colours()
    dim = c.get("text_dim", "#888888")
    ids = page.selected_ids()
    rows = [r for r in (page.model.row_for_id(i) for i in ids) if r is not None]
    menu = QMenu(page)
    if not rows:
        return menu
    one = rows[0] if len(rows) == 1 else None
    if page._entry.kind == "bin":
        menu.addAction(icon("archive-restore", dim), "Restore", lambda: page._bulk("restore", None))
    else:
        if one is not None:
            if one.email:
                menu.addAction(icon("mail", dim), "Copy email address", lambda: copy_text(one.email))
            if one.phone:
                menu.addAction(icon("phone", dim), "Copy phone number", lambda: copy_text(one.phone))
            menu.addAction(icon("copy", dim), "Copy as vCard", lambda: copy_vcard(page, one.id))
            menu.addAction(icon("qr-code", dim), "Show QR code or map…", lambda: page.open_tool("qr"))
            menu.addSeparator()
        menu.addAction(icon("star", dim), "Favourite (or not)", lambda: page._bulk("favourite", None))
        menu.addAction(icon("download", dim), "Export…", lambda: page.open_tool("export"))
        menu.addSeparator()
        menu.addAction(icon("trash-2", c.get("danger", "#a32020")), "Move to the bin",
                       lambda: page._bulk("delete", None))
    open_menu(menu, global_pos)
    return menu


def copy_vcard(page: Any, cid: int) -> None:
    hub = page.hub

    def work() -> str:
        card = hub.get_card(cid)
        return vcard.dumps(card) if card is not None else ""

    loader.run_task(page, work, lambda text: (copy_text(text), page.notice.emit("Copied the contact as a vCard."))
                    if text else None)
