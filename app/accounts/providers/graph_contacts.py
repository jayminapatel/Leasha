"""Microsoft contacts over Graph v1.0 (graph.microsoft.com), read and write.

Layer: L7

Order 1k 4b. `GraphContactsProvider` implements `ContactProvider` against
`/me/contacts` and `/me/contactFolders/{id}/contacts` with delta queries. It
works for a personal Microsoft account and for a work or school one: Graph
serves both from the same URLs. Every request carries
`Prefer: IdType="ImmutableId"`, so an id survives a move between folders.

**Token format.** JSON in a string. Graph has one delta link *per folder*, so the
token is a small map:

* `{"v":1,"d":{"": "<deltaLink>", "<folderId>": "<deltaLink>"}}` - between
  syncs. The key `""` is the default Contacts folder (`/me/contacts/delta`).
* while a pull is in progress: the same, plus `"done":[keys finished in this
  pull]`, `"k":"<key in flight>"` and `"n":"<its @odata.nextLink>"`. A resumed
  pull skips the keys in `done`, continues `k` from `n`, and starts the rest
  from their delta link (or from the beginning if they have none).

A delta link Graph answers with 410 Gone raises `TokenExpired`. Text that is not
this JSON does too.

**Folders and groups (my choice, since a Graph contact has both).** A contact lives
in exactly one folder and may carry many categories. `Card.groups` is the
contact's categories, plus the name of its folder when the folder is not the
default one. Writing goes the other way: every group name becomes a category
(Outlook creates the category on first use), except a name equal to the
contact's own folder, which is already said by where it is. Creating a contact
whose group names a folder that exists puts it in that folder. A contact is never
moved between folders by an update. Every contact folder is walked, child folders
included (order 1t 3d); a folder deleted at Microsoft takes its contacts with it
without delta saying so, which the engine finds on the next full pull.

**A contact moved from one folder to another** shows as a removal in the first
folder's delta and a change in the second's. A removal whose `reason` is
`changed` (it left that folder's scope) is not a delete, and a removal of an id
that was changed earlier in the same pull is ignored.

**What Graph holds (D10), and what is dropped.** Every name part (prefix is
Graph's `title`, suffix is `generation`), nickname, company, department, job
title, notes; up to three email addresses (Outlook's own limit; more are not
sent) without labels; phones as mobile, home or business (a label that is
neither goes to business, and reads back as `work`); one home, one business and
one other address; one web page (`businessHomePage`); the birthday and no other
date; a spouse, manager and assistant as `related` and nothing else; IM
addresses. Dropped without an error: social profiles, custom values, other
dates, other related people, the star, the photo (a photo is a separate
download and upload; `caps.photo` is False). A year-less birthday is stored by
Outlook as year 1604 or 1; it reads back as `--MM-DD`.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlencode

from app.accounts.provider import (ContactPage, HttpResponse, RemoteContact, TokenExpired,
                                   TokenSource, Transport, VersionConflict, WriteResult)
from app.accounts.providers import _common
from app.contacts.card import Capabilities, Card, Field, project
from app.core.errors import AppErrorException, make_error

__all__ = ["GraphContactsProvider", "GRAPH_CAPS", "API_BASE", "MAX_EMAILS"]

API_BASE = "https://graph.microsoft.com/v1.0"
_COMPONENT = "accounts.providers.graph_contacts"

#: Outlook keeps three email slots per contact.
MAX_EMAILS = 3

#: What Outlook has room for (found against the owner's account, and Microsoft's own limits): three emails;
#: one mobile, two home and two business numbers; a home, a business and an other address; one web page; one
#: birthday; a spouse, a manager and an assistant. What does not fit stays in Leasha.
GRAPH_CAPS = Capabilities(
    kinds=frozenset({"email", "phone", "address", "url", "date", "related", "im"}),
    groups=True, favourite=False, photo=False, date_labels=frozenset({"birthday"}),
    max_per_kind=(("email", 3), ("phone", 5), ("address", 3), ("url", 1), ("date", 1), ("related", 3)),
    kind_labels=(("related", frozenset({"spouse", "manager", "assistant"})),), primary=False)

#: Graph answers HTTP 500 (ErrorInternalServerError) to a PATCH that sets an address to null on a contact that has
#: no such address - found 2026-10-12 against the owner's account, one property at a time. So a null address is
#: sent only when the contact has one to clear.
_ADDRESS_SLOTS = ("homeAddress", "businessAddress", "otherAddress")
#: Outlook holds at most two home and two business phone numbers (and one mobile). A third is answered with
#: HTTP 400 "exceeds the max allowed value of 2" - found 2026-10-12, with four contacts that each had three or
#: four work numbers. What does not fit stays in Leasha and in the other accounts.
_MAX_PER_PHONE_SLOT = 2
_RELATED = {"spouse": "spouseName", "manager": "manager", "assistant": "assistantName"}
_YEARLESS = (1, 1604)                   # the years Outlook uses for "no year"


def _phys(a: dict[str, Any]) -> dict[str, str]:
    return {k: str(a[g]) for k, g in (("street", "street"), ("city", "city"), ("region", "state"),
                                      ("postcode", "postalCode"), ("country", "countryOrRegion")) if a.get(g)}


def _to_phys(d: dict[str, str], text: str) -> dict[str, str]:
    if not d:
        return {"street": text} if text else {}
    return {g: d[k] for k, g in (("street", "street"), ("city", "city"), ("region", "state"),
                                 ("postcode", "postalCode"), ("country", "countryOrRegion")) if d.get(k)}


def _read_birthday(text: Any) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(text or ""))
    if not m:
        return ""
    year = int(m.group(1))
    return f"--{m.group(2)}-{m.group(3)}" if year in _YEARLESS else f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _write_birthday(text: str) -> Optional[str]:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text.strip())
    if m:
        return f"{text.strip()}T00:00:00Z"
    m = re.fullmatch(r"--(\d{2})-(\d{2})", text.strip())
    return f"1604-{m.group(1)}-{m.group(2)}T00:00:00Z" if m else None


class GraphContactsProvider:
    """One Microsoft account's contacts."""

    caps = GRAPH_CAPS

    def __init__(self, transport: Transport, token_source: TokenSource, *, account: str = "Microsoft",
                 base_url: str = API_BASE, page_size: int = 100, send_if_match: bool = False) -> None:
        """`send_if_match` also sends `If-Match: W/"<changeKey>"` on writes. Off by
        default: the pre-read compare below is what guards a write, and whether
        Graph's contact ETag is exactly that form has not been seen live."""
        self._t = transport
        self._token = token_source
        self._account = account
        self._base = base_url.rstrip("/")
        self._page_size = max(1, int(page_size))
        self._send_if_match = send_if_match
        self._folders: Optional[dict[str, str]] = None        # id -> display name
        self._default_id: Optional[str] = None

    # -- plumbing -----------------------------------------------------------

    def _call(self, method: str, url: str, *, body: Optional[dict[str, Any]] = None,
              extra: Optional[dict[str, str]] = None, paged: bool = False) -> HttpResponse:
        if not url.startswith("http"):
            url = f"{self._base}/{url.lstrip('/')}"
        prefer = 'IdType="ImmutableId"' + (f", odata.maxpagesize={self._page_size}" if paged else "")
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json", "Prefer": prefer}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers.update(extra or {})
        return self._t.request(method, url, headers=headers, body=data)

    def _fail(self, resp: HttpResponse, what: str) -> Exception:
        return _common.fail(resp, self._account, _COMPONENT, what)

    # -- folders ------------------------------------------------------------

    def _load_folders(self, force: bool = False) -> dict[str, str]:
        """Every contact folder, child folders included (order 1t 3d: Outlook counts the contacts in a child
        folder, and a sync that walked only the top level could never agree with it). Id -> display name."""
        if self._folders is not None and not force:
            return self._folders
        out: dict[str, str] = {}
        queue = ["me/contactFolders?" + urlencode({"$top": 100})]
        depth = 0
        while queue and depth < 6:                            # Outlook nests a few levels at most
            nxt: list[str] = []
            for start in queue:
                url = start
                while url:
                    resp = self._call("GET", url)
                    if resp.status == 404 and depth:
                        break                                 # a folder removed while it was being walked
                    if resp.status != 200:
                        raise self._fail(resp, "list contact folders")
                    data = resp.json() or {}
                    for f in data.get("value") or []:
                        if f.get("id") and f["id"] not in out:
                            out[f["id"]] = f.get("displayName") or f["id"]
                            nxt.append(f"me/contactFolders/{quote(f['id'], safe='')}/childFolders?"
                                       + urlencode({"$top": 100}))
                    url = data.get("@odata.nextLink") or ""
            queue, depth = nxt, depth + 1
        self._folders = out
        return out

    def _ensure_default_id(self) -> str:
        """The id of the default Contacts folder, which the folder list also
        contains: read from any contact in it. An empty default folder has no
        contacts to confuse anything, so "unknown" is then harmless."""
        if self._default_id is None:
            resp = self._call("GET", "me/contacts?" + urlencode({"$top": 1, "$select": "parentFolderId"}))
            if resp.status != 200:
                raise self._fail(resp, "find the default contact folder")
            items = (resp.json() or {}).get("value") or []
            self._default_id = items[0].get("parentFolderId", "") if items else ""
        return self._default_id

    def _folder_name(self, folder_id: str) -> str:
        if not folder_id or folder_id == self._default_id:
            return ""
        return self._load_folders().get(folder_id, "")

    # -- Graph contact <-> Card ---------------------------------------------

    def _to_card(self, c: dict[str, Any], folder_name: str = "") -> Card:
        card = Card()
        for ours, theirs in (("prefix", "title"), ("given", "givenName"), ("additional", "middleName"),
                             ("family", "surname"), ("suffix", "generation"), ("nickname", "nickName"),
                             ("company", "companyName"), ("department", "department"),
                             ("title", "jobTitle"), ("notes", "personalNotes")):
            if c.get(theirs):
                card.scalars[ours] = str(c[theirs])
        for e in c.get("emailAddresses") or []:
            if e.get("address"):
                card.fields.append(Field("email", e["address"]))
        if c.get("mobilePhone"):
            card.fields.append(Field("phone", c["mobilePhone"], "mobile"))
        for p in c.get("homePhones") or []:
            if p:
                card.fields.append(Field("phone", p, "home"))
        for p in c.get("businessPhones") or []:
            if p:
                card.fields.append(Field("phone", p, "work"))
        for key, label in (("homeAddress", "home"), ("businessAddress", "work"), ("otherAddress", "other")):
            d = _phys(c.get(key) or {})
            if d:
                text = ", ".join(d[k] for k in ("street", "city", "region", "postcode", "country") if k in d)
                card.fields.append(Field("address", text, label, data=d))
        if c.get("businessHomePage"):
            card.fields.append(Field("url", c["businessHomePage"]))
        bday = _read_birthday(c.get("birthday"))
        if bday:
            card.fields.append(Field("date", bday, "birthday"))
        for label, key in _RELATED.items():
            if c.get(key):
                card.fields.append(Field("related", c[key], label))
        for im in c.get("imAddresses") or []:
            if im:
                card.fields.append(Field("im", im))
        for g in ([folder_name] if folder_name else []) + list(c.get("categories") or []):
            if g and g not in card.groups:
                card.groups.append(g)
        return card

    def _to_body(self, card: Card, folder_name: str = "") -> dict[str, Any]:
        """The JSON for a write. Everything Graph holds is named, with null or an
        empty list where the card has nothing, so a PATCH also clears what the
        person removed in Leasha."""
        c = project(card, self.caps)
        s = c.get
        body: dict[str, Any] = {
            "title": s("prefix") or None, "givenName": s("given") or None, "middleName": s("additional") or None,
            "surname": s("family") or None, "generation": s("suffix") or None, "nickName": s("nickname") or None,
            "companyName": s("company") or None, "department": s("department") or None,
            "jobTitle": s("title") or None, "personalNotes": s("notes") or None,
            "emailAddresses": [], "homePhones": [], "businessPhones": [], "mobilePhone": None,
            "homeAddress": None, "businessAddress": None, "otherAddress": None, "businessHomePage": None,
            "birthday": None, "spouseName": None, "manager": None, "assistantName": None, "imAddresses": [],
        }
        for f in c.fields:
            tokens = f.label.lower().split()
            if f.kind == "email":
                if len(body["emailAddresses"]) < MAX_EMAILS:
                    body["emailAddresses"].append({"address": f.value, "name": f.value})
            elif f.kind == "phone":
                if ({"mobile", "cell"} & set(tokens)) and body["mobilePhone"] is None:
                    body["mobilePhone"] = f.value
                elif "home" in tokens:
                    body["homePhones"].append(f.value)
                else:
                    body["businessPhones"].append(f.value)
            elif f.kind == "address":
                slot = ("homeAddress" if "home" in tokens else
                        "businessAddress" if ({"work", "business"} & set(tokens)) else "otherAddress")
                if not body[slot]:
                    body[slot] = _to_phys(f.data, f.value) or None
            elif f.kind == "url":
                if body["businessHomePage"] is None:
                    body["businessHomePage"] = f.value
            elif f.kind == "date":
                iso = _write_birthday(f.value)
                if iso and body["birthday"] is None:
                    body["birthday"] = iso
            elif f.kind == "related":
                key = _RELATED.get(f.label.strip().lower())
                if key and body[key] is None:
                    body[key] = f.value
            elif f.kind == "im":
                body["imAddresses"].append(f.value)
        # Fit the numbers into the slots Outlook has: when business is full the next goes to home, and back.
        homes, work = body["homePhones"], body["businessPhones"]
        while len(work) > _MAX_PER_PHONE_SLOT and len(homes) < _MAX_PER_PHONE_SLOT:
            homes.append(work.pop())
        while len(homes) > _MAX_PER_PHONE_SLOT and len(work) < _MAX_PER_PHONE_SLOT:
            work.append(homes.pop())
        del homes[_MAX_PER_PHONE_SLOT:], work[_MAX_PER_PHONE_SLOT:]
        body["categories"] = [g for g in c.groups if g.casefold() != folder_name.casefold()]
        return body

    def _remote(self, c: dict[str, Any], folder_name: str) -> RemoteContact:
        return RemoteContact(remote_id=c.get("id", ""), version=c.get("changeKey", ""),
                             card=self._to_card(c, folder_name),
                             edited_at=_common.parse_time(c.get("lastModifiedDateTime")))

    # -- pull ---------------------------------------------------------------

    def _start_url(self, key: str) -> str:
        if not key:
            return "me/contacts/delta"
        return f"me/contactFolders/{quote(key, safe='')}/contacts/delta"

    def pull(self, token: Optional[str]) -> Iterator[ContactPage]:
        state = _common.decode_token(token)
        if state is None:
            raise TokenExpired("the saved Microsoft token is not readable")
        deltas: dict[str, str] = {str(k): str(v) for k, v in (state.get("d") or {}).items()}
        done: set[str] = set(state.get("done") or [])
        in_flight, next_link = state.get("k"), str(state.get("n") or "")
        folders = self._load_folders(force=True)
        default_id = self._ensure_default_id()
        order = [""] + [fid for fid in folders if fid != default_id]
        seen: set[str] = set()                      # ids changed during this pull (see the module note)

        def remaining() -> bool:
            return any(k not in done for k in order)

        for key in order:
            if key in done:
                continue
            url = next_link if (key == in_flight and next_link) else (deltas.get(key) or self._start_url(key))
            name = folders.get(key, "") if key else ""
            while True:
                resp = self._call("GET", url, paged=True)
                if resp.status == 410:
                    raise TokenExpired("Microsoft no longer accepts the saved delta link")
                if resp.status == 404 and key:
                    deltas.pop(key, None)             # the folder is gone; its contacts are not reported
                    done.add(key)
                    break
                if resp.status != 200:
                    raise self._fail(resp, "read changes")
                data = resp.json() or {}
                changes: list[RemoteContact] = []
                for item in data.get("value") or []:
                    if "@removed" in item:
                        if (item["@removed"] or {}).get("reason") == "changed" or item.get("id") in seen:
                            continue
                        changes.append(RemoteContact(remote_id=item.get("id", ""), card=None))
                    elif item.get("id"):
                        seen.add(item["id"])
                        changes.append(self._remote(item, name))
                nxt = data.get("@odata.nextLink") or ""
                if nxt:
                    yield ContactPage(changes, _common.encode_token(
                        {"v": 1, "d": deltas, "done": sorted(done), "k": key, "n": nxt}))
                    url = nxt
                    continue
                link = data.get("@odata.deltaLink") or ""
                if not link:
                    raise self._fail(HttpResponse(502, {}, b"no deltaLink"), "read changes")
                deltas[key] = link
                done.add(key)
                if remaining():
                    yield ContactPage(changes, _common.encode_token({"v": 1, "d": deltas, "done": sorted(done)}))
                else:
                    final = {k: v for k, v in deltas.items() if k == "" or k in folders}
                    yield ContactPage(changes, _common.encode_token({"v": 1, "d": final}), done=True)
                    return
                break
        # Every key was already done (a resume that lost nothing) or the last folder was gone.
        final = {k: v for k, v in deltas.items() if k == "" or k in folders}
        yield ContactPage([], _common.encode_token({"v": 1, "d": final}), done=True)

    # -- reads and writes ---------------------------------------------------

    def get(self, remote_id: str) -> Optional[RemoteContact]:
        resp = self._call("GET", f"me/contacts/{quote(remote_id, safe='')}")
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise self._fail(resp, "read a contact")
        self._ensure_default_id()
        c = resp.json() or {}
        return self._remote(c, self._folder_name(c.get("parentFolderId", "")))

    def _current(self, remote_id: str, version: str) -> dict[str, Any]:
        """Read the contact's changeKey and folder; raise `VersionConflict` if it is
        gone or has changed since `version`. Nothing is written blind."""
        resp = self._call("GET", f"me/contacts/{quote(remote_id, safe='')}?" +
                          urlencode({"$select": "changeKey,parentFolderId," + ",".join(_ADDRESS_SLOTS)}))
        if resp.status == 404:
            raise VersionConflict(remote_id)
        if resp.status != 200:
            raise self._fail(resp, "read a contact before changing it")
        cur = resp.json() or {}
        if version and cur.get("changeKey", "") != version:
            raise VersionConflict(remote_id)
        return cur

    def _write_headers(self, version: str) -> dict[str, str]:
        return {"If-Match": f'W/"{version}"'} if (self._send_if_match and version) else {}

    def create(self, card: Card) -> tuple[str, str]:
        self._ensure_default_id()
        folders = self._load_folders()
        target, folder_name = "me/contacts", ""
        for g in card.groups:
            fid = next((i for i, n in folders.items() if n.casefold() == g.casefold() and i != self._default_id), "")
            if fid:
                target, folder_name = f"me/contactFolders/{quote(fid, safe='')}/contacts", g
                break
        body = {k: v for k, v in self._to_body(card, folder_name).items() if v not in (None, [], {})}
        resp = self._call("POST", target, body=body)
        if resp.status not in (200, 201):
            raise self._fail(resp, "create a contact")
        made = resp.json() or {}
        return made.get("id", ""), made.get("changeKey", "")

    def update(self, remote_id: str, version: str, card: Card) -> str:
        cur = self._current(remote_id, version)
        self._ensure_default_id()
        body = self._to_body(card, self._folder_name(cur.get("parentFolderId", "")))
        for slot in _ADDRESS_SLOTS:
            if body.get(slot) is None and not cur.get(slot):
                del body[slot]                                  # nothing to clear, and Graph answers 500
        resp = self._call("PATCH", f"me/contacts/{quote(remote_id, safe='')}", body=body,
                          extra=self._write_headers(version))
        if resp.status in (409, 412, 404):
            raise VersionConflict(remote_id)
        if resp.status in (400, 500):
            return self._update_piecewise(remote_id, version, body, resp)
        if resp.status != 200:
            raise self._fail(resp, "update a contact")
        return (resp.json() or {}).get("changeKey", "")

    def _update_piecewise(self, remote_id: str, version: str, body: dict[str, Any], refused: HttpResponse) -> str:
        """The whole change was refused: send it one property at a time, so one property Outlook will not take
        cannot stop the rest of the contact from being written.

        Graph accepts each of this contact's properties alone and sometimes refuses them together (HTTP 500
        with no reason), and a 400 names one property and rejects all. A contact where nothing at all goes
        through is still an error; where some do, the rest are logged and the contact is written as far as
        it can be - the read-back after the write records what the account really holds."""
        key, failed, written = version, [], 0
        for name, value in body.items():
            resp = self._call("PATCH", f"me/contacts/{quote(remote_id, safe='')}", body={name: value},
                              extra=self._write_headers(key))
            if resp.status == 200:
                written += 1
                key = (resp.json() or {}).get("changeKey", "") or key
            elif resp.status in (409, 412, 404):
                raise VersionConflict(remote_id)
            else:
                failed.append(name)
        if not written:
            raise self._fail(refused, "update a contact")
        if failed:
            from app.core.logging import logger  # noqa: PLC0415

            logger.bind(component=_COMPONENT).warning("{}: Outlook would not take {} of contact {}; the rest was written",
                                                      self._account, ", ".join(failed), remote_id)
        return key

    def delete(self, remote_id: str, version: str) -> None:
        try:
            self._current(remote_id, version)
        except VersionConflict:
            # Gone is not a conflict for a delete; changed is.
            if self._call("GET", f"me/contacts/{quote(remote_id, safe='')}?$select=id").status == 404:
                return
            raise
        resp = self._call("DELETE", f"me/contacts/{quote(remote_id, safe='')}",
                          extra=self._write_headers(version))
        if resp.status in (409, 412):
            raise VersionConflict(remote_id)
        if resp.status not in (200, 204, 404):
            raise self._fail(resp, "delete a contact")

    # -- batches (order 1t 5d) -------------------------------------------------------------------
    #: Graph's $batch carries 20 requests; Outlook runs up to four of them at a time per mailbox. A write is
    #: guarded by a batch of reads of the change keys first (rule 3c), and the answer to a PATCH or POST is the
    #: contact as Outlook now holds it - the read-back - so nothing is read twice.
    batch_size = 20
    one_by_one_cost = {"create": 2, "update": 3, "delete": 2}
    batch_cost = {"create": 1, "update": 2, "delete": 2}
    #: A request inside a batch answered 429 is sent again in the next batch after the wait Graph asks for, up to
    #: this many rounds; beyond that the account is busy and the run says so.
    _THROTTLE_ROUNDS = 3

    def _batch(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Send `requests` (each `method`, `url`, optional `body`) as $batch calls of 20; the answers in the same
        order, each {"status", "body", "headers"}. A request throttled inside a batch (429) is sent again."""
        answers: list[Optional[dict[str, Any]]] = [None] * len(requests)
        pending = list(range(len(requests)))
        for _round in range(self._THROTTLE_ROUNDS + 1):
            throttled: list[int] = []
            wait = 0.0
            for start in range(0, len(pending), self.batch_size):
                chunk = pending[start:start + self.batch_size]
                body = {"requests": []}
                for n, i in enumerate(chunk):
                    r = requests[i]
                    sub: dict[str, Any] = {"id": str(n), "method": r["method"], "url": "/" + r["url"].lstrip("/"),
                                           "headers": {"Prefer": 'IdType="ImmutableId"'}}
                    if r.get("body") is not None:
                        sub["body"] = r["body"]
                        sub["headers"]["Content-Type"] = "application/json"
                    if r.get("headers"):
                        sub["headers"].update(r["headers"])
                    body["requests"].append(sub)
                resp = self._call("POST", "$batch", body=body)
                if resp.status != 200:
                    raise self._fail(resp, "send a batch")
                for a in (resp.json() or {}).get("responses") or []:
                    try:
                        i = chunk[int(a.get("id"))]
                    except (TypeError, ValueError, IndexError):
                        continue
                    status = int(a.get("status") or 0)
                    if status == 429:
                        throttled.append(i)
                        hdr = {str(k).lower(): v for k, v in (a.get("headers") or {}).items()}
                        try:
                            wait = max(wait, float(hdr.get("retry-after") or 1))
                        except ValueError:
                            wait = max(wait, 1.0)
                        continue
                    answers[i] = {"status": status, "body": a.get("body") or {}, "headers": a.get("headers") or {}}
            if not throttled:
                break
            if _round == self._THROTTLE_ROUNDS:
                raise AppErrorException(make_error(
                    "ERR_PROVIDER_LIMIT", _COMPONENT, account=self._account,
                    reason=f"too many requests, HTTP 429 inside a batch ({len(throttled)} left)",
                    retry_after_s=wait))
            import time  # noqa: PLC0415
            time.sleep(min(wait, 60.0))
            pending = throttled
        return [a or {"status": 0, "body": {}, "headers": {}} for a in answers]

    def _item_error(self, answer: dict[str, Any], what: str) -> Any:
        err = (answer.get("body") or {}).get("error") or {}
        return make_error("ERR_ACCOUNT_REJECTED", _COMPONENT, account=self._account,
                          reason=f"{what}: HTTP {answer.get('status')} {err.get('code') or ''}".strip(),
                          details=str(err.get("message") or "")[:500])

    def _currents(self, remote_ids: list[str]) -> list[dict[str, Any]]:
        sel = urlencode({"$select": "changeKey,parentFolderId," + ",".join(_ADDRESS_SLOTS)})
        return self._batch([{"method": "GET", "url": f"me/contacts/{quote(r, safe='')}?{sel}"} for r in remote_ids])

    def create_many(self, cards: list[Card]) -> list[WriteResult]:
        if not cards:
            return []
        self._ensure_default_id()
        folders = self._load_folders()
        reqs, names = [], []
        for card in cards:
            target, folder_name = "me/contacts", ""
            for g in card.groups:
                fid = next((i for i, n in folders.items() if n.casefold() == g.casefold() and i != self._default_id), "")
                if fid:
                    target, folder_name = f"me/contactFolders/{quote(fid, safe='')}/contacts", g
                    break
            body = {k: v for k, v in self._to_body(card, folder_name).items() if v not in (None, [], {})}
            reqs.append({"method": "POST", "url": target, "body": body})
            names.append(folder_name)
        out: list[WriteResult] = []
        for answer, folder_name in zip(self._batch(reqs), names):
            if answer["status"] in (200, 201) and (answer["body"] or {}).get("id"):
                c = answer["body"]
                out.append(WriteResult(ok=True, remote_id=c["id"], version=c.get("changeKey", ""),
                                       card=self._to_card(c, folder_name)))
            else:
                out.append(WriteResult(error=self._item_error(answer, "create a contact")))
        return out

    def update_many(self, items: list[tuple[str, str, Card]]) -> list[WriteResult]:
        if not items:
            return []
        self._ensure_default_id()
        currents = self._currents([rid for rid, _v, _c in items])
        out: list[Optional[WriteResult]] = [None] * len(items)
        reqs, where, bodies = [], [], []
        for i, ((rid, version, card), cur) in enumerate(zip(items, currents)):
            if cur["status"] == 404:
                out[i] = WriteResult(conflict=True)
                continue
            if cur["status"] != 200:
                out[i] = WriteResult(error=self._item_error(cur, "read a contact before changing it"))
                continue
            c = cur["body"] or {}
            if version and c.get("changeKey", "") != version:
                out[i] = WriteResult(conflict=True)            # changed since it was read: never written blind
                continue
            folder_name = self._folder_name(c.get("parentFolderId", ""))
            body = self._to_body(card, folder_name)
            for slot in _ADDRESS_SLOTS:
                if body.get(slot) is None and not c.get(slot):
                    del body[slot]                              # nothing to clear, and Graph answers 500
            reqs.append({"method": "PATCH", "url": f"me/contacts/{quote(rid, safe='')}", "body": body,
                         "headers": self._write_headers(version)})
            where.append((i, folder_name))
            bodies.append(body)
        for (i, folder_name), answer, body in zip(where, self._batch(reqs) if reqs else [], bodies):
            rid, version, _card = items[i]
            if answer["status"] == 200:
                c = answer["body"] or {}
                out[i] = WriteResult(ok=True, remote_id=rid, version=c.get("changeKey", ""),
                                     card=self._to_card(c, folder_name) if c.get("id") else None)
            elif answer["status"] in (409, 412, 404):
                out[i] = WriteResult(conflict=True)
            elif answer["status"] in (400, 500):
                try:                                            # one property Outlook will not take: piece by piece
                    key = self._update_piecewise(rid, version, body, HttpResponse(answer["status"], {}, b""))
                    out[i] = WriteResult(ok=True, remote_id=rid, version=key)
                except VersionConflict:
                    out[i] = WriteResult(conflict=True)
                except AppErrorException as exc:
                    if exc.error.code in _common.ACCOUNT_WIDE:
                        raise
                    out[i] = WriteResult(error=exc.error)
            else:
                out[i] = WriteResult(error=self._item_error(answer, "update a contact"))
        return [o or WriteResult(error=make_error("ERR_ACCOUNT_REJECTED", _COMPONENT, account=self._account,
                                                  reason="no answer for this contact")) for o in out]

    def delete_many(self, items: list[tuple[str, str]]) -> list[WriteResult]:
        if not items:
            return []
        currents = self._currents([rid for rid, _v in items])
        out: list[Optional[WriteResult]] = [None] * len(items)
        reqs, where = [], []
        for i, ((rid, version), cur) in enumerate(zip(items, currents)):
            if cur["status"] == 404:
                out[i] = WriteResult(ok=True, remote_id=rid)    # already gone
            elif cur["status"] != 200:
                out[i] = WriteResult(error=self._item_error(cur, "read a contact before deleting it"))
            elif version and (cur["body"] or {}).get("changeKey", "") != version:
                out[i] = WriteResult(conflict=True)             # changed since: the engine looks before deleting
            else:
                reqs.append({"method": "DELETE", "url": f"me/contacts/{quote(rid, safe='')}",
                             "headers": self._write_headers(version)})
                where.append(i)
        for i, answer in zip(where, self._batch(reqs) if reqs else []):
            if answer["status"] in (200, 204, 404):
                out[i] = WriteResult(ok=True, remote_id=items[i][0])
            elif answer["status"] in (409, 412):
                out[i] = WriteResult(conflict=True)
            else:
                out[i] = WriteResult(error=self._item_error(answer, "delete a contact"))
        return [o or WriteResult(conflict=True) for o in out]

    def get_many(self, remote_ids: list[str]) -> dict[str, Optional[RemoteContact]]:
        self._ensure_default_id()
        found: dict[str, Optional[RemoteContact]] = {}
        answers = self._batch([{"method": "GET", "url": f"me/contacts/{quote(r, safe='')}"} for r in remote_ids])
        for rid, answer in zip(remote_ids, answers):
            c = answer["body"] or {}
            if answer["status"] != 200 or not c.get("id"):
                found[rid] = None
                continue
            found[rid] = self._remote(c, self._folder_name(c.get("parentFolderId", "")))
        return found

    def list_ids(self) -> dict[str, tuple[str, str]]:
        """Every contact in every folder, child folders included, with its change key and name (the check after
        a sync, 1t 3c). Ids, keys and names only: a few requests for a whole book."""
        folders = self._load_folders(force=True)
        default_id = self._ensure_default_id()
        out: dict[str, tuple[str, str]] = {}
        sel = urlencode({"$select": "id,changeKey,displayName", "$top": 999})
        starts = [f"me/contacts?{sel}"] + [f"me/contactFolders/{quote(f, safe='')}/contacts?{sel}"
                                           for f in folders if f != default_id]
        for start in starts:
            url = start
            while url:
                resp = self._call("GET", url, paged=True)
                if resp.status == 404:
                    break
                if resp.status != 200:
                    raise self._fail(resp, "list contacts")
                data = resp.json() or {}
                for c in data.get("value") or []:
                    if c.get("id"):
                        out[c["id"]] = (c.get("changeKey", ""), c.get("displayName") or "")
                url = data.get("@odata.nextLink") or ""
        return out

    def close(self) -> None:
        self._folders = None
        _common.close_transport(self._t)
