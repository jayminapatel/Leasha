"""A fake account server and provider for the sync-engine tests.

Layer: L7

`FakeServer` is the account: records with version tags and a change log.
`FakeProvider` is a `ContactProvider` over it, with the faults the engine must
survive (a token that expired, a crash after some pages, a write that loses a
race, a rate limit). No network, no sleeping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from app.accounts.provider import (ContactPage, RemoteContact, TokenExpired, VersionConflict)
from app.contacts.card import ALL_CAPS, Capabilities, Card, Field
from app.core.errors import AppErrorException, make_error


def person(given: str, family: str = "", *, email: str = "", phone: str = "", company: str = "",
           groups: tuple = ()) -> Card:
    c = Card(scalars={k: v for k, v in (("given", given), ("family", family), ("company", company)) if v})
    if email:
        c.fields.append(Field("email", email, "work"))
    if phone:
        c.fields.append(Field("phone", phone, "mobile"))
    c.groups = list(groups)
    return c


@dataclass
class Rec:
    version: int
    card: Optional[Card]
    edited_at: float
    seq: int


class FakeServer:
    def __init__(self) -> None:
        self.recs: dict[str, Rec] = {}
        self.seq = 0
        self.n = 0
        self.expire_before = -1          # tokens whose `since` is below this are expired
        self.writes: list[tuple[str, str]] = []

    def add(self, card: Card, edited_at: float = 0.0) -> str:
        self.n += 1
        rid = f"r{self.n}"
        self.seq += 1
        self.recs[rid] = Rec(1, card, edited_at or time.time(), self.seq)
        return rid

    def edit(self, rid: str, card: Card, edited_at: float = 0.0) -> None:
        r = self.recs[rid]
        self.seq += 1
        self.recs[rid] = Rec(r.version + 1, card, edited_at or time.time(), self.seq)

    def remove(self, rid: str) -> None:
        r = self.recs[rid]
        self.seq += 1
        self.recs[rid] = Rec(r.version + 1, None, time.time(), self.seq)

    def live(self) -> dict[str, Card]:
        return {k: r.card for k, r in self.recs.items() if r.card is not None}


class FakeProvider:
    def __init__(self, server: FakeServer, caps: Capabilities = ALL_CAPS, *, page_size: int = 2) -> None:
        self.server = server
        self.caps = caps
        self.page_size = page_size
        self.crash_after_pages: Optional[int] = None     # raise after serving this many pages
        self.fail_with: Optional[Exception] = None
        self.before_update: Optional[Callable[[str], None]] = None
        self.pulls = 0
        self.closed = False

    # -- reads
    def pull(self, token: Optional[str]) -> Iterator[ContactPage]:
        if self.fail_with is not None:
            raise self.fail_with
        since, top, offset = 0, self.server.seq, 0
        if token:
            parts = token.split("|")
            if len(parts) == 1:
                since = int(parts[0])
            else:
                since, top, offset = int(parts[0]), int(parts[1]), int(parts[2])
        if token and since < self.server.expire_before:
            raise TokenExpired()
        ids = sorted((k for k, r in self.server.recs.items() if since < r.seq <= top),
                     key=lambda k: self.server.recs[k].seq)
        if since == 0:                                    # a full pull has nothing to report as deleted
            ids = [k for k in ids if self.server.recs[k].card is not None]
        self.pulls += 1
        served = 0
        if not ids:
            yield ContactPage([], str(top), done=True)
            return
        for start in range(offset, len(ids), self.page_size):
            chunk = ids[start:start + self.page_size]
            last = start + self.page_size >= len(ids)
            changes = [RemoteContact(k, str(self.server.recs[k].version),
                                     self.server.recs[k].card.copy() if self.server.recs[k].card else None,
                                     self.server.recs[k].edited_at) for k in chunk]
            nxt = str(top) if last else f"{since}|{top}|{start + self.page_size}"
            yield ContactPage(changes, nxt, done=last)
            served += 1
            if self.crash_after_pages is not None and served >= self.crash_after_pages and not last:
                raise RuntimeError("simulated crash mid-sync")

    def get(self, remote_id: str) -> Optional[RemoteContact]:
        r = self.server.recs.get(remote_id)
        if r is None:
            return None
        return RemoteContact(remote_id, str(r.version), r.card.copy() if r.card else None, r.edited_at)

    # -- writes
    def create(self, card: Card) -> tuple[str, str]:
        rid = self.server.add(card.copy())
        self.server.writes.append(("create", rid))
        return rid, "1"

    def update(self, remote_id: str, version: str, card: Card) -> str:
        if self.before_update is not None:
            hook, self.before_update = self.before_update, None
            hook(remote_id)
        r = self.server.recs[remote_id]
        if str(r.version) != version:
            raise VersionConflict()
        self.server.edit(remote_id, card.copy())
        self.server.writes.append(("update", remote_id))
        return str(self.server.recs[remote_id].version)

    def delete(self, remote_id: str, version: str) -> None:
        r = self.server.recs.get(remote_id)
        if r is not None and r.card is not None:
            self.server.remove(remote_id)
        self.server.writes.append(("delete", remote_id))

    def close(self) -> None:
        self.closed = True

    # -- order 1t 3b: the check after a sync
    def list_ids(self) -> dict[str, tuple[str, str]]:
        from app.contacts.card import display_name  # noqa: PLC0415
        return {k: (str(r.version), display_name(r.card) or "") for k, r in self.server.recs.items()
                if r.card is not None}


class BatchFakeProvider(FakeProvider):
    """A `FakeProvider` with the batch methods (order 1t 5b): counts the calls, can refuse or race named records."""

    batch_size = 3

    def __init__(self, server: FakeServer, caps: Capabilities = ALL_CAPS, *, page_size: int = 2) -> None:
        super().__init__(server, caps, page_size=page_size)
        self.batch_calls: list[tuple[str, int]] = []
        self.refuse: set[str] = set()            # remote ids (or "new:<given>") answered as refused
        self.race: set[str] = set()              # remote ids answered as a conflict once

    def create_many(self, cards):
        from app.accounts.provider import WriteResult  # noqa: PLC0415
        self.batch_calls.append(("create", len(cards)))
        out = []
        for card in cards:
            if f"new:{card.get('given')}" in self.refuse:
                out.append(WriteResult(error=make_error("ERR_ACCOUNT_REJECTED", "fake", account="F", reason="no")))
                continue
            rid, version = self.create(card)
            out.append(WriteResult(ok=True, remote_id=rid, version=version, card=self.server.recs[rid].card.copy()))
        return out

    def update_many(self, items):
        from app.accounts.provider import WriteResult  # noqa: PLC0415
        self.batch_calls.append(("update", len(items)))
        out = []
        for rid, version, card in items:
            if rid in self.refuse:
                out.append(WriteResult(error=make_error("ERR_ACCOUNT_REJECTED", "fake", account="F", reason="no")))
            elif rid in self.race:
                self.race.discard(rid)
                out.append(WriteResult(conflict=True))
            else:
                try:
                    v = self.update(rid, version, card)
                    out.append(WriteResult(ok=True, remote_id=rid, version=v))
                except VersionConflict:
                    out.append(WriteResult(conflict=True))
        return out

    def delete_many(self, items):
        from app.accounts.provider import WriteResult  # noqa: PLC0415
        self.batch_calls.append(("delete", len(items)))
        out = []
        for rid, version in items:
            self.delete(rid, version)
            out.append(WriteResult(ok=True, remote_id=rid))
        return out

    def get_many(self, ids):
        self.batch_calls.append(("get", len(ids)))
        return {i: self.get(i) for i in ids}
