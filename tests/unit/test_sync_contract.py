"""The sync contract (review 2026-10-14, section 2.4): what a held, refused or rate-limited sync tells the
person is true, and the remedy it names works.

Layer: L7

CS-3 an Undo for a held run, and a toast chosen by why it was held; CS-5 the provider's Retry-After reaches
the schedule; CS-6 a clash settled in the Changed-in-two-places dialog is not asked again; CS-7 a server
having trouble is retried and is the account's, not one contact's; CS-11 a run that holds clashes says so;
CS-12 a live refusal names its contact.
"""

from __future__ import annotations

import pytest

from app.accounts.engine import ContactSyncEngine
from app.accounts.store import AccountStore
from app.contacts import book_actions
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore

from tests.unit.accounts_fakes import FakeProvider, FakeServer, person


class World:
    def __init__(self, tmp_path):
        self.store = SqliteStore(tmp_path / "k.db").connect()
        self.hub = ContactHub(self.store)
        self.accounts = AccountStore(self.store)
        self.accounts.update_settings(dry_run_first=False)
        self.servers: dict[int, FakeServer] = {}
        self.providers: dict[int, FakeProvider] = {}
        self.engine = ContactSyncEngine(self.store, self.hub, self.accounts, lambda a: self.providers[a.id])

    def account(self, name: str) -> int:
        aid = self.accounts.add("google", f"{name.lower()}@example.com", name, direction="two-way")
        self.servers[aid] = FakeServer()
        self.providers[aid] = FakeProvider(self.servers[aid], page_size=2)
        return aid


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.store.close()


def only(outcomes, aid):
    return next(o for o in outcomes if o.account_id == aid)


def _held_clash(world):
    """Ann's company changed in Leasha and at Google under 'ask me every time': one clash held."""
    world.accounts.update_settings(conflict_policy="ask")
    g = world.account("Google")
    rid = world.servers[g].add(person("Ann", "Lee", company="A"), edited_at=1000)
    world.engine.sync()
    cid = world.hub.list_rows()[0].id
    card = world.hub.get_card(cid)
    card.scalars["company"] = "Hub"
    world.hub.update(cid, card)
    rc = world.servers[g].recs[rid].card.copy()
    rc.scalars["company"] = "Remote"
    world.servers[g].edit(rid, rc)
    out = only(world.engine.sync(), g)
    assert out.plan.held_conflicts == 1
    return g, rid, cid


# ----------------------------------------------------------------------------------------------------- CS-3
@pytest.fixture
def svc(tmp_path):
    from app.accounts.service import AccountService
    from tests.unit.mail_sync_fakes import secret_store
    store = SqliteStore(tmp_path / "svc.db").connect()
    servers: dict[int, FakeServer] = {}
    providers: dict[int, FakeProvider] = {}
    s = AccountService(store, secrets=secret_store(), provider_for=lambda a: providers[a.id])
    s.accounts.update_settings(dry_run_first=False)

    def account(name):
        aid = s.accounts.add("google", f"{name.lower()}@example.com", name, direction="two-way")
        servers[aid] = FakeServer()
        providers[aid] = FakeProvider(servers[aid], page_size=50)
        return aid
    s.t_account, s.t_servers = account, servers
    yield s
    store.close()


def _sync(svc, **kw):
    return {o.account_id: o for o in svc.engine.sync(**kw)}


def test_a_run_the_delete_brake_held_can_be_undone_and_nothing_is_deleted(svc):
    from app.ui.accounts import logic
    aid = svc.t_account("Home")
    for i in range(10):
        svc.t_servers[aid].add(person(f"P{i}", email=f"p{i}@x.com"))
    _sync(svc)
    svc.accounts.update_settings(brake_count=2, brake_percent=100.0)
    for r in svc.hub.list_rows()[:4]:
        svc.hub.delete(r.id)
    out = _sync(svc)[aid]
    assert out.status == "held" and out.error.code == "ERR_SYNC_DELETE_BRAKE"
    run = svc.accounts.run(out.run_id)
    assert logic.can_undo(run), "the brake's own suggestion says: if not, press Undo"
    svc.undo_run(run.id)
    assert svc.hub.count() == 10                              # the four are back out of the Bin
    assert len(svc.t_servers[aid].live()) == 10               # and nothing was deleted at the account
    again = _sync(svc)[aid]
    assert again.status == "ok" and again.plan.held_deletes == 0 and len(svc.t_servers[aid].live()) == 10
    assert logic.run_row(svc.accounts.run(run.id))["result"] == "Undone"


