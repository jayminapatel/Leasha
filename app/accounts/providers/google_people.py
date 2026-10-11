"""Google contacts over the People API v1 (people.googleapis.com), read and write.

Layer: L7

Order 1k 4a. `GooglePeopleProvider` implements `ContactProvider` against
`people.connections.list` (with `requestSyncToken` / `syncToken`),
`people:createContact`, `people/{id}:updateContact`, `people/{id}:deleteContact`
and `contactGroups`. It is handed a `Transport` and a `TokenSource` (an OAuth
access token, refreshed per call) and never opens a socket.

**Token format.** A token is a JSON string, so the engine can store it as text
and a crash costs one page:

* `{"v":1,"s":"<syncToken>"}` - between syncs: the sync token Google gave on
  the last page of the last pull. Absent or empty means "everything".
* `{"v":1,"s":"<syncToken or ''>","p":"<pageToken>"}` - in the middle of a
  pull: the next page to ask for. Google insists a page token is used with the
  same parameters as the call that produced it, so the sync token the pull
  started with travels beside it.

Text that is not that JSON raises `TokenExpired` (a full pull is the safe
answer to a token nobody can read). So does Google's own expiry: HTTP 410, or
HTTP 400 with `EXPIRED_SYNC_TOKEN`, when a sync token was in the request.

**Groups.** Google labels are contact groups. `Card.groups` carries their
names; the group list is read once per pull and per write session, a name that
does not exist yet is created with `contactGroups.create` before the contact
is written. `myContacts` (every contact is in it) and `starred` are not
groups here: `starred` is `Card.favourite`. A rename of a label alone does not
make Google return the people in it, so a renamed label shows on a contact
the next time that contact changes. Google's system labels (Friends, Family,
Coworkers) are treated as ordinary groups under their displayed names.

**Photos.** A People API photo is a URL. Fetching it would be a second kind of
outbound request (and a download per contact on a first sync of thousands), so
it is never followed and `Card.photo` stays None; `caps.photo` is False because
this connector does not upload one either.

**What Google holds (D10).** Names with prefix, suffix, middle name and
nickname; one organisation (company, department, title); notes; email, phone,
address, url, date (the birthday and labelled events), related, im and custom
(`userDefined`) values; groups; starred. It has no social-profile field, so a
`social` value is not sent and nothing complains; the hub keeps it.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlencode

from app.accounts.provider import (ContactPage, HttpResponse, RemoteContact, TokenExpired,
                                   TokenSource, Transport, VersionConflict)
from app.accounts.providers import _common
from app.contacts.card import Capabilities, Card, Field, project

__all__ = ["GooglePeopleProvider", "GOOGLE_CAPS", "API_BASE"]

API_BASE = "https://people.googleapis.com/v1"
_COMPONENT = "accounts.providers.google_people"

#: Read mask. `photos` is deliberately absent: see the module note on photos.
_READ_FIELDS = ("names,nicknames,organizations,biographies,emailAddresses,phoneNumbers,"
                "addresses,urls,birthdays,events,relations,userDefined,imClients,memberships,metadata")

#: The fields an update replaces. A field named here and absent from the body is
#: cleared by Google, which is how a value removed in Leasha is removed there.
_WRITE_FIELDS = ("names,nicknames,organizations,biographies,emailAddresses,phoneNumbers,"
                 "addresses,urls,birthdays,events,relations,userDefined,imClients,memberships")

GOOGLE_CAPS = Capabilities(
    kinds=frozenset({"email", "phone", "address", "url", "date", "related", "im", "custom"}),
    groups=True, favourite=True, photo=False, date_labels=None)

_MY_CONTACTS = "contactGroups/myContacts"
_STARRED = "contactGroups/starred"
#: System groups that are not user-visible labels.
_HIDDEN_GROUPS = {_MY_CONTACTS, _STARRED, "contactGroups/all", "contactGroups/chatBuddies"}


def _fmt_date(d: dict[str, Any]) -> str:
    month, day, year = d.get("month"), d.get("day"), d.get("year")
    if not month or not day:
        return ""
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}" if year else f"--{int(month):02d}-{int(day):02d}"


def _parse_date(text: str) -> Optional[dict[str, int]]:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text.strip())
    if m:
        return {"year": int(m.group(1)), "month": int(m.group(2)), "day": int(m.group(3))}
    m = re.fullmatch(r"--(\d{2})-(\d{2})", text.strip())
    if m:
        return {"month": int(m.group(1)), "day": int(m.group(2))}
    return None                    # a date Google cannot hold in its three numbers is not sent


def _label(entry: dict[str, Any]) -> str:
    return str(entry.get("type") or "").strip()


def _typed(label: str) -> dict[str, str]:
    """Google's `type`; the vCard word for a mobile phone is `cell`."""
    label = label.strip()
    if label.lower() == "cell":
        label = "mobile"
    return {"type": label} if label else {}


