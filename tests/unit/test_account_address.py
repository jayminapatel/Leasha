"""Order 1k: a Microsoft account's address is read from the signed-in profile.

Layer: L2

Seen on the owner's index (2026-10-11): MS Main kept the placeholder `pending-<time>` because
Microsoft's sign-in carries no email. `AccountService.resolve_address` fills it in once.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("keyring")

from app.accounts.provider import HttpResponse                     # noqa: E402
from app.accounts.secrets import SecretStore                       # noqa: E402
from app.accounts.service import AccountService                    # noqa: E402
from app.storage.sqlite_store import SqliteStore                   # noqa: E402
from tests.unit.accounts_ui_fakes import MemoryKeyring             # noqa: E402


class _Graph:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.seen = status, body if body is not None else {}, []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.seen.append((method, url, (headers or {}).get("Authorization")))
        return HttpResponse(self.status, {}, json.dumps(self.body).encode())


def _service(tmp_path, transport):
    store = SqliteStore(tmp_path / "addr.db").connect()
    svc = AccountService(store, secrets=SecretStore(backend=MemoryKeyring()), transport=transport)
    svc.token_source = lambda account: (lambda: "tok")          # no real sign-in here
    return store, svc


def test_a_pending_microsoft_address_is_read_from_the_profile(tmp_path):
    graph = _Graph(body={"mail": None, "userPrincipalName": "me@work.example"})
    store, svc = _service(tmp_path, graph)
    try:
        aid = svc.accounts.add("microsoft", "pending-1791709065", "MS Main")
        assert svc.resolve_address(svc.accounts.get(aid)) == "me@work.example"
        assert svc.accounts.get(aid).address == "me@work.example"
        assert svc.accounts.get(aid).name == "MS Main"                    # the owner's own name stays
        assert graph.seen == [("GET", "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName",
                               "Bearer tok")]
    finally:
        store.close()


def test_a_real_address_or_another_provider_is_never_asked_about(tmp_path):
    graph = _Graph(body={"mail": "x@y.example"})
    store, svc = _service(tmp_path, graph)
    try:
        a = svc.accounts.add("microsoft", "me@work.example", "Has one")
        b = svc.accounts.add("google", "pending-1", "Google")
        assert svc.resolve_address(svc.accounts.get(a)) == "me@work.example"
        assert svc.resolve_address(svc.accounts.get(b)) == "pending-1"
        assert graph.seen == []
    finally:
        store.close()


def test_a_failed_lookup_keeps_the_placeholder_and_raises_nothing(tmp_path):
    store, svc = _service(tmp_path, _Graph(status=403))
    try:
        aid = svc.accounts.add("microsoft", "pending-7", "MS")
        assert svc.resolve_address(svc.accounts.get(aid)) == "pending-7"
        assert svc.accounts.get(aid).address == "pending-7"
    finally:
        store.close()
