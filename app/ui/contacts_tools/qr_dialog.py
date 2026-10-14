"""The QR code of a contact and its quick actions (order 1k 6r).

Layer: L5

A phone camera pointed at the code adds the contact. The code is drawn here from the card's vCard
(`qr.vcard_qr_svg`), always dark on white, with no network. Beside it:

* **Copy vCard** puts the card on the clipboard as vCard text.
* **Copy email** and **Copy phone** put the main address or number on the clipboard.
* **Open address in maps** opens the person's chosen maps site (Settings > Accounts, `maps_site`)
  in their browser, **only when they press it**; with several addresses the button offers a menu.
  Nothing is sent anywhere before that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import QByteArray, QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.contacts.card import Card, Field, display_name
from app.ui.contacts_tools._common import ToolDialog, btn, named

__all__ = ["QrDialog", "copy_text", "open_url", "MAP_NAMES", "render_svg"]

MAP_NAMES = {"google": "Google Maps", "openstreetmap": "OpenStreetMap", "apple": "Apple Maps", "bing": "Bing Maps"}
_QR_PX = 300


def copy_text(text: str) -> None:
    """Put `text` on the clipboard. A function of its own so a test can watch it."""
    QApplication.clipboard().setText(text)


def open_url(url: str) -> bool:
    """Open `url` in the person's browser. Called only from a click."""
    return QDesktopServices.openUrl(QUrl(url))


def render_svg(svg: str, px: int = _QR_PX) -> QPixmap:
    from PySide6.QtSvg import QSvgRenderer                   # noqa: PLC0415

    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.GlobalColor.white)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if renderer.isValid():
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
    return pixmap


@dataclass
class _Loaded:
    card: Optional[Card] = None
    svg: str = ""
    error: str = ""
    maps_site: str = "openstreetmap"


def load_qr(ctx: Any, contact_id: int) -> _Loaded:
    """Read the card and the maps choice, and draw the code. A worker's job."""
    from app.contacts import qr                              # noqa: PLC0415

    card = ctx.hub.get_card(contact_id)
    if card is None:
        return _Loaded(error="That contact is no longer in the book.")
    site = "openstreetmap"
    store = getattr(ctx, "accounts", None)
    if store is not None:
        site = getattr(store.settings(), "maps_site", site) or site
    try:
        return _Loaded(card, qr.vcard_qr_svg(card), maps_site=site)
    except ValueError as exc:
        return _Loaded(card, error=str(exc), maps_site=site)


