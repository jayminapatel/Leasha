"""The Google People connector, against hand-written People API replies.

Layer: L7

Order 1k 4a, 4d, D10. A fake `Transport` serves canned JSON and records every
request, so each test can say exactly which calls, headers and bodies the
connector made. No network is opened anywhere.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from app.accounts.provider import (ContactProvider, HttpResponse, TokenExpired, VersionConflict)
from app.accounts.providers.google_people import API_BASE, GooglePeopleProvider
from app.contacts.card import Card, Field


class FakeTransport:
    """(method, path-without-query) -> queue of responses; the last one repeats."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[HttpResponse]] = {}
        self.calls: list[dict] = []

    def add(self, method: str, path: str, body=None, status: int = 200) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.routes.setdefault((method, path), []).append(HttpResponse(status, {}, data))

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        u = urlparse(url)
        self.calls.append({"method": method, "path": u.path, "query": parse_qs(u.query),
                           "headers": dict(headers or {}), "body": json.loads(body) if body else None})
        q = self.routes.get((method, u.path))
        if not q:
            raise AssertionError(f"unexpected request {method} {url}")
        return q.pop(0) if len(q) > 1 else q[0]

    def to(self, method: str, path: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method and c["path"] == path]


GROUPS = {"contactGroups": [
    {"resourceName": "contactGroups/myContacts", "groupType": "SYSTEM_CONTACT_GROUP", "formattedName": "My Contacts"},
    {"resourceName": "contactGroups/starred", "groupType": "SYSTEM_CONTACT_GROUP", "formattedName": "Starred"},
    {"resourceName": "contactGroups/friends", "groupType": "SYSTEM_CONTACT_GROUP", "formattedName": "Friends"},
    {"resourceName": "contactGroups/g1", "groupType": "USER_CONTACT_GROUP", "name": "contactGroups/g1",
     "formattedName": "Book club"},
]}


def person(n: int, **extra) -> dict:
    p = {"resourceName": f"people/c{n}", "etag": f"etag{n}",
         "metadata": {"sources": [{"type": "CONTACT", "updateTime": "2026-10-01T10:00:00.123456Z"}]},
         "names": [{"givenName": f"Given{n}", "familyName": "Family"}],
         "emailAddresses": [{"value": f"p{n}@example.com", "type": "work"}]}
    p.update(extra)
    return p


@pytest.fixture
def tr():
    t = FakeTransport()
    t.add("GET", "/v1/contactGroups", GROUPS)
    return t


@pytest.fixture
def gp(tr):
    return GooglePeopleProvider(tr, lambda: "tok-1")


def pages(gp, token=None):
    return list(gp.pull(token))


def test_it_satisfies_the_contract(gp):
    assert isinstance(gp, ContactProvider)
    assert gp.caps.photo is False and gp.caps.favourite and gp.caps.groups


