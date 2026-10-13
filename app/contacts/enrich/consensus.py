"""Order 1s 3j/3k: one value per field, agreed across a person's messages - newest wins (D3).

Layer: L7

Pure: readings in (what each message's signature said, newest first), agreed values
out. For each field the newest message's value is the one offered; every other
message that says the same adds confidence; an older message that says something
else becomes history ("was Senior Engineer until March 2026"), shown beside the
proposal and never offered - a person's old title is not a suggestion.

Confidence, as the order sets it: 0.6, +0.2 when the block held the person's name
line, +0.1 for each other message that agrees (at most +0.3), -0.2 when a newer
message with a signature no longer has the field (they may have dropped that number),
-0.15 when the value was only ever seen quoted inside somebody else's message (4c),
and never above 0.97 - a signature is never certain.

**The receipt is a block that holds the value (order 1s 9b).** The first precision
check found a sample row printed beside a block the value was not in. So the reading a
value is offered from - its receipt, and the block the sample prints - is the newest
one (a direct message before a quoted one) whose own block holds that value; a value
that is in none of the person's blocks is not offered at all (`holds_value`).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from app.contacts import phone as phones
from app.contacts.enrich.sigparse import Signature, flat

__all__ = ["Reading", "Agreed", "agree", "slots", "holds_value"]

_NUMBERISH = re.compile(r"\+?\d[\d\s().\-]{5,}\d")

BASE, NAME_LINE, PER_AGREEING, MAX_AGREEING, NEWER_LACKED, QUOTED, CAP = 0.6, 0.2, 0.1, 0.3, 0.2, 0.15, 0.97


@dataclass
class Reading:
    """One message's signature: the message it is in (`row`), when the signature's own message was sent
    (`when`: a quoted message's Sent date, 4c), what was read, and the block it was read from."""

    row: Any
    when: Optional[int]
    sig: Signature
    block: list = field(default_factory=list)
    quoted: bool = False


@dataclass
class Agreed:
    slot: str                      # "title", "phone:mobile", "email:x@y" ...
    value: Any
    data: dict
    reading: Reading               # the receipt: the newest direct message with the value, else the newest
    seen: int
    first_seen: Optional[int]
    confidence: float
    history: str = ""


def _norm_url(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", u.lower()).rstrip("/")


def slots(sig: Signature, region: str) -> dict[str, tuple[str, Any, dict]]:
    """slot -> (comparison key, value, data) for everything one signature said."""
    out: dict[str, tuple[str, Any, dict]] = {}
    for key in ("title", "company", "department"):
        if sig.get(key):
            out[key] = (re.sub(r"\s+", " ", sig[key].casefold()), sig[key], {})
    if sig.get("website"):
        out["website"] = (_norm_url(sig["website"]), sig["website"], {})
    if sig.get("address"):
        out["address"] = (flat(sig["address"].get("postcode", "")) or flat(sig["address_text"]),
                          sig["address_text"], dict(sig["address"]))
    for label, num in sig.get("phone", []):
        slot = f"phone:{label}"
        n = 2
        while slot in out:                                 # two work numbers keep a slot each
            slot, n = f"phone:{label}:{n}", n + 1
        out[slot] = (phones.match_key(num, region), num, {})
    for addr in sig.get("emails", []):
        out[f"email:{addr}"] = (addr, addr, {})
    return out


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def holds_value(slot: str, value: Any, data: dict, block: Sequence[str]) -> bool:
    """Is `value` (of `slot`) written in `block`? Compared the way it was read: letters and digits only (so
    a re-joined "www.x.co m" and an address's commas still match); a phone by its last eight digits (it
    may be offered in international form); an address by its street, city and postcode - its country may
    have come from the domain, not the block."""
    text = "\n".join(block or ())
    if not text.strip():
        return False
    kind = slot.split(":", 1)[0]
    whole = flat(text)
    if kind == "phone":
        want = _digits(str(value))
        return any(_digits(m.group(0))[-8:] == want[-8:] for m in _NUMBERISH.finditer(text)) and len(want) >= 7
    if kind == "website":
        host = re.sub(r"^https?://", "", str(value).lower()).split("/", 1)[0]
        return bool(flat(host)) and flat(host) in whole
    if kind == "address":
        parts = [data.get(k, "") for k in ("street", "city", "postcode")]
        return any(parts) and all(flat(p) in whole for p in parts if p)
    return bool(flat(str(value))) and flat(str(value)) in whole


def _month(epoch_s: Optional[int]) -> str:
    return time.strftime("%B %Y", time.gmtime(int(epoch_s))) if epoch_s else "an undated message"


def agree(readings: Sequence[Reading], region: str) -> list[Agreed]:
    """3j over `readings` (any order; sorted newest first here, undated last)."""
    ordered = sorted(readings, key=lambda r: (r.when is None, -(r.when or 0)))
    said = [slots(r.sig, region) for r in ordered]
    names = {s for d in said for s in d}
    out: list[Agreed] = []
    for slot in sorted(names, key=lambda s: min(i for i, d in enumerate(said) if s in d)):
        obs = [(i, said[i][slot]) for i in range(len(ordered)) if slot in said[i]]
        newest_i, (key, value, data) = obs[0]
        agreeing = [i for i, (k, _, _) in obs if k == key]
        holding = [i for i in agreeing if holds_value(slot, *said[i][slot][1:], ordered[i].block)]
        if not holding:
            continue                                       # in no block of theirs: never offered (9b)
        direct = [i for i in holding if not ordered[i].quoted]
        at = direct[0] if direct else holding[0]
        receipt = ordered[at]
        value, data = said[at][slot][1:]
        dates = [ordered[i].when for i in agreeing if ordered[i].when]
        lacked = any(slot not in said[i] for i in range(newest_i))
        conf = BASE + (NAME_LINE if receipt.sig.get("name_matched") else 0.0) \
            + min(MAX_AGREEING, PER_AGREEING * (len(agreeing) - 1)) - (NEWER_LACKED if lacked else 0.0) \
            - (0.0 if direct else QUOTED)
        history: list[str] = []
        seen_keys = {key}
        for i, (k, v, _) in obs:
            if k not in seen_keys:
                seen_keys.add(k)
                history.append(f"was {v} until {_month(ordered[i].when)}")
        out.append(Agreed(slot, value, dict(data), receipt, len(agreeing), min(dates) if dates else None,
                          round(min(CAP, conf), 2), "; ".join(history[:2])))
    return out
