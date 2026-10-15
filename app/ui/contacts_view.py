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
from PySide6.QtWidgets import QWidget

from app.ui.later import later
from app.ui.widgets.page_frame import PageFrame

__all__ = ["ContactsView"]


def _import_page() -> bool:
    """`ContactsView.warm`'s work, on a pool thread: the imports `ensure_built` makes, and nothing else."""
    import app.ui.contacts.page  # noqa: F401, PLC0415 - the import is the work
    from app.ui.widgets.icons import preload  # noqa: PLC0415

    preload()
    return True


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
        layout = PageFrame(self)                 # the one page frame (UI phase B)
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
        # The phone region is read with the first snapshot, on a worker (`loader.load_snapshot`).
        hub = ContactHub(self._store, region=phones.DEFAULT_REGION)
        self.page = ContactsPage(self._store, hub, accounts, service=self._service, host=self._host)
        self.page.error.connect(self.error.emit)
        self.page.notice.connect(self.notice.emit)
        self._layout.addWidget(self.page)
        return self.page

    def warm(self) -> Any:
        """Import the page's code on a worker, so the first visit does not (2026-10-15). **Worker.**

        Importing `app.ui.contacts.page` and the 25 modules it brings was 150-250 ms on the window's
        thread at the first click on Contacts, measured in `gui_mainwindow` - most of that visit, which
        `test_navigation_is_instant.py` holds to 400 ms. An import builds no widget and opens no store,
        so it may run on a pool thread; Python's import lock makes a click that lands first wait for it,
        never import it twice. The window calls this from `_start_background_work`, with the other
        warm-ups. Returns the worker, or None when the page is already built.
        """
        if self.page is not None:
            return None
        from PySide6.QtCore import QThreadPool

        from app.ui.workers import CallableWorker, run

        return run(QThreadPool.globalInstance(), CallableWorker(_import_page, component="ui.contacts.warm"))

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

    def show_contact(self, contact_id: int) -> None:
        """Select one contact and show it (a search hit's card opens here)."""
        self.ensure_built().show_contact(int(contact_id))

    def retint(self, colours: dict) -> None:
        if self.page is not None:
            self.page.retint(colours)

    def shutdown(self) -> None:
        if self.page is not None:
            self.page.shutdown()