def test_full_pull_reads_a_person_into_a_card(tr, gp):
    p = person(1,
               names=[{"givenName": "Ann", "familyName": "Lee", "middleName": "M", "honorificPrefix": "Dr",
                       "honorificSuffix": "PhD"}],
               nicknames=[{"value": "Annie"}],
               organizations=[{"name": "Acme", "department": "R&D", "title": "Chief"}],
               biographies=[{"value": "Met at a fair", "contentType": "TEXT_PLAIN"}],
               phoneNumbers=[{"value": "+447700900123", "type": "mobile"}],
               addresses=[{"streetAddress": "1 High St", "city": "Leeds", "postalCode": "LS1", "country": "UK",
                           "type": "home"}],
               urls=[{"value": "https://ann.example", "type": "homePage"}],
               birthdays=[{"date": {"year": 1980, "month": 5, "day": 17}}],
               events=[{"date": {"month": 6, "day": 1}, "type": "anniversary"}],
               relations=[{"person": "Bob", "type": "spouse"}],
               imClients=[{"username": "ann@jabber", "protocol": "jabber"}],
               userDefined=[{"key": "Pet", "value": "Rex"}],
               photos=[{"url": "https://lh3.example/photo"}],
               memberships=[
                   {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}},
                   {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/starred"}},
                   {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/g1"}}])
    tr.add("GET", "/v1/people/me/connections", {"connections": [p], "nextSyncToken": "S1"})
    [page] = pages(gp)
    assert page.done and json.loads(page.next_token) == {"v": 1, "s": "S1"}
    [rc] = page.changes
    assert rc.remote_id == "people/c1" and rc.version == "etag1" and rc.edited_at > 1.7e9
    c = rc.card
    assert c.scalars == {"given": "Ann", "family": "Lee", "additional": "M", "prefix": "Dr", "suffix": "PhD",
                         "nickname": "Annie", "company": "Acme", "department": "R&D", "title": "Chief",
                         "notes": "Met at a fair"}
    by = {(f.kind, f.label): f for f in c.fields}
    assert by[("email", "work")].value == "p1@example.com"
    assert by[("phone", "mobile")].value == "+447700900123"
    assert by[("address", "home")].data == {"street": "1 High St", "city": "Leeds", "postcode": "LS1",
                                            "country": "UK"}
    assert by[("url", "homePage")].value == "https://ann.example"
    assert by[("date", "birthday")].value == "1980-05-17"
    assert by[("date", "anniversary")].value == "--06-01"
    assert by[("related", "spouse")].value == "Bob"
    assert by[("im", "jabber")].value == "ann@jabber"
    assert by[("custom", "Pet")].value == "Rex"
    assert c.groups == ["Book club"] and c.favourite is True
    assert c.photo is None                        # a photo URL is never followed
    call = tr.to("GET", "/v1/people/me/connections")[0]
    assert call["headers"]["Authorization"] == "Bearer tok-1"
    assert call["query"]["requestSyncToken"] == ["true"]
    assert "photos" not in call["query"]["personFields"][0]
    assert "syncToken" not in call["query"]


def test_paging_resumes_from_the_page_token(tr, gp):
    tr.add("GET", "/v1/people/me/connections", {"connections": [person(1)], "nextPageToken": "PT2"})
    tr.add("GET", "/v1/people/me/connections", {"connections": [person(2)], "nextSyncToken": "S9"})
    it = gp.pull(None)
    first = next(it)
    assert not first.done and json.loads(first.next_token) == {"v": 1, "s": "", "p": "PT2"}
    it.close()                                      # the engine stops after page 1 (a crash, say)
    n_before = len(tr.to("GET", "/v1/people/me/connections"))
    rest = pages(GooglePeopleProvider(tr, lambda: "t"), first.next_token)
    assert [r.remote_id for r in rest[-1].changes] == ["people/c2"] and rest[-1].done
    assert json.loads(rest[-1].next_token) == {"v": 1, "s": "S9"}
    resumed = tr.to("GET", "/v1/people/me/connections")[n_before]
    assert resumed["query"]["pageToken"] == ["PT2"]


def test_incremental_pull_sends_the_sync_token_on_every_page_and_reports_deletes(tr, gp):
    tr.add("GET", "/v1/people/me/connections",
           {"connections": [person(3), {"resourceName": "people/c4", "metadata": {"deleted": True}}],
            "nextPageToken": "PT2"})
    tr.add("GET", "/v1/people/me/connections", {"connections": [], "nextSyncToken": "S2"})
    out = pages(gp, json.dumps({"v": 1, "s": "S1"}))
    assert [c.remote_id for c in out[0].changes] == ["people/c3", "people/c4"]
    assert out[0].changes[1].card is None
    assert json.loads(out[0].next_token) == {"v": 1, "s": "S1", "p": "PT2"}
    for call in tr.to("GET", "/v1/people/me/connections"):
        assert call["query"]["syncToken"] == ["S1"]
    assert out[1].done and json.loads(out[1].next_token) == {"v": 1, "s": "S2"}


@pytest.mark.parametrize("status,body", [
    (410, {"error": {"code": 410, "status": "GONE", "message": "EXPIRED_SYNC_TOKEN"}}),
    (400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Sync token is expired. "
                                                                         "EXPIRED_SYNC_TOKEN"}}),
])
def test_an_expired_sync_token_is_token_expired(tr, gp, status, body):
    tr.add("GET", "/v1/people/me/connections", body, status=status)
    with pytest.raises(TokenExpired):
        pages(gp, json.dumps({"v": 1, "s": "OLD"}))


def test_a_token_nobody_can_read_is_token_expired(gp):
    with pytest.raises(TokenExpired):
        pages(gp, "not json at all")


def test_create_sends_only_what_google_holds_and_creates_a_missing_group(tr, gp):
    """D10: a social profile and a photo are not sent, and nothing complains."""
    tr.add("POST", "/v1/contactGroups", {"resourceName": "contactGroups/new1", "name": "contactGroups/new1"},
           status=200)
    tr.add("POST", "/v1/people:createContact", person(7, etag="e-new"))
    card = Card(scalars={"given": "Zed", "family": "Zee", "company": "Acme"},
                fields=[Field("email", "z@example.com", "home"), Field("phone", "+441", "cell"),
                        Field("social", "@zed", "twitter"), Field("date", "1990-01-02", "birthday"),
                        Field("custom", "Rex", "Pet")],
                groups=["Book club", "Chess"], favourite=True)
    from app.contacts.card import Photo
    card.photo = Photo("image/jpeg", b"\xff\xd8abc")
    rid, ver = gp.create(card)
    assert (rid, ver) == ("people/c7", "e-new")
    assert tr.to("POST", "/v1/contactGroups")[0]["body"] == {"contactGroup": {"name": "Chess"}}
    body = tr.to("POST", "/v1/people:createContact")[0]["body"]
    assert "photos" not in body and "socialProfiles" not in body
    assert not any("@zed" in json.dumps(v) for v in body.values())
    assert body["names"] == [{"givenName": "Zed", "familyName": "Zee"}]
    assert body["emailAddresses"] == [{"value": "z@example.com", "type": "home"}]
    assert body["phoneNumbers"] == [{"value": "+441", "type": "mobile"}]
    assert body["birthdays"] == [{"date": {"year": 1990, "month": 1, "day": 2}}]
    assert body["userDefined"] == [{"key": "Pet", "value": "Rex"}]
    res = [m["contactGroupMembership"]["contactGroupResourceName"] for m in body["memberships"]]
    assert res == ["contactGroups/myContacts", "contactGroups/starred", "contactGroups/g1", "contactGroups/new1"]


def test_update_uses_the_etag_and_a_field_mask_and_returns_the_new_etag(tr, gp):
    tr.add("PATCH", "/v1/people/c5:updateContact", person(5, etag="etag5b"))
    new = gp.update("people/c5", "etag5", Card(scalars={"given": "Ann"}))
    assert new == "etag5b"
    call = tr.to("PATCH", "/v1/people/c5:updateContact")[0]
    assert call["body"]["etag"] == "etag5"
    mask = call["query"]["updatePersonFields"][0].split(",")
    assert "emailAddresses" in mask and "memberships" in mask       # an emptied field is cleared
    assert "emailAddresses" not in call["body"]


@pytest.mark.parametrize("status,body", [
    (400, {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                     "message": "Request person.etag is different than the current person.etag."}}),
    (409, {"error": {"code": 409, "status": "ABORTED"}}),
    (412, {"error": {"code": 412}}),
])
def test_a_stale_etag_is_a_version_conflict(tr, gp, status, body):
    tr.add("PATCH", "/v1/people/c5:updateContact", body, status=status)
    with pytest.raises(VersionConflict):
        gp.update("people/c5", "stale", Card())


