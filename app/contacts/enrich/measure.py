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

import calendar
import csv
import random
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.contacts.enrich import company, faces, logo, signatures, wikidata
from app.contacts.enrich.base import address_of, body_text, emails_of, mail_from
from app.contacts.enrich.run import REPEAT_DAYS, seen_key
from app.contacts.enrich.signatures import MAX_MESSAGES, find_block, parse_block, signature_proposals

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


def window_start(months: int, today: Optional[date] = None) -> Optional[int]:
    """Unix seconds at the start of the age window: `months` calendar months before today, or None for any age."""
    if not months:
        return None
    t = today or datetime.now(timezone.utc).date()
    y, m = divmod(t.month - 1 - int(months), 12)
    year, month = t.year + y, m + 1
    day = min(t.day, calendar.monthrange(year, month)[1])
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


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
    sources: dict = field(default_factory=dict)          # source -> contacts with a proposal / would ask
    fields: Counter = field(default_factory=Counter)      # proposal field -> count
    seconds: float = 0.0
    samples: list = field(default_factory=list)           # signature proposals, for 1b

    def per_contact(self) -> float:
        return round(self.seconds / self.live, 4) if self.live else 0.0

    def as_dict(self) -> dict:
        out = {k: getattr(self, k) for k in ("months", "since", "live", "with_email", "with_mail", "block_found",
                                             "read", "proposed", "already_on_card", "sources", "seconds")}
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
            keep_samples: bool = False) -> Funnel:
    """The funnel over every live contact. Reads only; see the module docstring."""
    from app.contacts.enrich.gravatar import email_hash  # noqa: PLC0415 - only the hash function, no request

    region = getattr(settings, "phone_region", "") or hub.region
    fun = Funnel(months=int(months or 0), since=window_start(months))
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
        rows = [r for r in mail_from(store, emails, limit=MAX_MESSAGES)
                if fun.since is None or (r.sent_at or 0) >= fun.since]
        if not rows:
            continue
        fun.with_mail += 1
        found_block, read, first = False, False, None
        for row in rows:
            block = find_block(body_text(store, row.file_id))
            if not block:
                continue
            found_block = True
            sig = parse_block(block, name_tokens=signatures._name_tokens(card, signatures._display(row.sender)),
                              sender_email=address_of(row.sender), region=region)
            if any(k != "name_matched" for k in sig):
                read, first = True, first or (row, block)
        fun.block_found += found_block
        fun.read += read
        props = signature_proposals(store, cid, card, region=region, rows=rows)
        if props:
            fun.proposed += 1
            src["signature"] += 1
            for p in props:
                fun.fields[p.field_kind] += 1
                if keep_samples and first is not None:
                    fun.samples.append((cid, card.get("fn") or card.get("given"), p, first[1]))
        elif read:
            fun.already_on_card += 1
    fun.seconds = round(time.perf_counter() - started, 2)
    fun.sources = dict(src)
    return fun


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