def _churned(svc):
    from app.contacts.card import Field
    home, other = svc.t_account("Home"), svc.t_account("Other")
    for i in range(60):
        svc.t_servers[home].add(person(f"P{i}", email=f"p{i}@x.com"))
    svc.accounts.update_settings(mirror_contacts=True)
    _sync(svc)
    _sync(svc)
    for rid, rec in list(svc.t_servers[home].recs.items()):
        card = rec.card.copy()
        card.fields.append(Field("custom", "zzz", "server tag"))
        svc.t_servers[home].edit(rid, card)
    out = _sync(svc)
    assert out[home].status == "held" and out[home].error.code == "ERR_SYNC_CHANGES_HELD"
    return home, other, out


def _tagged(cards):
    return sum(1 for c in cards if any(f.label == "server tag" for f in c.fields))


@pytest.mark.slow
def test_a_run_the_churn_guard_held_can_be_undone_and_the_hold_is_lifted(svc):
    from app.ui.accounts import logic
    home, other, out = _churned(svc)
    run = svc.accounts.run(out[home].run_id)
    assert logic.can_undo(run), "the churn guard's own suggestion says: undo this sync in the sync log"
    assert not logic.can_undo(svc.accounts.run(out[other].run_id))   # it read nothing: nothing to undo
    svc.undo_run(run.id)
    assert _tagged(svc.hub.get_card(r.id) for r in svc.hub.list_rows()) == 0
    assert _tagged(svc.t_servers[home].live().values()) == 0          # the account is put back as it was
    assert svc.engine._churn_hold() is None
    after = _sync(svc)
    assert after[home].status == "ok" and after[other].status == "ok"
    assert _tagged(svc.t_servers[other].live().values()) == 0         # and the change never spread


def test_undoing_another_accounts_held_run_leaves_the_churn_hold_standing(svc):
    home, other, out = _churned(svc)
    svc.undo_run(out[other].run_id)
    assert svc.engine._churn_hold() is not None
    assert _sync(svc)[other].status == "held"


def test_the_held_toast_says_why_the_sync_was_held():
    from datetime import datetime
    from types import SimpleNamespace as NS
    from app.core.errors import make_error
    from app.ui.accounts import logic
    settings = NS(notify_brake=True, notify_conflict=True, notify_signin=False, notify_failed_24h=False)
    churn = make_error("ERR_SYNC_CHANGES_HELD", "t", account="Home", count="60", linked="60", what="custom added")
    brake = make_error("ERR_SYNC_DELETE_BRAKE", "t", account="Work", count=40)
    outs = [NS(status="held", account_id=1, account_name="Home", run_id=7, error=churn, plan=None),
            NS(status="held", account_id=2, account_name="Other", run_id=8, error=churn, plan=None),
            NS(status="held", account_id=3, account_name="Work", run_id=9, error=brake, plan=None)]
    toasts = logic.toasts_for(settings, [], outs, datetime.now(), set())
    assert [t.title for t in toasts] == ["Sync held", "Sync held"]    # one for the guard, one for the brake
    assert toasts[0].text.startswith("Home: most of its contacts came back changed in the same way")
    assert "safety limit" not in toasts[0].text
    assert toasts[1].text.startswith("Work: this sync would delete more contacts than your safety limit")


