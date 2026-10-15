"""Corrections: values on a card that were plainly misread, offered as fixes in the same review.

Layer: L7

Owner, 2026-10-15: misread values "need to be part of review for enrichment". Found on the owner's book:
addresses at `schneider-elecmc.com`, `schneidef-electric.com` and `schneiderwlectric.com` beside some
eighty contacts at `schneider-electric.com`, and web addresses such as
`http://www.schneider-electric.com.saicustomercarecentertoll` - a real site with words run on after its
`.com`. Local only: this reads the contact book and, for one guard, the senders of the indexed mail.

**What it never does.** It writes nothing: every proposal is an update (`update=True`, `old` the value
now, `new` the corrected one), so the review never ticks it by itself (3k, D4) and nothing changes until
the person accepts it. It never touches a name - the owner, the same day: do not change surnames; this
source reads only email and web addresses.

**An email address at a near-miss of a common domain.** All of these must hold:

* the odd domain is rare in the book - on at most `RARE_MAX` contacts;
* another domain is common - on at least `COMMON_MIN` contacts, and at least `RATIO` times as many as
  the odd one;
* the two have the same public ending (`company.split_suffix`: `.com`, `.co.uk`) and what comes before it
  is within `allowed_edits` of each other (Damerau-Levenshtein: a letter added, dropped, changed or two
  swapped). The allowance grows with the name: none under five letters (`abb.com` and `abc.com` are two
  companies), one up to seven, two up to fourteen, three beyond - `schneider-elecmc` is three edits from
  `schneider-electric`, and a long name three letters wrong is still plainly that name, where two
  edits alone would have missed one of the owner's three cases;
* no indexed message was sent from the odd domain: a domain mail came from is real, however close its
  spelling (a misreading copies an address out of text; it never sends mail).

The offer is the same address at the common domain. **When the card already holds that address**, the
proposal is the same shape - `old` the misread address, `new` the right one - and accepting it removes
the misread one: `base._replace` drops the field holding `old` when `new` is already on the card. So a
removal needs no empty value to show, store or turn down; the receipt says that is what it does.

**A web address with words run on after a real domain.** The host's last label is no web ending
(`sigparse.known_tld` knows none of it, it is at least `JUNK_MIN` letters - every real ending that long
that could follow `.com` is in that list - and it is not two letters, which could be a country) and an
earlier label is one (`www.schneider-electric.com` . `saicustomercarecentertoll`). The offer is the
address cut after that label, scheme and path kept. Its `opens` is the corrected address, so the person
can see the site is there before accepting; an email correction opens nothing (`mailto:` is not a place).

**Book-wide counts once a run.** `domain_counts` is one query over `contact_fields`; `enrich_selection`
keeps it for the whole run (the `_book` memo in `run.enrich_contact`), as it keeps the owner's details.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from app.contacts.card import Card, Field, field_key
from app.contacts.enrich.base import Proposal
from app.contacts.enrich.company import split_suffix
from app.contacts.enrich.logo import usable_domain
from app.contacts.enrich.sigparse import _GENERIC_TLDS, known_tld

__all__ = ["correction_proposals", "domain_counts", "near_miss", "trimmed_url", "allowed_edits",
           "edit_distance", "RARE_MAX", "COMMON_MIN", "RATIO", "JUNK_MIN", "CONFIDENCE"]

#: The thresholds (rule 11: constants, not settings - nobody tunes a misreading). What would change them:
#: a measured book where a real company's domain is offered as a misreading of another's.
RARE_MAX = 2
#: Never the deciding test while RATIO is 10 (ten times one contact is already ten); kept as the floor the
#: owner named, so a lower RATIO cannot make a domain on three contacts "common".
COMMON_MIN = 5
RATIO = 10
#: The shortest run-on taken for junk rather than an ending the reader does not know.
JUNK_MIN = 5
#: Shown by default (at or above "Hide suggestions below", 0.5) and never ticked: it is an update. An email
#: one edit out is surer than one three out.
CONFIDENCE = {1: 0.75, 2: 0.7, 3: 0.65}
URL_CONFIDENCE = 0.7


def edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment): insertions, deletions, substitutions, swaps."""
    if a == b:
        return 0
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def allowed_edits(name: str) -> int:
    """How far a misreading of `name` (the common domain, before its ending) may be (module docstring)."""
    n = len(name)
    return 0 if n < 5 else 1 if n <= 7 else 2 if n <= 14 else 3


def domain_counts(store: Any) -> Counter:
    """domain -> how many live contacts have an address there. One query over the whole book."""
    out: Counter = Counter()
    try:
        rows = store.conn.execute(
            "SELECT lower(trim(substr(f.value, instr(f.value, '@') + 1))) AS d, COUNT(DISTINCT f.contact_id) "
            "FROM contact_fields f JOIN contacts c ON c.id = f.contact_id "
            "WHERE f.kind = 'email' AND c.deleted_at IS NULL AND instr(f.value, '@') > 0 GROUP BY d").fetchall()
    except Exception:                                    # noqa: BLE001 - a store without the contact tables
        return out
    for d, n in rows:
        if d:
            out[str(d)] += int(n)
    return out


