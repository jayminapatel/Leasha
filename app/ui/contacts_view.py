r"""The Contacts tab: a full contact manager - your people, kept in the accounts you choose.

Layer: L5

Order 1k section 6 (6a: the pill; D14). The rail entry is "Contacts", icon `contact`, after
Files. **The page is built the first time it is shown**, not when the window is: the table, the
cards, the sidebar and the editor are real cost, and nothing needs them until somebody clicks
the pill (order 0r's start-up budget; the numbers are in the order's close-out). Until then this
is an empty widget.

All the work is in `app/ui/contacts/` - `page.py` wires it; every read and write of the contact
hub is on a worker (rule 5). The window gives this page its store, the optional `service` that
runs account syncs (`push_accounts(account_ids)`), and an optional `host` that may offer
`pick_photo_from_person(contact_id)` for the editor's "from my photos" button.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from app.ui.later import later

__all__ = ["ContactsView"]


class ContactsView(QWidget):
    error = Signal(object)
    notice = Signal(str)

    def __init__(self, store: Any, parent: Optional[QWidget] = None, *, service: Any = None,
                 host: Any = None) -> None:
        super().__init__(parent)
        self._store = store
        self._service = service
        self._host = host
        self.page: Any = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout

    # ---------------------------------------------------------------- building
    def is_built(self) -> bool:
        return self.page is not None

    def ensure_built(self) -> Any:
        """Build the page once. Idempotent; the first call is the cost the window avoided."""
        if self.page is not None:
            return self.page
        from app.accounts.store import AccountStore
        from app.contacts import phone as phones
        from app.contacts.hub import ContactHub
        from app.ui.contacts.page import ContactsPage

        accounts = AccountStore(self._store)
        try:
            region = accounts.settings().phone_region or phones.DEFAULT_REGION
        except Exception:                                  # noqa: BLE001 - a default region is fine
            region = phones.DEFAULT_REGION
        hub = ContactHub(self._store, region=region)
        self.page = ContactsPage(self._store, hub, accounts, service=self._service, host=self._host)
        self.page.error.connect(self.error.emit)
        self.page.notice.connect(self.notice.emit)
        self._layout.addWidget(self.page)
        return self.page

    @property
    def hub(self) -> Any:
        return self.ensure_built().hub

    def showEvent(self, event: Any) -> None:                      # noqa: N802
        super().showEvent(event)
        if self.page is None:
            later(self, 0, self._first_show)

    def _first_show(self) -> None:
        if self.page is None:
            self.ensure_built().refresh()

    # ---------------------------------------------------- what the window calls
    def refresh(self) -> None:
        """The tab came forward: build on the first visit, re-read on every later one."""
        self.ensure_built().refresh()

    def focus(self) -> None:
        page = self.ensure_built()
        page.focus_filter()

    def new_contact(self) -> None:
        self.ensure_built().new_contact()

    def retint(self, colours: dict) -> None:
        if self.page is not None:
            self.page.retint(colours)

    def shutdown(self) -> None:
        if self.page is not None:
            self.page.shutdown()
