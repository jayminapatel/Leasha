"""Order 1t 5a, 5c, 5d, 1a, 1c: the kept connection, the meter, and the Google and Microsoft batch writes.

Layer: L7

No socket is opened: `KeepAliveTransport` is handed a fake connection factory, and the connectors a fake
`Transport` that answers by URL.
"""

from __future__ import annotations

import http.client
import json

import pytest

from app.accounts.http import AllowedHosts, KeepAliveTransport, Meter, RetryingTransport
from app.accounts.provider import HttpResponse
from app.accounts.providers.google_people import API_BASE as G, GooglePeopleProvider
from app.accounts.providers.graph_contacts import API_BASE as M, GraphContactsProvider
from app.contacts.card import Card, Field
from app.core.errors import AppErrorException

from tests.unit.accounts_fakes import person


# ------------------------------------------------------------------------------------------ the kept connection
class _Raw:
    def __init__(self, status=200, body=b"{}", close=False):
        self.status, self._body, self.will_close = status, body, close

    def read(self):
        return self._body

    def getheaders(self):
        return [("Content-Type", "application/json")]


class _Conn:
    made = 0

    def __init__(self, script):
        _Conn.made += 1
        self.script, self.sock, self.timeout, self.closed = script, None, None, False
        self.sent: list = []

    def request(self, method, path, body=None, headers=None):
        self.sent.append((method, path))
        step = self.script.pop(0) if self.script else "ok"
        if step == "drop-send":
            raise http.client.RemoteDisconnected("closed while idle")
        self._next = step

    def getresponse(self):
        if self._next == "drop-read":
            raise ConnectionResetError("reset mid answer")
        return _Raw()

    def close(self):
        self.closed = True


def _transport(script, **kw):
    _Conn.made = 0
    conns: list[_Conn] = []

    def connect(host, port, timeout):
        c = _Conn(script)
        conns.append(c)
        return c
    meter = Meter()
    t = KeepAliveTransport(AllowedHosts(), account="G", connect=connect, meter=meter, **kw)
    return t, conns, meter


def test_one_connection_carries_many_requests_and_the_meter_counts_them():
    t, conns, meter = _transport([])
    for _ in range(3):
        assert t.request("GET", f"{G}/people/me").status == 200
    t.request("PATCH", f"{G}/people/c1:updateContact", body=b"{}")
    assert len(conns) == 1
    snap = meter.snapshot()
    assert snap["requests"] == {"GET": 3, "PATCH": 1} and snap["connections"] == 1 and snap["total"] == 4


def test_a_connection_closed_while_idle_is_sent_again_on_a_new_one_even_for_a_write():
    t, conns, _m = _transport(["ok", "drop-send"])                   # it fails while sending: never reached
    t.request("GET", f"{G}/people/me")
    assert t.request("POST", f"{G}/people:createContact", body=b"{}").status == 200
    assert len(conns) == 2


def test_a_write_lost_after_its_answer_began_is_not_repeated():
    t, conns, _m = _transport(["ok", "drop-read"])
    t.request("GET", f"{G}/people/me")
    with pytest.raises(AppErrorException) as caught:
        t.request("POST", f"{G}/people:createContact", body=b"{}")
    assert caught.value.error.code == "ERR_ACCOUNT_UNREACHABLE" and len(conns) == 1


def test_a_read_lost_after_it_was_sent_is_sent_again():
    t, conns, _m = _transport(["ok", "drop-read"])
    t.request("GET", f"{G}/people/me")
    assert t.request("GET", f"{G}/people/me").status == 200 and len(conns) == 2


def test_a_write_after_a_pause_takes_a_new_connection():
    now = [0.0]
    t, conns, _m = _transport([], clock=lambda: now[0])
    t.request("GET", f"{G}/people/me")
    now[0] = 15.0
    t.request("GET", f"{G}/people/me")                                 # a read: 15 s is not idle
    now[0] = 30.0
    t.request("POST", f"{G}/people:createContact", body=b"{}")         # a write after 15 s: a new one
    assert len(conns) == 2


def test_an_idle_connection_is_replaced_before_it_is_used():
    now = [0.0]
    t, conns, _m = _transport([], idle_s=30.0, clock=lambda: now[0])
    t.request("GET", f"{G}/people/me")
    now[0] = 45.0
    t.request("GET", f"{G}/people/me")
    assert len(conns) == 2 and conns[0].closed


def test_the_allow_list_still_refuses_a_stranger():
    t, conns, _m = _transport([])
    with pytest.raises(AppErrorException):
        t.request("GET", "https://evil.example.com/steal")
    assert conns == []


