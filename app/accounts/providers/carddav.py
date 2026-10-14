"""Contacts over CardDAV (RFC 6352): iCloud, Fastmail, Nextcloud, any server.

Layer: L7

Order 1k 4c and 4e. `CardDavProvider` implements `ContactProvider` over a
`Transport`. It speaks only standard CardDAV, so the same class serves iCloud
(the `icloud` preset: contacts.icloud.com with an app-specific password) and any
server the person points it at, by a typed-in address book URL or by discovery.

**Discovery** (RFC 6764, without DNS: a Transport is the only way out). From a
server URL: `/.well-known/carddav`, else the URL itself; a PROPFIND for
`current-user-principal` and `addressbook-home-set`, then the home's children
that are address books. Redirects are followed by hand (the transport hands
back the 3xx), and the Authorization header is only ever sent to the host the
person gave or a host of the same site (iCloud answers from `pNN-contacts.icloud.com`).

**Reading.** If the server lists the `sync-collection` report (RFC 6578) the
connector uses it, keeping the server's `sync-token`; otherwise it lists the book
with PROPFIND and compares etags against what it delivered last time (after
checking the `getctag`, when the server has one, to skip the listing). Card data
is always fetched with `addressbook-multiget`, a page at a time. XML is read with
the standard library after refusing any body that has a DOCTYPE (no entity can
be declared, so none can be expanded).

**Token format.** JSON in a string, so the engine can resume after any page:

    {"v":1,
     "m":"sync" | "etag",                  how this book is followed
     "t":"<sync-token>",                   sync mode: the token to ask from
     "c":"<ctag>",                         etag mode: the ctag at the last full listing
     "i":{"<name>.vcf":["<etag>","<uid>","c"|"g","<groups delivered>"]},
     "g":{"<name>.vcf":["<group name>",["<member uid>", ...]]},
     "p":{"f":[names still to fetch],"x":[names removed],"nt":"<new sync-token>","nc":"<new ctag>"}}

`i` is the connector's private index of what it has delivered (name -> etag,
vCard UID, kind, and the group names it gave that contact); `g` is the
membership of every group card. They live in the token because the connector
keeps no other state between runs, and a group card changing must re-deliver
the members whose cards did not change. `p` exists only in the middle of a pull.
A sync-token the server refuses (the `valid-sync-token` precondition), or text
that is not this JSON, raises `TokenExpired`. A token that carries 2,000
contacts is about 200 KB.

**remote_id** is the card's file name inside the address book (`<uid>.vcf`),
percent-decoded, so it survives the book being reached by a different URL.

**Groups.** Two conventions exist and both are read. A group card is a vCard
with `X-ADDRESSBOOKSERVER-KIND:group` (Apple, vCard 3.0) or `KIND:group` (vCard
4.0), whose members are `X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:<uid>` / `MEMBER:`
lines; group cards are not contacts and are never delivered as one. Other
servers (Nextcloud) put names in the contact's own `CATEGORIES`. `group_mode`
chooses what is written: `"cards"` edits the group cards (and writes no
CATEGORIES, so the two cannot disagree), `"categories"` writes CATEGORIES and
never touches a group card, `"auto"` (default) is `cards` once the book has been
seen to contain a group card and `categories` until then; the `icloud` preset
is `cards`. Reading unions both. A new group name in cards mode creates a group
card. The group list used for a write is the one the last `pull` saw, and each
group card is re-read just before it is edited.

**What is lost.** A vCard property that `app.contacts.vcard` does not map to a
`Card` field is not rewritten on update (the card is written whole, not patched).
A pull that finds a group card after the contacts that belong to it re-fetches
those contacts once at the end, so a first sync of a book with groups reads its
members up to twice.
"""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterator, Optional
from urllib.parse import quote, unquote, urljoin, urlparse
from xml.sax.saxutils import escape

from app.accounts.provider import (ContactPage, HttpResponse, RemoteContact, TokenExpired, TokenSource,
                                   Transport, VersionConflict, WriteResult)
from app.accounts.providers import _common
from app.contacts import vcard
from app.contacts.card import ALL_CAPS, Card
from app.core.errors import AppErrorException, make_error

__all__ = ["CardDavProvider", "AddressBook", "basic_auth_header", "ICLOUD_URL", "CARDDAV_CAPS"]

ICLOUD_URL = "https://contacts.icloud.com"
#: Everything a vCard can say. A server may still drop some of it; the engine's
#: round trip never removes it from Leasha (D10).
CARDDAV_CAPS = replace(ALL_CAPS, primary=False)   # see `Capabilities.primary`
_COMPONENT = "accounts.providers.carddav"