def _mail_from(store: Any, domain: str) -> bool:
    """Has any indexed message come from an address at `domain`? Then it is a real domain."""
    try:
        return store.conn.execute("SELECT 1 FROM messages WHERE instr(lower(sender), ?) > 0 LIMIT 1",
                                  ("@" + domain,)).fetchone() is not None
    except Exception:                                    # noqa: BLE001 - no mail tables: nothing says it is real
        return False


def near_miss(domain: str, counts: Counter, store: Any = None, memo: Optional[dict] = None
              ) -> Optional[tuple[str, int]]:
    """(the common domain `domain` looks like a misreading of, how many contacts use it), or None.

    `memo` keeps each domain's answer for a run: colleagues share the odd domain, and the mail guard reads
    the messages table."""
    d = (domain or "").strip().lower()
    if memo is not None and d in memo:
        return memo[d]
    found: Optional[tuple[str, int]] = None
    odd = counts.get(d, 0)
    before, suffix = split_suffix(d)
    if d and before and odd <= RARE_MAX:
        best: Optional[tuple[int, int, str]] = None
        for other, n in counts.items():
            if other == d or n < COMMON_MIN or n < RATIO * max(odd, 1):
                continue
            o_before, o_suffix = split_suffix(other)
            if o_suffix != suffix or not o_before:
                continue
            dist = edit_distance(before, o_before)
            if 0 < dist <= allowed_edits(o_before):
                key = (dist, -n, other)
                if best is None or key < best:
                    best = key
        if best is not None and not (store is not None and _mail_from(store, d)):
            found = (best[2], -best[1])
    if memo is not None:
        memo[d] = found
    return found


def trimmed_url(value: str) -> str:
    """`value` cut after its real domain when words are run on after it, else "" (module docstring)."""
    text = (value or "").strip()
    if not text:
        return ""
    has_scheme = "://" in text
    parts = urlsplit(text if has_scheme else "//" + text)
    host = (parts.hostname or "").lower()
    labels = host.split(".")
    last = labels[-1] if labels else ""
    if len(labels) < 3 or known_tld(host) or len(last) < JUNK_MIN or last in _GENERIC_TLDS or not last.isalpha():
        return ""
    for i in range(len(labels) - 2, 0, -1):
        kept = ".".join(labels[:i + 1])
        if known_tld(kept) and usable_domain(kept) and split_suffix(kept)[0]:
            netloc = kept + (f":{parts.port}" if parts.port else "")
            fixed = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
            return fixed if has_scheme else fixed.removeprefix("//")
    return ""


def _opens_of(url: str) -> str:
    return url if "://" in url else "https://" + url


def correction_proposals(store: Any, cid: int, card: Card, counts: Counter, *, region: str = "GB",
                         memo: Optional[dict] = None) -> list[Proposal]:
    """Fixes for this card's misread email and web addresses. Changes nothing; never a name."""
    out: list[Proposal] = []
    have = {field_key(f, region) for f in card.fields}
    for f in card.values("email"):
        addr = f.value.strip()
        if addr.count("@") != 1:
            continue
        local, domain = addr.rsplit("@", 1)
        hit = near_miss(domain, counts, store, memo)
        if hit is None or not local:
            continue
        right, n = hit
        new = f"{local}@{right}"
        dist = edit_distance(split_suffix(domain.lower())[0], split_suffix(right)[0])
        why = (f"{domain.lower()} looks like a misreading of {right}, which {n:,} contact{'s' if n != 1 else ''} "
               f"use")
        if field_key(Field("email", new), region) in have:
            why += "; the card already has the address there, so accepting removes the misread one"
        out.append(Proposal(cid, "email", f.label, addr, new, "correction", why, CONFIDENCE.get(dist, 0.65),
                            update=True))
    for f in card.values("url"):
        fixed = trimmed_url(f.value)
        if not fixed:
            continue
        host = (urlsplit(_opens_of(fixed)).hostname or "").lower()
        run_on = (urlsplit(_opens_of(f.value.strip())).hostname or "").lower()[len(host) + 1:]
        why = f'"{run_on}" is run on after {host.removeprefix("www.")}, and is no web ending'
        if field_key(Field("url", fixed, f.label), region) in have:
            why += "; the card already has the address without it, so accepting removes this one"
        out.append(Proposal(cid, "url", f.label, f.value, fixed, "correction", why, URL_CONFIDENCE, update=True,
                            opens=_opens_of(fixed)))
    return out