# ----------------------------------------------------------------------------------------------------- CS-6
@pytest.mark.parametrize("side", ["local", "remote"])
def test_a_clash_settled_in_the_dialog_is_not_asked_again(world, side):
    g, rid, cid = _held_clash(world)
    (row,) = world.hub.open_conflicts(cid)
    book_actions.accept_conflicts(world.hub, [(int(row["id"]), side)])
    out = only(world.engine.sync(), g)
    assert out.plan.held_conflicts == 0, "the settled clash was asked again at the next sync"
    assert world.hub.open_conflicts(cid) == []
    want = "Hub" if side == "local" else "Remote"
    assert world.hub.get_card(cid).get("company") == want
    assert world.servers[g].live()[rid].get("company") == want


@pytest.mark.parametrize("side", ["local", "remote"])
def test_a_field_clash_settled_in_the_dialog_is_not_asked_again(world, side):
    """The same for a field (a phone's label), whose value is a tuple, not a string."""
    from app.contacts.card import Field
    world.accounts.update_settings(conflict_policy="ask")
    g = world.account("Google")
    rid = world.servers[g].add(person("Ann", "Lee", phone="07700 900123"), edited_at=1000)
    world.engine.sync()
    cid = world.hub.list_rows()[0].id
    card = world.hub.get_card(cid)
    card.fields = [Field("phone", "07700 900123", "work")]
    world.hub.update(cid, card)
    rc = world.servers[g].recs[rid].card.copy()
    rc.fields = [Field("phone", "07700 900123", "home")]
    world.servers[g].edit(rid, rc)
    assert only(world.engine.sync(), g).plan.held_conflicts == 1
    (row,) = world.hub.open_conflicts(cid)
    assert row["unit"].startswith("f:")
    book_actions.accept_conflicts(world.hub, [(int(row["id"]), side)])
    out = only(world.engine.sync(), g)
    assert out.plan.held_conflicts == 0 and out.status == "ok"
    want = "work" if side == "local" else "home"
    assert [f.label for f in world.hub.get_card(cid).fields] == [want]
    assert [f.label for f in world.servers[g].live()[rid].fields] == [want]


# ----------------------------------------------------------------------------------------------------- CS-7
class _Scripted:
    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        from app.accounts.provider import HttpResponse
        self.calls += 1
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return HttpResponse(status, {}, b"oops")


@pytest.mark.parametrize("status", [500, 502, 504])
def test_a_read_the_server_fails_is_tried_again_like_a_503(status):
    from app.accounts.http import RetryingTransport
    inner, slept = _Scripted(status, status, 200), []
    t = RetryingTransport(inner, sleep=slept.append, clock=lambda: 1_000_000.0)
    assert t.request("GET", "https://people.googleapis.com/x").status == 200
    assert inner.calls == 3 and slept == [1.0, 2.0]


@pytest.mark.parametrize("status", [500, 502, 504])
def test_a_read_still_failing_is_the_accounts_trouble_not_a_refusal(status):
    from app.accounts.http import RetryingTransport
    from app.accounts.providers import _common
    from app.core.errors import AppErrorException
    inner = _Scripted(status)
    resp = RetryingTransport(inner, sleep=lambda s: None).request("GET", "https://people.googleapis.com/x")
    assert inner.calls == 5
    with pytest.raises(AppErrorException) as caught:
        _common.check_ok(resp, "Home", "test", "read changes")
    err = caught.value.error
    assert err.code == "ERR_ACCOUNT_TROUBLE" and err.code in _common.ACCOUNT_WIDE
    assert err.message == f"Home is having trouble at its end (read changes: HTTP {status})."
    assert "tries again" in err.suggestion


def test_a_write_is_never_repeated_and_only_a_500_on_it_stays_that_contacts_refusal():
    from app.accounts.http import RetryingTransport
    from app.accounts.providers import _common
    from app.core.errors import AppErrorException
    for status, code in ((500, "ERR_ACCOUNT_REJECTED"), (502, "ERR_ACCOUNT_TROUBLE"), (504, "ERR_ACCOUNT_TROUBLE")):
        inner = _Scripted(status)
        resp = RetryingTransport(inner, sleep=lambda s: None).request("PUT", "https://x/", body=b"{}")
        assert inner.calls == 1
        with pytest.raises(AppErrorException) as caught:
            _common.check_ok(resp, "Home", "test", "change a contact")
        assert caught.value.error.code == code


