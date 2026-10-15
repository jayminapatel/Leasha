"""7b: a company from a work email's domain, and colleagues grouped by company.

Layer: L7

Order 1k section 7b. Local only. The guess itself is `app.contacts.cleanup.
company_guess_from_email` (one list of free-mail domains for the whole package,
so clean-up and enrichment always agree). If that module is missing or changes
shape, the small copy below takes over; it is kept deliberately in step with it
and `tests/unit/test_contacts_enrich_local.py` pins the two to the same answers.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from app.contacts.card import Card
from app.contacts.enrich.base import Proposal

__all__ = ["company_guess", "company_proposals", "group_by_company", "company_key", "registrable_name",
           "work_domains", "agrees_with_domain", "LEGAL_SUFFIX", "split_suffix"]

# A small standalone copy, used only when app.contacts.cleanup cannot be imported.
_FALLBACK_FREE = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com",
    "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "btinternet.com", "sky.com", "virginmedia.com", "ntlworld.com", "talktalk.net"})


def _fallback_guess(email: str) -> Optional[str]:
    text = (email or "").strip()
    if "@" not in text:
        return None
    domain = text.rsplit("@", 1)[1].strip().lower()
    labels = [x for x in domain.split(".") if x]
    if len(labels) < 2 or not all(re.fullmatch(r"[a-z0-9-]+", x) for x in labels):
        return None
    take = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in {"co", "com", "org", "ac", "gov"} else 2
    reg = ".".join(labels[-take:])
    if reg in _FALLBACK_FREE:
        return None
    return " ".join(w.capitalize() for w in labels[-take].split("-") if w) or None


try:                                                     # the shared list lives in clean-up (6p.3)
    from app.contacts.cleanup import company_guess_from_email as _guess
except Exception:                                        # noqa: BLE001 - never let a helper's file break this one
    _guess = _fallback_guess


def company_guess(email: str) -> Optional[str]:
    """`jane@acme-widgets.co.uk` -> `Acme Widgets`; None for free mail or a non-address."""
    return _guess(email)


def company_proposals(cid: int, card: Card) -> list[Proposal]:
    """Offer a company for a card that has none, from the first work email that gives one."""
    if card.get("company").strip():
        return []
    emails = card.values("email")
    ranked = sorted(range(len(emails)), key=lambda i: (0 if "work" in emails[i].label.lower() else 1,
                                                       0 if emails[i].primary else 1, i))
    for i in ranked:
        guess = company_guess(emails[i].value)
        if guess:
            domain = emails[i].value.strip().rsplit("@", 1)[1].lower()
            return [Proposal(cid, "company", "", "", guess, "company", f"work email domain {domain}", 0.5)]
    return []


_SUFFIX = re.compile(r"\b(?:ltd|limited|llc|llp|inc|plc|gmbh|corp|corporation|co|company|group)\b\.?", re.I)


def company_key(name: str) -> str:
    """What makes two spellings one company: case, punctuation and a legal suffix ignored."""
    return re.sub(r"[^a-z0-9]+", " ", _SUFFIX.sub(" ", (name or "").casefold())).strip()


def _fallback_registrable(domain: str) -> tuple[str, str]:
    labels = [x for x in (domain or "").lower().strip(".").split(".") if x]
    if len(labels) < 2:
        return "", ""
    take = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in {"co", "com", "org", "ac", "gov"} else 2
    reg = labels[-take:]
    return reg[0], ".".join(reg)


try:                                                     # one public-suffix rule for the package, as above
    from app.contacts.cleanup import _registrable
except Exception:                                        # noqa: BLE001
    _registrable = _fallback_registrable


def registrable_name(domain: str) -> str:
    """The name part of a domain's registrable domain: `shop.acme-widgets.co.uk` -> `acme-widgets`.

    Order 1s 6d compares sites by this, so `acme-widgets.com` and `acme-widgets.co.uk` are one company
    but `other-site.net` is not."""
    return _registrable((domain or "").strip().lower())[0]


def split_suffix(domain: str) -> tuple[str, str]:
    """A domain as (what comes before its public ending, the ending): `mail.acme.co.uk` -> ("mail.acme",
    "co.uk"), by the same rule as `registrable_name`. ("", "") for a single word."""
    d = (domain or "").strip().lower().strip(".")
    name, reg = _registrable(d)
    if not name:
        return "", ""
    suffix = reg[len(name) + 1:]
    return d[:-len(suffix) - 1], suffix


def work_domains(card: Card) -> list[str]:
    """The domains of the card's work emails - free mail is not a work email (`company_guess` says None)."""
    out: list[str] = []
    for f in card.values("email"):
        if company_guess(f.value):
            d = f.value.strip().rsplit("@", 1)[-1].lower()
            if d not in out:
                out.append(d)
    return out


#: Order 1s 6c. A legal form says a text is a registered company's name, so it may go to Wikidata even when
#: no work email agrees. Narrower than `_SUFFIX`: "Group" or "Partners" is a word in many names, not a form.
LEGAL_SUFFIX = re.compile(
    r"\b(?:ltd|limited|llc|l\.l\.c|llp|inc|incorporated|plc|gmbh|ag|bv|b\.v|nv|n\.v|sa|s\.a|sarl|sas|spa|"
    r"s\.p\.a|srl|pty|corp|corporation|co|company|oy|ab|kk|pjsc|psc|wll|w\.l\.l|fze|fzco|fz-llc|fzllc|"
    r"lp|pte|bhd|sdn)\.?\s*$", re.I)


def agrees_with_domain(name: str, domains: list[str]) -> bool:
    """Is `name` the company these work-email domains belong to? `Halvern Partners` and `halvern-partners.com`
    agree; so do `ACME` and `acme-corp.com` (a name of four letters or more the domain starts with), and
    `IBM` and `ibm.com` (initials). Anything looser would send someone else's company to Wikidata."""
    words = company_key(name).split()
    if not words:
        return False
    joined = "".join(words)
    initials = "".join(w[0] for w in words) if len(words) > 1 else ""
    for d in domains:
        reg = re.sub(r"[^a-z0-9]", "", registrable_name(d))
        if not reg:
            continue
        if reg in (joined, words[0], initials) or (len(joined) >= 4 and reg.startswith(joined)) \
                or (len(reg) >= 4 and joined.startswith(reg)):
            return True
    return False


def group_by_company(hub: Any, *, min_members: int = 1) -> dict[str, list[int]]:
    """company -> contact ids, for people who share one.

    A contact's company is the one on its card, or - when blank - the guess from
    its work email. Free-mail contacts with no company are in no group. The key is
    the first spelling met; `min_members=2` lists only companies with colleagues.
    """
    groups: dict[str, tuple[str, list[int]]] = {}
    for cid, card in hub.all_cards():
        name = card.get("company").strip()
        if not name:
            for f in card.values("email"):
                name = company_guess(f.value) or ""
                if name:
                    break
        key = company_key(name)
        if not key:
            continue
        groups.setdefault(key, (name, []))[1].append(cid)
    return {name: ids for name, ids in groups.values() if len(ids) >= min_members}
