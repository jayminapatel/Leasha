"""Order 1k 0b: with accounts connected, search and Chat (web off) still open no connection.

Layer: L7

The Chat order's guard says a turn with the web switch off opens no non-loopback
connection. This joins it: the same holds **with accounts connected**, and only
the account worker (and enrichment, when the person has switched it on) may reach
out - to the hosts pinned here and nowhere else.

What is covered here, and what is not: a keyword search, a Chat turn with the web
switch off, a scan of a folder, reading account status, asking what is due and
hub edits all run under a socket guard with two accounts in the index. A full
index run with the embedding models cannot run in the Linux cloud session (the
models are the owner's) - it is the same code path as the scan above plus the
embedder, which opens no socket (UNVERIFIED on Windows).
"""

from __future__ import annotations

import socket
from datetime import datetime

import keyring
import pytest
from keyring.backend import KeyringBackend

from app.accounts import http as accounts_http
from app.accounts.service import AccountService
from app.accounts.secrets import SecretStore, account_key
from app.accounts.store import ScheduleSetting
from tests.unit.accounts_fakes import person
from tests.unit.chat_env import Env, ask
from tests.fixtures import chat_eval as fx

LOCAL = ("127.0.0.1", "localhost", "::1", "", "None")


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def set_password(self, s, u, p):
        self.data[(s, u)] = p

    def get_password(self, s, u):
        return self.data.get((s, u))

    def delete_password(self, s, u):
        self.data.pop((s, u), None)


@pytest.fixture
def only_this_computer(monkeypatch):
    seen: list[str] = []
    real_create, real_gai = socket.create_connection, socket.getaddrinfo

    def create_connection(address, *a, **k):
        seen.append(str(address[0]))
        if str(address[0]) not in LOCAL:
            raise ConnectionRefusedError(f"blocked: {address[0]!r}")
        return real_create(address, *a, **k)

    def getaddrinfo(host, *a, **k):
        seen.append(str(host))
        if str(host) not in LOCAL:
            raise OSError(f"blocked: {host!r}")
        return real_gai(host, *a, **k)

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return seen


def _remote(seen):
    return [h for h in seen if h not in LOCAL]


@pytest.fixture
def connected(tmp_path):
    keyring.set_keyring(MemoryKeyring())
    env = Env(tmp_path)
    svc = AccountService(env.store, secrets=SecretStore(backend=keyring.get_keyring()))
    for name, provider in (("Home", "google"), ("Work", "microsoft")):
        aid = svc.accounts.add(provider, f"{name.lower()}@example.com", name, mail_on=True)
        svc.secrets.put(account_key(aid, "refresh_token"), "refresh-" + name)
    svc.accounts.update_settings(contacts=ScheduleSetting("interval", 5), mail=ScheduleSetting("interval", 5))
    yield env, svc
    env.close()


def test_search_and_a_chat_turn_with_the_web_off_open_nothing_with_accounts_connected(connected, only_this_computer):
    env, svc = connected
    hits = env.search.search("notice tenant")
    assert hits is not None
    eng = env.engine("extractive", web_enabled=False)
    turn, _events = ask(eng, "How much notice must the tenant give?")
    assert turn.text
    assert _remote(only_this_computer) == []


def test_reading_status_asking_what_is_due_and_editing_contacts_open_nothing(connected, only_this_computer):
    env, svc = connected
    svc.status(datetime.now())
    svc.due_work(datetime.now())                       # asking never starts anything (D11.2)
    svc.hold_reason(datetime.now())
    cid = svc.hub.create(person("Ann", "Lee", email="ann@x.com"))
    svc.hub.search("ann")
    svc.hub.delete(cid)
    assert _remote(only_this_computer) == []
    assert svc.accounts.runs() == []                   # and no sync was recorded either


@pytest.mark.slow
def test_a_scan_of_a_folder_opens_nothing_with_accounts_connected(connected, only_this_computer, tmp_path):
    from app.cli import main
    from tests.unit.test_cli_wiring import env_file
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("hello world", encoding="utf-8")
    assert main(["--env", env_file(tmp_path), "scan", str(docs)]) in (0, 1)
    assert _remote(only_this_computer) == []


def test_the_hosts_the_account_worker_may_reach_are_pinned():
    """Add a host here only with the owner's say-so: it is the privacy statement in code."""
    assert accounts_http.PROVIDER_HOSTS == {
        "google": ("accounts.google.com", "oauth2.googleapis.com", "people.googleapis.com",
                   "gmail.googleapis.com", "www.googleapis.com"),
        "microsoft": ("login.microsoftonline.com", "graph.microsoft.com"),
        "apple": ("contacts.icloud.com", "imap.mail.me.com"),
    }
    enrich = getattr(accounts_http, "ENRICHMENT_HOSTS", None)
    if enrich is not None:                              # section 7: only these, and only when switched on
        flat = {h for hosts in enrich.values() for h in hosts}
        assert flat <= {"www.gravatar.com", "gravatar.com", "www.wikidata.org", "query.wikidata.org"}


def test_nothing_outside_the_account_packages_imports_the_account_transport():
    """Only the account worker, and enrichment, may use the transport that can leave the machine."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    allowed_prefixes = ("accounts", "contacts/enrich", "cli/accounts.py", "cli/contacts.py",
                        "ui/account", "ui/contacts")
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(allowed_prefixes):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "app.accounts.http" in text or "UrllibTransport" in text:
            offenders.append(rel)
    assert offenders == []
