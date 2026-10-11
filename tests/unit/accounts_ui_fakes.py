"""Shared fixtures for the Settings > Accounts tests: a real service over a temp store, a
memory keyring, a Qt application, and a way to wait for a worker.

Layer: L5
"""

from __future__ import annotations

import time

import keyring  # noqa: F401 - the backend class below
from keyring.backend import KeyringBackend

from app.accounts.secrets import SecretStore
from app.accounts.service import AccountService
from app.storage.sqlite_store import SqliteStore


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def get_password(self, service, username):
        return self.data.get((service, username))

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


def make_service(tmp_path, **kw):
    store = SqliteStore(tmp_path / "ui.db").connect()
    svc = AccountService(store, secrets=SecretStore(backend=MemoryKeyring()), **kw)
    store._account_service = svc          # what `account_runtime.service_for_store` hands out
    return store, svc


def qt():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def wait_until(predicate, timeout_s: float = 8.0) -> bool:
    """Pump the event loop until `predicate()`: a worker reports through a queued signal."""
    app = qt()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def settle(page, timeout_s: float = 8.0) -> None:
    """Wait until the page has no read in flight and nothing is queued behind it."""
    wait_until(lambda: not page._reading, timeout_s)
    wait_until(lambda: False, 0.15)
    wait_until(lambda: not page._reading, timeout_s)
