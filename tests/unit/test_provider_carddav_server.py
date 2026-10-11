"""The CardDAV connector against a small real CardDAV server on 127.0.0.1.

Layer: L7

Order 1k 4c, 4e, 4d. The server below speaks real HTTP: PROPFIND, REPORT
(sync-collection and addressbook-multiget), GET, PUT and DELETE with etags,
`If-Match` / `If-None-Match`, a redirect from `/.well-known/carddav`, Basic
authentication and group cards. The connector reaches it through a loopback
`urllib` transport defined here (not `app/accounts/http.py`), so the whole path
- discovery, paging, resume, delta, deletes, conflicts, groups - runs for real
and nothing leaves the machine.
"""

from __future__ import annotations

import base64
import re
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import unquote, urlparse

import pytest

from app.accounts.provider import ContactProvider, HttpResponse, TokenExpired, VersionConflict
from app.accounts.providers.carddav import CardDavProvider, basic_auth_header
from app.contacts import vcard
from app.contacts.card import Card, Field, Photo

BOOK = "/dav/books/u/main/"
OTHER = "/dav/books/u/other/"
NS = ('xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav" '
      'xmlns:cs="http://calendarserver.org/ns/"')


# --------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------

class Server:
    def __init__(self, *, sync: bool = True, sync_broken: bool = False, truncate: int = 0) -> None:
        self.sync, self.sync_broken, self.truncate = sync, sync_broken, truncate
        self.items: dict[str, tuple[str, str]] = {}          # name -> (etag, vcard text)
        self.log: list[tuple[int, str]] = []                 # (seq, name)
        self.seq = 0
        self.min_valid = 0                                    # tokens older than this are refused
        self.requests: list[tuple[str, str, dict]] = []
        self.expected_auth = "Basic " + base64.b64encode(b"alice:app-pw").decode()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):                        # noqa: D401 - silence
                pass

            def _read(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _reply(self, status, body=b"", headers=None, ctype="application/xml; charset=utf-8"):
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _go(self):
                body = self._read()
                server.requests.append((self.command, self.path, dict(self.headers.items())))
                if self.headers.get("Authorization") != server.expected_auth:
                    return self._reply(401, "", {"WWW-Authenticate": 'Basic realm="x"'})
                server.handle(self, self.command, unquote(urlparse(self.path).path), body)

            do_PROPFIND = do_REPORT = do_GET = do_PUT = do_DELETE = _go

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- direct state changes (what another client would do) -------------------

    def _etag(self) -> str:
        self.seq += 1
        return f'"e{self.seq}"'

    def store(self, name: str, text: str) -> str:
        etag = self._etag()
        self.items[name] = (etag, text)
        self.log.append((self.seq, name))
        return etag

    def remove(self, name: str) -> None:
        del self.items[name]
        self._etag()
        self.log.append((self.seq, name))

    def put_card(self, uid: str, card: Card) -> str:
        return self.store(f"{uid}.vcf", vcard.dumps(card, version="3.0", uid=uid))

    def put_group(self, uid: str, name: str, members: list[str]) -> str:
        text = ("BEGIN:VCARD\r\nVERSION:3.0\r\nN:%s;;;;\r\nFN:%s\r\nX-ADDRESSBOOKSERVER-KIND:group\r\nUID:%s\r\n%s"
                "END:VCARD\r\n" % (name, name, uid, "".join(
                    f"X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:{m}\r\n" for m in members)))
        return self.store(f"{uid}.vcf", text)

    # -- protocol ------------------------------------------------------------------

    def handle(self, h, method: str, path: str, body: bytes) -> None:
        if path == "/.well-known/carddav":
            return h._reply(301, "", {"Location": "/dav/"})
        if method == "PROPFIND":
            return self.propfind(h, path, body)
        if method == "REPORT" and path == BOOK:
            return self.report(h, body)
        if path.startswith(BOOK) and path != BOOK:
            return self.resource(h, method, path[len(BOOK):], body)
        h._reply(404, "")

    @staticmethod
    def _multi(rows: list[str]) -> str:
        return f'<?xml version="1.0"?><d:multistatus {NS}>{"".join(rows)}</d:multistatus>'

    @staticmethod
    def _row(href: str, props: str, status: str = "HTTP/1.1 200 OK") -> str:
        return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
                f"<d:status>{status}</d:status></d:propstat></d:response>")

    def propfind(self, h, path: str, body: bytes) -> None:
        depth = h.headers.get("Depth", "0")
        want = body.decode()
        if path == "/dav/":
            return h._reply(207, self._multi([self._row(
                "/dav/", "<d:current-user-principal><d:href>/dav/principals/u/</d:href></d:current-user-principal>")]))
        if path == "/dav/principals/u/":
            return h._reply(207, self._multi([self._row(
                path, "<c:addressbook-home-set><d:href>/dav/books/u/</d:href></c:addressbook-home-set>")]))
        if path == "/dav/books/u/":
            rows = [self._row(path, "<d:resourcetype><d:collection/></d:resourcetype>")]
            for slug, title in (("main", "Main"), ("other", "Other")):
                rows.append(self._row(f"/dav/books/u/{slug}/",
                                      "<d:resourcetype><d:collection/><c:addressbook/></d:resourcetype>"
                                      f"<d:displayname>{title}</d:displayname>"))
            return h._reply(207, self._multi(rows))
        if path in (BOOK, OTHER):                       # the second book is always empty
            items = self.items if path == BOOK else {}
            props = "<d:resourcetype><d:collection/><c:addressbook/></d:resourcetype>"
            if "supported-report-set" in want:
                reports = "<d:supported-report><d:report><c:addressbook-multiget/></d:report></d:supported-report>"
                if self.sync and path == BOOK:
                    reports += "<d:supported-report><d:report><d:sync-collection/></d:report></d:supported-report>"
                props += f"<d:supported-report-set>{reports}</d:supported-report-set>"
            if "getctag" in want:
                props += f"<cs:getctag>ctag-{self.seq}</cs:getctag>"
            rows = [self._row(path, props)]
            if depth == "1":
                for name, (etag, _) in sorted(items.items()):
                    rows.append(self._row(path + name, f"<d:getetag>{etag}</d:getetag><d:resourcetype/>"))
            return h._reply(207, self._multi(rows))
        if path.startswith(BOOK):
            name = path[len(BOOK):]
            if name in self.items:
                return h._reply(207, self._multi([self._row(path, f"<d:getetag>{self.items[name][0]}</d:getetag>")]))
        h._reply(404, "")

    def report(self, h, body: bytes) -> None:
        root = ET.fromstring(body)
        tag = root.tag.rsplit("}", 1)[-1]
        if tag == "addressbook-multiget":
            rows = []
            for href in root.findall("{DAV:}href"):
                name = unquote(href.text or "")[len(BOOK):]
                if name in self.items:
                    etag, text = self.items[name]
                    esc = text.replace("&", "&amp;").replace("<", "&lt;")
                    rows.append(self._row(href.text, f"<d:getetag>{etag}</d:getetag>"
                                                     f"<c:address-data>{esc}</c:address-data>"))
                else:
                    rows.append(f"<d:response><d:href>{href.text}</d:href><d:status>HTTP/1.1 404 Not Found"
                                f"</d:status></d:response>")
            return h._reply(207, self._multi(rows))
        if tag == "sync-collection":
            if not self.sync or self.sync_broken:
                return h._reply(501, "")
            token = (root.findtext("{DAV:}sync-token") or "").strip()
            if token:
                m = re.fullmatch(r"tok-(\d+)", token)
                if not m or int(m.group(1)) < self.min_valid:
                    return h._reply(403, f'<?xml version="1.0"?><d:error {NS}><d:valid-sync-token/></d:error>')
                since = int(m.group(1))
            else:
                since = 0
            latest: dict[str, int] = {}
            for seq, name in self.log:
                if seq > since:
                    latest[name] = seq
            entries = sorted(latest.items(), key=lambda kv: kv[1])
            new = self.seq
            rows = []
            if self.truncate and len(entries) > self.truncate:
                entries = entries[:self.truncate]
                new = entries[-1][1]
                rows.append(f"<d:response><d:href>{BOOK}</d:href><d:status>HTTP/1.1 507 Insufficient Storage"
                            f"</d:status></d:response>")
            for name, _ in entries:
                if name in self.items:
                    rows.append(self._row(BOOK + name, f"<d:getetag>{self.items[name][0]}</d:getetag>"))
                else:
                    rows.append(f"<d:response><d:href>{BOOK + name}</d:href><d:status>HTTP/1.1 404 Not Found"
                                f"</d:status></d:response>")
            return h._reply(207, self._multi(rows).replace(
                "</d:multistatus>", f"<d:sync-token>tok-{new}</d:sync-token></d:multistatus>"))
        h._reply(400, "")

    def resource(self, h, method: str, name: str, body: bytes) -> None:
        cur = self.items.get(name)
        if method == "GET":
            if cur is None:
                return h._reply(404, "")
            return h._reply(200, cur[1], {"ETag": cur[0]}, ctype="text/vcard; charset=utf-8")
        if method == "PUT":
            if h.headers.get("If-None-Match") == "*" and cur is not None:
                return h._reply(412, "")
            im = h.headers.get("If-Match")
            if im and (cur is None or cur[0] != im):
                return h._reply(412, "")
            etag = self.store(name, body.decode("utf-8"))
            return h._reply(201 if cur is None else 204, "", {"ETag": etag})
        if method == "DELETE":
            if cur is None:
                return h._reply(404, "")
            im = h.headers.get("If-Match")
            if im and cur[0] != im:
                return h._reply(412, "")
            self.remove(name)
            return h._reply(204, "")
        h._reply(405, "")


