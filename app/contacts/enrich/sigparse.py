"""Order 1k 7a, rebuilt by order 1s 3d-3g: read one signature block into values.

Layer: L7

Pure: a block's lines in, a `Signature` out. The rules stay deliberately cautious -
a wrong title offered is worse than none, because "Accept all" keeps it and it
syncs to a phone (order 1s, "precision before reach"):

* a phone must parse as a real number, read in the block's own country (3d: a `+`,
  a country named in the block, the domain's country code, then the owner's
  region); a number read in another country than the owner's is kept in
  international form, so the card cannot later read it as a local one; fax never;
* a title needs a title word and must sit within two lines of the name line (3f) -
  no name line, no title;
* a company needs a legal suffix, agreement with the sender's domain, or the same
  company already on another contact at that domain (3f); a department line goes
  to `department`;
* a sentence (a verb and a full stop) is never a title or a company;
* an address needs a postcode or ZIP, a PO Box with a city or country, or a
  building or street line followed by a recognised city or country (3e);
* a website is a web address that is not social, tracking or an email's domain;
* another address of the same person (3g) shares their local part or their name.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence
from urllib.parse import urlsplit

from app.contacts import phone as phones
from app.contacts.enrich import sigplaces
from app.contacts.enrich.sigblock import holds_name
from app.contacts.enrich.sigtail import is_tracking

__all__ = ["Signature", "parse_block", "domain_label", "flat"]

_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?:https?://[^\s<>\"')]+|\bwww\.[^\s<>\"')]+)", re.I)
_NUM = r"(?P<num>\+?[\d(][\d\s().\-]{5,}\d)"
_LABELS = (r"tel(?:ephone)?|t|phone|ph|mobile|mob|m|cell|gsm|direct(?: line| dial)?|ddi|dl|d|office|o|work|w|"
           r"landline|switchboard|fax|f")
_PHONE_LABEL = re.compile(rf"^\s*(?P<label>{_LABELS})\.?\s*(?:\([^)]{{0,12}}\))?\s*[:.\-/]?\s*{_NUM}\s*$", re.I)
_PHONE_ONLY = re.compile(r"^\s*\+?[\d(][\d\s().\-]{6,}\d\s*$")
_LABELLED_ANYWHERE = re.compile(
    rf"\b(?P<label>tel(?:ephone)?|phone|mobile|mob|cell|gsm|direct|ddi|office|fax|[tmdfowp])\s*[:.]\s*{_NUM}", re.I)
_UK_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b")
_US_ZIP = re.compile(r"\b([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b")
_PO_BOX = re.compile(r"\b(?:p\.?\s*o\.?\s*box|post\s+box|postbox)\s*[:#.]?\s*\d{1,7}\b", re.I)
_STREET = re.compile(r"\b(?:street|st\.?|road|rd\.?|avenue|ave\.?|boulevard|blvd|lane|way|drive|square|"
                     r"tower|towers|building|bldg|floor|level|suite|office|plaza|centre|center|house|park|"
                     r"complex|block|plot|district|area|zone|industrial|estate|business bay|marina|corniche|"
                     r"straat|laan|weg|plein|strasse|stra\u00dfe|rue|avenida|calle)\b", re.I)
_COMPANY_SUFFIX = re.compile(
    r"\b(?:ltd|limited|llc|l\.l\.c|llp|inc|incorporated|plc|gmbh|corp|corporation|ag|bv|b\.v|nv|n\.v|sa|s\.a|"
    r"sarl|sas|spa|srl|pty|co\.|company|group|holdings|partners|solutions|associates|w\.?l\.?l|fze|fzco|fz-llc|"
    r"pjsc|psc|spc|saog|saoc|ksc|kscc|qsc|qpsc|bsc|jsc|pvt|est|establishment|ab|oy|as|a/s|kg)\.?\s*$", re.I)
_TITLE_WORDS = re.compile(
    r"\b(?:director|manager|engineer|head|officer|ceo|cto|cfo|coo|cio|partner|consultant|analyst|associate|"
    r"president|vice|vp|lead|specialist|administrator|assistant|executive|owner|founder|co-founder|developer|"
    r"designer|architect|accountant|solicitor|lawyer|teacher|nurse|doctor|professor|supervisor|coordinator|"
    r"secretary|principal|chair|chairman|technician|advisor|adviser|controller|recruiter|surveyor|"
    r"representative|agent|buyer|planner|editor|producer|strategist|scientist|researcher|lecturer|"
    r"practitioner|therapist|dentist|surgeon|mgr|sr\.?|jr\.?|superintendent|foreman|inspector|expert)\b", re.I)
_DEPARTMENT = re.compile(r"\b(?:department|dept\.?|division|business unit|directorate|section|team|"
                         r"group functions?|function)\b", re.I)
#: Words that make a line a sentence someone wrote, not a label in a signature (3f).
_VERBS = re.compile(r"\b(?:is|are|am|was|were|be|been|will|would|shall|should|can|could|may|might|must|have|has|"
                    r"had|do|does|did|please|thank|thanks|let|see|find|attached|call|send|sent|hope|look|"
                    r"contact|reply|note|kindly|regret|apologi[sz]e)\b", re.I)
_SOCIAL_HOSTS = ("linkedin.", "facebook.", "twitter.", "x.com", "instagram.", "youtube.", "youtu.be", "tiktok.",
                 "wa.me", "t.me", "github.com")
_ADDRESS_STOP = re.compile(r"[@]|^\s*(?:tel|t|m|mob|phone|fax|www|http|email|e)\b\s*[:.]", re.I)
_NAME_SPLIT = re.compile(r"\s+(?:\||-|\u2013|\u2014|,|/)\s+|\s*\|\s*")
_GENERIC_LOCAL = {"info", "sales", "admin", "office", "hello", "contact", "enquiries", "support", "accounts",
                  "mail", "team", "hr", "careers", "noreply", "no-reply"}


class Signature(dict):
    """What was read from one block: `phone` (list of (label, number)), `title`, `company`, `department`,
    `address` (dict of parts) with `address_text`, `website`, `emails`. Only keys that were found;
    `name_matched` says the block held the person's name line."""