class GooglePeopleProvider:
    """One Google account's contacts."""

    caps = GOOGLE_CAPS

    def __init__(self, transport: Transport, token_source: TokenSource, *, account: str = "Google",
                 base_url: str = API_BASE, page_size: int = 200) -> None:
        self._t = transport
        self._token = token_source
        self._account = account
        self._base = base_url.rstrip("/")
        self._page_size = max(1, min(int(page_size), 1000))
        self._by_resource: Optional[dict[str, str]] = None     # contactGroups/x -> display name
        self._by_name: dict[str, str] = {}                      # casefolded name -> contactGroups/x

    # -- plumbing -----------------------------------------------------------

    def _call(self, method: str, path: str, *, params: Optional[dict[str, Any]] = None,
              body: Optional[dict[str, Any]] = None) -> HttpResponse:
        url = f"{self._base}/{path.lstrip('/')}"
        if params:
            url += "?" + urlencode(params, doseq=True, safe=",")
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        return self._t.request(method, url, headers=headers, body=data)

    def _fail(self, resp: HttpResponse, what: str) -> Exception:
        return _common.fail(resp, self._account, _COMPONENT, what)

    @staticmethod
    def _is_etag_conflict(resp: HttpResponse) -> bool:
        """Google reports a stale etag as 400 FAILED_PRECONDITION (documented
        behaviour), and 409 / 412 on some paths; a plain 400 is not a conflict."""
        if resp.status in (409, 412):
            return True
        if resp.status == 400:
            blob = _common.error_text(resp).lower()
            return "failed_precondition" in blob or "etag" in blob
        return False

    # -- groups -------------------------------------------------------------

    def _load_groups(self, force: bool = False) -> None:
        if self._by_resource is not None and not force:
            return
        names: dict[str, str] = {}
        page = ""
        while True:
            params: dict[str, Any] = {"pageSize": 200}
            if page:
                params["pageToken"] = page
            resp = self._call("GET", "contactGroups", params=params)
            if resp.status != 200:
                raise self._fail(resp, "list contact groups")
            data = resp.json() or {}
            for g in data.get("contactGroups") or []:
                res = g.get("resourceName", "")
                if res and res not in _HIDDEN_GROUPS:
                    names[res] = g.get("formattedName") or g.get("name") or res
            page = data.get("nextPageToken") or ""
            if not page:
                break
        self._by_resource = names
        self._by_name = {n.casefold(): r for r, n in names.items()}

    def _group_resource(self, name: str) -> str:
        """The resource name of the group called `name`, created if it is new."""
        self._load_groups()
        found = self._by_name.get(name.casefold())
        if found:
            return found
        resp = self._call("POST", "contactGroups", body={"contactGroup": {"name": name}})
        if resp.status not in (200, 201):
            raise self._fail(resp, "create a contact group")
        res = (resp.json() or {}).get("resourceName", "")
        assert self._by_resource is not None
        self._by_resource[res] = name
        self._by_name[name.casefold()] = res
        return res

    # -- Google person <-> Card ---------------------------------------------

    def _to_card(self, person: dict[str, Any]) -> Card:
        card = Card()
        s = card.scalars

        def first(key: str) -> dict[str, Any]:
            items = person.get(key) or []
            for it in items:
                if (it.get("metadata") or {}).get("primary"):
                    return it
            return items[0] if items else {}

        n = first("names")
        for ours, theirs in (("given", "givenName"), ("family", "familyName"), ("additional", "middleName"),
                             ("prefix", "honorificPrefix"), ("suffix", "honorificSuffix")):
            if n.get(theirs):
                s[ours] = n[theirs]
        if first("nicknames").get("value"):
            s["nickname"] = first("nicknames")["value"]
        o = first("organizations")
        for ours, theirs in (("company", "name"), ("department", "department"), ("title", "title")):
            if o.get(theirs):
                s[ours] = o[theirs]
        if first("biographies").get("value"):
            s["notes"] = first("biographies")["value"]

        def primary(e: dict[str, Any]) -> bool:
            return bool((e.get("metadata") or {}).get("primary"))

        for e in person.get("emailAddresses") or []:
            if e.get("value"):
                card.fields.append(Field("email", e["value"], _label(e), primary=primary(e)))
        for e in person.get("phoneNumbers") or []:
            if e.get("value"):
                card.fields.append(Field("phone", e["value"], _label(e), primary=primary(e)))
        for e in person.get("addresses") or []:
            data = {k: e[g] for k, g in (("street", "streetAddress"), ("city", "city"), ("region", "region"),
                                         ("postcode", "postalCode"), ("country", "country")) if e.get(g)}
            if data or e.get("formattedValue"):
                text = ", ".join(data[k] for k in ("street", "city", "region", "postcode", "country") if k in data)
                card.fields.append(Field("address", text or e.get("formattedValue", ""), _label(e),
                                         data=data, primary=primary(e)))
        for e in person.get("urls") or []:
            if e.get("value"):
                card.fields.append(Field("url", e["value"], _label(e)))
        for e in person.get("birthdays") or []:
            text = _fmt_date(e.get("date") or {})
            if text:
                card.fields.append(Field("date", text, "birthday"))
        for e in person.get("events") or []:
            text = _fmt_date(e.get("date") or {})
            if text:
                card.fields.append(Field("date", text, _label(e) or "event"))
        for e in person.get("relations") or []:
            if e.get("person"):
                card.fields.append(Field("related", e["person"], _label(e)))
        for e in person.get("imClients") or []:
            if e.get("username"):
                card.fields.append(Field("im", e["username"], str(e.get("protocol") or e.get("type") or "")))
        for e in person.get("userDefined") or []:
            if e.get("value"):
                card.fields.append(Field("custom", e["value"], str(e.get("key") or "")))

        self._load_groups()
        assert self._by_resource is not None
        for m in person.get("memberships") or []:
            res = (m.get("contactGroupMembership") or {}).get("contactGroupResourceName", "")
            if res == _STARRED:
                card.favourite = True
            elif res in self._by_resource:
                if self._by_resource[res] not in card.groups:
                    card.groups.append(self._by_resource[res])
            # a group the list does not know (deleted between calls) is skipped
        return card

    def _to_person(self, card: Card) -> dict[str, Any]:
        """The body for a write: only what `caps` holds, so there is nothing to
        reject and no error for what is left out (D10)."""
        c = project(card, self.caps)
        s = c.get
        person: dict[str, Any] = {}
        name = {k: s(v) for k, v in (("givenName", "given"), ("familyName", "family"),
                                    ("middleName", "additional"), ("honorificPrefix", "prefix"),
                                    ("honorificSuffix", "suffix")) if s(v)}
        if name:
            person["names"] = [name]
        if s("nickname"):
            person["nicknames"] = [{"value": s("nickname")}]
        org = {k: s(v) for k, v in (("name", "company"), ("department", "department"), ("title", "title")) if s(v)}
        if org:
            person["organizations"] = [org]
        if s("notes"):
            person["biographies"] = [{"value": s("notes"), "contentType": "TEXT_PLAIN"}]

        def add(key: str, entry: dict[str, Any]) -> None:
            person.setdefault(key, []).append(entry)

        birthday_done = False
        for f in c.fields:
            if f.kind == "email":
                add("emailAddresses", {"value": f.value, **_typed(f.label)})
            elif f.kind == "phone":
                add("phoneNumbers", {"value": f.value, **_typed(f.label)})
            elif f.kind == "address":
                d = f.data or {}
                entry = ({"streetAddress": d.get("street", ""), "city": d.get("city", ""),
                          "region": d.get("region", ""), "postalCode": d.get("postcode", ""),
                          "country": d.get("country", "")} if d else {"streetAddress": f.value})
                add("addresses", {**{k: v for k, v in entry.items() if v}, **_typed(f.label)})
            elif f.kind == "url":
                add("urls", {"value": f.value, **_typed(f.label)})
            elif f.kind == "date":
                date = _parse_date(f.value)
                if date is None:
                    continue
                if f.label.strip().lower() == "birthday" and not birthday_done:
                    person["birthdays"] = [{"date": date}]
                    birthday_done = True
                else:
                    add("events", {"date": date, **_typed(f.label or "other")})
            elif f.kind == "related":
                add("relations", {"person": f.value, **_typed(f.label)})
            elif f.kind == "im":
                add("imClients", {"username": f.value, **({"protocol": f.label} if f.label else {})})
            elif f.kind == "custom":
                add("userDefined", {"key": f.label or "Custom", "value": f.value})

        members = [_MY_CONTACTS]
        if c.favourite:
            members.append(_STARRED)
        for g in c.groups:
            res = self._group_resource(g)
            if res not in members:
                members.append(res)
        person["memberships"] = [{"contactGroupMembership": {"contactGroupResourceName": r}} for r in members]
        return person

    def _remote(self, person: dict[str, Any]) -> RemoteContact:
        rid = person.get("resourceName", "")
        meta = person.get("metadata") or {}
        if meta.get("deleted"):
            return RemoteContact(remote_id=rid, version=person.get("etag", ""), card=None)
        edited = 0.0
        for src in meta.get("sources") or []:
            edited = max(edited, _common.parse_time(src.get("updateTime")))
        return RemoteContact(remote_id=rid, version=person.get("etag", ""), card=self._to_card(person),
                             edited_at=edited)

    # -- ContactProvider ----------------------------------------------------

    def pull(self, token: Optional[str]) -> Iterator[ContactPage]:
        state = _common.decode_token(token)
        if state is None:
            raise TokenExpired("the saved Google token is not readable")
        sync = str(state.get("s") or "")
        page = str(state.get("p") or "")
        self._load_groups(force=True)
        while True:
            params: dict[str, Any] = {"personFields": _READ_FIELDS, "pageSize": self._page_size,
                                      "requestSyncToken": "true", "sources": "READ_SOURCE_TYPE_CONTACT"}
            if sync:
                params["syncToken"] = sync
            if page:
                params["pageToken"] = page
            resp = self._call("GET", "people/me/connections", params=params)
            if sync and (resp.status == 410 or (resp.status == 400 and (
                    "expired_sync_token" in resp.text().lower() or "sync token" in resp.text().lower()))):
                raise TokenExpired("Google no longer accepts the saved sync token")
            if resp.status != 200:
                raise self._fail(resp, "list contacts")
            data = resp.json() or {}
            changes = [self._remote(p) for p in data.get("connections") or [] if p.get("resourceName")]
            nxt = data.get("nextPageToken") or ""
            if nxt:
                yield ContactPage(changes, _common.encode_token({"v": 1, "s": sync, "p": nxt}))
                page = nxt
                continue
            new = data.get("nextSyncToken") or ""
            yield ContactPage(changes, _common.encode_token({"v": 1, "s": new}) if new else None, done=True)
            return

    def get(self, remote_id: str) -> Optional[RemoteContact]:
        resp = self._call("GET", quote(remote_id, safe="/"), params={"personFields": _READ_FIELDS})
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise self._fail(resp, "read a contact")
        person = resp.json() or {}
        person.setdefault("resourceName", remote_id)
        return self._remote(person)

    def create(self, card: Card) -> tuple[str, str]:
        resp = self._call("POST", "people:createContact", params={"personFields": _READ_FIELDS},
                          body=self._to_person(card))
        if resp.status not in (200, 201):
            raise self._fail(resp, "create a contact")
        person = resp.json() or {}
        return person.get("resourceName", ""), person.get("etag", "")

    def update(self, remote_id: str, version: str, card: Card) -> str:
        body = self._to_person(card)
        body["etag"] = version
        resp = self._call("PATCH", quote(remote_id, safe="/") + ":updateContact",
                          params={"updatePersonFields": _WRITE_FIELDS, "personFields": _READ_FIELDS},
                          body=body)
        if self._is_etag_conflict(resp):
            raise VersionConflict(remote_id)
        if resp.status == 404:
            raise VersionConflict(remote_id)          # gone at Google: the engine re-reads and sees that
        if resp.status != 200:
            raise self._fail(resp, "update a contact")
        return (resp.json() or {}).get("etag", "")

    def delete(self, remote_id: str, version: str) -> None:
        # deleteContact takes no etag, so a delete would otherwise be blind. One
        # cheap read first keeps rule 3c: never overwrite what changed meanwhile.
        if version:
            cur = self._call("GET", quote(remote_id, safe="/"), params={"personFields": "metadata"})
            if cur.status == 404:
                return
            if cur.status != 200:
                raise self._fail(cur, "read a contact before deleting it")
            if (cur.json() or {}).get("etag", "") != version:
                raise VersionConflict(remote_id)
        resp = self._call("DELETE", quote(remote_id, safe="/") + ":deleteContact")
        if resp.status not in (200, 204, 404):
            raise self._fail(resp, "delete a contact")

    def close(self) -> None:
        self._by_resource = None