def test_mail_says_the_server_is_having_trouble_too():
    from app.accounts.http import RetryingTransport
    from app.accounts.providers import _mailcommon
    from app.core.errors import AppErrorException
    resp = RetryingTransport(_Scripted(502), sleep=lambda s: None).request("GET", "https://graph.microsoft.com/x")
    with pytest.raises(AppErrorException) as caught:
        _mailcommon.fail_for_status(resp, account="Work", component="test", what="list folders")
    assert caught.value.error.code == "ERR_ACCOUNT_TROUBLE"


def test_a_server_in_trouble_ends_the_run_once_and_lists_no_contact_as_refused(world):
    from app.core.errors import AppErrorException, make_error
    g = world.account("Google")
    world.servers[g].add(person("Ann", "Lee"))
    world.engine.sync()
    for given in ("Bob", "Cy", "Di"):
        cid = world.hub.create(person(given))
        world.hub.set_kept_in(cid, [g])
    calls = []

    def trouble(card):
        calls.append(card)
        raise AppErrorException(make_error("ERR_ACCOUNT_TROUBLE", "fake", account="Google",
                                           reason="create a contact: HTTP 502"))
    world.providers[g].create = trouble
    out = only(world.engine.sync(), g)
    assert out.status == "failed" and out.error.code == "ERR_ACCOUNT_TROUBLE"
    assert len(calls) == 1 and out.plan.failed_out == 0
    assert world.accounts.failures(g) == []


# ---------------------------------------------------------------------------------------------------- CS-11
def test_a_run_that_holds_a_clash_says_so_with_the_fix(world):
    from app.ui.accounts import logic
    g, _rid, cid = _held_clash(world)
    run = world.accounts.runs(account_id=g, kind="contacts", limit=1)[0]
    assert run.status == "ok"                                 # the rest was written: not a safety hold
    assert run.error_code == "ERR_SYNC_CONFLICT_HELD"
    row = logic.run_row(run)
    assert row["error"] == "1 contact(s) changed in two places and are waiting for your choice."
    assert row["fix"] == "Open Contacts > Changed in two places and choose which value to keep."
    assert not logic.can_allow(run)
    (conflict,) = world.hub.open_conflicts(cid)
    book_actions.accept_conflicts(world.hub, [(int(conflict["id"]), "local")])
    out = only(world.engine.sync(), g)
    after = world.accounts.run(out.run_id)
    assert out.error is None and after.error_code is None and after.error_text is None


def test_a_partial_run_that_holds_a_clash_says_both(world):
    from tests.unit.accounts_fakes import BatchFakeProvider
    g, _rid, _cid = _held_clash(world)
    world.providers[g] = BatchFakeProvider(world.servers[g])
    bob = world.hub.create(person("Bob", "Bell"))
    world.hub.set_kept_in(bob, [g])
    world.providers[g].refuse.add("new:Bob")
    out = only(world.engine.sync(), g)
    run = world.accounts.run(out.run_id)
    assert run.status == "partial" and run.error_code == "ERR_SYNC_CONFLICT_HELD"
    assert run.error_text.startswith("1 contact(s) could not be written (Bob Bell: ")
    assert "1 contact(s) changed in two places" in run.error_text
    assert run.error_text.endswith(" FIX: Open Contacts > Changed in two places and choose which value to keep.")


# ---------------------------------------------------------------------------------------------------- CS-12
def test_a_live_refusal_names_the_contact_it_is_about(world):
    from tests.unit.accounts_fakes import BatchFakeProvider
    g = world.account("Google")
    world.providers[g] = BatchFakeProvider(world.servers[g])
    cid = world.hub.create(person("Bob", "Bell"))
    world.hub.set_kept_in(cid, [g])
    world.providers[g].refuse.add("new:Bob")
    told = []
    out = only(world.engine.sync(progress=told.append), g)
    assert out.status == "partial"
    assert out.plan.failures and out.plan.failures[0][0] == "Bob Bell" and out.plan.failures[0][2] == cid
    live = [f for e in told for f in e.get("failures") or []]
    assert live and all(f[2] == cid for f in live)
    run = world.accounts.run(out.run_id)
    assert run.plan["failures"][0][2] == cid and "Bob Bell: " in run.error_text