DAV = "DAV:"
CARD = "urn:ietf:params:xml:ns:carddav"
CS = "http://calendarserver.org/ns/"
_PUT_HEADERS = {"Content-Type": "text/vcard; charset=utf-8"}
_XML = {"Content-Type": "application/xml; charset=utf-8"}
_NS = f'xmlns:d="{DAV}" xmlns:c="{CARD}" xmlns:cs="{CS}"'
_PATH_SAFE = "/@:=+,;$&'()!*~-._"


def basic_auth_header(username: str, secret: TokenSource) -> Callable[[], str]:
    """An `auth_header` provider for HTTP Basic: iCloud and most CardDAV servers
    take an (app-specific) password this way. The secret is asked for on every
    request, so one the person changes is picked up."""
    import base64  # noqa: PLC0415

    def header() -> str:
        raw = f"{username}:{secret()}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")
    return header


@dataclass
class AddressBook:
    url: str
    name: str = ""


@dataclass
class _Resp:
    href: str
    status: int = 200                                   # the response's own status, else 200
    props: dict[str, ET.Element] = field(default_factory=dict)


def _status_code(text: Optional[str]) -> int:
    m = re.search(r"\s(\d{3})\b", text or "")
    return int(m.group(1)) if m else 200


def _multistatus(body: bytes) -> tuple[list[_Resp], str]:
    root = _common.parse_xml(body)
    out: list[_Resp] = []
    for r in root.findall(f"{{{DAV}}}response"):
        href = (r.findtext(f"{{{DAV}}}href") or "").strip()
        resp = _Resp(href=href, status=_status_code(r.findtext(f"{{{DAV}}}status")) if r.find(f"{{{DAV}}}status") is not None else 200)
        for ps in r.findall(f"{{{DAV}}}propstat"):
            if _status_code(ps.findtext(f"{{{DAV}}}status")) == 200:
                prop = ps.find(f"{{{DAV}}}prop")
                if prop is not None:
                    for el in prop:
                        resp.props[el.tag] = el
        out.append(resp)
    return out, (root.findtext(f"{{{DAV}}}sync-token") or "").strip()


def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if out and line[:1] in (" ", "\t"):
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _prop(lines: list[str], *names: str) -> list[str]:
    """Values of the properties called `names` (any parameters, any Apple `item1.` group)."""
    found = []
    for ln in lines:
        head, sep, value = ln.partition(":")
        if sep and head.split(";")[0].split(".")[-1].upper() in names:
            found.append(value)
    return found