# --------------------------------------------------------------------------
# A loopback transport (about twenty lines; deliberately not app/accounts/http.py)
# --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class LoopbackTransport:
    def request(self, method, url, *, headers=None, body=None, timeout=30.0) -> HttpResponse:
        assert urlparse(url).hostname == "127.0.0.1", url          # nothing leaves the machine
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            r = _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            r = e
        return HttpResponse(r.status if hasattr(r, "status") else r.code,
                            {k.lower(): v for k, v in r.headers.items()}, r.read())


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------

@pytest.fixture
def srv():
    s = Server()
    yield s
    s.stop()


def make(srv: Server, **kw) -> CardDavProvider:
    kw.setdefault("server_url", srv.base)
    return CardDavProvider(LoopbackTransport(), basic_auth_header("alice", lambda: "app-pw"), **kw)


def person(given: str, **kw) -> Card:
    return Card(scalars={"given": given, "family": "Test"},
                fields=[Field("email", f"{given.lower()}@example.com", "work")], **kw)


def drain(cd: CardDavProvider, token: Optional[str] = None):
    pages = list(cd.pull(token))
    changes = [c for p in pages for c in p.changes]
    return pages, changes, pages[-1].next_token


def seed(srv: Server, n: int) -> None:
    for i in range(n):
        srv.put_card(f"uid-{i}", person(f"P{i}"))


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_it_satisfies_the_contract(srv):
    assert isinstance(make(srv), ContactProvider)


