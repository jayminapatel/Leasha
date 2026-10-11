"""The Microsoft Graph contacts connector, against hand-written Graph replies.

Layer: L7

Order 1k 4b, 4d, D10. A fake `Transport` serves canned JSON by full URL (the
delta and next links) or by path, and records every request so the tests can
assert the exact calls, headers and bodies. No network is opened.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from app.accounts.provider import ContactProvider, HttpResponse, TokenExpired, VersionConflict
from app.accounts.providers.graph_contacts import API_BASE, GraphContactsProvider
from app.contacts.card import Card, Field, Photo, cards_equal

B = API_BASE
DEFAULT = "AAMk-default"
FOLDER_A = "AAMk-family"


class FakeTransport:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[HttpResponse]] = {}
        self.calls: list[dict] = []

    def add(self, method: str, url: str, body=None, status: int = 200) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.routes.setdefault((method, url), []).append(HttpResponse(status, {}, data))

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        u = urlparse(url)
        self.calls.append({"method": method, "url": url, "path": u.path, "query": parse_qs(u.query),
                           "headers": dict(headers or {}), "body": json.loads(body) if body else None})
        q = self.routes.get((method, url)) or self.routes.get((method, f"{u.scheme}://{u.netloc}{u.path}"))
        if not q:
            raise AssertionError(f"unexpected request {method} {url}")
        return q.pop(0) if len(q) > 1 else q[0]

    def to(self, method: str, path_suffix: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method and c["path"].endswith(path_suffix)]


def contact(n: str, folder: str = DEFAULT, **extra) -> dict:
    c = {"id": f"id-{n}", "changeKey": f"ck-{n}", "parentFolderId": folder,
         "lastModifiedDateTime": "2026-10-02T08:30:00Z", "givenName": f"Given{n}", "surname": "Family",
         "emailAddresses": [{"name": "x", "address": f"{n}@example.com"}]}
    c.update(extra)
    return c


def removed(n: str, reason: str = "deleted") -> dict:
    return {"id": f"id-{n}", "@removed": {"reason": reason}}


def delta(url_key: str, token: str) -> str:
    return f"{B}/{url_key}?$deltatoken={token}"


@pytest.fixture
def tr():
    t = FakeTransport()
    t.add("GET", f"{B}/me/contactFolders", {"value": [
        {"id": DEFAULT, "displayName": "Contacts"}, {"id": FOLDER_A, "displayName": "Family"}]})
    t.add("GET", f"{B}/me/contacts", {"value": [contact("probe")]})        # the default-folder probe
    return t


@pytest.fixture
def gc(tr):
    return GraphContactsProvider(tr, lambda: "tok-g")


def run(gc, token=None):
    return list(gc.pull(token))


def empty_folder(tr, key, tokname):
    tr.add("GET", f"{B}/me/contactFolders/{FOLDER_A}/contacts/delta" if key else f"{B}/me/contacts/delta",
           {"value": [], "@odata.deltaLink": delta("x", tokname)})


def test_it_satisfies_the_contract(gc):
    assert isinstance(gc, ContactProvider)
    assert gc.caps.photo is False and gc.caps.favourite is False
    assert gc.caps.date_labels == frozenset({"birthday"})


def test_full_pull_walks_the_default_folder_then_each_other_folder(tr, gc):
    tr.add("GET", f"{B}/me/contacts/delta", {"value": [contact("1", categories=["Red"])],
                                             "@odata.deltaLink": delta("me/contacts/delta", "D0")})
    tr.add("GET", f"{B}/me/contactFolders/{FOLDER_A}/contacts/delta",
           {"value": [contact("2", FOLDER_A)], "@odata.deltaLink": delta("f", "D1")})
    out = run(gc)
    assert [p.done for p in out] == [False, True]
    c1 = out[0].changes[0]
    assert c1.remote_id == "id-1" and c1.version == "ck-1" and c1.card.groups == ["Red"]
    assert c1.edited_at > 1.7e9
    c2 = out[1].changes[0].card
    assert c2.groups == ["Family"]                       # the folder's name is a group
    assert json.loads(out[1].next_token) == {"v": 1, "d": {"": delta("me/contacts/delta", "D0"),
                                                           FOLDER_A: delta("f", "D1")}}
    first = tr.to("GET", "/me/contacts/delta")[0]
    assert first["headers"]["Authorization"] == "Bearer tok-g"
    assert 'IdType="ImmutableId"' in first["headers"]["Prefer"]
    assert "odata.maxpagesize=100" in first["headers"]["Prefer"]
    # the default folder is not walked twice even though the folder list holds it
    assert not tr.to("GET", f"/me/contactFolders/{DEFAULT}/contacts/delta")


def test_reads_a_contact_into_a_card(tr, gc):
    c = contact("1", title="Dr", middleName="M", generation="Jr", nickName="Bee", companyName="Acme",
                department="R&D", jobTitle="Chief", personalNotes="hello", mobilePhone="+447700900001",
                homePhones=["+441"], businessPhones=["+442"],
                homeAddress={"street": "1 St", "city": "Leeds", "postalCode": "LS1"},
                businessAddress={"street": "2 Rd", "countryOrRegion": "UK"},
                businessHomePage="https://x.example", birthday="1980-05-17T00:00:00Z",
                spouseName="Sam", manager="Mo", assistantName="Al", imAddresses=["me@im"])
    tr.add("GET", f"{B}/me/contacts/delta", {"value": [c], "@odata.deltaLink": delta("a", "D0")})
    empty_folder(tr, "f", "D1")
    card = run(gc)[0].changes[0].card
    assert card.scalars == {"prefix": "Dr", "given": "Given1", "additional": "M", "family": "Family",
                            "suffix": "Jr", "nickname": "Bee", "company": "Acme", "department": "R&D",
                            "title": "Chief", "notes": "hello"}
    by = {(f.kind, f.label): f.value for f in card.fields}
    assert by[("phone", "mobile")] == "+447700900001" and by[("phone", "home")] == "+441"
    assert by[("phone", "work")] == "+442" and by[("date", "birthday")] == "1980-05-17"
    assert by[("related", "spouse")] == "Sam" and by[("related", "manager")] == "Mo"
    assert by[("related", "assistant")] == "Al" and by[("im", "")] == "me@im"
    assert by[("url", "")] == "https://x.example"
    home = next(f for f in card.fields if f.kind == "address" and f.label == "home")
    assert home.data == {"street": "1 St", "city": "Leeds", "postcode": "LS1"}
    assert next(f for f in card.fields if f.label == "work" and f.kind == "address").data["country"] == "UK"


def test_a_yearless_birthday_reads_without_the_placeholder_year(tr, gc):
    tr.add("GET", f"{B}/me/contacts/delta", {"value": [contact("1", birthday="1604-03-09T00:00:00Z")],
                                             "@odata.deltaLink": delta("a", "D0")})
    empty_folder(tr, "f", "D1")
    assert run(gc)[0].changes[0].card.values("date")[0].value == "--03-09"


def test_paging_follows_next_links_and_the_token_resumes_there(tr, gc):
    nxt = f"{B}/me/contacts/delta?$skiptoken=P2"
    tr.add("GET", f"{B}/me/contacts/delta", {"value": [contact("1")], "@odata.nextLink": nxt})
    tr.add("GET", nxt, {"value": [contact("2")], "@odata.deltaLink": delta("a", "D0")})
    empty_folder(tr, "f", "D1")
    it = gc.pull(None)
    first = next(it)
    st = json.loads(first.next_token)
    assert not first.done and st["k"] == "" and st["n"] == nxt
    it.close()
    before = len(tr.calls)
    rest = list(GraphContactsProvider(tr, lambda: "t").pull(first.next_token))
    assert [c.remote_id for c in rest[0].changes] == ["id-2"]
    assert any(c["url"] == nxt for c in tr.calls[before:])         # it asked for the saved next link
    assert not any(c["url"] == f"{B}/me/contacts/delta" for c in tr.calls[before:])   # not from the start
    assert rest[-1].done


def test_resume_skips_folders_already_done(tr, gc):
    token = json.dumps({"v": 1, "d": {"": delta("a", "D0")}, "done": [""]})
    tr.add("GET", f"{B}/me/contactFolders/{FOLDER_A}/contacts/delta",
           {"value": [contact("9", FOLDER_A)], "@odata.deltaLink": delta("f", "D1")})
    out = run(gc, token)
    assert not tr.to("GET", "/me/contacts/delta")
    assert out[-1].done and out[-1].changes[0].remote_id == "id-9"
    assert json.loads(out[-1].next_token)["d"][""] == delta("a", "D0")


def test_incremental_pull_starts_from_each_saved_delta_link_and_reports_deletes(tr, gc):
    d0, d1 = delta("me/contacts/delta", "D0"), delta("f", "D1")
    tr.add("GET", d0, {"value": [contact("1"), removed("3")], "@odata.deltaLink": delta("a", "E0")})
    tr.add("GET", d1, {"value": [], "@odata.deltaLink": delta("f", "E1")})
    out = run(gc, json.dumps({"v": 1, "d": {"": d0, FOLDER_A: d1}}))
    assert [(c.remote_id, c.card is None) for c in out[0].changes] == [("id-1", False), ("id-3", True)]
    assert json.loads(out[-1].next_token)["d"] == {"": delta("a", "E0"), FOLDER_A: delta("f", "E1")}


def test_a_gone_delta_link_is_token_expired(tr, gc):
    d0 = delta("me/contacts/delta", "D0")
    tr.add("GET", d0, {"error": {"code": "syncStateNotFound"}}, status=410)
    with pytest.raises(TokenExpired):
        run(gc, json.dumps({"v": 1, "d": {"": d0}}))
    with pytest.raises(TokenExpired):
        run(gc, "garbage")


def test_a_contact_moved_between_folders_is_not_deleted(tr, gc):
    tr.add("GET", f"{B}/me/contacts/delta", {"value": [removed("7", "changed"), removed("8")],
                                             "@odata.deltaLink": delta("a", "D0")})
    tr.add("GET", f"{B}/me/contactFolders/{FOLDER_A}/contacts/delta",
           {"value": [contact("8", FOLDER_A)], "@odata.deltaLink": delta("f", "D1")})
    out = run(gc)
    # "changed" is never a delete; the later change to id-8 stands (the engine sees the delete first, then the add)
    assert [c.remote_id for c in out[0].changes] == ["id-8"] and out[0].changes[0].card is None
    assert out[1].changes[0].card is not None


def test_get_maps_the_folder_and_missing_is_none(tr, gc):
    tr.add("GET", f"{B}/me/contacts/id-5", contact("5", FOLDER_A, categories=["Red"]))
    rc = gc.get("id-5")
    assert rc.card.groups == ["Family", "Red"]
    tr.add("GET", f"{B}/me/contacts/id-6", {"error": {"code": "ErrorItemNotFound"}}, status=404)
    assert gc.get("id-6") is None


def test_create_writes_only_what_graph_holds(tr, gc):
    tr.add("POST", f"{B}/me/contacts", contact("n", **{"id": "id-new", "changeKey": "ck-new"}), status=201)
    card = Card(scalars={"given": "Zed", "family": "Zee", "prefix": "Mr"},
                fields=[Field("email", f"e{i}@x.com") for i in range(4)] + [
                    Field("phone", "+441", "cell"), Field("phone", "+442", "home"), Field("phone", "+443", "fax"),
                    Field("social", "@zed", "twitter"), Field("custom", "Rex", "Pet"),
                    Field("date", "2000-01-02", "birthday"), Field("date", "2001-02-03", "anniversary"),
                    Field("related", "Sam", "spouse"), Field("related", "Mum", "mother"),
                    Field("address", "1 St, Leeds", "work", data={"street": "1 St", "city": "Leeds"})],
                groups=["Red"], favourite=True, photo=Photo("image/jpeg", b"abc"))
    assert gc.create(card) == ("id-new", "ck-new")
    body = tr.to("POST", "/me/contacts")[0]["body"]
    dumped = json.dumps(body)
    assert "@zed" not in dumped and "Rex" not in dumped and "mother" not in dumped and "2001-02-03" not in dumped
    assert "photo" not in body and "favourite" not in body
    assert body["title"] == "Mr" and body["givenName"] == "Zed"
    assert [e["address"] for e in body["emailAddresses"]] == ["e0@x.com", "e1@x.com", "e2@x.com"]
    assert body["mobilePhone"] == "+441" and body["homePhones"] == ["+442"] and body["businessPhones"] == ["+443"]
    assert body["birthday"] == "2000-01-02T00:00:00Z" and body["spouseName"] == "Sam"
    assert body["businessAddress"] == {"street": "1 St", "city": "Leeds"}
    assert body["categories"] == ["Red"]


def test_create_puts_a_contact_in_the_folder_its_group_names(tr, gc):
    tr.add("POST", f"{B}/me/contactFolders/{FOLDER_A}/contacts", contact("n", **{"id": "id-f", "changeKey": "ck-f"}),
           status=201)
    assert gc.create(Card(scalars={"given": "A"}, groups=["family", "Red"])) == ("id-f", "ck-f")
    assert tr.to("POST", f"/me/contactFolders/{FOLDER_A}/contacts")[0]["body"]["categories"] == ["Red"]


def test_update_reads_first_then_patches_and_clears_what_was_removed(tr, gc):
    tr.add("GET", f"{B}/me/contacts/id-1", {"id": "id-1", "changeKey": "ck-1", "parentFolderId": FOLDER_A})
    tr.add("PATCH", f"{B}/me/contacts/id-1", contact("1", changeKey="ck-2"))
    assert gc.update("id-1", "ck-1", Card(scalars={"given": "Ann"}, groups=["Family", "Red"])) == "ck-2"
    body = tr.to("PATCH", "/me/contacts/id-1")[0]["body"]
    assert body["givenName"] == "Ann" and body["surname"] is None and body["emailAddresses"] == []
    assert body["categories"] == ["Red"]                     # its own folder is not also a category
    assert "If-Match" not in tr.to("PATCH", "/me/contacts/id-1")[0]["headers"]


def test_update_can_send_if_match(tr):
    gc = GraphContactsProvider(tr, lambda: "t", send_if_match=True)
    tr.add("GET", f"{B}/me/contacts/id-1", {"id": "id-1", "changeKey": "ck-1", "parentFolderId": DEFAULT})
    tr.add("PATCH", f"{B}/me/contacts/id-1", contact("1", changeKey="ck-2"))
    gc.update("id-1", "ck-1", Card())
    assert tr.to("PATCH", "/me/contacts/id-1")[0]["headers"]["If-Match"] == 'W/"ck-1"'


def test_update_with_a_changed_key_is_a_conflict_and_writes_nothing(tr, gc):
    tr.add("GET", f"{B}/me/contacts/id-1", {"id": "id-1", "changeKey": "ck-NEWER", "parentFolderId": DEFAULT})
    with pytest.raises(VersionConflict):
        gc.update("id-1", "ck-1", Card())
    assert not tr.to("PATCH", "/me/contacts/id-1")


@pytest.mark.parametrize("status", [409, 412])
def test_a_refused_write_is_a_conflict(tr, gc, status):
    tr.add("GET", f"{B}/me/contacts/id-1", {"id": "id-1", "changeKey": "ck-1", "parentFolderId": DEFAULT})
    tr.add("PATCH", f"{B}/me/contacts/id-1", {"error": {"code": "ErrorIrresolvableConflict"}}, status=status)
    with pytest.raises(VersionConflict):
        gc.update("id-1", "ck-1", Card())


def test_delete_checks_then_deletes_and_a_gone_contact_is_quiet(tr, gc):
    tr.add("GET", f"{B}/me/contacts/id-1", {"id": "id-1", "changeKey": "ck-1", "parentFolderId": DEFAULT})
    tr.add("DELETE", f"{B}/me/contacts/id-1", status=204)
    gc.delete("id-1", "ck-1")
    assert len(tr.to("DELETE", "/me/contacts/id-1")) == 1
    tr.add("GET", f"{B}/me/contacts/id-2", {"error": {}}, status=404)
    gc.delete("id-2", "ck-2")
    tr.add("GET", f"{B}/me/contacts/id-3", {"id": "id-3", "changeKey": "other", "parentFolderId": DEFAULT})
    with pytest.raises(VersionConflict):
        gc.delete("id-3", "ck-3")
    assert not tr.to("DELETE", "/me/contacts/id-3")


def test_other_failures_are_registered_errors(tr, gc):
    from app.core.errors import AppErrorException
    tr.add("POST", f"{B}/me/contacts", {"error": {"code": "ErrorAccessDenied", "message": "no"}}, status=403)
    with pytest.raises(AppErrorException) as e:
        gc.create(Card(scalars={"given": "A"}))
    assert e.value.error.code == "ERR_ACCOUNT_REJECTED"


def test_a_card_round_trips_through_graph_json(tr, gc):
    card = Card(scalars={"given": "Ann", "family": "Lee", "prefix": "Dr", "company": "Acme"},
                fields=[Field("email", "a@x.com"), Field("phone", "+441", "mobile"), Field("phone", "+442", "work"),
                        Field("address", "1 St, Leeds", "home", data={"street": "1 St", "city": "Leeds"}),
                        Field("date", "1980-05-17", "birthday"), Field("related", "Sam", "spouse")],
                groups=["Red"])
    body = gc._to_body(card)
    body.update({"id": "i", "changeKey": "k"})
    assert cards_equal(card, gc._to_card(body), gc.caps)