def test_a_retry_is_logged_and_counted(caplog):
    meter = Meter()

    class Busy:
        def __init__(self):
            self.n = 0

        def request(self, method, url, **kw):
            self.n += 1
            return HttpResponse(429 if self.n == 1 else 200, {"retry-after": "2"})
    slept = []
    rt = RetryingTransport(Busy(), sleep=slept.append, meter=meter, account="G")
    assert rt.request("GET", f"{G}/x").status == 200
    assert slept == [2.0] and meter.snapshot()["retries"] == 1 and meter.snapshot()["slept_s"] == 2.0


# ---------------------------------------------------------------------------------------------- Google batches
class Fake:
    def __init__(self):
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[dict] = []

    def add(self, method, path, body=None, status=200):
        self.routes.setdefault((method, path), []).append(
            HttpResponse(status, {}, b"" if body is None else json.dumps(body).encode()))

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        path = url.split("?")[0]
        self.calls.append({"method": method, "url": url, "body": json.loads(body) if body else None})
        q = self.routes.get((method, path))
        if not q:
            raise AssertionError(f"unexpected {method} {url}")
        return q.pop(0) if len(q) > 1 else q[0]


def _person(rid, etag, given):
    return {"resourceName": rid, "etag": etag, "names": [{"givenName": given, "displayName": given}],
            "metadata": {"sources": [{"updateTime": "2026-10-12T10:00:00Z"}]}}


@pytest.fixture
def gfake():
    f = Fake()
    f.add("GET", f"{G}/contactGroups", {"contactGroups": []})
    return f


def test_google_updates_two_hundred_at_a_time_and_the_answer_is_the_read_back(gfake):
    gfake.add("POST", f"{G}/people:batchUpdateContacts", {"updateResult": {
        "people/c1": {"person": _person("people/c1", "e1b", "Ann")},
        "people/c2": {"status": {"code": 9, "message": "etag mismatch"}}}})
    p = GooglePeopleProvider(gfake, lambda: "t")
    out = p.update_many([("people/c1", "e1", person("Ann")), ("people/c2", "e2", person("Bob"))])
    assert out[0].ok and out[0].version == "e1b" and out[0].card.get("given") == "Ann"
    assert out[1].conflict
    sent = next(c for c in gfake.calls if c["url"].endswith("batchUpdateContacts"))["body"]
    assert sent["contacts"]["people/c1"]["etag"] == "e1" and "readMask" in sent


def test_google_falls_back_to_one_at_a_time_when_a_batch_is_refused_for_its_content(gfake):
    gfake.add("POST", f"{G}/people:batchUpdateContacts", {"error": {"message": "bad"}}, status=400)
    gfake.add("PATCH", f"{G}/people/c1:updateContact", {"etag": "e1b"})
    p = GooglePeopleProvider(gfake, lambda: "t")
    out = p.update_many([("people/c1", "e1", person("Ann"))])
    assert out[0].ok and out[0].version == "e1b"


def test_google_sign_in_failure_in_a_batch_is_the_accounts(gfake):
    gfake.add("POST", f"{G}/people:batchUpdateContacts", {}, status=401)
    p = GooglePeopleProvider(gfake, lambda: "t")
    with pytest.raises(AppErrorException) as caught:
        p.update_many([("people/c1", "e1", person("Ann"))])
    assert caught.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED"


def test_google_deletes_only_what_did_not_change_since(gfake):
    gfake.add("GET", f"{G}/people:batchGet", {"responses": [
        {"requestedResourceName": "people/c1", "person": _person("people/c1", "e1", "Ann")},
        {"requestedResourceName": "people/c2", "person": _person("people/c2", "CHANGED", "Bob")},
        {"requestedResourceName": "people/c3", "status": {"code": 5}}]})
    gfake.add("POST", f"{G}/people:batchDeleteContacts", {})
    p = GooglePeopleProvider(gfake, lambda: "t")
    out = p.delete_many([("people/c1", "e1"), ("people/c2", "e2"), ("people/c3", "e3")])
    assert out[0].ok and out[1].conflict and out[2].ok
    deleted = [c for c in gfake.calls if c["url"].endswith("batchDeleteContacts")][0]["body"]["resourceNames"]
    assert deleted == ["people/c1"]


def test_google_lists_every_contact_with_its_etag_and_name(gfake):
    gfake.add("GET", f"{G}/people/me/connections", {"connections": [_person("people/c1", "e1", "Ann")]})
    p = GooglePeopleProvider(gfake, lambda: "t")
    assert p.list_ids() == {"people/c1": ("e1", "Ann")}
    assert "pageSize=1000" in gfake.calls[-1]["url"]


# -------------------------------------------------------------------------------------------- Microsoft batches
@pytest.fixture
def mfake():
    f = Fake()
    f.add("GET", f"{M}/me/contacts", {"value": [{"id": "c0", "parentFolderId": "DEF"}]})
    f.add("GET", f"{M}/me/contactFolders", {"value": [{"id": "DEF", "displayName": "Contacts"}]})
    f.add("GET", f"{M}/me/contactFolders/DEF/childFolders", {"value": [{"id": "KID", "displayName": "Club"}]})
    f.add("GET", f"{M}/me/contactFolders/KID/childFolders", {"value": []})
    return f