def test_discovery_follows_the_well_known_redirect_to_the_address_books(srv):
    cd = make(srv)
    books = cd.list_addressbooks()
    assert [(b.name, urlparse(b.url).path) for b in books] == [("Main", BOOK), ("Other", "/dav/books/u/other/")]
    assert srv.requests[0][1] == "/.well-known/carddav"
    assert all(r[2].get("Authorization") == srv.expected_auth for r in srv.requests)


def test_discovery_picks_the_named_book(srv):
    seed(srv, 1)
    cd = make(srv, addressbook_name="other")
    _, changes, _ = drain(cd)
    assert changes == [] and cd.addressbook_url.endswith("/dav/books/u/other/")


def test_a_typed_in_address_book_url_skips_discovery(srv):
    seed(srv, 2)
    cd = make(srv, server_url="", addressbook_url=srv.base + BOOK)
    _, changes, _ = drain(cd)
    assert len(changes) == 2
    assert not any(r[1] == "/.well-known/carddav" for r in srv.requests)


def test_a_wrong_password_is_a_registered_signin_error(srv):
    from app.core.errors import AppErrorException
    cd = CardDavProvider(LoopbackTransport(), basic_auth_header("alice", lambda: "wrong"), server_url=srv.base)
    with pytest.raises(AppErrorException) as e:
        list(cd.pull(None))
    assert e.value.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED"


