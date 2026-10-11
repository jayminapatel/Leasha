"""7a: read a person's newest email signature for phone, title, company, address, website.

Layer: L7

Order 1k section 7a. Local only: nothing here touches the network. The newest
messages *from* the person (`messages.sender` matches one of the contact's
emails) are read from the index, and the block under the sign-off is searched.

**What the index holds.** When a message is indexed, `app.extract.quoting` cuts
it at a `-- ` line, "Sent from my ..." and the confidentiality notices, so a
signature written below one of those is not in `chunks` and cannot be read here.
A signature under "Kind regards" (what Outlook writes) is. Reading the original
message again would need the file; this module does not do that.

The rules are deliberately cautious - a wrong title offered is worse than none:

* a phone number must parse as a real number (`phonenumbers`, via `app.contacts.phone`);
* a title needs a title word (manager, director, engineer ...);
* a company needs a legal suffix (Ltd, Inc, GmbH ...) or must agree with the
  sender's own email domain;
* an address needs a postcode or ZIP;
* a website is any web address that is not a social site, a tracking link or an
  email address's domain.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlsplit

from app.contacts import phone as phones
from app.contacts.card import Card
from app.contacts.enrich.base import (Proposal, address_of, body_text, emails_of, format_day, mail_from)

__all__ = ["Signature", "find_block", "parse_block", "signature_proposals"]

#: How many of the newest messages are tried before giving up.
MAX_MESSAGES = 20

_SIGN_OFF = re.compile(
    r"^\s*(?:(?:best|kind|warm|warmest|many|with)\s+)?(?:regards|wishes|thanks|thank you|cheers|sincerely|"
    r"yours(?:\s+(?:sincerely|faithfully|truly))?|best|rgds|regds|cordially|respectfully|all the best|"
    r"take care|many thanks|with thanks|thanks and regards|thanks & regards)\s*[,.!]?\s*$", re.I)
_DELIMITER = re.compile(r"^--\s?$")

_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?:https?://[^\s<>\"')]+|\bwww\.[^\s<>\"')]+)", re.I)
_PHONE_LABEL = re.compile(r"^\s*(?P<label>tel(?:ephone)?|t|phone|ph|mobile|mob|m|cell|direct(?: line| dial)?|ddi|"
                          r"d|office|o|work|w|fax|f)\s*[:.\-/]?\s*(?P<num>\+?[\d(][\d\s().\-]{5,}\d)\s*$", re.I)
_PHONE_ONLY = re.compile(r"^\s*\+?[\d(][\d\s().\-]{6,}\d\s*$")
_LABELLED_ANYWHERE = re.compile(
    r"\b(?P<label>tel(?:ephone)?|phone|mobile|mob|cell|direct|ddi|office|fax|[tmdfowp])\s*[:.]\s*"
    r"(?P<num>\+?[\d(][\d\s().\-]{5,}\d)", re.I)
_UK_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b")
_US_ZIP = re.compile(r"\b([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b")
_COMPANY_SUFFIX = re.compile(r"\b(?:ltd|limited|llc|llp|inc|incorporated|plc|gmbh|corp|corporation|ag|bv|nv|"
                             r"sa|sarl|pty|co\.|company|group|holdings|partners|solutions|associates)\.?\s*$", re.I)
_TITLE_WORDS = re.compile(
    r"\b(?:director|manager|engineer|head|officer|ceo|cto|cfo|coo|cio|partner|consultant|analyst|associate|"
    r"president|vice|vp|lead|specialist|administrator|assistant|executive|owner|founder|co-founder|developer|"
    r"designer|architect|accountant|solicitor|lawyer|teacher|nurse|doctor|professor|supervisor|coordinator|"
    r"secretary|principal|chair|chairman|technician|advisor|adviser|controller|recruiter|surveyor|"
    r"representative|agent|buyer|planner|editor|producer|strategist|scientist|researcher|lecturer|"
    r"practitioner|therapist|dentist|surgeon|manager|mgr|sr\.?|jr\.?)\b", re.I)
_SOCIAL_HOSTS = ("linkedin.", "facebook.", "twitter.", "x.com", "instagram.", "youtube.", "youtu.be", "tiktok.",
                 "wa.me", "t.me", "github.com")
_TRACKING = ("safelinks.protection", "unsubscribe", "click.", "track.", "/redirect", "mailchi.mp", "list-manage")
_ADDRESS_STOP = re.compile(r"[@]|^\s*(?:tel|t|m|mob|phone|fax|www|http|email|e)\b\s*[:.]", re.I)


class Signature(dict):
    """What was read from one message: `phone` (list of (label, number)), `title`,
    `company`, `address` (dict of parts), `website`. Only keys that were found."""


def find_block(body: str) -> list[str]:
    """The lines under the sign-off (or under a `-- ` line), without blank edges.

    Empty when the message has no recognisable sign-off. The block is the text
    after the *last* sign-off in the final 25 lines, capped at 14 lines.
    """
    lines = [ln.rstrip() for ln in (body or "").splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    tail_start = max(0, len(lines) - 25)
    start: Optional[int] = None
    for i in range(len(lines) - 1, tail_start - 1, -1):
        if _DELIMITER.match(lines[i]) or _SIGN_OFF.match(lines[i]):
            start = i + 1
            break
    if start is None:
        return []
    block = [ln.strip() for ln in lines[start:start + 14]]
    while block and not block[0]:
        block.pop(0)
    return block


def _name_tokens(card: Card, sender_name: str = "") -> list[str]:
    toks: list[str] = []
    for key in ("given", "family"):
        for t in re.split(r"\s+", card.get(key).strip().lower()):
            if len(t) > 1:
                toks.append(t)
    if not toks and sender_name:
        toks = [t for t in re.split(r"[\s,]+", sender_name.lower()) if len(t) > 1]
    return toks


def _clean_phone(raw: str, region: str) -> str:
    text = re.sub(r"\s+", " ", raw).strip(" .-")
    return text if phones.normalise(text, region) else ""


def _domain_label(email: str) -> str:
    """`jane@mail.acme-widgets.co.uk` -> `acmewidgets`: letters only, for comparing names."""
    dom = email.rsplit("@", 1)[-1].lower()
    labels = [x for x in dom.split(".") if x]
    sld = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc"}
    if len(labels) >= 3 and labels[-2] in sld:
        base = labels[-3]
    elif len(labels) >= 2:
        base = labels[-2]
    else:
        base = dom
    return re.sub(r"[^a-z0-9]", "", base)


def parse_block(block: list[str], *, name_tokens: list[str], sender_email: str = "",
                region: str = phones.DEFAULT_REGION) -> Signature:
    """Read one signature block. Pure: text in, a `Signature` out."""
    sig = Signature()
    if not block:
        return sig
    used: set[int] = set()

    # name line: the first line holding a token of the person's name
    name_at: Optional[int] = None
    for i, ln in enumerate(block[:6]):
        low = ln.lower()
        if name_tokens and any(re.search(rf"\b{re.escape(t)}\b", low) for t in name_tokens) \
                and "@" not in ln and not _PHONE_ONLY.match(ln):
            name_at = i
            used.add(i)
            break
    if name_at is not None:
        sig["name_matched"] = "1"

    # phones
    found: list[tuple[str, str]] = []
    for i, ln in enumerate(block):
        m = _PHONE_LABEL.match(ln)
        if m:
            label = m.group("label").lower()
            num = _clean_phone(m.group("num"), region)
            if num and label not in ("f", "fax"):
                found.append((_phone_label(label), num))
                used.add(i)
            elif label in ("f", "fax"):
                used.add(i)
            continue
        if _PHONE_ONLY.match(ln):
            num = _clean_phone(ln, region)
            if num:
                found.append(("work", num))
                used.add(i)
            continue
        for m2 in _LABELLED_ANYWHERE.finditer(ln):       # "T: 020 ... | M: 07..."
            label = m2.group("label").lower()
            num = _clean_phone(m2.group("num"), region)
            if num and label not in ("f", "fax"):
                found.append((_phone_label(label), num))
                used.add(i)
    uniq: list[tuple[str, str]] = []
    for label, num in found:
        if all(phones.match_key(num, region) != phones.match_key(n, region) for _, n in uniq):
            uniq.append((label, num))
    if uniq:
        sig["phone"] = uniq[:3]

    # website: the first web address that is not social, tracking or part of an email address
    for i, ln in enumerate(block):
        scrubbed = _EMAIL.sub(" ", ln)
        for m3 in _URL.finditer(scrubbed):
            url = m3.group(0).rstrip(".,;:")
            low = url.lower()
            if any(s in low for s in _SOCIAL_HOSTS) or any(t in low for t in _TRACKING):
                used.add(i)
                continue
            full = url if low.startswith("http") else "https://" + url
            host = (urlsplit(full).hostname or "")
            if "." in host:
                sig.setdefault("website", full)
                used.add(i)

    # address: a line with a postcode / ZIP, and the street line(s) above it
    for i, ln in enumerate(block):
        if i in used or _ADDRESS_STOP.search(ln):
            continue
        pc = _UK_POSTCODE.search(ln.upper())
        zp = _US_ZIP.search(ln)
        if not (pc or zp):
            continue
        street_lines = []
        j = i - 1
        while j >= 0 and len(street_lines) < 2 and j not in used and block[j] and not _ADDRESS_STOP.search(block[j]) \
                and not _TITLE_WORDS.search(block[j]) and not _COMPANY_SUFFIX.search(block[j]):
            street_lines.insert(0, block[j])
            j -= 1
        data: dict[str, str] = {}
        if pc:
            data["postcode"] = re.sub(r"\s+", " ", pc.group(1)).strip()
            remainder = re.sub(_UK_POSTCODE.pattern, "", ln, flags=re.I)
        else:
            data["region"], data["postcode"] = zp.group(1), zp.group(2)
            remainder = ln.replace(zp.group(0), "")
        rest = [p.strip() for p in [*street_lines, *remainder.split(",")] if p.strip(" ,.")]
        if rest:
            data["street"] = ", ".join(rest[:-1]) if len(rest) >= 3 else rest[0]
            if len(rest) >= 2:
                data["city"] = rest[-1]
        sig["address"] = data
        sig["address_text"] = ", ".join([*rest, *( [data["postcode"]] if data.get("postcode") else [])])
        used.update(range(j + 1, i + 1))
        break

    # title and company: the lines right after the name line (or at the top of the block)
    first = (name_at + 1) if name_at is not None else 0
    candidates = [(i, block[i]) for i in range(first, min(first + 4, len(block)))
                  if block[i] and i not in used]
    own = _domain_label(sender_email) if sender_email else ""
    for i, ln in candidates:
        if "@" in ln or _URL.search(ln) or len(ln) > 70 or len(ln.split()) > 9:
            continue
        if "title" not in sig and _TITLE_WORDS.search(ln) and not _COMPANY_SUFFIX.search(ln) and not any(c.isdigit() for c in ln):
            sig["title"] = ln.strip(" ,|-")
            continue
        if "company" not in sig and not any(c.isdigit() for c in ln):
            flat_ln = re.sub(r"[^a-z0-9]", "", ln.lower())
            agrees = bool(own) and len(own) >= 3 and (own in flat_ln or (len(flat_ln) >= 4 and flat_ln in own))
            if (_COMPANY_SUFFIX.search(ln) or agrees) and not _TITLE_WORDS.search(ln):
                sig["company"] = ln.strip(" ,|-")
    return sig


def _phone_label(label: str) -> str:
    label = label.lower()
    if label in ("m", "mob", "mobile", "cell"):
        return "mobile"
    return "work"


def signature_proposals(store: Any, cid: int, card: Card, *, region: str = phones.DEFAULT_REGION
                        ) -> list[Proposal]:
    """Proposals from the newest message of this person that has a readable signature."""
    emails = emails_of(card)
    if not emails:
        return []
    rows = mail_from(store, emails, limit=MAX_MESSAGES)
    tokens_card = _name_tokens(card)
    parsed: list[tuple[Any, Signature]] = []
    for row in rows:
        sender_email = address_of(row.sender)
        tokens = tokens_card or _name_tokens(card, _display(row.sender))
        sig = parse_block(find_block(body_text(store, row.file_id)), name_tokens=tokens,
                          sender_email=sender_email, region=region)
        meaningful = {k for k in sig if k not in ("name_matched",)}
        if meaningful:
            parsed.append((row, sig))
        if len(parsed) >= 3:
            break
    if not parsed:
        return []
    row, sig = parsed[0]
    older = [s for _, s in parsed[1:]]
    receipt = f"{(row.subject or '(no subject)')[:80]} - {format_day(row.sent_at)}".rstrip(" -")
    base = 0.6 + (0.2 if sig.get("name_matched") else 0.0)

    def conf(key: str, value: Any) -> float:
        return round(min(0.95, base + (0.15 if any(o.get(key) == value for o in older) else 0.0)), 2)

    out: list[Proposal] = []
    have_phones = {phones.match_key(f.value, region) for f in card.values("phone")}
    for label, num in sig.get("phone", []):
        if phones.match_key(num, region) in have_phones:
            continue
        out.append(Proposal(cid, "phone", label, "", num, "signature", receipt,
                            conf("phone", sig.get("phone"))))
    for key in ("title", "company"):
        new = sig.get(key)
        if new and new.strip().casefold() != card.get(key).strip().casefold():
            out.append(Proposal(cid, key, "", card.get(key), new, "signature", receipt,
                                round(conf(key, new) - (0.1 if card.get(key) else 0.0), 2)))
    site = sig.get("website")
    if site:
        norm = lambda u: re.sub(r"^https?://(www\.)?", "", u.lower()).rstrip("/")   # noqa: E731
        if all(norm(f.value) != norm(site) for f in card.values("url")):
            out.append(Proposal(cid, "url", "work", "", site, "signature", receipt, conf("website", site)))
    addr = sig.get("address")
    if addr:
        text = sig["address_text"]
        have = {re.sub(r"\W", "", (f.data.get("postcode") or "").lower()) for f in card.values("address")}
        if re.sub(r"\W", "", addr.get("postcode", "").lower()) not in have:
            out.append(Proposal(cid, "address", "work", "", text, "signature", receipt,
                                round(base - 0.1, 2), data=dict(addr)))
    return out


def _display(sender: str) -> str:
    from email.utils import parseaddr  # noqa: PLC0415
    return parseaddr(sender or "")[0]