def test_microsoft_child_folders_are_walked(mfake):
    p = GraphContactsProvider(mfake, lambda: "t")
    assert p._load_folders() == {"DEF": "Contacts", "KID": "Club"}


def test_microsoft_updates_twenty_at_a_time_reading_change_keys_first(mfake):
    reads = {"responses": [{"id": str(i), "status": 200, "body": {"changeKey": f"k{i}", "parentFolderId": "DEF"}}
                           for i in range(20)]}
    reads2 = {"responses": [{"id": "0", "status": 200, "body": {"changeKey": "STALE", "parentFolderId": "DEF"}}]}
    patches = {"responses": [{"id": str(i), "status": 200, "body": {"id": f"c{i}", "changeKey": f"k{i}b",
                                                                    "givenName": f"P{i}"}} for i in range(20)]}
    mfake.add("POST", f"{M}/$batch", reads)
    mfake.add("POST", f"{M}/$batch", reads2)
    mfake.add("POST", f"{M}/$batch", patches)
    p = GraphContactsProvider(mfake, lambda: "t")
    items = [(f"c{i}", f"k{i}", person(f"P{i}")) for i in range(20)] + [("c20", "k20", person("P20"))]
    out = p.update_many(items)
    assert all(o.ok for o in out[:20]) and out[20].conflict
    assert out[3].version == "k3b" and out[3].card.get("given") == "P3"
    batches = [c["body"]["requests"] for c in mfake.calls if c["url"].endswith("$batch")]
    assert [len(b) for b in batches] == [20, 1, 20] and batches[2][0]["method"] == "PATCH"


def test_microsoft_resends_what_was_throttled_inside_a_batch(mfake, monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    mfake.add("POST", f"{M}/$batch", {"responses": [
        {"id": "0", "status": 200, "body": {"id": "c0", "givenName": "A"}},
        {"id": "1", "status": 429, "headers": {"Retry-After": "1"}, "body": {}}]})
    mfake.add("POST", f"{M}/$batch", {"responses": [{"id": "0", "status": 200, "body": {"id": "c1"}}]})
    p = GraphContactsProvider(mfake, lambda: "t")
    found = p.get_many(["c0", "c1"])
    assert found["c0"] is not None and found["c1"] is not None


def test_microsoft_lists_every_folder_for_the_check(mfake):
    mfake.add("GET", f"{M}/me/contactFolders/KID/contacts", {"value": [{"id": "k1", "changeKey": "x",
                                                                        "displayName": "Kid"}]})
    mfake.routes[("GET", f"{M}/me/contacts")] = [HttpResponse(200, {}, json.dumps(
        {"value": [{"id": "c0", "changeKey": "y", "displayName": "Ann", "parentFolderId": "DEF"}]}).encode())]
    p = GraphContactsProvider(mfake, lambda: "t")
    assert p.list_ids() == {"c0": ("y", "Ann"), "k1": ("x", "Kid")}


def test_a_card_field_survives_the_microsoft_body():
    c = Card(scalars={"given": "Ann"})
    c.fields.append(Field("email", "a@x.com"))
    assert GraphContactsProvider(Fake(), lambda: "t")._to_body(c)["emailAddresses"][0]["address"] == "a@x.com"


def test_google_never_takes_a_contact_it_could_not_read_for_deleted(gfake):
    gfake.add("GET", f"{G}/people:batchGet", {"responses": [
        {"requestedResourceName": "people/c1", "status": {"code": 7, "message": "permission denied"}},
        {"requestedResourceName": "people/c2", "status": {"code": 5}}]})
    gfake.add("POST", f"{G}/people:batchDeleteContacts", {})
    p = GooglePeopleProvider(gfake, lambda: "t")
    out = p.delete_many([("people/c1", "e1"), ("people/c2", "e2")])
    assert out[0].error is not None and not out[0].ok
    assert out[1].ok


def test_carddav_keeps_the_contacts_written_when_a_group_card_fails(monkeypatch):
    from app.accounts.provider import VersionConflict
    from app.accounts.providers.carddav import CardDavProvider
    p = CardDavProvider(Fake(), lambda: "Basic x", server_url="https://dav.example.com", group_mode="cards")

    def boom(_wanted):
        raise VersionConflict("group")
    monkeypatch.setattr(p, "_set_memberships", boom)
    card = person("Ann")
    card.groups = ["Family"]
    out = p._many([card], lambda c: ("a.vcf", "e1", "UID-A"))
    assert out[0].ok and out[0].remote_id == "a.vcf"