def test_full_pull_pages_then_incremental_delta_with_deletes(srv):
    seed(srv, 5)
    cd = make(srv, page_size=2)
    pages, changes, token = drain(cd)
    assert len(pages) == 3 and [p.done for p in pages] == [False, False, True]
    assert sorted(c.remote_id for c in changes) == [f"uid-{i}.vcf" for i in range(5)]
    c0 = next(c for c in changes if c.remote_id == "uid-0.vcf")
    assert c0.version == '"e1"' and c0.card.scalars["given"] == "P0"
    assert c0.card.values("email")[0].value == "p0@example.com"

    srv.put_card("uid-1", person("P1-changed"))
    srv.remove("uid-2.vcf")
    srv.put_card("uid-new", person("Newbie"))
    _, delta, token2 = drain(make(srv), token)
    got = {c.remote_id: c for c in delta}
    assert set(got) == {"uid-1.vcf", "uid-2.vcf", "uid-new.vcf"}
    assert got["uid-2.vcf"].card is None
    assert got["uid-1.vcf"].card.scalars["given"] == "P1-changed"

    _, none, _ = drain(make(srv), token2)
    assert none == []                                   # nothing changed: nothing comes back


def test_paging_resumes_after_any_page(srv):
    seed(srv, 5)
    it = make(srv, page_size=2).pull(None)
    first = next(it)
    it.close()
    _, rest, token = drain(make(srv, page_size=2), first.next_token)
    ids = sorted(c.remote_id for c in first.changes + rest)
    assert ids == [f"uid-{i}.vcf" for i in range(5)]
    # and the final token is a real one: the next pull is empty
    assert drain(make(srv), token)[1] == []


def test_a_server_that_truncates_its_sync_report_is_followed_to_the_end(srv):
    srv.truncate = 2
    seed(srv, 5)
    _, changes, _ = drain(make(srv))
    assert len(changes) == 5


def test_an_expired_sync_token_is_token_expired(srv):
    seed(srv, 2)
    _, _, token = drain(make(srv))
    srv.min_valid = 10 ** 6
    with pytest.raises(TokenExpired):
        drain(make(srv), token)
    with pytest.raises(TokenExpired):
        drain(make(srv), "not json")


@pytest.mark.parametrize("broken", [False, True])
def test_without_sync_collection_etags_and_ctag_find_the_changes(broken):
    s = Server(sync=broken, sync_broken=broken)         # absent, or advertised but refused with 501
    try:
        seed(s, 4)
        _, changes, token = drain(make(s, page_size=3))
        assert len(changes) == 4
        assert '"m":"etag"' in token and '"c":"ctag-' in token

        before = len([r for r in s.requests if r[0] == "REPORT"])
        _, none, token = drain(make(s), token)
        assert none == []
        assert len([r for r in s.requests if r[0] == "REPORT"]) == before   # ctag unchanged: not even a listing

        s.put_card("uid-1", person("P1-changed"))
        s.remove("uid-3.vcf")
        _, delta, _ = drain(make(s), token)
        assert {c.remote_id: c.card is None for c in delta} == {"uid-1.vcf": False, "uid-3.vcf": True}
    finally:
        s.stop()


