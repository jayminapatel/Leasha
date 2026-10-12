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

import contextlib
import csv
import random
import sqlite3
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from app.contacts.enrich import company, faces, invitations, linkedin, logo, vcards, wikidata
from app.contacts.enrich.base import emails_of, mail_from, window_start
from app.contacts.enrich.owner import learn_owner
from app.contacts.enrich.run import REPEAT_DAYS, is_recent, repeat_days, seen_key
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
    #: 9a with `read_originals`: how many original messages were opened again, and the median milliseconds
    #: one took - what switching "Read the original messages" on would cost the owner.
    read_originals: bool = False
    rereads: int = 0
    reread_ms_median: float = 0.0

    def per_contact(self) -> float:
        return round(self.seconds / self.live, 4) if self.live else 0.0

    def as_dict(self) -> dict:
        out = {k: getattr(self, k) for k in ("months", "since", "live", "with_email", "with_mail", "block_found",
                                             "read", "proposed", "already_on_card", "quoted_only", "sources",
                                             "seconds", "read_originals", "rereads", "reread_ms_median")}
        out["fields"] = dict(self.fields)
        out["seconds_per_contact"] = self.per_contact()
        return out


def _live_ids(conn: Any) -> list[int]:
    return [int(r[0]) for r in conn.execute("SELECT id FROM contacts WHERE deleted_at IS NULL ORDER BY id")]


def _recent(store: ReadOnlyStore, kind: str, key: str, now: float, days: int = REPEAT_DAYS) -> bool:
    # Order 1s 6b: the same test a run makes, with the owner's "Ask the internet again after".
    return is_recent(store.get_state(seen_key(kind, key)), now, days)


@contextlib.contextmanager
def _timed_rereads(times: list[float]) -> Iterator[None]:
    """Every original read again during a measurement, timed. `base.full_text` calls
    `reread.original_body` through the module, so wrapping it here sees each re-read without the
    reader knowing it is measured; the original function is back as soon as the measurement ends."""
    from app.extract import reread  # noqa: PLC0415 - only a measurement with originals needs the readers

    inner = reread.original_body

    def timed(row: Any) -> Optional[str]:
        started = time.perf_counter()
        try:
            return inner(row)
        finally:
            times.append((time.perf_counter() - started) * 1000.0)

    reread.original_body = timed
    try:
        yield
    finally:
        reread.original_body = inner


def measure(store: ReadOnlyStore, hub: Any, settings: Any, *, months: int = 0, clock: Any = time.time,
            keep_samples: bool = False, owner_emails: Any = (), read_originals: Optional[bool] = None) -> Funnel:
    """The funnel over every live contact. Reads only; see the module docstring.

    The signature steps are the rebuilt reader's own (order 1s section 3): "block found" is a block found
    by what repeats (3b) or by an anchor (3c) in a message from the person; "something read" is a value
    read from one; "proposals" are what the consensus offers (3j/3k), the owner's own details removed (3h,
    from `owner_emails`). A contact with no message from them can still get a proposal from a quoted
    signature (4c); those are counted apart, in `quoted_only`, so the funnel's steps still nest.

    `read_originals` (order 1s 9a) measures as if "Read the original messages" were on (True) or off
    (False) whatever the setting says; None takes the setting. The originals are opened read-only, through
    `base.full_text`, and each re-read is counted and timed (`rereads`, `reread_ms_median`)."""
    times: list[float] = []
    setting = bool(getattr(settings, "enrich_read_originals", False))
    originals = setting if read_originals is None else bool(read_originals)
    from app.extract.pst_attachment import kept_open  # noqa: PLC0415 - archives opened once for the run

    with _timed_rereads(times) if originals else contextlib.nullcontext(), kept_open():
        fun = _measure(store, hub, settings, months=months, clock=clock, keep_samples=keep_samples,
                       owner_emails=owner_emails, originals=originals)
    fun.read_originals = originals
    fun.rereads = len(times)
    fun.reread_ms_median = round(statistics.median(times), 1) if times else 0.0
    return fun


def _measure(store: ReadOnlyStore, hub: Any, settings: Any, *, months: int, clock: Any, keep_samples: bool,
             owner_emails: Any, originals: bool) -> Funnel:
    from app.contacts.enrich.gravatar import (
        email_hash,  # noqa: PLC0415 - only the hash function, no request
    )

    region = getattr(settings, "phone_region", "") or hub.region
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
        _local_sources(src, hub, settings, cid, card, region, fun.since)
        emails = emails_of(card)
        if not emails:
            continue
        fun.with_email += 1
        src["gravatar: would ask"] += 1
        if _recent(store, "gravatar", email_hash(emails[0]), now, repeat_days(settings)):
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


#: Order 1s section 5's sources, counted as a run would call them: (name, AccountSettings switch, call).
_LOCAL = (
    ("vcard file", "enrich_vcards",
     lambda hub, s, cid, card, region, since: vcards.vcard_proposals(hub, cid, card, region=region, since=since)),
    ("linkedin export", "enrich_linkedin",
     lambda hub, s, cid, card, region, since: linkedin.linkedin_proposals(
         cid, card, str(getattr(s, "enrich_linkedin_file", "") or "")) if getattr(s, "enrich_linkedin_file", "")
     else []),
    ("calendar invitation", "enrich_calendar",
     lambda hub, s, cid, card, region, since: invitations.calendar_proposals(hub, cid, card, since=since)),
)


def _local_sources(src: Counter, hub: Any, settings: Any, cid: int, card: Any, region: str,
                   since: Any) -> None:
    """Contacts each section-5 source would offer something to. A source that fails on this read-only
    index is counted as failing, not skipped silently - a zero must mean "found nothing"."""
    for name, switch, call in _LOCAL:
        if settings is not None and not getattr(settings, switch, True):
            continue
        try:
            if call(hub, settings, cid, card, region, since):
                src[name] += 1
        except Exception:                                   # noqa: BLE001 - measured, not hidden
            src[f"{name}: could not be read"] += 1


def _count(fun: Funnel, cid: int, card: Any, props: list, blocks: dict, keep_samples: bool) -> None:
    """Count each proposal by field; for 1b, keep it with the block it was read from - its own `block`,
    which is its receipt's message's (9b: the sample once showed another message's block)."""
    for p in props:
        fun.fields[p.field_kind] += 1
        if keep_samples:
            fun.samples.append((cid, card.get("fn") or card.get("given"), p, p.block or blocks.get(id(p), [])))


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
