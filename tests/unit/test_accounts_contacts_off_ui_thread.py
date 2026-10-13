"""Order 1k: mail and contacts work never runs on the UI thread.

Layer: L5

A hub, account-store or account-service call that touches SQLite, the network or a file
freezes the window for as long as it takes. The pages are built so that each of those runs on
the global `QThreadPool` and only the answer comes back to the UI thread. This proves it the
only way that cannot be argued with: every public method of `ContactHub`, `AccountStore` and
`AccountService` is wrapped to record the thread it ran on, the pages are driven through
their ordinary actions offscreen, and any call made on the thread that owns the
`QApplication` fails the test, naming the method and the line that called it.
"""

from __future__ import annotations

import inspect
import os
import threading
import traceback

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
pytest.importorskip("keyring")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.unit.test_contacts_view_world import card, settle, world      # noqa: E402,F401

#: Cheap in-memory reads and pure helpers: no SQLite, no network, nothing that can block.
_ALLOWED = {"close", "connect", "shutdown"}


def _wrap_class(monkeypatch, cls, hits: list) -> None:
    ui = threading.current_thread()
    for name, fn in list(vars(cls).items()):
        if name.startswith("_") or name in _ALLOWED or not inspect.isfunction(fn):
            continue

        def make(fn=fn, name=name):
            def wrapper(*a, **kw):
                if threading.current_thread() is ui:
                    where = [f for f in traceback.extract_stack()[:-1] if "app" + os.sep + "ui" in f.filename]
                    hits.append(f"{cls.__name__}.{name} called on the UI thread from "
                                f"{where[-1].filename.split('app' + os.sep)[-1]}:{where[-1].lineno}"
                                if where else f"{cls.__name__}.{name} called on the UI thread")
                return fn(*a, **kw)
            wrapper.__name__ = name
            return wrapper

        monkeypatch.setattr(cls, name, make())


@pytest.mark.slow
def test_the_contacts_page_reads_and_writes_only_on_workers(world, monkeypatch):
    from app.accounts.store import AccountStore
    from app.contacts.hub import ContactHub

    a = world.add(card("Ann", "Lee", email="ann@x.example"))
    b = world.add(card("Bob", "Bell"), kept=[world.a_work])
    page = world.page()                       # built and refreshed before the guard goes on
    hits: list[str] = []
    _wrap_class(monkeypatch, ContactHub, hits)
    _wrap_class(monkeypatch, AccountStore, hits)

    page.refresh()
    settle(page)
    page.select_ids([a])
    settle(page)
    page.sidebar.select_key(f"account:{world.a_work}")
    settle(page)
    page.sidebar.select_key("all")
    settle(page)
    page.select_ids([b])
    settle(page)
    page.delete_selected()
    settle(page)
    page.undo_last()
    settle(page)

    assert hits == [], "\n".join(sorted(set(hits)))


@pytest.mark.slow
def test_the_accounts_page_reads_and_writes_only_on_workers(tmp_path, monkeypatch):
    pytest.importorskip("keyring")
    from tests.unit.accounts_ui_fakes import make_service, qt, settle as settle_page

    qt()
    store, svc = make_service(tmp_path)
    svc.accounts.update_settings(dry_run_first=False)
    from app.accounts.service import AccountService
    from app.accounts.store import AccountStore
    from app.ui.accounts_page import AccountsPage

    page = AccountsPage(lambda: svc)
    page.ensure_built()
    hits: list[str] = []
    _wrap_class(monkeypatch, AccountService, hits)
    _wrap_class(monkeypatch, AccountStore, hits)
    try:
        page.refresh()
        settle_page(page)
        page.globals.same_for_all.setCurrentIndex(1)
        page.globals.flush()
        settle_page(page)
        page.refresh()
        settle_page(page)
        assert hits == [], "\n".join(sorted(set(hits)))
    finally:
        page.close()
        page.deleteLater()
        store.close()


def test_the_guard_does_catch_a_call_made_on_the_ui_thread(world, monkeypatch):
    """The control: without this a guard that never fires would pass for the right reason."""
    from app.contacts.hub import ContactHub

    hits: list[str] = []
    _wrap_class(monkeypatch, ContactHub, hits)
    world.hub.counts()
    assert any("ContactHub.counts" in h for h in hits)
