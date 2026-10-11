"""Order 1k 10b, 10c, 10e-10i, 1f: the account service and what starts a sync.

Layer: L7
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import keyring
import pytest
from keyring.backend import KeyringBackend

from app.accounts.secrets import SecretStore, account_key
from app.accounts.service import AccountService
from app.accounts.store import ScheduleSetting
from app.storage.sqlite_store import SqliteStore
from tests.unit.accounts_fakes import FakeProvider, FakeServer, person


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


@pytest.fixture
def svc(tmp_path):
    backend = MemoryKeyring()
    store = SqliteStore(tmp_path / "k.db").connect()
    providers = {}
    service = AccountService(store, secrets=SecretStore(backend=backend),
                             provider_for=lambda a: providers[a.id])
    service.accounts.update_settings(dry_run_first=False)
    service._providers = providers
    service._backend = backend
    yield service
    store.close()


def add(svc, name="Home", **kw):
    aid = svc.accounts.add("google", f"{name.lower()}@x.com", name, **kw)
    server = FakeServer()
    svc._providers[aid] = FakeProvider(server)
    return aid, server


NOW = datetime(2026, 10, 12, 10, 0, 0)


def test_a_manual_schedule_means_nothing_is_ever_due(svc):
    # 2026-10-11: the default became hourly (see the next test); Manual is the person's choice.
    svc.accounts.update_settings(mail=ScheduleSetting("manual"), contacts=ScheduleSetting("manual"))
    add(svc)
    assert svc.due_work(NOW) == []
    assert svc.due_work(NOW + timedelta(days=30), window_opened_at=NOW) == []     # not even a catch-up


def test_a_timed_schedule_is_due_after_its_interval_and_only_then(svc):
    aid, server = add(svc)
    server.add(person("Ann", "Lee"))
    svc.accounts.update_settings(contacts=ScheduleSetting("interval", 60))
    out = svc.sync_now(aid)
    assert out[0].status == "ok"
    last = datetime.fromtimestamp(svc.accounts.get(aid).last_synced_at)
    assert svc.due_work(last + timedelta(minutes=30)) == []
    due = svc.due_work(last + timedelta(minutes=61))
    assert [(w.account.id, w.kind) for w in due] == [(aid, "contacts")]


def test_a_fortnights_sleep_syncs_once_not_once_per_missed_hour(svc):
    aid, server = add(svc)
    server.add(person("Ann", "Lee"))
    svc.accounts.update_settings(contacts=ScheduleSetting("interval", 60))
    svc.sync_now(aid)
    later = datetime.fromtimestamp(svc.accounts.get(aid).last_synced_at) + timedelta(days=14)
    assert len(svc.due_work(later)) == 1
    outcomes = svc.run_due(later)
    assert len(outcomes) == 1
    assert svc.accounts.runs(account_id=aid)[0].trigger == "schedule"


def test_one_catch_up_after_a_closed_spell(svc):
    """D16: the app was closed over the due time; one sync a minute after the window opens."""
    aid, server = add(svc)
    svc.accounts.update_settings(contacts=ScheduleSetting("daily", 60, "02:00"))
    svc.sync_now(aid)
    last = datetime.fromtimestamp(svc.accounts.get(aid).last_synced_at)
    opened = last + timedelta(days=3)
    assert svc.due_work(opened, window_opened_at=opened)                           # overdue already
    assert [w.reason for w in svc.due_work(opened + timedelta(minutes=1), window_opened_at=opened)]


def test_pause_and_quiet_hours_hold_the_schedule_but_not_sync_now(svc):
    aid, server = add(svc)
    svc.accounts.update_settings(contacts=ScheduleSetting("interval", 5))
    svc.sync_now(aid)
    t = datetime.fromtimestamp(svc.accounts.get(aid).last_synced_at) + timedelta(hours=2)
    assert svc.due_work(t)
    svc.accounts.update_settings(paused_until=(t + timedelta(hours=1)).timestamp())
    assert svc.due_work(t) == [] and "Paused" in svc.hold_reason(t)
    assert svc.sync_now(aid)[0].status == "ok"                                    # Sync now still works
    svc.accounts.update_settings(paused_until=None, quiet_on=True, quiet_start="09:00", quiet_end="11:00")
    assert svc.due_work(datetime(2026, 10, 12, 10, 0)) == []
    assert "Quiet hours" in svc.hold_reason(datetime(2026, 10, 12, 10, 0))


def test_each_account_can_have_its_own_schedule(svc):
    a, _ = add(svc, "A")
    b, _ = add(svc, "B")
    svc.accounts.update_settings(same_for_all=False, contacts=ScheduleSetting("manual"))
    svc.accounts.update(a, options={"contacts_schedule": {"mode": "interval", "minutes": 15}})
    svc.accounts.update(b, options={"contacts_schedule": {"mode": "interval", "minutes": 240}})
    svc.sync_now()
    base = datetime.fromtimestamp(max(x.last_synced_at for x in svc.accounts.list()))
    due = svc.due_work(base + timedelta(minutes=20))
    assert [w.account.id for w in due] == [a]
    due = svc.due_work(base + timedelta(minutes=250))
    assert sorted(w.account.id for w in due) == sorted([a, b])


def test_a_failed_account_backs_off_and_a_signed_out_one_waits_for_the_person(svc):
    from app.core.errors import AppErrorException, make_error
    aid, _ = add(svc)
    svc.accounts.update_settings(contacts=ScheduleSetting("interval", 5))
    svc._providers[aid].fail_with = AppErrorException(make_error("ERR_ACCOUNT_UNREACHABLE", "t", account="Home", reason="x"))
    svc.sync_now(aid)
    st = svc.status(datetime.now())[0]
    assert st.word == "Failed"
    svc._providers[aid].fail_with = AppErrorException(make_error("ERR_ACCOUNT_SIGNIN_EXPIRED", "t", account="Home"))
    svc.sync_now(aid)
    assert svc.status(datetime.now())[0].word == "Needs sign-in"
    assert svc.due_work(datetime.now() + timedelta(days=2)) == []


def test_two_sync_nows_fold_into_one(svc):
    aid, server = add(svc)
    gate = threading.Event()
    release = threading.Event()
    orig = svc._providers[aid].pull

    def slow_pull(token):
        gate.set()
        release.wait(5)
        yield from orig(token)
    svc._providers[aid].pull = slow_pull
    results = []
    t = threading.Thread(target=lambda: results.append(svc.sync_now(aid)))
    t.start()
    assert gate.wait(5)
    assert svc.is_running(aid)
    assert svc.sync_now(aid) == []                                                  # folded, not queued
    release.set()
    t.join(5)
    assert len(results[0]) == 1 and not svc.is_running(aid)


def test_nothing_but_the_schedule_and_sync_now_starts_a_sync(svc):
    """D11.2: building the service, reading status, asking what is due: no run is recorded."""
    aid, _ = add(svc)
    svc.accounts.update_settings(contacts=ScheduleSetting("interval", 5))
    svc.status(NOW)
    svc.due_work(NOW)
    svc.hold_reason(NOW)
    assert svc.accounts.runs() == []


def test_removing_an_account_deletes_its_secret_and_keeps_the_contacts(svc):
    aid, server = add(svc)
    other, _ = add(svc, "Other")
    server.add(person("Ann", "Lee"))
    svc.sync_now(aid)
    cid = svc.hub.list_rows()[0].id
    svc.hub.set_kept_in(cid, [aid, other])
    svc.secrets.put(account_key(aid, "refresh_token"), "SECRET-TOKEN-123")
    svc.remove_account(aid)
    assert svc.accounts.get(aid) is None
    assert svc.secrets.get(account_key(aid, "refresh_token")) is None
    assert svc.hub.count() == 1 and svc.hub.kept_in(cid) == [other]                  # taken out of Kept in


def test_removing_an_account_can_bin_the_contacts_only_it_held(svc):
    aid, server = add(svc)
    server.add(person("Ann", "Lee"))
    server.add(person("Bob", "Ray"))
    svc.sync_now(aid)
    svc.remove_account(aid, keep_contacts=False)
    assert svc.hub.count() == 0 and svc.hub.count(include_bin=True) == 2


def test_no_secret_reaches_sqlite_or_the_log_after_adding_a_password_account(svc, tmp_path, caplog):
    """1d: a test greps the log and the database."""
    from loguru import logger
    sink = tmp_path / "log.txt"
    hid = logger.add(sink, level="DEBUG")
    try:
        svc.accounts.update_settings(google_client_id="client-id-not-secret")
        acct = svc.add_password_account("carddav", "me@fastmail.com", "p4ssw0rd-SECRET", "Fast",
                                        server="carddav.fastmail.com", options={"server_url": "https://carddav.fastmail.com"},
                                        test=False)
        svc.sync_now  # noqa: B018
        logger.info("account added {} with password {}", acct.name, "p4ssw0rd-SECRET")
    finally:
        logger.remove(hid)
    assert "p4ssw0rd-SECRET" not in sink.read_text(encoding="utf-8")
    svc.store.conn.execute("PRAGMA wal_checkpoint(FULL)")
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("k.db*"))
    assert b"p4ssw0rd-SECRET" not in raw
    assert svc.secrets.get(account_key(acct.id, "app_password")) == "p4ssw0rd-SECRET"


def test_a_contact_made_in_leasha_goes_to_the_default_account(svc):
    """D4, D5."""
    a, server_a = add(svc, "A")
    b, server_b = add(svc, "B")
    ro, _ = add(svc, "ReadOnly", direction="in-only")
    cid = svc.new_contact(person("Nina", "Bell", email="n@x.com"))
    assert svc.hub.kept_in(cid) == [a]                                 # the first two-way account
    svc.accounts.update_settings(default_account_id=b)
    cid2 = svc.new_contact(person("Omar", "Cole"))
    assert svc.hub.kept_in(cid2) == [b]
    svc.accounts.update_settings(default_account_id=ro)               # an Into-Leasha-only account is not written to
    assert svc.hub.kept_in(svc.new_contact(person("Pia", "Dunn"))) == [a]
    svc.sync_now()
    assert len(server_a.live()) == 2 and len(server_b.live()) == 1


def test_the_default_is_automatic_hourly_with_no_approval_before_a_first_sync(svc):
    """2026-10-11, the owner: "as automated as possible ... manual options still there"."""
    s = svc.settings()
    assert (s.mail.mode, s.mail.minutes) == ("interval", 60)
    assert (s.contacts.mode, s.contacts.minutes) == ("interval", 60)
    assert s.dry_run_first is False
    aid, server = add(svc)
    server.add(person("Ann", "Lee"))
    out = svc.sync_now(aid)
    assert out[0].status == "ok" and not out[0].needs_approval       # imported, nothing waits
