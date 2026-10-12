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

**What the first precision check changed (order 1s 9b, 18 of 100 wrong).**

* An address is the contiguous run of address lines that *ends* at its postcode,
  city or country line - building, street, PO Box, floor and office lines, at most
  four - so "Level 2 / Gate Village 5 / Dubai" is not cut to "Level 2, Dubai". A
  person's name, title, department or business unit, a company line and legal text
  are never part of it ("... Business Line, 3 Wharf Square" keeps only the street);
  a line that is itself a whole address is never joined to another (the first is
  offered); a no-postcode address needs a number, a street word or a PO Box
  ("Process Solutions Centre, Doha" is a department, not an address); and a value
  with a separator, legal words, an unclosed bracket, a dangling comma or more than
  110 characters is not offered. A US address keeps its state ("Fairview, PA 17025").
* A web address cut by a line wrap ("www.x.co m") is joined again, or dropped; one
  that is the start of the person's own email domain is truncated and dropped. A web
  address unlike every email domain of the person, with no TLD this reader knows, is
  marked unsure and offered only when two messages agree (`website_unsure`).
* A leading country or city glued to a company or department ("Oman, Sohar ...
  Refining") is cut off.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence
from urllib.parse import urlsplit

from app.contacts import phone as phones
from app.contacts.enrich import sigplaces
from app.contacts.enrich.sigblock import holds_name
from app.contacts.enrich.sigtail import LEGAL, is_tracking, split_line

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
_DEPARTMENT = re.compile(r"\b(?:department|dept\.?|division|business unit|business line|business group|"
                         r"directorate|section|team|group functions?|function|operations|practice)\b", re.I)
#: The street words that make a line a street or a building on their own. "Centre", "House", "Park" and the
#: like are building names too, but also departments ("Process Solutions Centre"), so a no-postcode
#: address with only those needs a number or a PO Box as well (9b).
_STRONG_STREET = re.compile(r"\b(?:street|st|road|rd|avenue|ave|boulevard|blvd|lane|drive|tower|towers|building|"
                            r"bldg|floor|level|suite|office|unit|plot|block|villa|straat|laan|weg|plein|strasse|"
                            r"stra\u00dfe|rue|avenida|calle)\b", re.I)
_SEPARATOR_CHAR = re.compile(r"[|\u2022\u00b7\u25aa\u25cf]")
#: 9b: the longest address offered, and the most lines one is read from. Longer is a paragraph.
MAX_ADDRESS = 110
MAX_ADDRESS_LINES = 4
#: Top-level domains a web address may end in when nothing else vouches for it (9b): the generic ones,
#: and every country code `sigplaces` uses as a country. Not ".co" alone - in a signature that is far
#: more often ".com" or ".co.uk" cut by a line wrap than Colombia.
_GENERIC_TLDS = frozenset({"com", "org", "net", "edu", "gov", "int", "mil", "info", "biz", "io", "ai", "app",
                           "dev", "eu", "us", "global", "group", "energy", "tech", "online", "agency", "studio",
                           "consulting", "services", "solutions", "systems", "engineering", "law", "health"})
_US_STATES = frozenset({
                        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
                        "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
                        "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
                        "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
                        "WV", "WI", "WY", "PR"})
_WRAPPED = re.compile(r"\s+(\.?[a-z]{1,3})(?=$|[\s,;|)>\]])")
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
    # 9b: the separators split here too, for a block that did not come through `clean_tail`.
    block = [part for ln in block for part in (split_line(ln.strip()) or [""])]
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
    person_emails = [e for e in (sender_email, *own_emails) if e and "@" in e]
    _read_website(block, sig, used, person_emails)
    _read_emails(block, sig, used, name_tokens, sender_email, own_emails)
    _read_address(block, sig, used, sender_email, name_tokens)
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


def known_tld(host: str) -> bool:
    """`host` ends in a whole top-level domain this reader knows (`_GENERIC_TLDS`, `sigplaces.CCTLD`)."""
    last = (host or "").lower().rstrip(".").rsplit(".", 1)[-1]
    return "." in (host or "") and (last in _GENERIC_TLDS or last in sigplaces.CCTLD)


def _unwrapped(value: str, host: str, after: str) -> Optional[str]:
    """9b: a web or email address the line wrap cut ("www.x.co m", "www.x.co .uk"), joined again; the
    value as it is when nothing follows it that continues its domain; None when it is cut and cannot
    be mended - a truncated domain is never offered."""
    m = _WRAPPED.match(after)
    if m:
        frag = m.group(1).lower()                          # ".uk" adds a label; "m" continues "co" to "com"
        if known_tld(host + frag):
            return value + frag
    return value if known_tld(host) or not m else None


def _host_of(url: str) -> str:
    return (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()


def _read_website(block: list[str], sig: Signature, used: set[int], person_emails: Sequence[str]) -> None:
    domains = [e.rsplit("@", 1)[-1].lower() for e in person_emails]
    for i, ln in enumerate(block):
        scrubbed = _EMAIL.sub(" ", ln)
        for m in _URL.finditer(scrubbed):
            url = m.group(0).rstrip(".,;:")
            low = url.lower()
            used.add(i)
            if any(s in low for s in _SOCIAL_HOSTS) or is_tracking(url):
                continue
            mended = _unwrapped(url, _host_of(url), scrubbed[m.end():]) if url == m.group(0) else url
            if mended is None or "website" in sig:
                continue
            full = mended if mended.lower().startswith("http") else "https://" + mended
            host = _host_of(full)
            bare = host[4:] if host.startswith("www.") else host
            if "." not in host or any(d != bare and d.startswith(bare) for d in domains):
                continue                                   # "kestrelfoods.co" of kestrelfoods.com: cut short
            sig["website"] = full
            shared = any(domain_label("x@" + host) == domain_label(e) for e in person_emails)
            if not (shared or known_tld(host)):
                sig["website_unsure"] = "1"                # offered only when two messages agree (9b)


def _read_emails(block: list[str], sig: Signature, used: set[int], tokens: Sequence[str], sender: str,
                 own: Iterable[str]) -> None:
    """3g: another address of this person - their local part at another domain, or one holding their name."""
    have = {e.lower() for e in own} | ({sender.lower()} if sender else set())
    locals_ = {e.split("@", 1)[0] for e in have if "@" in e}
    out: list[str] = []
    for i, ln in enumerate(block):
        for m in _EMAIL.finditer(ln):
            used.add(i)
            mended = _unwrapped(m.group(0), m.group(0).rsplit("@", 1)[-1], ln[m.end():])
            if mended is None:
                continue                                   # "ann@x.co" cut by a wrap: never offered
            addr = mended.lower().strip(".")
            local = addr.split("@", 1)[0]
            if addr in have or addr in out or local in _GENERIC_LOCAL:
                continue
            parts = set(re.split(r"[._+-]", local))
            if local in locals_ or any(len(t) >= 3 and t in parts for t in tokens):
                out.append(addr)
    if out:
        sig["emails"] = out


def _place_size(part: str) -> tuple[int, int]:
    """(words of the longest city or country named in `part`, letter-words in `part`)."""
    words = [w.strip(".") or w for w in re.findall(r"[a-z][a-z.]*", part.lower())]
    for n in (4, 3, 2, 1):
        for k in range(len(words) - n + 1):
            phrase = " ".join(words[k:k + n])
            if phrase in sigplaces.COUNTRIES or phrase in sigplaces.CITIES:
                return n, len(words)
    return 0, len(words)


def _is_place(part: str) -> bool:
    """`part` is a city or a country, with at most one word more ("Kuwait City", "Muscat 112") - not a
    company or a building that happens to hold one ("Dubai Internet City", "Sohar ... Refining")."""
    n, total = _place_size(part)
    return n > 0 and total - n <= 1


def _parts(text: str) -> list[str]:
    return [p.strip(" .;") for p in text.split(",") if p.strip(" .;")]


def _postcode_in(text: str):
    """(UK postcode match, US ZIP match) in `text`; either may be None. A ZIP needs a real state before it:
    in "P.O. BOX 12345" the "OX" is not a state."""
    zp = _US_ZIP.search(text)
    return _UK_POSTCODE.search(text.upper()), (zp if zp and zp.group(1) in _US_STATES else None)


def _end_kind(text: str) -> str:
    """Can an address end on this line: "postcode" (a postcode or ZIP), "place" (its last part is a city or
    a country), or ""."""
    pc, zp = _postcode_in(text)
    if pc or zp:
        return "postcode"
    parts = _parts(text)
    return "place" if parts and _is_place(parts[-1]) else ""


def _street_like(text: str) -> bool:
    return bool(any(c.isdigit() for c in text) or _STREET.search(text) or _PO_BOX.search(text))


def _complete(text: str) -> bool:
    """A line that is a whole address on its own: a street before its postcode, or before its city/country.
    Two of them are two addresses, never one (9b)."""
    kind = _end_kind(text)
    if kind == "postcode":
        pc, zp = _postcode_in(text)
        before = text[:pc.start() if pc else zp.start()]
        return len(_parts(before)) >= 2 or _street_like(before)
    if kind == "place":
        before = _parts(text)[:-1]
        return bool(before) and any(_street_like(p) for p in before if not _is_place(p))
    return False


def _not_address_part(part: str, tokens: Sequence[str]) -> bool:
    """A part of a line that is never part of an address: a name, a title, a department or business unit,
    a company, legal text, a sentence."""
    digits = any(c.isdigit() for c in part)
    return bool(
        (holds_name(part, tokens) and not digits and not _STRONG_STREET.search(part))
        or (_TITLE_WORDS.search(part) and not _STREET.search(part))
        or _DEPARTMENT.search(part)
        or (_COMPANY_SUFFIX.search(part) and not digits)
        or LEGAL.search(part) or _sentence(part))


def _address_part(line: str, tokens: Sequence[str]) -> Optional[str]:
    """What of `line` may be address: all of it; what follows its last name/title/department/company part
    ("... Services Business Line, 3 Wharf Square," -> "3 Wharf Square,"); or None for nothing."""
    if not line or _ADDRESS_STOP.search(line) or len(line) > MAX_ADDRESS or LEGAL.search(line):
        return None                                        # "Registered Office: 1 Mill Lane, ..." is legal text
    pieces = line.split(",")
    bad = [k for k, p in enumerate(pieces) if p.strip() and _not_address_part(p.strip(), tokens)]
    if not bad:
        return line
    rest = ",".join(pieces[bad[-1] + 1:]).strip()
    return rest if rest.strip(" ,.") else None


def _read_address(block: list[str], sig: Signature, used: set[int], sender: str, tokens: Sequence[str]) -> None:
    """3e, as 9b rebuilt it: the first run of address lines that ends at a postcode, city or country line."""
    n = len(block)
    for i in range(n):
        if i in used:
            continue
        end = _address_part(block[i], tokens)
        if end is None:
            continue
        kind = _end_kind(end)
        if not kind:
            continue
        lines = [*_run_up(block, used, i, tokens), (i, end)]
        lines += _run_down(block, used, i, end, kind, tokens, len(lines))
        data, text = _build(kind, [t for _, t in lines], sender)
        if data and _offerable(kind, data, text):
            sig["address"], sig["address_text"] = data, text
            used.update(k for k, _ in lines)
            return


def _run_up(block: list[str], used: set[int], i: int, tokens: Sequence[str]) -> list[tuple[int, str]]:
    """The address lines above line `i`, top first: building, street, PO Box, floor and office lines, one short
    line between two of them allowed ("Gate Village / DIFC / Dubai"), none past a name, title, department,
    company, a line already read, or a line that is a whole address of its own."""
    got: list[tuple[int, str]] = []
    gap: list[tuple[int, str]] = []
    j = i - 1
    while j >= 0 and len(got) + len(gap) + 1 < MAX_ADDRESS_LINES:
        if j in used or not block[j] or _complete(block[j]) or (_end_kind(block[j]) == "postcode"):
            break
        part = _address_part(block[j], tokens)
        if part is None:
            break
        if _street_like(part) or _end_kind(part):
            got += [*gap, (j, part)]
            gap = []
            if part != block[j]:
                break                                      # the line began with a department: none above it
        elif not gap and len(part.split()) <= 4:
            gap = [(j, part)]
        else:
            break
        j -= 1
    return got[::-1]


def _run_down(block: list[str], used: set[int], i: int, end: str, kind: str, tokens: Sequence[str],
              have: int) -> list[tuple[int, str]]:
    """Below the end line: what continues it - the line after a dangling comma, or a city/country line under a
    postcode or a city ("Dubai" / "United Arab Emirates")."""
    got: list[tuple[int, str]] = []
    last = end
    k = i + 1
    while k < len(block) and have + len(got) < MAX_ADDRESS_LINES + 1 and k not in used:
        nxt = _address_part(block[k], tokens)
        if nxt is None or nxt != block[k] or _complete(nxt):
            break
        places = _parts(nxt)
        if last.rstrip().endswith(",") or (places and all(_is_place(p) for p in places)):
            got.append((k, nxt))
            last = nxt
            k += 1
            continue
        break
    return got


def _build(kind: str, lines: Sequence[str], sender: str) -> tuple[dict, str]:
    parts = [p for ln in lines for p in _parts(ln)]
    if kind == "postcode":
        return _build_postcode(parts)
    code = None
    if parts and _is_place(parts[-1]) and sigplaces.country_in(parts[-1]):
        code = sigplaces.country_in(parts.pop())
    city = parts.pop() if parts and _is_place(parts[-1]) and sigplaces.city_in(parts[-1]) else ""
    if not parts:
        return {}, ""
    code = code or sigplaces.domain_country(sender)
    data = {"street": ", ".join(parts)}
    if city:
        data["city"] = city
    if code:
        data["country"] = sigplaces.COUNTRY_NAME.get(code, code)
    return data, ", ".join(v for v in (data["street"], city, data.get("country", "")) if v)


def _build_postcode(parts: list[str]) -> tuple[dict, str]:
    at = next((k for k in range(len(parts) - 1, -1, -1) if any(_postcode_in(parts[k]))), None)
    if at is None:
        return {}, ""
    pc, zp = _postcode_in(parts[at])
    data: dict[str, str] = {}
    if pc:
        data["postcode"] = re.sub(r"\s+", " ", pc.group(1)).strip()
        remainder = re.sub(_UK_POSTCODE.pattern, "", parts[at], flags=re.I)
        code_text = data["postcode"]
    else:
        data["region"], data["postcode"] = zp.group(1), zp.group(2)
        remainder = parts[at].replace(zp.group(0), "")
        code_text = f"{data['region']} {data['postcode']}"          # 9b: "Fairview, PA 17025" keeps its state
    rest = [*parts[:at], *([remainder.strip(" ,.")] if remainder.strip(" ,.") else [])]
    after = parts[at + 1:]
    country = next((p for p in after if _is_place(p) and sigplaces.country_in(p)), "")
    if not rest or any(not (_is_place(p) and sigplaces.country_in(p)) for p in after):
        return {}, ""                                     # nothing before the postcode, or more than a country after
    if len(rest) < 2 and not _street_like(rest[0]):
        return {}, ""                                     # "Leeds LS1 9ZZ" alone: a town, not an address (9b)
    data["street"] = ", ".join(rest[:-1]) if len(rest) >= 2 else rest[0]
    if len(rest) >= 2:
        data["city"] = rest[-1]
    if country:
        data["country"] = sigplaces.COUNTRY_NAME.get(sigplaces.country_in(country), country)
    return data, ", ".join([*rest, code_text, *([data["country"]] if country else [])])


def _offerable(kind: str, data: dict, text: str) -> bool:
    """9b's last word on an address: never a separator, legal words, an unclosed bracket, a dangling comma or
    more than `MAX_ADDRESS` characters; with no postcode, a number, a street word or a PO Box."""
    if not text or len(text) > MAX_ADDRESS or _SEPARATOR_CHAR.search(text) or LEGAL.search(text):
        return False
    if text.rstrip().endswith(",") or text.count("(") != text.count(")") or text.count("[") != text.count("]"):
        return False
    street = data.get("street", "")
    if kind == "place":
        return bool(any(c.isdigit() for c in street) or _STRONG_STREET.search(street) or _PO_BOX.search(street))
    return True


def _strip_place(text: str) -> str:
    """9b: "Oman, Sohar ... Refining" -> "Sohar ... Refining": a country or city glued before a company or a
    department is not part of its name."""
    m = re.match(r"^\s*([^,]{2,40}),\s*(\S.*)$", text)
    return m.group(2).strip() if m and _is_place(m.group(1)) and not _is_place(m.group(2)) else text


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
            sig["department"] = _strip_place(ln).strip(" ,|-")
            continue
        if "company" not in sig and not digits and not _TITLE_WORDS.search(ln):
            flat_ln = flat(ln)
            agrees = bool(own) and len(own) >= 3 and (own in flat_ln or (len(flat_ln) >= 4 and flat_ln in own))
            if _COMPANY_SUFFIX.search(ln) or agrees or flat_ln in known:
                sig["company"] = _strip_place(ln).strip(" ,|-")