def _text_unescape(v: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


@dataclass
class _Parsed:
    is_group: bool
    uid: str = ""
    name: str = ""
    members: list[str] = field(default_factory=list)
    rev: float = 0.0


def _inspect(text: str) -> _Parsed:
    lines = _unfold(text)
    kinds = [v.strip().lower() for v in _prop(lines, "KIND", "X-ADDRESSBOOKSERVER-KIND")]
    uid = (_prop(lines, "UID") or [""])[0].strip()
    rev = _common.parse_time((_prop(lines, "REV") or [""])[0].strip())
    if "group" not in kinds:
        return _Parsed(False, uid=uid, rev=rev)
    members = []
    for v in _prop(lines, "MEMBER", "X-ADDRESSBOOKSERVER-MEMBER"):
        v = v.strip()
        if v.lower().startswith("urn:uuid:"):
            members.append(v[9:])
    name = (_prop(lines, "FN") or [""])[0]
    if not name:
        name = " ".join(p for p in (_prop(lines, "N") or [""])[0].split(";") if p)
    return _Parsed(True, uid=uid, name=_text_unescape(name).strip(), members=members, rev=rev)


def _group_card(name: str, uid: str, members: list[str], version: str) -> str:
    """A group card in the Apple convention (3.0) or the vCard 4.0 one."""
    esc = vcard._esc                                           # the one escaping routine
    if version == "4.0":
        lines = ["BEGIN:VCARD", "VERSION:4.0", "KIND:group", f"FN:{esc(name)}", f"UID:{uid}"]
        lines += [f"MEMBER:urn:uuid:{m}" for m in members]
    else:
        lines = ["BEGIN:VCARD", "VERSION:3.0", f"N:{esc(name)};;;;", f"FN:{esc(name)}",
                 "X-ADDRESSBOOKSERVER-KIND:group", f"UID:{uid}"]
        lines += [f"X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:{m}" for m in members]
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def _sig(names: list[str]) -> str:
    return "\x1f".join(sorted(names, key=str.casefold))


class CardDavProvider:
    """One CardDAV address book."""

    caps = CARDDAV_CAPS

    def __init__(self, transport: Transport, auth_header: Callable[[], str], *, server_url: str = "",
                 addressbook_url: str = "", addressbook_name: str = "", account: str = "CardDAV",
                 vcard_version: str = "3.0", group_mode: str = "auto", page_size: int = 50) -> None:
        if not (server_url or addressbook_url):
            raise ValueError("a CardDAV account needs a server URL or an address book URL")
        if vcard_version not in ("3.0", "4.0"):
            raise ValueError("vcard_version must be 3.0 or 4.0")
        if group_mode not in ("auto", "cards", "categories"):
            raise ValueError("group_mode must be auto, cards or categories")
        self._t = transport
        self._auth = auth_header
        self._server = server_url.rstrip("/") if server_url else ""
        self._book = (addressbook_url.rstrip("/") + "/") if addressbook_url else ""
        self._book_name = addressbook_name
        self._account = account
        self._version = vcard_version
        self._group_mode = group_mode
        self._page = max(1, int(page_size))
        origin = urlparse(self._book or self._server)
        self._origin_host = (origin.hostname or "").lower()
        self._has_sync: Optional[bool] = None
        self._i: dict[str, list] = {}
        self._g: dict[str, list] = {}

    @classmethod
    def icloud(cls, transport: Transport, username: str, app_password: TokenSource, **kw: Any) -> "CardDavProvider":
        """iCloud: contacts.icloud.com, an app-specific password, vCard 3.0, Apple groups."""
        kw.setdefault("group_mode", "cards")
        kw.setdefault("account", "iCloud")
        return cls(transport, basic_auth_header(username, app_password), server_url=ICLOUD_URL, **kw)

    # -- transport ------------------------------------------------------------

    def _trusted(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if not host or not self._origin_host:
            return False
        if host == self._origin_host:
            return True
        a, b = host.split("."), self._origin_host.split(".")
        return len(a) >= 3 and len(b) >= 2 and a[-2:] == b[-2:] and not host.replace(".", "").isdigit()

    def _send(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
              body: Optional[bytes] = None) -> tuple[HttpResponse, str]:
        """One request, with redirects followed by hand. Returns the response and
        the URL it finally came from."""
        for _ in range(6):
            h = dict(headers or {})
            if self._trusted(url):
                h["Authorization"] = self._auth()
            resp = self._t.request(method, url, headers=h, body=body)
            if resp.status in (301, 302, 303, 307, 308) and resp.header("location"):
                url = urljoin(url, resp.header("location"))
                continue
            return resp, url
        raise AppErrorException(make_error(
            "ERR_ACCOUNT_REJECTED", _COMPONENT, account=self._account,
            reason="too many redirects", details=url))

    def _fail(self, resp: HttpResponse, what: str) -> Exception:
        return _common.fail(resp, self._account, _COMPONENT, what)

    def _dav(self, method: str, url: str, xml: str, depth: str, what: str,
             ok: tuple[int, ...] = (207,)) -> tuple[list[_Resp], str, str]:
        resp, final = self._send(method, url, headers={**_XML, "Depth": depth}, body=xml.encode("utf-8"))
        if resp.status not in ok:
            raise self._fail(resp, what)
        rows, token = _multistatus(resp.body)
        return rows, token, final

    # -- discovery ------------------------------------------------------------

    def list_addressbooks(self) -> list[AddressBook]:
        """The address books this account has. Used by the account panel, and by
        `pull` when no address book URL was typed in."""
        if self._book and not self._server:
            return [AddressBook(self._book, self._book_name)]
        origin = urlparse(self._server)
        root = f"{origin.scheme}://{origin.netloc}"
        starts = [root + "/.well-known/carddav"]
        if self._server != root:
            starts.insert(0, self._server)
        else:
            starts.append(self._server + "/")
        find = (f'<?xml version="1.0"?><d:propfind {_NS}><d:prop><d:current-user-principal/>'
                f'<c:addressbook-home-set/><d:resourcetype/></d:prop></d:propfind>')
        home = principal = ""
        base = ""
        for start in starts:
            resp, final = self._send("PROPFIND", start, headers={**_XML, "Depth": "0"}, body=find.encode())
            if resp.status == 401:
                raise self._fail(resp, "sign in")
            if resp.status != 207:
                continue
            rows, _ = _multistatus(resp.body)
            for r in rows:
                rt = r.props.get(f"{{{DAV}}}resourcetype")
                if rt is not None and rt.find(f"{{{CARD}}}addressbook") is not None:
                    return [AddressBook(urljoin(final, r.href or final))]
                h = r.props.get(f"{{{CARD}}}addressbook-home-set")
                if h is not None and (h.findtext(f"{{{DAV}}}href") or "").strip():
                    home = urljoin(final, h.findtext(f"{{{DAV}}}href").strip())
                p = r.props.get(f"{{{DAV}}}current-user-principal")
                if p is not None and (p.findtext(f"{{{DAV}}}href") or "").strip():
                    principal = urljoin(final, p.findtext(f"{{{DAV}}}href").strip())
            base = final
            if home or principal:
                break
        if not home and principal:
            find2 = (f'<?xml version="1.0"?><d:propfind {_NS}><d:prop><c:addressbook-home-set/></d:prop>'
                     f'</d:propfind>')
            rows, _, final = self._dav("PROPFIND", principal, find2, "0", "find the address book home")
            for r in rows:
                h = r.props.get(f"{{{CARD}}}addressbook-home-set")
                if h is not None and (h.findtext(f"{{{DAV}}}href") or "").strip():
                    home = urljoin(final, h.findtext(f"{{{DAV}}}href").strip())
        if not home:
            raise AppErrorException(make_error(
                "ERR_ACCOUNT_REJECTED", _COMPONENT, account=self._account,
                reason="the server did not say where the address books are",
                details=f"no addressbook-home-set from {base or self._server}"))
        listing = (f'<?xml version="1.0"?><d:propfind {_NS}><d:prop><d:resourcetype/><d:displayname/>'
                   f'</d:prop></d:propfind>')
        rows, _, final = self._dav("PROPFIND", home, listing, "1", "list the address books")
        books = []
        for r in rows:
            rt = r.props.get(f"{{{DAV}}}resourcetype")
            if rt is not None and rt.find(f"{{{CARD}}}addressbook") is not None:
                name = r.props.get(f"{{{DAV}}}displayname")
                books.append(AddressBook(urljoin(final, r.href), (name.text or "").strip() if name is not None else ""))
        if not books:
            raise AppErrorException(make_error(
                "ERR_ACCOUNT_REJECTED", _COMPONENT, account=self._account,
                reason="the account has no address book", details=home))
        return books

    def _ensure_book(self) -> str:
        if self._book:
            return self._book
        books = self.list_addressbooks()
        pick = None
        if self._book_name:
            want = self._book_name.casefold()
            pick = next((b for b in books if b.name.casefold() == want
                         or unquote(urlparse(b.url).path.rstrip("/").rsplit("/", 1)[-1]).casefold() == want), None)
        pick = pick or books[0]
        self._book = pick.url.rstrip("/") + "/"
        return self._book

    @property
    def addressbook_url(self) -> str:
        """The address book in use (after discovery), for the account to remember."""
        return self._book

    # -- names and URLs ---------------------------------------------------------

    def _url(self, name: str) -> str:
        return self._book + quote(name, safe=_PATH_SAFE)

    def _name(self, href: str) -> Optional[str]:
        """A response's file name inside the book, None for the book itself or
        anything that is not directly in it."""
        path = urlparse(urljoin(self._book, href)).path
        base = urlparse(self._book).path
        if not path.startswith(base) or path == base or path.endswith("/"):
            return None
        return unquote(path[len(base):])

    # -- planning a pull -----------------------------------------------------------

    def _supports_sync(self) -> bool:
        if self._has_sync is None:
            xml = f'<?xml version="1.0"?><d:propfind {_NS}><d:prop><d:supported-report-set/></d:prop></d:propfind>'
            try:
                rows, _, _ = self._dav("PROPFIND", self._book, xml, "0", "ask which reports the server offers")
            except AppErrorException:
                rows = []
            srs = rows[0].props.get(f"{{{DAV}}}supported-report-set") if rows else None
            self._has_sync = bool(srs is not None and srs.find(f".//{{{DAV}}}sync-collection") is not None)
        return self._has_sync

    def _sync_report(self, token: str) -> tuple[list[tuple[str, str]], list[str], str]:
        changed: list[tuple[str, str]] = []
        gone: list[str] = []
        current = token
        for _ in range(200):                               # a server that truncates says so with 507
            xml = (f'<?xml version="1.0"?><d:sync-collection {_NS}><d:sync-token>{escape(current)}</d:sync-token>'
                   f'<d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>')
            resp, _ = self._send("REPORT", self._book, headers={**_XML, "Depth": "0"}, body=xml.encode())
            body = resp.text()
            if resp.status in (403, 409, 412) and "valid-sync-token" in body:
                raise TokenExpired("the CardDAV server no longer accepts the saved sync-token")
            if resp.status == 400 and current:
                raise TokenExpired("the CardDAV server refused the saved sync-token")
            if resp.status != 207:
                raise self._fail(resp, "ask what changed")
            rows, new = _multistatus(resp.body)
            truncated = False
            for r in rows:
                name = self._name(r.href)
                if name is None:
                    if r.status == 507:
                        truncated = True
                    continue
                if r.status == 404:
                    gone.append(name)
                elif r.status < 400:
                    et = r.props.get(f"{{{DAV}}}getetag")
                    changed.append((name, (et.text or "").strip() if et is not None else ""))
            if not new:
                raise self._fail(HttpResponse(502, {}, b"no sync-token"), "ask what changed")
            current = new
            if not truncated:
                return changed, gone, new
        raise self._fail(HttpResponse(502, {}, b"sync did not finish"), "ask what changed")

    def _list_etags(self) -> tuple[dict[str, str], str]:
        xml = (f'<?xml version="1.0"?><d:propfind {_NS}><d:prop><d:getetag/><d:resourcetype/></d:prop>'
               f'</d:propfind>')
        rows, _, _ = self._dav("PROPFIND", self._book, xml, "1", "list the contacts")
        out = {}
        for r in rows:
            name = self._name(r.href)
            et = r.props.get(f"{{{DAV}}}getetag")
            rt = r.props.get(f"{{{DAV}}}resourcetype")
            if name is None or (rt is not None and rt.find(f"{{{DAV}}}collection") is not None):
                continue
            out[name] = (et.text or "").strip() if et is not None else ""
        return out, ""

    def _ctag(self) -> str:
        xml = f'<?xml version="1.0"?><d:propfind {_NS}><d:prop><cs:getctag/></d:prop></d:propfind>'
        try:
            rows, _, _ = self._dav("PROPFIND", self._book, xml, "0", "read the address book's tag")
        except AppErrorException:
            return ""
        el = rows[0].props.get(f"{{{CS}}}getctag") if rows else None
        return (el.text or "").strip() if el is not None else ""

    def _plan(self, state: dict[str, Any]) -> dict[str, Any]:
        mode = state.get("m") or ("sync" if self._supports_sync() else "etag")
        index = state["i"]
        if mode == "sync":
            try:
                changed, gone, new = self._sync_report(str(state.get("t") or ""))
            except AppErrorException as exc:
                # A server that lists the report but refuses it on a first pull
                # is followed by etags instead; a refused saved token is not.
                # (Some servers answer a report they do not support with HTTP 500: that is followed by etags too.)
                if state.get("t") or exc.error.code not in ("ERR_ACCOUNT_REJECTED", "ERR_ACCOUNT_TROUBLE"):
                    raise
                state["m"] = "etag"
                return self._plan(state)
            state["m"] = "sync"
            return {"f": [n for n, _ in changed], "x": gone, "nt": new}
        state["m"] = "etag"
        ctag = self._ctag()
        if ctag and ctag == state.get("c") and index:
            return {"f": [], "x": [], "nc": ctag}
        listing, _ = self._list_etags()
        fetch = [n for n, et in listing.items() if n not in index or index[n][0] != et or not et]
        return {"f": fetch, "x": [n for n in index if n not in listing], "nc": ctag}

    # -- fetching ----------------------------------------------------------------------

    def _multiget(self, names: list[str]) -> list[tuple[str, str, Optional[str]]]:
        """(name, etag, vCard text or None when the server no longer has it)."""
        hrefs = "".join(f"<d:href>{escape(urlparse(self._url(n)).path)}</d:href>" for n in names)
        xml = (f'<?xml version="1.0"?><c:addressbook-multiget {_NS}><d:prop><d:getetag/><c:address-data/>'
               f'</d:prop>{hrefs}</c:addressbook-multiget>')
        rows, _, _ = self._dav("REPORT", self._book, xml, "1", "read contacts")
        seen: dict[str, tuple[str, Optional[str]]] = {}
        for r in rows:
            name = self._name(r.href)
            if name is None:
                continue
            data = r.props.get(f"{{{CARD}}}address-data")
            et = r.props.get(f"{{{DAV}}}getetag")
            if r.status >= 400 or data is None or not (data.text or "").strip():
                seen[name] = ("", None)
            else:
                seen[name] = ((et.text or "").strip() if et is not None else "", data.text)
        return [(n, *seen.get(n, ("", None))) for n in names]

    def _groups_of(self, uid: str) -> list[str]:
        return [g[0] for g in self._g.values() if uid in g[1]]

    def _use_cards(self) -> bool:
        return self._group_mode == "cards" or (self._group_mode == "auto" and bool(self._g))

    def _card(self, text: str, uid: str) -> Card:
        card = vcard.parse_one(text)
        for g in self._groups_of(uid):
            if g not in card.groups:
                card.groups.append(g)
        return card

    # -- ContactProvider --------------------------------------------------------------

    def pull(self, token: Optional[str]) -> Iterator[ContactPage]:
        state = _common.decode_token(token)
        if state is None:
            raise TokenExpired("the saved CardDAV token is not readable")
        self._ensure_book()
        state.setdefault("i", {})
        state.setdefault("g", {})
        self._i, self._g = state["i"], state["g"]
        if "p" not in state:
            state["p"] = self._plan(state)
        plan = state["p"]
        while True:
            changes: list[RemoteContact] = []
            for name in plan["x"]:
                self._i.pop(name, None)
                self._g.pop(name, None)       # a removed group card is a membership change, not a contact
                changes.append(RemoteContact(remote_id=name, card=None))
            plan["x"] = []
            batch, plan["f"] = plan["f"][:self._page], plan["f"][self._page:]
            fetched = self._multiget(batch) if batch else []
            contacts: list[tuple[str, str, str, _Parsed]] = []
            for name, etag, text in fetched:
                if text is None:                                   # gone between the listing and the read
                    self._i.pop(name, None)
                    self._g.pop(name, None)
                    changes.append(RemoteContact(remote_id=name, card=None))
                    continue
                info = _inspect(text)
                if info.is_group:
                    self._g[name] = [info.name, info.members]
                    self._i[name] = [etag, info.uid, "g", ""]
                else:
                    contacts.append((name, etag, text, info))
            for name, etag, text, info in contacts:               # groups of this batch are known now
                uid = info.uid or name
                card = self._card(text, uid)
                self._i[name] = [etag, uid, "c", _sig(self._groups_of(uid))]
                changes.append(RemoteContact(remote_id=name, version=etag, card=card, edited_at=info.rev))
            if not plan["f"]:
                # Contacts delivered earlier may belong to a group card that came later.
                stale = [n for n, e in self._i.items()
                         if e[2] == "c" and e[3] != _sig(self._groups_of(e[1]))]
                plan["f"] = stale
            if plan["f"]:
                yield ContactPage(changes, _common.encode_token(state))
                continue
            if state["m"] == "sync":
                state["t"] = plan["nt"]
            else:
                state["c"] = plan.get("nc", "")
            state.pop("p", None)
            yield ContactPage(changes, _common.encode_token(state), done=True)
            return

    def _head(self, name: str) -> Optional[tuple[str, str]]:
        """(etag, text) of one card, None if it is not there."""
        resp, _ = self._send("GET", self._url(name))
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise self._fail(resp, "read a contact")
        return resp.header("etag") or self._etag_of(name), resp.text()

    def _etag_of(self, name: str) -> str:
        xml = f'<?xml version="1.0"?><d:propfind {_NS}><d:prop><d:getetag/></d:prop></d:propfind>'
        rows, _, _ = self._dav("PROPFIND", self._url(name), xml, "0", "read a contact's tag")
        el = rows[0].props.get(f"{{{DAV}}}getetag") if rows else None
        return (el.text or "").strip() if el is not None else ""

    def get(self, remote_id: str) -> Optional[RemoteContact]:
        self._ensure_book()
        got = self._head(remote_id)
        if got is None:
            return None
        etag, text = got
        info = _inspect(text)
        if info.is_group:
            return None
        uid = info.uid or remote_id
        return RemoteContact(remote_id=remote_id, version=etag, card=self._card(text, uid), edited_at=info.rev)

    def _body_for(self, card: Card, uid: str) -> bytes:
        out = card.copy()
        if self._use_cards():
            out.groups = []                  # membership lives in the group cards, not in CATEGORIES
        return vcard.dumps(out, version=self._version, uid=uid).encode("utf-8")

    def _put(self, name: str, body: bytes, extra: dict[str, str]) -> str:
        resp, _ = self._send("PUT", self._url(name), headers={**_PUT_HEADERS, **extra}, body=body)
        if resp.status == 412 or (resp.status == 404 and "If-Match" in extra):
            raise VersionConflict(name)
        if resp.status not in (200, 201, 204):
            raise self._fail(resp, "save a contact")
        return resp.header("etag") or self._etag_of(name)

    def create(self, card: Card, *, membership: bool = True) -> tuple[str, str]:
        """`membership=False`: the group cards are left to the caller (`create_many` writes them once a batch)."""
        self._ensure_book()
        for _ in range(3):
            uid = str(uuid.uuid4()).upper()
            name = f"{uid}.vcf"
            try:
                etag = self._put(name, self._body_for(card, uid), {"If-None-Match": "*"})
            except VersionConflict:
                continue                                         # the name was taken: another uid
            if self._use_cards() and membership:
                self._set_membership(uid, card.groups)
            self._i[name] = [etag, uid, "c", _sig(self._groups_of(uid))]
            return name, etag
        raise self._fail(HttpResponse(412, {}, b"name taken"), "create a contact")

    def _uid_of(self, name: str) -> str:
        known = self._i.get(name)
        if known and known[1]:
            return known[1]
        got = self._head(name)
        if got is None:
            raise VersionConflict(name)
        return _inspect(got[1]).uid or name

    def update(self, remote_id: str, version: str, card: Card, *, membership: bool = True) -> str:
        self._ensure_book()
        uid = self._uid_of(remote_id)
        extra = {"If-Match": version} if version else {}
        etag = self._put(remote_id, self._body_for(card, uid), extra)
        if self._use_cards() and membership:
            self._set_membership(uid, card.groups)
        self._i[remote_id] = [etag, uid, "c", _sig(self._groups_of(uid))]
        return etag

    def delete(self, remote_id: str, version: str) -> None:
        self._ensure_book()
        uid = (self._i.get(remote_id) or ["", ""])[1]
        resp, _ = self._send("DELETE", self._url(remote_id), headers={"If-Match": version} if version else {})
        if resp.status == 412:
            raise VersionConflict(remote_id)
        if resp.status not in (200, 204, 404):
            raise self._fail(resp, "delete a contact")
        self._i.pop(remote_id, None)
        if uid and self._use_cards():
            try:
                self._set_membership(uid, [])
            except VersionConflict:
                # The contact is already gone. A group card that still lists it
                # is harmless (every client ignores an unknown member), and the
                # next pull sees the group card as it is.
                pass

    # -- batches (order 1t 5e) ---------------------------------------------------------------
    #: CardDAV has no batch write, so a "batch" is its contacts written one after another on the kept
    #: connection, then their group cards written once each (not once per contact), and the read-back done as
    #: one addressbook-multiget.
    batch_size = 50
    one_by_one_cost = {"create": 2, "update": 2, "delete": 1}
    batch_cost = {"create": 51, "update": 51, "delete": 50}

    def _many(self, items: list, write: Callable[[Any], tuple[str, str, str]]) -> list[WriteResult]:
        out: list[WriteResult] = []
        wanted: dict[str, list[str]] = {}
        for item in items:
            try:
                remote_id, version, uid = write(item)
            except VersionConflict:
                out.append(WriteResult(conflict=True))
                continue
            except AppErrorException as exc:
                if exc.error.code in _common.ACCOUNT_WIDE:
                    raise
                out.append(WriteResult(error=exc.error))
                continue
            card = item if isinstance(item, Card) else item[2]
            wanted[uid] = list(card.groups)
            out.append(WriteResult(ok=True, remote_id=remote_id, version=version))
        if wanted and self._use_cards():
            # Membership is secondary to the contact: a group card that fails is logged and fixed by a later sync
            # (the `_sig` mismatch), and the contacts already written are still reported as written - losing those
            # results would leave creates waiting and write them again (CodeRabbit on #70).
            try:
                self._set_memberships(wanted)
            except (VersionConflict, AppErrorException) as exc:
                from app.core.logging import logger  # noqa: PLC0415
                logger.bind(component=_COMPONENT).warning(
                    "{}: group cards not updated for {} contact(s): {}", self._account, len(wanted),
                    getattr(getattr(exc, "error", None), "code", type(exc).__name__))
        return out

    def create_many(self, cards: list[Card]) -> list[WriteResult]:
        def write(card: Card) -> tuple[str, str, str]:
            name, etag = self.create(card, membership=False)
            return name, etag, self._i[name][1]
        return self._many(cards, write)

    def update_many(self, items: list[tuple[str, str, Card]]) -> list[WriteResult]:
        def write(item: tuple[str, str, Card]) -> tuple[str, str, str]:
            remote_id, version, card = item
            etag = self.update(remote_id, version, card, membership=False)
            return remote_id, etag, self._i[remote_id][1]
        return self._many(items, write)

    def delete_many(self, items: list[tuple[str, str]]) -> list[WriteResult]:
        out: list[WriteResult] = []
        for remote_id, version in items:
            try:
                self.delete(remote_id, version)
                out.append(WriteResult(ok=True, remote_id=remote_id))
            except VersionConflict:
                out.append(WriteResult(conflict=True))
            except AppErrorException as exc:
                if exc.error.code in _common.ACCOUNT_WIDE:
                    raise
                out.append(WriteResult(error=exc.error))
        return out

    def get_many(self, remote_ids: list[str]) -> dict[str, Optional[RemoteContact]]:
        """Several contacts in one addressbook-multiget a page: what the account holds after a batch."""
        self._ensure_book()
        found: dict[str, Optional[RemoteContact]] = {}
        for start in range(0, len(remote_ids), self._page):
            for name, etag, text in self._multiget(remote_ids[start:start + self._page]):
                if text is None:
                    found[name] = None
                    continue
                info = _inspect(text)
                if info.is_group:
                    found[name] = None
                    continue
                uid = info.uid or name
                found[name] = RemoteContact(remote_id=name, version=etag, card=self._card(text, uid),
                                            edited_at=info.rev)
        return found

    def list_ids(self) -> dict[str, tuple[str, str]]:
        """Every contact card in the address book with its etag (the check after a sync, 1t 3c). Group cards
        are not contacts; the ones this connector knows (from the pull before it) are left out."""
        self._ensure_book()
        listing, _ = self._list_etags()
        groups = {n for n, e in self._i.items() if e and len(e) > 2 and e[2] == "g"} | set(self._g)
        return {n: (et, "") for n, et in listing.items() if n not in groups}

    def _set_memberships(self, wanted_by_uid: dict[str, list[str]]) -> None:
        """Every contact of a batch into exactly the groups it names: each group card that changes is read and
        written once, with the change applied to what it holds now (so a member added elsewhere stays)."""
        want = {uid: {n.casefold(): n for n in names if n.strip()} for uid, names in wanted_by_uid.items()}
        assigned: set[tuple[str, str]] = set()
        for name, (gname, members) in list(self._g.items()):
            key = gname.casefold()
            add: set[str] = set()
            remove: set[str] = set()
            for uid, keys in want.items():
                if key in keys:
                    if (uid, key) in assigned:
                        continue                               # a second group of the same name: the first has it
                    assigned.add((uid, key))
                    if uid not in members:
                        add.add(uid)
                elif uid in members:
                    remove.add(uid)
            if add or remove:
                self._apply_group_delta(name, add, remove)
        new_groups: dict[str, tuple[str, list[str]]] = {}
        for uid, keys in want.items():
            for key, gname in keys.items():
                if (uid, key) not in assigned:
                    new_groups.setdefault(key, (gname, []))[1].append(uid)
        for gname, uids in new_groups.values():
            guid = str(uuid.uuid4()).upper()
            name = f"{guid}.vcf"
            etag = self._put(name, _group_card(gname, guid, uids, self._version).encode("utf-8"),
                             {"If-None-Match": "*"})
            self._g[name] = [gname, list(uids)]
            self._i[name] = [etag, guid, "g", ""]

    def _apply_group_delta(self, name: str, add: set[str], remove: set[str]) -> None:
        for _ in range(2):                                     # one retry if the group card moved
            got = self._head(name)
            if got is None:
                self._g.pop(name, None)
                return
            etag, text = got
            info = _inspect(text)
            members = [m for m in info.members if m not in remove] + sorted(a for a in add if a not in info.members)
            body = _group_card(info.name, info.uid or name.rsplit(".", 1)[0], members, self._version).encode("utf-8")
            try:
                new = self._put(name, body, {"If-Match": etag} if etag else {})
            except VersionConflict:
                continue
            self._g[name] = [info.name, members]
            self._i[name] = [new, info.uid, "g", ""]
            return
        raise VersionConflict(name)

    def close(self) -> None:
        self._i, self._g = {}, {}
        _common.close_transport(self._t)

    # -- group cards ------------------------------------------------------------------------

    def _edit_group(self, name: str, uid: str, add: bool) -> None:
        for _ in range(2):                                     # one retry if the group card moved
            got = self._head(name)
            if got is None:
                self._g.pop(name, None)
                return
            etag, text = got
            info = _inspect(text)
            members = [m for m in info.members if m != uid]
            if add:
                members.append(uid)
            body = _group_card(info.name, info.uid or name.rsplit(".", 1)[0], members, self._version).encode("utf-8")
            try:
                new = self._put(name, body, {"If-Match": etag} if etag else {})
            except VersionConflict:
                continue
            self._g[name] = [info.name, members]
            self._i[name] = [new, info.uid, "g", ""]
            return
        raise VersionConflict(name)

    def _set_membership(self, uid: str, wanted_names: list[str]) -> None:
        wanted = {n.casefold(): n for n in wanted_names if n.strip()}
        satisfied: set[str] = set()
        for name, (gname, members) in list(self._g.items()):
            key, has = gname.casefold(), uid in members
            if key in wanted:
                if has:
                    satisfied.add(key)
                elif key not in satisfied:
                    self._edit_group(name, uid, True)
                    satisfied.add(key)
            elif has:
                self._edit_group(name, uid, False)
        for key, gname in wanted.items():
            if key in satisfied:
                continue
            guid = str(uuid.uuid4()).upper()
            name = f"{guid}.vcf"
            body = _group_card(gname, guid, [uid], self._version).encode("utf-8")
            etag = self._put(name, body, {"If-None-Match": "*"})
            self._g[name] = [gname, [uid]]
            self._i[name] = [etag, guid, "g", ""]
