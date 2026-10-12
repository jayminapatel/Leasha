"""7h: a company's website, headquarters and industry from Wikidata. Never a person.

Layer: L7

Order 1k section 7h. **Internet; off by default** (`AccountSettings.enrich_wikidata`).
Wikidata's data is public domain (CC0).

**Companies only, never people - three guards.**

1. The only thing sent is a *company name*, taken from the card's company field.
   `company_term` returns "" - and nothing is asked - when that text is the same
   as the person's own name, or is blank.
2. `lookup_company` takes a string and no card, so it has no person to send.
3. An entity Wikidata says is a human (`instance of` Q5) is rejected, and an entity
   is accepted only if it has a headquarters or an industry, or is an instance of a
   known organisation class.

The request goes to `www.wikidata.org/w/api.php` (on `ENRICHMENT_HOSTS`):
`wbsearchentities` for the name, `wbgetentities` for the chosen entity, and one more
`wbgetentities` to turn the headquarters and industry ids into names. A name is
accepted only if its label matches the company exactly (ignoring case, punctuation
and a legal suffix); anything fuzzier would put the wrong company on a card.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlencode

from app.accounts.provider import Transport
from app.contacts.card import Card
from app.contacts.enrich.base import Proposal
from app.contacts.enrich.company import LEGAL_SUFFIX, agrees_with_domain, company_key, work_domains

__all__ = ["company_term", "clean_company", "lookup_company", "wikidata_proposals", "CompanyFacts", "API"]

API = "https://www.wikidata.org/w/api.php"
_USER_AGENT = "Leasha (desktop app; looks up public company facts by name)"
_HUMAN = "Q5"
_ORG_CLASSES = {"Q4830453", "Q783794", "Q43229", "Q6881511", "Q891723", "Q167037", "Q1616075", "Q15265344",
                "Q3918", "Q31855", "Q245065", "Q163740", "Q484652", "Q2659904", "Q327333"}
_P_WEBSITE, _P_HQ, _P_INDUSTRY, _P_INSTANCE = "P856", "P159", "P452", "P31"


class CompanyFacts:
    def __init__(self, qid: str, label: str, website: str = "", headquarters: str = "",
                 industries: Optional[list[str]] = None, ambiguous: bool = False) -> None:
        self.qid, self.label, self.website = qid, label, website
        self.headquarters = headquarters
        self.industries = industries or []
        self.ambiguous = ambiguous

    @property
    def receipt(self) -> str:
        return f"https://www.wikidata.org/wiki/{self.qid} (Wikidata, CC0)"


def _person_names(card: Card) -> set[str]:
    names = {company_key(card.get("nickname"))}
    given, family = card.get("given"), card.get("family")
    names |= {company_key(f"{given} {family}"), company_key(f"{family} {given}"), company_key(family),
              company_key(given)}
    return {n for n in names if n}


#: Order 1s 6c. Words that name a part of a company or a person's job, not the company: a card's company
#: field often holds "Wipro Technologies Account Energy" (company, account team, sector), which matches no
#: Wikidata label. Only words that are rarely the second word of a company's own name are here; sector
#: words such as "Energy" are not, because they are ("Shell Energy").
_DEPARTMENT_WORDS = frozenset({
    "account", "accounts", "department", "dept", "division", "unit", "team", "practice", "desk", "region",
    "regional", "function", "sales", "marketing", "finance", "procurement", "purchasing", "operations", "hr",
    "support", "management", "commercial", "projects", "project"})
#: "Acme - Procurement", "Acme | Senior Engineer", "Acme / EMEA": the company is the part before.
_SEPARATOR = re.compile(r"\s+[-/]\s+|\s*[|–—]\s*")


def clean_company(text: str) -> str:
    """The company in a company field, without department and role words (6c).

    A name ending in a legal form ("Orlesk Sales Ltd") is a registered name and kept whole; otherwise the
    name ends before its first department or role word after the first word."""
    from app.contacts.enrich.signatures import _TITLE_WORDS  # noqa: PLC0415 - one title list for the package

    first = _SEPARATOR.split((text or "").strip())[0].strip()
    if "," in first:                                     # "Acme, Inc." keeps its form; ", Sales Department" goes
        head, rest = first.split(",", 1)
        first = first if (LEGAL_SUFFIX.search(rest) and len(rest.split()) <= 2) else head.strip()
    if LEGAL_SUFFIX.search(first):
        return first
    words = first.split()
    for i, w in enumerate(words[1:], start=1):
        bare = re.sub(r"[^\w-]", "", w).lower()
        if bare in _DEPARTMENT_WORDS or (bare == "business" and i + 1 < len(words)
                                         and words[i + 1].lower().startswith("unit")) or _TITLE_WORDS.fullmatch(w):
            return " ".join(words[:i]).rstrip(" &,-")
    return first


def company_term(card: Card) -> str:
    """The company name to look up, or "" when there is nothing safe to ask.

    Never a person's name: if the company field holds the person's own name (or
    part of it) it is not sent. Order 1s 6c: what is sent is `clean_company`'s name, and only when it
    carries a legal form or agrees with one of the card's work-email domains - a name that is neither
    could be anyone's company, and a wrong match puts another company's facts on the card.
    """
    term = clean_company(card.get("company"))
    if not term or len(term) < 3 or len(term) > 80 or company_key(term) in _person_names(card):
        return ""
    if not (LEGAL_SUFFIX.search(term) or agrees_with_domain(term, work_domains(card))):
        return ""
    return term


def _get(transport: Transport, params: dict[str, str]) -> Any:
    url = API + "?" + urlencode({**params, "format": "json", "formatversion": "2"})
    r = transport.request("GET", url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
                          timeout=15.0)
    if r.status != 200:
        return None
    try:
        return json.loads(r.body.decode("utf-8", "replace"))
    except ValueError:
        return None


def _claim_values(entity: dict, prop: str) -> list[Any]:
    claims = [c for c in (entity.get("claims") or {}).get(prop, []) if c.get("rank") != "deprecated"]
    claims.sort(key=lambda c: 0 if c.get("rank") == "preferred" else 1)
    out = []
    for c in claims:
        try:
            out.append(c["mainsnak"]["datavalue"]["value"])
        except (KeyError, TypeError):
            continue
    return out


def _ids(values: list[Any]) -> list[str]:
    return [v["id"] for v in values if isinstance(v, dict) and re.fullmatch(r"Q\d+", str(v.get("id", "")))]


def lookup_company(transport: Transport, name: str) -> Optional[CompanyFacts]:
    """Facts for the company called exactly `name`, or None. Asks about a name only."""
    name = (name or "").strip()
    if not name:
        return None
    found = _get(transport, {"action": "wbsearchentities", "search": name, "language": "en", "type": "item",
                             "limit": "5"})
    hits = [h for h in ((found or {}).get("search") or [])
            if company_key(str(h.get("label") or "")) == company_key(name) and re.fullmatch(r"Q\d+", str(h.get("id")))]
    if not hits:
        return None
    ids = "|".join(h["id"] for h in hits[:3])
    got = _get(transport, {"action": "wbgetentities", "ids": ids, "props": "claims|labels", "languages": "en"})
    entities = (got or {}).get("entities") or {}
    ok: list[tuple[dict, str]] = []
    for h in hits[:3]:
        ent = entities.get(h["id"]) if isinstance(entities, dict) else None
        if not isinstance(ent, dict):
            continue
        classes = set(_ids(_claim_values(ent, _P_INSTANCE)))
        if _HUMAN in classes:
            continue                                     # never a person
        if _claim_values(ent, _P_HQ) or _claim_values(ent, _P_INDUSTRY) or classes & _ORG_CLASSES:
            ok.append((ent, h["id"]))
    if not ok:
        return None
    ent, qid = ok[0]
    sites = [v for v in _claim_values(ent, _P_WEBSITE) if isinstance(v, str) and v.lower().startswith("https://")]
    hq_ids = _ids(_claim_values(ent, _P_HQ))[:1]
    ind_ids = _ids(_claim_values(ent, _P_INDUSTRY))[:3]
    labels: dict[str, str] = {}
    if hq_ids or ind_ids:
        names = _get(transport, {"action": "wbgetentities", "ids": "|".join(hq_ids + ind_ids),
                                 "props": "labels", "languages": "en"})
        for k, v in ((names or {}).get("entities") or {}).items():
            lab = ((v or {}).get("labels") or {}).get("en") or {}
            if lab.get("value"):
                labels[k] = str(lab["value"])
    label = ((ent.get("labels") or {}).get("en") or {}).get("value") or name
    return CompanyFacts(qid, str(label), sites[0] if sites else "", labels.get(hq_ids[0], "") if hq_ids else "",
                        [labels[i] for i in ind_ids if i in labels], ambiguous=len(ok) > 1)


def wikidata_proposals(transport: Transport, cid: int, card: Card, facts: Optional[CompanyFacts] = None
                       ) -> list[Proposal]:
    """Proposals for a card's company. `facts` lets the caller reuse one lookup for colleagues."""
    term = company_term(card)
    if not term:
        return []
    # A person's name must never get this far: company_term already refused it.
    assert company_key(term) not in _person_names(card), "Wikidata is for companies only, never people"
    facts = facts if facts is not None else lookup_company(transport, term)
    return proposals_from_facts(cid, card, facts)


def proposals_from_facts(cid: int, card: Card, facts: Optional[CompanyFacts]) -> list[Proposal]:
    if facts is None:
        return []
    conf = 0.4 if facts.ambiguous else 0.6
    out: list[Proposal] = []
    if facts.website:
        norm = lambda u: re.sub(r"^https?://(www\.)?", "", u.lower()).rstrip("/")     # noqa: E731
        if all(norm(f.value) != norm(facts.website) for f in card.values("url")):
            out.append(Proposal(cid, "url", "company", "", facts.website, "wikidata", facts.receipt, conf))
    if facts.headquarters and not any(f.label.lower() == "headquarters" for f in card.values("address")):
        out.append(Proposal(cid, "address", "headquarters", "", facts.headquarters, "wikidata", facts.receipt,
                            conf, data={"city": facts.headquarters}))
    if facts.industries and not any(f.label.lower() == "industry" for f in card.values("custom")):
        out.append(Proposal(cid, "custom", "industry", "", ", ".join(facts.industries), "wikidata",
                            facts.receipt, conf))
    return out
