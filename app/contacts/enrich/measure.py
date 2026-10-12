"""Order 1s section 1: the enrichment funnel as a command - read-only, on any index.

Layer: L7

`leasha contacts enrich --measure` runs every enrichment source over every live
contact the way a real run would, and counts instead of proposing. It is the
acceptance instrument for order 1s: every later section is judged by the numbers
it prints (the funnel of the order's opening table, per source, per field, and the
seconds a contact).

**It cannot write, by construction, not by care.** The index is opened with
SQLite's own read-only URI (`mode=ro`) and `PRAGMA query_only`, never through
`SqliteStore.connect()` - which applies the branch's migrations (the 2026-10-12
trap in HANDOFF: a branch's CLI moved the owner's index to v40) and deletes FTS
statistics rows. A source that tried to write would raise, not succeed.

**It never calls the internet.** The three internet sources are counted as "would
ask": how many contacts each would be asked about, and how many of those its
90-day memory would skip.
"""

from __future__ import annotations

import csv
import random
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.contacts.enrich import company, faces, logo, wikidata
from app.contacts.enrich.base import emails_of, mail_from, window_start
from app.contacts.enrich.owner import learn_owner
from app.contacts.enrich.run import REPEAT_DAYS, seen_key
from app.contacts.enrich.signatures import (
    MAX_MESSAGES,
    UPDATE_FROM,
    proposals_from,
    read_signatures,
)

__all__ = ["ReadOnlyStore", "Funnel", "window_start", "measure", "write_sample"]


class ReadOnlyStore:
    """Just enough of `SqliteStore` for the enrichment sources: `conn` and `get_state`.

    `set_state` raises, so a source that tries to remember something fails loudly here."""

    def __init__(self, path: Path) -> None:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = ON")

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM index_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        raise PermissionError(f"the measuring command never writes (tried to set {key!r})")

    def close(self) -> None:
        self.conn.close()



@dataclass
class Funnel:
    months: int = 0
    since: Optional[int] = None
    live: int = 0
    with_email: int = 0
    with_mail: int = 0
    block_found: int = 0
    read: int = 0
    proposed: int = 0
    already_on_card: int = 0
    #: 4c: contacts with no message from them in the window that got a proposal from a quoted signature.
    quoted_only: int = 0
    sources: dict = field(default_factory=dict)          # source -> contacts with a proposal / would ask
    fields: Counter = field(default_factory=Counter)      # proposal field -> count
    seconds: float = 0.0
    samples: list = field(default_factory=list)           # signature proposals, for 1b

    def per_contact(self) -> float:
        return round(self.seconds / self.live, 4) if self.live else 0.0

    def as_dict(self) -> dict:
        out = {k: getattr(self, k) for k in ("months", "since", "live", "with_email", "with_mail", "block_found",
                                             "read", "proposed", "already_on_card", "quoted_only", "sources",
                                             "seconds")}
        out["fields"] = dict(self.fields)
        out["seconds_per_contact"] = self.per_contact()
        return out


def _live_ids(conn: Any) -> list[int]:
    return [int(r[0]) for r in conn.execute("SELECT id FROM contacts WHERE deleted_at IS NULL ORDER BY id")]


def _recent(store: ReadOnlyStore, kind: str, key: str, now: float) -> bool:
    raw = store.get_state(seen_key(kind, key))
    try:
        return raw is not None and (now - float(raw)) < REPEAT_DAYS * 86400
    except ValueError:
        return False


def measure(store: ReadOnlyStore, hub: Any, settings: Any, *, months: int = 0, clock: Any = time.time,
            keep_samples: bool = False, owner_emails: Any = ()) -> Funnel:
    """The funnel over every live contact. Reads only; see the module docstring.

    The signature steps are the rebuilt reader's own (order 1s section 3): "block found" is a block found
    by what repeats (3b) or by an anchor (3c) in a message from the person; "something read" is a value
    read from one; "proposals" are what the consensus offers (3j/3k), the owner's own details removed (3h,
    from `owner_emails`). A contact with no message from them can still get a proposal from a quoted
    signature (4c); those are counted apart, in `quoted_only`, so the funnel's steps still nest."""
    from app.contacts.enrich.gravatar import (
        email_hash,  # noqa: PLC0415 - only the hash function, no request
    )

    region = getattr(settings, "phone_region", "") or hub.region
    originals = bool(getattr(settings, "enrich_read_originals", False))
    update_from = float(getattr(settings, "enrich_tick_from", UPDATE_FROM))
    fun = Funnel(months=int(months or 0), since=window_start(months))
    owner = learn_owner(store, owner_emails, region=region, read_original=originals)
    src: Counter = Counter()
    now = clock()
    started = time.perf_counter()
    for cid in _live_ids(store.conn):
        card = hub.get_card(cid)
        if card is None:
            continue
        fun.live += 1
        if company.company_proposals(cid, card):
            src["company"] += 1
        if faces.pile_of(store, cid) is not None:
            src["face (has a named pile)"] += 1
        emails = emails_of(card)
        if not emails:
            continue
        fun.with_email += 1
        src["gravatar: would ask"] += 1
        if _recent(store, "gravatar", email_hash(emails[0]), now):
            src["gravatar: remembered, not asked again"] += 1
        domain = logo.company_domain(card)
        if domain:
            src["logo: would ask"] += 1
        if wikidata.company_term(card):
            src["wikidata: would ask"] += 1
        rows = mail_from(store, emails, limit=MAX_MESSAGES, since=fun.since)
        got = read_signatures(store, cid, card, region=region, rows=rows, since=fun.since, owner=owner,
                              read_original=originals)
        blocks: dict = {}
        props = proposals_from(cid, card, got.readings, region=region, update_from=update_from, blocks=blocks)
        if not rows:
            if props:
                fun.quoted_only += 1
                src["signature (quoted only)"] += 1
                _count(fun, cid, card, props, blocks, keep_samples)
            continue
        fun.with_mail += 1
        direct = [r for r in got.readings if not r.quoted]
        fun.block_found += any(len(b) for b in got.blocks)
        fun.read += bool(direct)
        if props:
            fun.proposed += 1
            src["signature"] += 1
            _count(fun, cid, card, props, blocks, keep_samples)
        elif direct:
            fun.already_on_card += 1
    fun.seconds = round(time.perf_counter() - started, 2)
    fun.sources = dict(src)
    return fun


def _count(fun: Funnel, cid: int, card: Any, props: list, blocks: dict, keep_samples: bool) -> None:
    """Count each proposal by field; for 1b, keep it with the block it was read from (its receipt's)."""
    for p in props:
        fun.fields[p.field_kind] += 1
        if keep_samples:
            fun.samples.append((cid, card.get("fn") or card.get("given"), p, blocks.get(id(p), [])))


def write_sample(fun: Funnel, n: int, out: Path, *, seed: int = 1) -> int:
    """1b: `n` random signature proposals with what a person checks them against. Returns how many were written."""
    rng = random.Random(seed)
    picked = rng.sample(fun.samples, min(n, len(fun.samples)))
    with Path(out).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["contact_id", "name", "field", "label", "now", "suggested", "receipt", "confidence",
                    "signature_block", "correct (Y/N)", "why wrong"])
        for cid, name, p, block in picked:
            w.writerow([cid, name, p.field_kind, p.field_label, p.old, p.new, p.receipt, p.confidence,
                        "\n".join(block), "", ""])
    return len(picked)