def test_create_update_delete_round_trip(srv):
    cd = make(srv)
    drain(cd)
    rid, etag = cd.create(Card(scalars={"given": "Ann", "family": "Lee", "company": "Acme"},
                               fields=[Field("email", "ann@x.com", "home"), Field("phone", "+441", "mobile"),
                                       Field("date", "1980-05-17", "birthday")],
                               photo=Photo("image/jpeg", b"\xff\xd8photo")))
    assert rid.endswith(".vcf") and etag and rid in srv.items
    put = next(r for r in srv.requests if r[0] == "PUT")
    assert put[2]["If-None-Match"] == "*" and put[2]["Content-Type"].startswith("text/vcard")
    stored = srv.items[rid][1]
    assert "VERSION:3.0" in stored and "UID:" in stored and "PHOTO;ENCODING=b" in stored

    got = cd.get(rid)
    assert got.version == etag and got.card.scalars["company"] == "Acme"
    assert got.card.photo is not None and got.card.photo.data == b"\xff\xd8photo"

    card = got.card
    card.scalars["company"] = "Beta"
    new = cd.update(rid, etag, card)
    assert new != etag and "Beta" in srv.items[rid][1]
    assert "UID:" + rid.removesuffix(".vcf") in srv.items[rid][1]          # the uid survives an update
    assert any(r[0] == "PUT" and r[2].get("If-Match") == etag for r in srv.requests)

    cd.delete(rid, new)
    assert rid not in srv.items and cd.get(rid) is None


def test_a_write_that_lost_the_race_is_a_version_conflict(srv):
    cd = make(srv)
    rid, etag = cd.create(person("Race"))
    srv.put_card(rid.removesuffix(".vcf"), person("Race-by-someone-else"))      # the server moves on
    with pytest.raises(VersionConflict):
        cd.update(rid, etag, person("Race-mine"))
    with pytest.raises(VersionConflict):
        cd.delete(rid, etag)
    assert "someone-else" in srv.items[rid][1]                                  # nothing was overwritten


def test_updating_a_contact_deleted_meanwhile_is_a_version_conflict(srv):
    cd = make(srv)
    rid, etag = cd.create(person("Gone"))
    srv.remove(rid)
    with pytest.raises(VersionConflict):
        cd.update(rid, etag, person("Gone"))
    cd.delete(rid, etag)                                                         # already gone: quiet


def test_group_cards_give_card_groups_and_a_membership_change_redelivers_the_member(srv):
    srv.put_card("u-ann", person("Ann"))
    srv.put_card("u-bob", person("Bob"))
    srv.put_group("g-fam", "Family", ["u-ann", "u-bob"])
    srv.put_group("g-club", "Club", ["u-bob"])
    _, changes, token = drain(make(srv, page_size=2))
    by = {c.remote_id: c.card for c in changes}
    assert set(by) == {"u-ann.vcf", "u-bob.vcf"}                  # group cards are not contacts
    assert by["u-ann.vcf"].groups == ["Family"] and sorted(by["u-bob.vcf"].groups) == ["Club", "Family"]

    # Only the group card changes; Bob's own card does not. Bob is still delivered, with his new groups.
    srv.put_group("g-club", "Club", [])
    _, delta, token = drain(make(srv), token)
    assert [(c.remote_id, c.card.groups) for c in delta] == [("u-bob.vcf", ["Family"])]

    # A group deleted outright.
    srv.remove("g-fam.vcf")
    _, delta, _ = drain(make(srv), token)
    assert sorted((c.remote_id, tuple(c.card.groups)) for c in delta if c.card) == [("u-ann.vcf", ()), ("u-bob.vcf", ())]