class QrDialog(ToolDialog):
    def __init__(self, ctx: Any, parent: Optional[QWidget] = None, contact_id: Optional[int] = None) -> None:
        super().__init__(ctx, parent, title="QR code and quick actions", glyph="qr-code",
                         subtitle="Point a phone camera at the code to add this contact. Nothing leaves this computer "
                                  "unless you press Open address in maps.",
                         size=(640, 520), name="contacts-qr")
        ids = list(getattr(ctx, "selected_ids", []) or [])
        self.contact_id = contact_id if contact_id is not None else (ids[0] if ids else None)
        self.card: Optional[Card] = None
        self.site = "openstreetmap"
        self.qr_pixmap: Optional[QPixmap] = None

        row = QHBoxLayout()
        self.image = QLabel()
        self.image.setFixedSize(QSize(_QR_PX, _QR_PX))
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        named(self.image, "QR code", "A QR code holding this contact's details. Scan it with a phone camera")
        side = QVBoxLayout()
        self.name_label = QLabel("")
        self.name_label.setWordWrap(True)
        named(self.name_label, "Contact name", "Whose code this is")
        self.copy_vcard = btn("Copy vCard", "copy", "secondary", "Copy this contact as vCard text", "Copy vCard")
        self.copy_email_button = btn("Copy email", "mail", "secondary", "Copy the main email address", "Copy email")
        self.copy_phone_button = btn("Copy phone", "phone", "secondary", "Copy the main phone number", "Copy phone")
        self.maps_button = btn("Open address in maps", "map-pin", "secondary",
                               "Open the address in your maps site. Nothing is sent until you press this", "Open address in maps")
        self.copy_vcard.clicked.connect(self.do_copy_vcard)
        self.copy_email_button.clicked.connect(self.do_copy_email)
        self.copy_phone_button.clicked.connect(self.do_copy_phone)
        self.maps_button.clicked.connect(self.do_open_maps)
        side.addWidget(self.name_label)
        for b in (self.copy_vcard, self.copy_email_button, self.copy_phone_button, self.maps_button):
            side.addWidget(b)
            b.setEnabled(False)
        side.addStretch(1)
        row.addWidget(self.image)
        row.addLayout(side, 1)
        self.body.addLayout(row)
        self.body.addStretch(1)

        if self.contact_id is None:
            self.say("Select a contact first.")
            return
        self.busy(True, "Drawing the code…")
        ctx_, cid = ctx, self.contact_id
        self.job(lambda: load_qr(ctx_, cid), self._loaded)

    def _loaded(self, loaded: _Loaded) -> None:
        self.busy(False)
        self.card = loaded.card
        self.site = loaded.maps_site if loaded.maps_site in MAP_NAMES else "openstreetmap"
        if loaded.card is not None:
            self.name_label.setText(f"<b>{display_name(loaded.card)}</b>")
        if loaded.svg:
            self.qr_pixmap = render_svg(loaded.svg)
            self.image.setPixmap(self.qr_pixmap)
        if loaded.error:
            self.show_error(loaded.error)
        if loaded.card is None:
            return
        from app.contacts import quick_actions as qa          # noqa: PLC0415

        self.copy_vcard.setEnabled(True)
        self.copy_email_button.setEnabled(bool(qa.copy_email(loaded.card)))
        self.copy_phone_button.setEnabled(bool(qa.copy_phone(loaded.card)))
        addresses = self._addresses()
        self.maps_button.setEnabled(bool(addresses))
        name = MAP_NAMES[self.site]
        self.maps_button.setToolTip(f"Open the address in {name}. Nothing is sent until you press this")

    # -- actions -------------------------------------------------------------
    def _addresses(self) -> list[Field]:
        if self.card is None:
            return []
        return [f for f in self.card.values("address") if f.value.strip() or any(f.data.values())]

    def do_copy_vcard(self) -> None:
        from app.contacts import quick_actions as qa          # noqa: PLC0415

        if self.card is not None:
            copy_text(qa.card_as_vcard(self.card))
            self.say("The vCard is on the clipboard.")

    def do_copy_email(self) -> None:
        from app.contacts import quick_actions as qa          # noqa: PLC0415

        text = qa.copy_email(self.card) if self.card is not None else ""
        if text:
            copy_text(text)
            self.say(f"Copied {text}.")

    def do_copy_phone(self) -> None:
        from app.contacts import quick_actions as qa          # noqa: PLC0415

        text = qa.copy_phone(self.card) if self.card is not None else ""
        if text:
            copy_text(text)
            self.say(f"Copied {text}.")

    def do_open_maps(self) -> None:
        addresses = self._addresses()
        if not addresses:
            return
        if len(addresses) == 1:
            self.open_address(addresses[0])
            return
        from app.ui.widgets import menus                      # noqa: PLC0415

        items = []
        for f in addresses:
            text = (f.label + ": " if f.label else "") + (f.value.replace("\n", ", ") or ", ".join(
                v for v in f.data.values() if v))
            items.append(menus.Item(text, lambda field=f: self.open_address(field), icon="map-pin"))
        menu = menus.build(self, items, grouped=False)
        named(menu, "Choose an address", "Which address to open in maps")
        menu.exec(self.maps_button.mapToGlobal(self.maps_button.rect().bottomLeft()))

    def open_address(self, address: Field) -> None:
        from app.contacts import quick_actions as qa          # noqa: PLC0415

        try:
            url = qa.maps_url(address, self.site)
        except ValueError as exc:
            self.show_error(str(exc))
            return
        open_url(url)
        self.say(f"Opened in {MAP_NAMES[self.site]}.")