def test_other_failures_are_registered_errors_not_conflicts(tr, gp):
    from app.core.errors import AppErrorException
    tr.add("PATCH", "/v1/people/c5:updateContact", {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                              "message": "bad field"}}, status=400)
    with pytest.raises(AppErrorException) as e:
        gp.update("people/c5", "e", Card())
    assert e.value.error.code == "ERR_ACCOUNT_REJECTED"
    tr.add("GET", "/v1/people/c9", {"error": {"code": 401}}, status=401)
    with pytest.raises(AppErrorException) as e:
        gp.get("people/c9")
    assert e.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED"


def test_delete_checks_the_etag_first(tr, gp):
    tr.add("GET", "/v1/people/c5", {"resourceName": "people/c5", "etag": "newer"})
    with pytest.raises(VersionConflict):
        gp.delete("people/c5", "old")
    assert not tr.to("DELETE", "/v1/people/c5:deleteContact")
    tr.add("GET", "/v1/people/c6", {"resourceName": "people/c6", "etag": "same"})
    tr.add("DELETE", "/v1/people/c6:deleteContact", {}, status=200)
    gp.delete("people/c6", "same")
    assert len(tr.to("DELETE", "/v1/people/c6:deleteContact")) == 1
    # the etag is compared with one read for the same fields as every other read: Google's etag changes with them
    from app.accounts.providers.google_people import _READ_FIELDS

    assert tr.to("GET", "/v1/people/c6")[0]["query"]["personFields"] == [_READ_FIELDS]


def test_delete_of_a_contact_already_gone_is_quiet(tr, gp):
    tr.add("GET", "/v1/people/c8", {"error": {"code": 404}}, status=404)
    gp.delete("people/c8", "x")


def test_get_returns_none_for_a_missing_contact(tr, gp):
    tr.add("GET", "/v1/people/c404", {"error": {"code": 404}}, status=404)
    assert gp.get("people/c404") is None
    tr.add("GET", "/v1/people/c1", person(1))
    rc = gp.get("people/c1")
    assert rc is not None and rc.card.scalars["given"] == "Given1"


def test_a_card_round_trips_through_google_json(tr, gp):
    """What goes out as a body reads back as the same card (for what Google holds)."""
    from app.contacts.card import cards_equal
    card = Card(scalars={"given": "Ann", "family": "Lee", "company": "Acme", "notes": "n"},
                fields=[Field("email", "a@x.com", "work"), Field("address", "1 St, Leeds", "home",
                                                                  data={"street": "1 St", "city": "Leeds"}),
                        Field("date", "1980-05-17", "birthday"), Field("related", "Bob", "spouse")],
                groups=["Book club"], favourite=True)
    body = gp._to_person(card)
    body.update({"resourceName": "people/c1", "etag": "e"})
    again = gp._to_card(body)
    assert cards_equal(card, again, gp.caps)
