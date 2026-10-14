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