def test_a_group_card_that_arrives_after_its_members_still_labels_them(srv):
    for i in range(6):
        srv.put_card(f"u{i}", person(f"M{i}"))
    srv.put_group("zz-group", "Late", ["u0", "u5"])
    _, changes, _ = drain(make(srv, page_size=2))
    last = {c.remote_id: c.card.groups for c in changes}                 # the later delivery wins
    assert last["u0.vcf"] == ["Late"] and last["u5.vcf"] == ["Late"] and last["u3.vcf"] == []


def test_writing_groups_in_cards_mode_edits_group_cards(srv):
    srv.put_group("g-fam", "Family", [])
    cd = make(srv, group_mode="cards")
    drain(cd)
    rid, _ = cd.create(person("Cy", groups=["Family", "Brand new"]))
    uid = rid.removesuffix(".vcf")
    assert "CATEGORIES" not in srv.items[rid][1]
    assert f"X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:{uid}" in srv.items["g-fam.vcf"][1]
    new_groups = [t for n, (_, t) in srv.items.items() if "FN:Brand new" in t]
    assert len(new_groups) == 1 and "X-ADDRESSBOOKSERVER-KIND:group" in new_groups[0]

    # drop Family from Cy, via update, with the group card's own etag guarding the edit
    got = cd.get(rid)
    assert sorted(got.card.groups) == ["Brand new", "Family"]
    got.card.groups = ["Brand new"]
    cd.update(rid, got.version, got.card)
    assert uid not in srv.items["g-fam.vcf"][1]
    assert any(r[0] == "PUT" and r[1].endswith("g-fam.vcf") and r[2].get("If-Match") for r in srv.requests)

    # deleting the contact takes it out of its groups too
    cd.delete(rid, cd.get(rid).version)
    assert not any(uid in t for n, (_, t) in srv.items.items())


def test_categories_mode_reads_and_writes_categories(srv):
    srv.put_card("u1", person("Nell", groups=["Friends", "Work"]))
    cd = make(srv)                                                       # auto: no group card seen
    _, changes, _ = drain(cd)
    assert changes[0].card.groups == ["Friends", "Work"]
    rid, _ = cd.create(person("Ola", groups=["Friends"]))
    assert "CATEGORIES:Friends" in srv.items[rid][1]
    assert not any("X-ADDRESSBOOKSERVER-KIND" in t for _, t in srv.items.values())


def _full_card(*, related: bool) -> Card:
    fields = [Field("email", "a@x.com", "work"), Field("phone", "+441", "home"),
              Field("address", "1 St, Leeds", "home", data={"street": "1 St", "city": "Leeds"}),
              Field("url", "https://a.example"), Field("date", "1980-05-17", "birthday"),
              Field("date", "2005-06-01", "anniversary")]
    if related:
        fields.append(Field("related", "Bob", "spouse"))
    return Card(scalars={"prefix": "Dr", "given": "Ann", "additional": "M", "family": "Lee", "suffix": "PhD",
                         "nickname": "Annie", "company": "Acme", "department": "R&D", "title": "Chief",
                         "notes": "line1\nline2, with; punctuation"}, fields=fields)


def test_a_full_card_survives_the_trip_as_vcard_3(srv):
    # `related` is left out here on purpose: app/contacts/vcard.py writes it as X-RELATED in 3.0 and does
    # not read that back as `related` (reported to the master thread); 4.0 below carries it.
    from app.contacts.card import cards_equal
    cd = make(srv)
    card = _full_card(related=False)
    rid, _ = cd.create(card)
    assert "VERSION:3.0" in srv.items[rid][1]
    assert cards_equal(card, cd.get(rid).card, cd.caps)


def test_a_full_card_survives_the_trip_as_vcard_4(srv):
    from app.contacts.card import cards_equal
    cd = make(srv, vcard_version="4.0")
    card = _full_card(related=True)
    rid, _ = cd.create(card)
    assert "VERSION:4.0" in srv.items[rid][1]
    assert cards_equal(card, cd.get(rid).card, cd.caps)
