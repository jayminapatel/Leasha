"""Finding people who are in the book twice, and merging them.

Layer: L7

Order 1k 6n, 6o, D9. Pure: cards in, groups out; the hub shows them and the
person decides.

**What is safe (D9, tightened 2026-10-11).** A group is `safe` only when every member
is joined to the others by an exact normalised email address or an exact E.164
**mobile** number *and the two names are compatible*. Two people who share an office
landline, a family line or a shared mailbox are not the same person, so:

* a pair whose names disagree (different family names, or different first names) is
  **never** an edge, whatever number or address they share;
* a landline, a toll-free or an unplaceable number never makes a safe edge - with
  compatible names it is a suggestion ("same landline and name"), never an
  automatic merge;
* a value the person has said is shared ("Not a duplicate" on two people who hold
  it, or `shared` below) never makes an edge at all.

A name match, a name-and-company match and a loose digit match are suggestions: the
person looks at each. That is what "Merge all safe matches" (6n.2) is allowed to touch.

**Groups.** Union-find over matching values. Safe edges are grouped first, so a
safe pair that is also linked to a third contact by name alone stays a safe
group of two *and* appears inside a wider, unsafe suggestion of three; after the
safe merge is applied and the book rescanned, only the wider suggestion is left.

**Rejected pairs ("Not a duplicate").** A rejected pair is not an edge. If two
rejected contacts are still joined through a third, the group is shown but is
never safe. `pairs_of(ids)` expands a rejected group into pairs.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Hashable, Iterable, Optional

from app.contacts import phone as phones
from app.contacts.card import Card, Field, SCALARS, field_key, units

__all__ = ["DuplicateGroup", "scan_duplicates", "names_compatible", "holds_only_a_name", "shared_keys", "safe_groups", "pairs_of", "MergePreview", "merge_cards",
           "CONFIDENCE"]

#: Confidence of each reason, 0..1. Exact matches sit above 0.9, everything a
#: person must look at below it. Fixed constants: nothing here is tunable (rule 11);
#: the ordering is what matters.
CONFIDENCE = {
    "same email": 0.97,
    "same phone": 0.95,
    "same name, one holds nothing else": 0.92,
    "same landline and name": 0.75,
    "same name and company": 0.70,
    "same phone digits": 0.60,
    "same name": 0.50,
}
_SAFE_REASONS = frozenset({"same email", "same phone", "same name, one holds nothing else"})
#: The name parts: a card holding nothing but these "holds only the name".
_NAME_PARTS = frozenset({"prefix", "given", "additional", "family", "suffix"})
#: Reasons that only count when the two names could be one person's.
_NEEDS_NAMES = frozenset({"same email", "same phone", "same landline and name", "same phone digits"})

Id = Hashable


@dataclass(frozen=True)
class DuplicateGroup:
    ids: tuple
    reasons: tuple
    confidence: float
    safe: bool


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def add(self, x: Any) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: Any) -> Any:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def pairs_of(ids: Iterable[Id]) -> set[tuple]:
    """Every pair inside one group, for "Not a duplicate" on a whole group."""
    items = list(ids)
    try:
        items.sort()
    except TypeError:
        items.sort(key=str)
    return set(combinations(items, 2))


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().split())


def _tokens(text: str) -> frozenset:
    return frozenset(t for t in _fold(text).replace("-", " ").replace(".", " ").split() if t)


def _nested(a: frozenset, b: frozenset) -> bool:
    return bool(a) and bool(b) and (a <= b or b <= a)


def _agree(a: frozenset, b: frozenset) -> bool:
    """Same words, one the other with more words, an initial ("A." / "Ann") or a short form
    that starts the long one ("Cy" / "Cyrus", "Rob" / "Robert")."""
    if _nested(a, b):
        return True
    return any(y.startswith(x) for x in a for y in b) or any(x.startswith(y) for x in a for y in b)


def names_compatible(a: Card, b: Card) -> bool:
    """Could these two cards be the same person's name? Unknown counts as compatible.

    Family names must agree (one may be the other with more words: "Bin Yusof" and "Yusof");
    first names must agree, or one may be an initial or the other with more words ("Manoj"
    and "Manoj Bhanu"). A company-only contact is compared by its company. A card with no
    name and no company at all can be anyone, so it is compatible with everyone."""
    ga, fa = _tokens(a.get("given")), _tokens(a.get("family"))
    gb, fb = _tokens(b.get("given")), _tokens(b.get("family"))
    if not (ga or fa) or not (gb or fb):
        ca, cb = _tokens(a.get("company")), _tokens(b.get("company"))
        a_org_only, b_org_only = not (ga or fa) and bool(ca), not (gb or fb) and bool(cb)
        if a_org_only and b_org_only:
            return _nested(ca, cb)
        if a_org_only or b_org_only:
            return False                              # a company against a person
        return True                                   # one side has no name at all
    if fa and fb and not _agree(fa, fb):
        return False
    if ga and gb and not _agree(ga, gb):
        return False
    return True


def holds_only_a_name(card: Card) -> bool:
    """A card with a name and nothing else - no number, address, company, note or photo.
    Merging it into another card loses nothing (2026-10-26: Teams' copies of a phone's
    contacts reached Outlook as names alone, 120 of them beside their originals)."""
    others = {k for k, v in card.scalars.items() if str(v or "").strip()} - _NAME_PARTS
    return not card.fields and card.photo is None and not others


def _edges(items: list[tuple[Id, Card]], region: str, shared: frozenset = frozenset()) -> dict[tuple, set[str]]:
    """{(index_a, index_b): reasons} over positions in `items` (a < b)."""
    buckets: dict[tuple[str, str], list[int]] = {}

    def put(reason: str, key: str, i: int) -> None:
        if key:
            lst = buckets.setdefault((reason, key), [])
            if not lst or lst[-1] != i:
                lst.append(i)

    for i, (_id, card) in enumerate(items):
        for f in card.fields:
            if f.kind == "email":
                key = _fold(f.value)
                if key and f"email:{key}" not in shared:
                    put("same email", key, i)
            elif f.kind == "phone":
                e164 = phones.normalise(f.value, region)
                if e164:
                    if f"phone:{e164}" in shared:
                        continue
                    put("same phone" if phones.is_mobile(f.value, region) else "same landline and name", e164, i)
                else:
                    put("same phone digits", phones.digits_key(f.value), i)
        # 2026-10-26: the whole name, however it is split. Microsoft Teams' copy of a
        # phone's contacts puts "Ravi Kumar Shah" all in the first name; keyed only when
        # both parts were set, 120 such copies were never offered beside their originals
        # ("Ravi Kumar" "Shah"). Two words at least, as before: one word is not a name.
        # With the middle name as well, so "Arun Dev Mehta" meets "Arun" "Dev" "Mehta";
        # without it too, so "Ann Lee" still meets "Ann Marie Lee".
        name = _fold(f"{card.get('given')} {card.get('family')}")
        whole = _fold(f"{card.get('given')} {card.get('additional')} {card.get('family')}")
        for key in {name, whole}:
            if len(key.split()) >= 2:
                put("same name", key, i)
            company = _fold(card.get("company"))
            if company:
                put("same name and company", f"{name}|{company}", i)
    edges: dict[tuple, set[str]] = {}
    # 2026-10-26, the owner: "let empty name-only copies merge into their original". Safe
    # only when the name is held by exactly two cards and one holds nothing else: with a
    # third card of that name, the copy could be either person, so it stays a suggestion.
    for (reason, _key), members in buckets.items():
        if reason == "same name" and len(set(members)) == 2:
            a, b = sorted(set(members))
            if holds_only_a_name(items[a][1]) != holds_only_a_name(items[b][1]):
                edges.setdefault((a, b), set()).add("same name, one holds nothing else")
    for (reason, _key), members in buckets.items():
        for a, b in combinations(members, 2):
            if reason in _NEEDS_NAMES and not names_compatible(items[a][1], items[b][1]):
                continue                              # a shared number or address is not a person
            edges.setdefault((a, b), set()).add(reason)
    for reasons in edges.values():
        if "same name and company" in reasons or "same name, one holds nothing else" in reasons:
            reasons.discard("same name")             # the stronger reason says it already
    return edges


def scan_duplicates(items: list[tuple[Id, Card]], rejected_pairs: set[tuple],
                    region: str = phones.DEFAULT_REGION, shared: Iterable[str] = ()) -> list[DuplicateGroup]:
    """Groups of contacts that look like one person, best first.

    Order: confidence (high to low), then larger groups, then position in `items`;
    ids inside a group are in the order of `items`, so the result is the same on
    every run for the same book.
    """
    rejected = {frozenset(p) for p in rejected_pairs}
    n = len(items)
    ids = [i for i, _ in items]
    edges = {k: v for k, v in _edges(items, region, frozenset(shared)).items()
             if frozenset((ids[k[0]], ids[k[1]])) not in rejected}

    # 1. components joined by safe edges only
    safe_uf = _UnionFind()
    for k in range(n):
        safe_uf.add(k)
    for (a, b), reasons in edges.items():
        if reasons & _SAFE_REASONS:
            safe_uf.union(a, b)
    comp: dict[int, list[int]] = {}
    for k in range(n):
        comp.setdefault(safe_uf.find(k), []).append(k)

    def has_rejected_inside(members: list[int]) -> bool:
        return any(frozenset((ids[a], ids[b])) in rejected for a, b in combinations(members, 2)) \
            if rejected else False

    out: list[tuple[float, int, int, DuplicateGroup]] = []

    def emit(members: list[int], reasons: set[str], confidence: float, safe: bool) -> None:
        members = sorted(members)
        ordered = tuple(sorted(reasons, key=lambda r: (-CONFIDENCE[r], r)))
        out.append((confidence, len(members), members[0],
                    DuplicateGroup(tuple(ids[m] for m in members), ordered, confidence, safe)))

    for members in comp.values():
        if len(members) < 2:
            continue
        mset = set(members)
        reasons: set[str] = set()
        best = 0.0
        for (a, b), rs in edges.items():
            if a in mset and b in mset:
                safe_rs = rs & _SAFE_REASONS
                reasons |= safe_rs
                for r in safe_rs:
                    best = max(best, CONFIDENCE[r])
        emit(members, reasons, best, safe=not has_rejected_inside(members))

    # 2. unsafe suggestions: join the safe components by the remaining edges
    uf = _UnionFind()
    links: list[tuple[int, int, set[str]]] = []
    for (a, b), rs in edges.items():
        weak = rs - _SAFE_REASONS
        ra, rb = safe_uf.find(a), safe_uf.find(b)
        if weak and ra != rb:
            uf.union(ra, rb)
            links.append((ra, rb, weak))
    groups: dict[Any, dict[str, Any]] = {}
    for ra, rb, weak in links:
        g = groups.setdefault(uf.find(ra), {"reasons": set(), "best": 0.0})
        g["reasons"] |= weak
        g["best"] = max(g["best"], *(CONFIDENCE[r] for r in weak))
    members_of: dict[Any, list[int]] = {}
    for root in {r for ra, rb, _ in links for r in (ra, rb)}:
        members_of.setdefault(uf.find(root), []).extend(comp[root])
    for top, members in members_of.items():
        g = groups[top]
        emit(members, g["reasons"], g["best"], safe=False)

    out.sort(key=lambda t: (-t[0], -t[1], t[2], not t[3].safe))
    return [t[3] for t in out]


def shared_keys(a: Card, b: Card, region: str = phones.DEFAULT_REGION) -> set[str]:
    """The emails and numbers two cards both hold, as the keys `scan_duplicates(shared=)` takes:
    what "Not a duplicate" teaches Leasha is a shared line or mailbox."""
    def keys(card: Card) -> set[str]:
        out: set[str] = set()
        for f in card.fields:
            if f.kind == "email" and _fold(f.value):
                out.add(f"email:{_fold(f.value)}")
            elif f.kind == "phone":
                e164 = phones.normalise(f.value, region)
                if e164:
                    out.add(f"phone:{e164}")
        return out
    return keys(a) & keys(b)


def safe_groups(groups: Iterable[DuplicateGroup]) -> list[DuplicateGroup]:
    """What "Merge all safe matches" may merge: exact email or exact E.164 phone only."""
    return [g for g in groups if g.safe]


# --------------------------------------------------------------------------
# Merge preview (6o)
# --------------------------------------------------------------------------

@dataclass
class MergePreview:
    """The merged contact and where each part of it came from.

    `field_sources` maps every unit key of `card` (see `card.units`) to the
    positions, in the list given to `merge_cards`, of the cards that held it.
    `per_field_choice` maps each scalar and `photo` to the position chosen, or -1
    where the value is a combination (notes). `conflicts` lists, for the units
    where the cards disagree, every card's value: what the person can pick from.
    """

    card: Card
    field_sources: dict[str, list[int]] = field(default_factory=dict)
    per_field_choice: dict[str, int] = field(default_factory=dict)
    conflicts: dict[str, list[tuple[int, Any]]] = field(default_factory=dict)


def merge_cards(cards_in_order: list[Card], choices: Optional[dict[str, int]] = None,
                region: str = phones.DEFAULT_REGION) -> MergePreview:
    """One card from several. Repeatable fields are the union (one value when
    two copies are the same value, keeping the first copy's order and adopting
    a label or main flag it lacked); a scalar is the first non-empty one unless
    `choices` names a card (`{"s:company": 1, "photo": 0}`). Notes that differ
    are kept together, separated by a blank line, unless chosen; a photo is the
    first one. Inputs are never changed.
    """
    if not cards_in_order:
        raise ValueError("There is nothing to merge: give at least one contact.")
    choices = dict(choices or {})
    for key, idx in choices.items():
        if not 0 <= idx < len(cards_in_order):
            raise ValueError(f"The choice for {key} points at contact {idx}, but only "
                             f"{len(cards_in_order)} are being merged.")
    out = Card()
    prev = MergePreview(card=out)

    for name in SCALARS:
        key = f"s:{name}"
        values = [(i, c.get(name).strip()) for i, c in enumerate(cards_in_order) if c.get(name).strip()]
        if not values:
            continue
        distinct = list(dict.fromkeys(v for _, v in values))
        if len(distinct) > 1:
            prev.conflicts[key] = values
        chosen = next(((i, v) for i, v in values if i == choices.get(key, -1)), None)
        if chosen is not None:
            out.scalars[name] = chosen[1]
            prev.per_field_choice[key] = chosen[0]
        elif name == "notes" and len(distinct) > 1:
            out.scalars[name] = "\n\n".join(distinct)
            prev.per_field_choice[key] = -1
        else:
            out.scalars[name] = values[0][1]
            prev.per_field_choice[key] = values[0][0]

    index: dict[str, Field] = {}
    for i, c in enumerate(cards_in_order):
        for f in c.fields:
            if not f.value.strip() and not any(f.data.values()):
                continue
            k = field_key(f, region)
            if k in index:
                kept = index[k]
                if not kept.label and f.label:
                    kept.label = f.label
                kept.primary = kept.primary or f.primary
                continue
            kept = Field(f.kind, f.value, f.label, dict(f.data), f.primary)
            index[k] = kept
            out.fields.append(kept)

    seen: set[str] = set()
    for c in cards_in_order:
        for g in c.groups:
            slug = " ".join(g.casefold().split())
            if g.strip() and slug not in seen:
                seen.add(slug)
                out.groups.append(g.strip())
    out.favourite = any(c.favourite for c in cards_in_order)

    photos = [(i, c.photo) for i, c in enumerate(cards_in_order) if c.photo is not None]
    if photos:
        shas = list(dict.fromkeys(p.sha for _, p in photos))
        if len(shas) > 1:
            prev.conflicts["photo"] = [(i, p.sha) for i, p in photos]
        pick = next(((i, p) for i, p in photos if i == choices.get("photo", -1)), photos[0])
        out.photo = pick[1]
        prev.per_field_choice["photo"] = pick[0]

    held = [units(c, region=region) for c in cards_in_order]
    for key in units(out, region=region):
        if key.startswith("s:"):
            name = key[2:]
            if prev.per_field_choice.get(key) == -1:                       # combined notes
                holders = [i for i, c in enumerate(cards_in_order) if c.get(name).strip()]
            else:
                holders = [i for i, c in enumerate(cards_in_order)
                           if c.get(name).strip() == out.get(name).strip()]
        elif key == "photo":
            holders = [i for i, c in enumerate(cards_in_order)
                       if c.photo is not None and c.photo.sha == out.photo.sha]
        else:
            holders = [i for i, u in enumerate(held) if key in u]
        prev.field_sources[key] = holders
    return prev
