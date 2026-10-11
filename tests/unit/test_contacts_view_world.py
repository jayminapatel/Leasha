"""The small world the Contacts page tests share: a store, three accounts, a hub and a page.

Layer: L5

Order 1k section 6. Offscreen Qt; every read the page makes is a real worker on the real
`QThreadPool`, so `settle` drains chains of them the way `test_photo_tagger_page` does.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool                                  # noqa: E402
from PySide6.QtWidgets import QApplication                              # noqa: E402

from app.accounts.store import AccountStore                             # noqa: E402
from app.contacts.card import Card, Field                               # noqa: E402
from app.contacts.hub import ContactHub                                 # noqa: E402
from app.storage.sqlite_store import SqliteStore                        # noqa: E402


def settle(page=None, rounds: int = 60) -> None:
    """Run workers and deliver their signals until the page has nothing in flight."""
    app = QApplication.instance()
    quiet = 0
    for _ in range(rounds):
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()
        if page is None or getattr(page, "_busy", 0) == 0:
            quiet += 1
            if quiet >= 3:
                return
        else:
            quiet = 0


def card(given="Ann", family="Lee", *, email="", phone="", company="", title="", groups=(), notes="") -> Card:
    c = Card(scalars={k: v for k, v in (("given", given), ("family", family), ("company", company),
                                        ("title", title), ("notes", notes)) if v})
    if email:
        c.fields.append(Field("email", email, "work", primary=True))
    if phone:
        c.fields.append(Field("phone", phone, "mobile"))
    c.groups = list(groups)
    return c


class World:
    def __init__(self, tmp_path):
        self.store = SqliteStore(tmp_path / "contacts.db").connect()
        self.accounts = AccountStore(self.store)
        self.hub = ContactHub(self.store)
        self.a_home = self.accounts.add("google", "ann@gmail.example", "Home Google", colour="#4C8BF5")
        self.a_work = self.accounts.add("microsoft", "ann@work.example", "Work", colour="#E8710A")
        self.a_fast = self.accounts.add("carddav", "ann@fast.example", "Fastmail", server="dav.example",
                                        colour="#188038")
        self.pages: list = []

    def add(self, c: Card, kept=None) -> int:
        return self.hub.create(c, kept_in=kept if kept is not None else [self.a_home])

    def page(self, **kw):
        from app.ui.contacts.page import ContactsPage

        page = ContactsPage(self.store, self.hub, self.accounts, **kw)
        page.resize(1200, 760)
        page.show()
        self.pages.append(page)
        page.refresh()
        settle(page)
        return page

    def close(self) -> None:
        for p in self.pages:
            try:
                p.shutdown()
                p.hide()
            except RuntimeError:
                pass
        self.store.close()


@pytest.fixture
def world(tmp_path, qapp):
    w = World(tmp_path)
    yield w
    w.close()


def test_the_world_builds_a_page_that_lists_what_the_hub_holds(world):
    world.add(card("Ann", "Lee", email="ann@x.example"))
    world.add(card("Bob", "Bell"), kept=[world.a_work])
    page = world.page()
    names = {page.model.row_at(i).display_name for i in range(page.model.rowCount())}
    assert names == {"Ann Lee", "Bob Bell"}