def flat(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def domain_label(email: str) -> str:
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


def _phone_label(label: str) -> str:
    label = label.lower()
    if label in ("m", "mob", "mobile", "cell", "gsm"):
        return "mobile"
    if label in ("d", "dl", "ddi") or label.startswith("direct"):
        return "direct"
    return "work"


def _number(raw: str, region: str, owner_region: str) -> str:
    """The number as typed when it reads the same anywhere; in international form when only `region` makes
    sense of it and that is not the owner's own (so the card cannot later read it as a local number)."""
    text = re.sub(r"\s+", " ", raw).strip(" .-")
    if not phones.normalise(text, region):
        return ""
    if text.startswith("+") or region == owner_region:
        return text
    return phones.format_number(text, "international", region) or text


def _sentence(line: str) -> bool:
    """3f: a line somebody wrote, not a label - more than nine words (as 7a had it), or a verb and a full stop.
    "Acme Widgets Ltd." ends in a full stop too; it has no verb."""
    return len(line.split()) > 9 or (line.rstrip().endswith(".") and bool(_VERBS.search(line)))


def parse_block(block: Sequence[str], *, name_tokens: Sequence[str], sender_email: str = "",
                region: str = phones.DEFAULT_REGION, own_emails: Iterable[str] = (),
                domain_companies: Iterable[str] = ()) -> Signature:
    """Read one signature block. Pure: text in, a `Signature` out.

    `region` is the owner's (the last guess for a number, 3d); `own_emails` the contact's addresses (not
    offered again, 3g); `domain_companies` the companies already on other contacts at the sender's domain
    (3f)."""
    sig = Signature()
    block = [ln.strip() for ln in block]
    if not any(block):
        return sig
    used: set[int] = set()

    name_at: Optional[int] = None
    for i, ln in enumerate(block[:6]):
        if holds_name(ln, name_tokens) and not _PHONE_ONLY.match(ln):
            name_at = i
            used.add(i)
            break
    if name_at is not None:
        sig["name_matched"] = "1"

    rest_lines = [ln for i, ln in enumerate(block) if i != name_at]
    num_region = sigplaces.region_for(rest_lines, sender_email, region)
    _read_phones(block, sig, used, num_region, region)
    _read_website(block, sig, used)
    _read_emails(block, sig, used, name_tokens, sender_email, own_emails)
    _read_address(block, sig, used, sender_email)
    _read_title_company(block, sig, used, name_at, sender_email, domain_companies)
    return sig


def _read_phones(block: list[str], sig: Signature, used: set[int], region: str, owner_region: str) -> None:
    found: list[tuple[str, str]] = []
    for i, ln in enumerate(block):
        m = _PHONE_LABEL.match(ln)
        if m:
            label = m.group("label").lower()
            used.add(i)
            num = _number(m.group("num"), region, owner_region)
            if num and label not in ("f", "fax"):
                found.append((_phone_label(label), num))
            continue
        if _PHONE_ONLY.match(ln):
            num = _number(ln, region, owner_region)
            if num:
                found.append(("work", num))
                used.add(i)
            continue
        for m2 in _LABELLED_ANYWHERE.finditer(ln):       # "T: 020 ... | M: 07..."
            label = m2.group("label").lower()
            num = _number(m2.group("num"), region, owner_region)
            used.add(i)
            if num and label not in ("f", "fax"):
                found.append((_phone_label(label), num))
    uniq: list[tuple[str, str]] = []
    for label, num in found:
        if all(phones.match_key(num, region) != phones.match_key(n, region) for _, n in uniq):
            uniq.append((label, num))
    if uniq:
        sig["phone"] = uniq[:3]


def _read_website(block: list[str], sig: Signature, used: set[int]) -> None:
    for i, ln in enumerate(block):
        scrubbed = _EMAIL.sub(" ", ln)
        for m in _URL.finditer(scrubbed):
            url = m.group(0).rstrip(".,;:")
            low = url.lower()
            used.add(i)
            if any(s in low for s in _SOCIAL_HOSTS) or is_tracking(url):
                continue
            full = url if low.startswith("http") else "https://" + url
            if "." in (urlsplit(full).hostname or ""):
                sig.setdefault("website", full)


def _read_emails(block: list[str], sig: Signature, used: set[int], tokens: Sequence[str], sender: str,
                 own: Iterable[str]) -> None:
    """3g: another address of this person - their local part at another domain, or one holding their name."""
    have = {e.lower() for e in own} | ({sender.lower()} if sender else set())
    locals_ = {e.split("@", 1)[0] for e in have if "@" in e}
    out: list[str] = []
    for i, ln in enumerate(block):
        for m in _EMAIL.finditer(ln):
            used.add(i)
            addr = m.group(0).lower().strip(".")
            local = addr.split("@", 1)[0]
            if addr in have or addr in out or local in _GENERIC_LOCAL:
                continue
            parts = set(re.split(r"[._+-]", local))
            if local in locals_ or any(len(t) >= 3 and t in parts for t in tokens):
                out.append(addr)
    if out:
        sig["emails"] = out


def _read_address(block: list[str], sig: Signature, used: set[int], sender: str) -> None:
    for i, ln in enumerate(block):
        if i in used or _ADDRESS_STOP.search(ln):
            continue
        pc = _UK_POSTCODE.search(ln.upper())
        zp = _US_ZIP.search(ln)
        if pc or zp:
            _postcode_address(block, sig, used, i, pc, zp)
            return
    for i, ln in enumerate(block):
        if i in used or _ADDRESS_STOP.search(ln) or (_TITLE_WORDS.search(ln) and not _STREET.search(ln)):
            continue
        if _COMPANY_SUFFIX.search(ln) and not any(c.isdigit() for c in ln):
            continue                                       # "Park House Holdings Ltd" is a company, not a street
        if _PO_BOX.search(ln) or _STREET.search(ln):
            if _place_address(block, sig, used, i, sender):
                return


def _postcode_address(block, sig, used, i, pc, zp) -> None:
    ln = block[i]
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
    sig["address_text"] = ", ".join([*rest, *([data["postcode"]] if data.get("postcode") else [])])
    used.update(range(j + 1, i + 1))


def _place_address(block: list[str], sig: Signature, used: set[int], i: int, sender: str) -> bool:
    """3e without a postcode: a PO Box or a building/street line, with a recognised city or country on the
    same line (after it) or the next one. The country is the line's, else the sender's domain's."""
    ln = block[i]
    nxt = block[i + 1] if i + 1 < len(block) and (i + 1) not in used else ""
    pobox = _PO_BOX.search(ln)
    head = ln[:pobox.end()] if pobox else ln
    tail_of_line = ln[pobox.end():] if pobox else ""
    place_text = ", ".join(x for x in (tail_of_line.strip(" ,-"), nxt) if x)
    city = sigplaces.city_in(place_text)
    country_code = sigplaces.country_in(place_text)
    if not (city or country_code):
        return False
    code = country_code or sigplaces.domain_country(sender)
    data = {"street": (pobox.group(0) if pobox else head).strip(" ,")}
    if city:
        data["city"] = city
    if code:
        data["country"] = sigplaces.COUNTRY_NAME.get(code, code)
    sig["address"] = data
    sig["address_text"] = ", ".join(v for v in (data["street"], data.get("city", ""), data.get("country", "")) if v)
    used.add(i)
    if nxt and (sigplaces.city_in(nxt) or sigplaces.country_in(nxt)):
        used.add(i + 1)
    return True


def _read_title_company(block: list[str], sig: Signature, used: set[int], name_at: Optional[int], sender: str,
                        domain_companies: Iterable[str]) -> None:
    own = domain_label(sender) if sender else ""
    known = {flat(c) for c in domain_companies if flat(c)}
    first = (name_at + 1) if name_at is not None else 0
    cands: list[tuple[int, str]] = []
    if name_at is not None:                                # "Ann Lee | Senior Engineer"
        parts = _NAME_SPLIT.split(block[name_at], maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            cands.append((name_at, parts[1].strip()))
    cands += [(i, block[i]) for i in range(first, min(first + 4, len(block))) if block[i] and i not in used]
    for i, ln in cands:
        if "@" in ln or _URL.search(ln) or len(ln) > 70 or _sentence(ln):
            continue
        digits = any(c.isdigit() for c in ln)
        near_name = name_at is not None and i - name_at <= 2
        if "title" not in sig and near_name and _TITLE_WORDS.search(ln) and not _COMPANY_SUFFIX.search(ln) \
                and not digits:
            sig["title"] = ln.strip(" ,|-")
            continue
        if "department" not in sig and _DEPARTMENT.search(ln) and not _TITLE_WORDS.search(ln) and not digits \
                and not _COMPANY_SUFFIX.search(ln):
            sig["department"] = ln.strip(" ,|-")
            continue
        if "company" not in sig and not digits and not _TITLE_WORDS.search(ln):
            flat_ln = flat(ln)
            agrees = bool(own) and len(own) >= 3 and (own in flat_ln or (len(flat_ln) >= 4 and flat_ln in own))
            if _COMPANY_SUFFIX.search(ln) or agrees or flat_ln in known:
                sig["company"] = ln.strip(" ,|-")
