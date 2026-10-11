"""The CardDAV connector against recorded-style replies (iCloud's shape) and hostile ones.

Layer: L7

Order 1k 4c, 4e, 4d. A fake `Transport` keyed by (method, URL) serves canned
XML and records every request. The real-HTTP behaviour (paging, delta, etags,
groups, conflicts) is in `test_provider_carddav_server.py`; this file covers what
a canned reply can pin exactly: the iCloud preset and its discovery with absolute
partition hosts, where credentials may and may not go, the exact request
headers, a refused sync token, and XML that tries to hurt the parser.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import urlparse

import pytest

from app.accounts.provider import HttpResponse, TokenExpired, VersionConflict
from app.accounts.providers.carddav import CardDavProvider, basic_auth_header
from app.contacts.card import Card, Field
from app.core.errors import AppErrorException

NS = ('xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav" '
      'xmlns:cs="http://calendarserver.org/ns/"')
ICLOUD = "https://contacts.icloud.com"
HOME_HOST = "https://p12-contacts.icloud.com:443"
BOOK = f"{HOME_HOST}/12345/carddavhome/card/"
AUTH = "Basic " + base64.b64encode(b"me@icloud.com:abcd-efgh-ijkl-mnop").decode()


class FakeTransport:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[HttpResponse]] = {}
        self.calls: list[dict] = []

    def add(self, method: str, url: str, body: str = "", status: int = 207, headers=None) -> None:
        self.routes.setdefault((method, url), []).append(
            HttpResponse(status, {k.lower(): v for k, v in (headers or {}).items()}, body.encode()))

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "body": (body or b"").decode()})
        q = self.routes.get((method, url))
        if not q:
            raise AssertionError(f"unexpected request {method} {url}")
        return q.pop(0) if len(q) > 1 else q[0]

    def of(self, method: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method]


def multi(*rows: str, token: str = "") -> str:
    tail = f"<d:sync-token>{token}</d:sync-token>" if token else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><d:multistatus {NS}>{"".join(rows)}{tail}</d:multistatus>'


def row(href: str, props: str) -> str:
    return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
            f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")


def icloud_transport() -> FakeTransport:
    t = FakeTransport()
    t.add("PROPFIND", f"{ICLOUD}/.well-known/carddav", "", 301, {"Location": f"{ICLOUD}/"})
    t.add("PROPFIND", f"{ICLOUD}/", multi(row(
        "/", "<d:current-user-principal><d:href>/12345/principal/</d:href></d:current-user-principal>")))
    t.add("PROPFIND", f"{ICLOUD}/12345/principal/", multi(row(
        "/12345/principal/",
        f"<c:addressbook-home-set><d:href>{HOME_HOST}/12345/carddavhome/</d:href></c:addressbook-home-set>")))
    t.add("PROPFIND", f"{HOME_HOST}/12345/carddavhome/", multi(
        row("/12345/carddavhome/", "<d:resourcetype><d:collection/></d:resourcetype>"),
        row("/12345/carddavhome/card/",
            "<d:resourcetype><d:collection/><c:addressbook/></d:resourcetype><d:displayname>card</d:displayname>"),
        row("/12345/carddavhome/inbox/", "<d:resourcetype><d:collection/></d:resourcetype>")))
    return t


def icloud(t) -> CardDavProvider:
    return CardDavProvider.icloud(t, "me@icloud.com", lambda: "abcd-efgh-ijkl-mnop")


def test_the_icloud_preset_discovers_the_address_book_on_its_partition_host():
    t = icloud_transport()
    cd = icloud(t)
    [book] = cd.list_addressbooks()
    assert book.url == BOOK and book.name == "card"
    # HTTP Basic with the app-specific password, on the first host and the partition host
    assert [c["headers"]["Authorization"] for c in t.calls] == [AUTH] * len(t.calls)
    assert t.calls[0]["url"] == f"{ICLOUD}/.well-known/carddav"
    assert cd.caps.photo and "social" in cd.caps.kinds


def test_the_secret_is_asked_for_on_every_request():
    secrets = iter(["one", "two", "three", "four", "five", "six", "seven"])
    t = icloud_transport()
    cd = CardDavProvider(t, basic_auth_header("u", lambda: next(secrets)), server_url=ICLOUD)
    cd.list_addressbooks()
    seen = {base64.b64decode(c["headers"]["Authorization"][6:]).decode() for c in t.calls}
    assert len(seen) == len(t.calls) > 1


def test_credentials_are_never_sent_to_another_site():
    t = FakeTransport()
    t.add("PROPFIND", "https://dav.example.org/.well-known/carddav", "", 302,
          {"Location": "https://evil.example.net/steal"})
    t.add("PROPFIND", "https://evil.example.net/steal", "", 401)
    cd = CardDavProvider(t, lambda: "Basic secret", server_url="https://dav.example.org")
    with pytest.raises(AppErrorException) as e:
        cd.list_addressbooks()
    assert e.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED"
    assert "Authorization" in t.calls[0]["headers"]
    assert all("Authorization" not in c["headers"] for c in t.calls[1:])


def test_a_server_with_no_address_book_gets_a_registered_error():
    t = FakeTransport()
    t.add("PROPFIND", "https://dav.example.org/.well-known/carddav", multi(row("/", "<d:resourcetype/>")))
    t.add("PROPFIND", "https://dav.example.org/", multi(row("/", "<d:resourcetype/>")))
    cd = CardDavProvider(t, lambda: "Basic x", server_url="https://dav.example.org")
    with pytest.raises(AppErrorException) as e:
        cd.list_addressbooks()
    assert e.value.error.code == "ERR_ACCOUNT_REJECTED"


def test_constructor_refuses_nonsense():
    t = FakeTransport()
    with pytest.raises(ValueError):
        CardDavProvider(t, lambda: "x")
    with pytest.raises(ValueError):
        CardDavProvider(t, lambda: "x", server_url="https://a", vcard_version="2.1")
    with pytest.raises(ValueError):
        CardDavProvider(t, lambda: "x", server_url="https://a", group_mode="sideways")


# -- reading ---------------------------------------------------------------------

def book_provider(t, **kw) -> CardDavProvider:
    return CardDavProvider(t, lambda: "Basic x", addressbook_url="https://dav.example.org/books/main/", **kw)


BASE = "https://dav.example.org/books/main/"


def srs(sync: bool) -> str:
    inner = "<d:supported-report><d:report><c:addressbook-multiget/></d:report></d:supported-report>"
    if sync:
        inner += "<d:supported-report><d:report><d:sync-collection/></d:report></d:supported-report>"
    return multi(row("/books/main/", f"<d:supported-report-set>{inner}</d:supported-report-set>"))


def vcf(uid: str, name: str) -> str:
    return f"BEGIN:VCARD\nVERSION:3.0\nN:{name};Ann;;;\nFN:Ann {name}\nUID:{uid}\nREV:20261001T100000Z\nEND:VCARD\n"


def test_a_sync_report_then_a_multiget_read_a_card_with_its_edit_time():
    t = FakeTransport()
    t.add("PROPFIND", BASE, srs(True))
    t.add("REPORT", BASE, multi(row("/books/main/a%40b.vcf", '<d:getetag>"1"</d:getetag>'), token="S-1"))
    t.add("REPORT", BASE, multi(row("/books/main/a%40b.vcf",
                                    f'<d:getetag>"1"</d:getetag><c:address-data>{vcf("A", "Lee")}</c:address-data>')))
    pages = list(book_provider(t).pull(None))
    [rc] = pages[-1].changes
    assert rc.remote_id == "a@b.vcf" and rc.version == '"1"' and rc.card.scalars["family"] == "Lee"
    assert rc.edited_at > 1.7e9
    assert json.loads(pages[-1].next_token)["t"] == "S-1"
    sync_body = t.of("REPORT")[0]["body"]
    assert "<d:sync-token></d:sync-token>" in sync_body and "sync-level>1<" in sync_body
    multiget = t.of("REPORT")[1]
    assert "<d:href>/books/main/a@b.vcf</d:href>" in multiget["body"] and multiget["headers"]["Depth"] == "1"


@pytest.mark.parametrize("status", [403, 409, 412])
def test_the_valid_sync_token_precondition_is_token_expired(status):
    t = FakeTransport()
    t.add("PROPFIND", BASE, srs(True))
    t.add("REPORT", BASE, f'<?xml version="1.0"?><d:error {NS}><d:valid-sync-token/></d:error>', status)
    token = json.dumps({"v": 1, "m": "sync", "t": "old", "i": {}, "g": {}})
    with pytest.raises(TokenExpired):
        list(book_provider(t).pull(token))


def test_a_403_without_that_precondition_is_just_a_refusal():
    t = FakeTransport()
    t.add("PROPFIND", BASE, srs(True))
    t.add("REPORT", BASE, "forbidden", 403)
    with pytest.raises(AppErrorException) as e:
        list(book_provider(t).pull(json.dumps({"v": 1, "m": "sync", "t": "old"})))
    assert e.value.error.code == "ERR_ACCOUNT_REJECTED"


BOMB = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
        f'<d:multistatus {NS}><d:response><d:href>&lol2;</d:href></d:response></d:multistatus>')


@pytest.mark.parametrize("xml", [BOMB, '<!DOCTYPE foo SYSTEM "file:///etc/passwd">' + multi()])
def test_xml_with_a_doctype_is_refused_before_it_is_parsed(xml):
    t = FakeTransport()
    t.add("PROPFIND", BASE, srs(False))
    t.add("PROPFIND", BASE, xml)
    with pytest.raises(AppErrorException) as e:
        list(book_provider(t).pull(None))
    assert e.value.error.code == "ERR_ACCOUNT_REJECTED" and "DOCTYPE" in (e.value.error.details or "")


def test_apple_and_vcard4_group_cards_are_read_and_are_not_contacts():
    apple = ("BEGIN:VCARD\nVERSION:3.0\nN:Family;;;;\nFN:Family\nX-ADDRESSBOOKSERVER-KIND:group\n"
             "X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:A\nX-ADDRESSBOOKSERVER-MEMBER:urn:uuid:MISSING\nUID:G1\nEND:VCARD\n")
    v4 = "BEGIN:VCARD\nVERSION:4.0\nKIND:group\nFN:Club\nMEMBER:urn:uuid:A\nUID:G2\nEND:VCARD\n"
    t = FakeTransport()
    t.add("PROPFIND", BASE, srs(True))
    t.add("REPORT", BASE, multi(row("/books/main/a.vcf", '<d:getetag>"1"</d:getetag>'),
                                row("/books/main/g1.vcf", '<d:getetag>"2"</d:getetag>'),
                                row("/books/main/g2.vcf", '<d:getetag>"3"</d:getetag>'), token="S"))
    t.add("REPORT", BASE, multi(
        row("/books/main/a.vcf", f'<d:getetag>"1"</d:getetag><c:address-data>{vcf("A", "Lee")}</c:address-data>'),
        row("/books/main/g1.vcf", f'<d:getetag>"2"</d:getetag><c:address-data>{apple}</c:address-data>'),
        row("/books/main/g2.vcf", f'<d:getetag>"3"</d:getetag><c:address-data>{v4}</c:address-data>')))
    pages = list(book_provider(t).pull(None))
    changes = [c for p in pages for c in p.changes]
    assert [c.remote_id for c in changes] == ["a.vcf"]
    assert sorted(changes[0].card.groups) == ["Club", "Family"]


# -- writing -----------------------------------------------------------------------

def test_create_sends_if_none_match_and_vcard_3_and_asks_for_the_etag_when_the_put_gives_none():
    t = FakeTransport()
    cd = book_provider(t)
    seen: dict = {}

    def request(method, url, *, headers=None, body=None, timeout=30.0):
        t.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "body": (body or b"").decode()})
        if method == "PUT":
            seen["url"] = url
            return HttpResponse(201, {}, b"")                       # no ETag header
        return HttpResponse(207, {}, multi(row(urlparse(url).path, '<d:getetag>"srv-etag"</d:getetag>')).encode())
    t.request = request                                             # type: ignore[method-assign]
    rid, etag = cd.create(Card(scalars={"given": "Zed", "family": "Zee"}, fields=[Field("email", "z@x.com")]))
    assert etag == '"srv-etag"' and seen["url"] == BASE + rid
    put = t.of("PUT")[0]
    assert put["headers"]["If-None-Match"] == "*" and put["headers"]["Content-Type"] == "text/vcard; charset=utf-8"
    assert put["headers"]["Authorization"] == "Basic x"
    assert "VERSION:3.0" in put["body"] and f"UID:{rid[:-4]}" in put["body"]


def test_update_and_delete_send_if_match_and_map_412():
    t = FakeTransport()
    cd = book_provider(t)
    t.add("PUT", BASE + "x.vcf", "", 412)
    t.add("DELETE", BASE + "x.vcf", "", 412)
    cd._i["x.vcf"] = ['"1"', "UX", "c", ""]
    with pytest.raises(VersionConflict):
        cd.update("x.vcf", '"1"', Card(scalars={"given": "A"}))
    with pytest.raises(VersionConflict):
        cd.delete("x.vcf", '"1"')
    assert t.of("PUT")[0]["headers"]["If-Match"] == '"1"' and t.of("DELETE")[0]["headers"]["If-Match"] == '"1"'
    assert "UID:UX" in t.of("PUT")[0]["body"]


def test_a_vcard_4_account_writes_vcard_4():
    t = FakeTransport()
    cd = book_provider(t, vcard_version="4.0")
    t.add("PUT", BASE + "x.vcf", "", 204, {"ETag": '"2"'})
    cd._i["x.vcf"] = ['"1"', "UX", "c", ""]
    assert cd.update("x.vcf", '"1"', Card(scalars={"given": "A"})) == '"2"'
    assert "VERSION:4.0" in t.of("PUT")[0]["body"]