# ----------------------------------------------------------------------------------------------------- CS-5
def _limit(retry_after_s):
    from app.core.errors import AppErrorException, make_error
    return AppErrorException(make_error("ERR_PROVIDER_LIMIT", "fake", account="Google",
                                        reason="too many requests, HTTP 429", retry_after_s=retry_after_s))


def test_the_providers_retry_after_reaches_the_schedule_of_a_contacts_sync(world):
    import time
    g = world.account("Google")
    world.providers[g].fail_with = _limit(900.0)
    before = time.time()
    out = only(world.engine.sync(), g)
    assert out.status == "failed" and out.error.code == "ERR_PROVIDER_LIMIT"
    at = world.accounts.state(g, "contacts").next_retry_at
    assert at is not None and before + 900 <= at <= time.time() + 901


def test_without_a_retry_after_the_schedule_backs_off_as_before(world):
    g = world.account("Google")
    world.providers[g].fail_with = _limit(None)
    world.engine.sync()
    assert world.accounts.state(g, "contacts").next_retry_at is None
    world.providers[g].fail_with = None
    world.engine.sync()                                     # a success clears it
    assert world.accounts.state(g, "contacts").next_retry_at is None


def test_the_providers_retry_after_reaches_the_schedule_of_a_mail_fetch(tmp_path):
    from app.accounts.mail_sync import MailSyncer
    from app.accounts.service import AccountService
    from tests.unit.mail_sync_fakes import secret_store
    store = SqliteStore(tmp_path / "m.db").connect()
    try:
        svc = AccountService(store, secrets=secret_store())
        aid = svc.accounts.add("google", "me@x.com", "Home", mail_on=True, contacts_on=False)

        def limited(_account):
            raise _limit(600.0)
        out = MailSyncer(svc, provider_for=limited, clock=lambda: 1_000_000.0)(svc.accounts.get(aid))
        assert out.status == "failed"
        assert svc.accounts.state(aid, "mail").next_retry_at == 1_000_600
    finally:
        store.close()


def test_the_activity_line_says_the_provider_asked_for_the_wait():
    from datetime import datetime
    from app.ui.presenter.account_activity import describe
    now = datetime(2026, 10, 12, 8, 14).timestamp()
    run = {"status": "failed", "error_text": "Home asked Leasha to slow down (too many requests, HTTP 429)."}
    base = {"account_id": 1, "name": "Home", "kind": "contacts", "enabled": True, "live": False, "hold": None,
            "signin_expired": False, "mode": "interval", "last_success": now - 600, "failures": 1, "run": run}
    asked = describe({**base, "next": now + 900, "retry_at": now + 900}, now)
    assert asked.state == "failed"
    assert "trying again in 15 min (08:29), as the provider asked" in asked.detail
    later = describe({**base, "next": now + 2700, "retry_at": now + 900}, now)  # the schedule is later still
    assert "trying again in 45 min" in later.detail and "provider asked" not in later.detail
    assert "provider asked" not in describe({**base, "next": now + 900}, now).detail


def test_the_activity_snapshot_carries_the_retry_time(tmp_path):
    from app.accounts.service import AccountService
    from tests.unit.mail_sync_fakes import secret_store
    store = SqliteStore(tmp_path / "s.db").connect()
    try:
        svc = AccountService(store, secrets=secret_store())
        aid = svc.accounts.add("google", "me@x.com", "Home", mail_on=False, contacts_on=True)
        svc.accounts.record_failure(aid, "contacts", "ERR_PROVIDER_LIMIT", "slow down", next_retry_at=2_000_000_000)
        snap = next(s for s in svc.activity_snapshot() if s["account_id"] == aid and s["kind"] == "contacts")
        assert snap["retry_at"] == 2_000_000_000
    finally:
        store.close()
