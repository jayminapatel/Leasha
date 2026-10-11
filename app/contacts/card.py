"""The card: one contact as plain data, and the three-way merge over it.

Layer: L7

Order 1k D6, D9, D10. A `Card` is everything Leasha knows about a person that
can travel to or from an account, with no database ids in it. The hub turns
rows into cards, a provider turns its own records into cards, and the sync
engine only ever compares cards.

**Units.** A card is compared as a set of keyed *units*: one per scalar
(`s:given`), one per repeatable value (`f:email:ann@x.com`, `f:phone:+4477...`),
one per group (`g:family`), plus `favourite` and `photo`. A unit's value is what
can differ without it being a different unit - a label, an order flag. The merge
is then the textbook three-way over a dictionary: a change on one side only wins
without a question, a change on both sides to the same value is no change, and
only a real clash is a conflict (D6).

**What an account cannot hold (D10).** `Capabilities` names what a provider
stores. `project` cuts a card to that, and `merge` touches only the units the
provider can hold: everything else stays exactly as the hub has it, so a round
trip through an account that drops a field never removes it from Leasha.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional

from app.contacts import phone as phones

__all__ = [
    "SCALARS", "KINDS", "Photo", "Field", "Card", "Capabilities", "ALL_CAPS",
    "Conflict", "MergeResult", "units", "project", "merge", "display_name",
    "field_key", "cards_equal", "empty_card",
]

#: The single-valued parts of a person.
SCALARS = ("prefix", "given", "additional", "family", "suffix", "nickname",
           "company", "department", "title", "notes")

#: The repeatable kinds (2a). `date` carries a label such as birthday or
#: anniversary; `custom` is a labelled free value.
KINDS = ("email", "phone", "address", "url", "date", "related", "social", "im", "custom")

_ADDRESS_PARTS = ("street", "city", "region", "postcode", "country")


@dataclass(frozen=True)
class Photo:
    mime: str
    data: bytes

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.data).hexdigest()[:16]


@dataclass
class Field:
    """One repeatable value: `kind`, a `label` (work, home, mobile, ...), the
    `value` as typed, and for an address its parts in `data`."""

    kind: str
    value: str
    label: str = ""
    data: dict[str, str] = field(default_factory=dict)
    primary: bool = False

    def key(self, region: str = phones.DEFAULT_REGION) -> str:
        return field_key(self, region)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "value": self.value}
        if self.label:
            out["label"] = self.label
        if self.data:
            out["data"] = dict(self.data)
        if self.primary:
            out["primary"] = True
        return out

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Field":
        return cls(kind=str(raw.get("kind", "custom")), value=str(raw.get("value", "")),
                   label=str(raw.get("label", "")), data=dict(raw.get("data") or {}),
                   primary=bool(raw.get("primary", False)))


@dataclass
class Card:
    scalars: dict[str, str] = field(default_factory=dict)
    fields: list[Field] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    favourite: bool = False
    photo: Optional[Photo] = None

    def get(self, name: str) -> str:
        return self.scalars.get(name, "") or ""

    def values(self, kind: str) -> list[Field]:
        return [f for f in self.fields if f.kind == kind]

    def copy(self) -> "Card":
        return Card(scalars=dict(self.scalars),
                    fields=[Field(f.kind, f.value, f.label, dict(f.data), f.primary) for f in self.fields],
                    groups=list(self.groups), favourite=self.favourite, photo=self.photo)

    def to_json(self) -> dict[str, Any]:
        """The snapshot form: no photo bytes, only the photo's short hash."""
        return {
            "scalars": {k: v for k, v in self.scalars.items() if v},
            "fields": [f.to_json() for f in self.fields],
            "groups": list(self.groups),
            "favourite": self.favourite,
            "photo": self.photo.sha if self.photo else "",
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Card":
        return cls(scalars={k: str(v) for k, v in (raw.get("scalars") or {}).items()},
                   fields=[Field.from_json(f) for f in raw.get("fields") or []],
                   groups=list(raw.get("groups") or []),
                   favourite=bool(raw.get("favourite", False)), photo=None)

    def dumps(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))


def empty_card() -> Card:
    return Card()


@dataclass(frozen=True)
class Capabilities:
    """What one kind of account can store (D10)."""

    scalars: frozenset = frozenset(SCALARS)
    kinds: frozenset = frozenset(KINDS)
    groups: bool = True
    favourite: bool = True
    photo: bool = True
    #: Dates the account can keep, by label (Google and Microsoft keep a
    #: birthday and little else); None means any label.
    date_labels: Optional[frozenset] = None
    #: `((kind, n), ...)`: the account keeps at most `n` of that kind (Outlook: three emails, five numbers).
    #: What does not fit stays in Leasha, and is not read as "deleted there" or "changed there": it is outside
    #: what this account is compared on, like a kind it does not hold at all.
    max_per_kind: tuple = ()
    #: `((kind, labels), ...)`: only these labels of that kind (Outlook's related people are a spouse, a
    #: manager and an assistant, and nothing else).
    kind_labels: tuple = ()


ALL_CAPS = Capabilities()


def _held_fields(card: Card, caps: Capabilities, region: str) -> list[Field]:
    """The fields of `card` that an account with `caps` holds: a supported kind and label, once each, and no
    more of a kind than the account has room for - the default one first, then in the card's order."""
    labelled = {k: set(v) for k, v in caps.kind_labels}
    out: list[Field] = []
    seen: set[str] = set()
    for f in card.fields:
        if f.kind not in caps.kinds or not f.value.strip() and not any(f.data.values()):
            continue
        if f.kind == "date" and caps.date_labels is not None and _slug(f.label) not in caps.date_labels:
            continue
        if f.kind in labelled and _slug(f.label) not in labelled[f.kind]:
            continue
        key = field_key(f, region)
        if key in seen:                       # an exact copy inside one card is one value
            continue
        seen.add(key)
        out.append(f)
    for kind, limit in caps.max_per_kind:
        mine = [f for f in out if f.kind == kind]
        if len(mine) > limit:
            keep = {id(f) for f in sorted(mine, key=lambda f: not f.primary)[:limit]}
            out = [f for f in out if f.kind != kind or id(f) in keep]
    return out


def display_name(card: Card) -> str:
    parts = [card.get("prefix"), card.get("given"), card.get("additional"),
             card.get("family"), card.get("suffix")]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    for fallback in (card.get("nickname"), card.get("company")):
        if fallback:
            return fallback
    for kind in ("email", "phone"):
        for f in card.values(kind):
            if f.value:
                return f.value
    return ""


def _slug(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


def field_key(f: Field, region: str = phones.DEFAULT_REGION) -> str:
    """What makes two values the same value: an address is the same address in
    any letter case, a phone number the same number in any format."""
    kind = f.kind
    if kind == "email":
        return f"email:{_slug(f.value)}"
    if kind == "phone":
        return f"phone:{phones.match_key(f.value, region) or _slug(f.value)}"
    if kind == "address":
        parts = [_slug(f.data.get(p, "")) for p in _ADDRESS_PARTS]
        joined = "|".join(parts) if any(parts) else _slug(f.value)
        return f"address:{joined}"
    if kind == "url":
        return "url:" + _slug(f.value).rstrip("/").removeprefix("https://").removeprefix("http://")
    if kind == "date":
        return f"date:{_slug(f.label)}:{_slug(f.value)}"
    if kind in ("social", "im", "custom", "related"):
        return f"{kind}:{_slug(f.label)}:{_slug(f.value)}"
    return f"{kind}:{_slug(f.value)}"


def _attr(f: Field) -> tuple:
    value = f.value
    if f.kind == "address" and f.data:
        # An address with parts is compared by its parts; the one-line text is
        # only how it is shown, and providers rebuild it differently.
        value = ", ".join(f.data[p] for p in _ADDRESS_PARTS if f.data.get(p))
    return (f.label, value, tuple(sorted(f.data.items())), f.primary)


def units(card: Card, caps: Capabilities = ALL_CAPS, region: str = phones.DEFAULT_REGION) -> dict[str, Any]:
    """The card as {unit key: comparable value}, cut to what `caps` can hold."""
    out: dict[str, Any] = {}
    for name in SCALARS:
        if name in caps.scalars and card.get(name).strip():
            out[f"s:{name}"] = card.get(name).strip()
    for f in _held_fields(card, caps, region):
        out["f:" + field_key(f, region)] = _attr(f)
    if caps.groups:
        for g in card.groups:
            if g.strip():
                out[f"g:{_slug(g)}"] = g.strip()
    if caps.favourite and card.favourite:
        out["favourite"] = True
    if caps.photo and card.photo is not None:
        out["photo"] = card.photo.sha
    return out


def project(card: Card, caps: Capabilities, region: str = phones.DEFAULT_REGION) -> Card:
    """The card as an account that holds only `caps` would hold it."""
    out = Card(photo=card.photo if caps.photo else None, favourite=card.favourite and caps.favourite)
    for name in SCALARS:
        if name in caps.scalars and card.get(name).strip():
            out.scalars[name] = card.get(name)
    for f in _held_fields(card, caps, region):
        out.fields.append(Field(f.kind, f.value, f.label, dict(f.data), f.primary))
    if caps.groups:
        out.groups = list(dict.fromkeys(g for g in card.groups if g.strip()))
    return out


def cards_equal(a: Card, b: Card, caps: Capabilities = ALL_CAPS) -> bool:
    return units(a, caps) == units(b, caps)


@dataclass
class Conflict:
    unit: str
    local: Any
    remote: Any
    winner: str                    # "local" | "remote" | "held"


@dataclass
class MergeResult:
    card: Card
    conflicts: list[Conflict] = field(default_factory=list)
    hub_changed: bool = False       # the merged card differs from the hub's
    push_needed: bool = False       # the merged card differs from what the account holds
    held: bool = False              # a clash was left for the person ("ask me every time")


def _join_notes(local: str, remote: str) -> str:
    """Both notes, once each: if one already contains the other the longer stands, else local then remote."""
    a, b = local.strip(), remote.strip()
    if b in a:
        return a
    if a in b:
        return b
    return a + "\n\n" + b


def merge(base: Optional[Card], local: Card, remote: Card, caps: Capabilities, *,
          local_time: float = 0.0, remote_time: float = 0.0, policy: str = "newest",
          remote_wins_ties: bool = False, region: str = phones.DEFAULT_REGION) -> MergeResult:
    """Three-way merge of `local` (the hub) and `remote` (the account) against
    `base` (what the account held when they last agreed), over what `caps` holds.

    `policy` is the person's choice for a real clash: `newest` (the later edit
    wins), `local` or `remote` (an account priority order resolves to one of
    them), or `ask` (the hub keeps its own value, the clash is returned held).
    """
    b = units(base, caps, region) if base is not None else {}
    l = units(local, caps, region)
    r = units(remote, caps, region)
    result: dict[str, Any] = {}
    conflicts: list[Conflict] = []
    held = False
    for key in list(dict.fromkeys([*l.keys(), *r.keys(), *b.keys()])):
        bv, lv, rv = b.get(key), l.get(key), r.get(key)
        if lv == rv:
            pick = lv
        elif lv == bv:
            pick = rv
        elif rv == bv:
            pick = lv
        elif key == "s:notes" and isinstance(lv, str) and isinstance(rv, str) and lv.strip() and rv.strip():
            # Notes are free text that both sides added to: keep both rather than pick one. No clash is
            # recorded - nothing is lost, so there is nothing to review.
            pick = _join_notes(lv, rv)
        else:
            if policy == "local":
                winner = "local"
            elif policy == "remote":
                winner = "remote"
            elif policy == "ask":
                winner = "held"
            else:
                if remote_time > local_time or (remote_time == local_time and remote_wins_ties):
                    winner = "remote"
                else:
                    winner = "local"
            conflicts.append(Conflict(key, lv, rv, winner))
            if winner == "held":
                held = True
            pick = rv if winner == "remote" else lv
        if pick is not None:
            result[key] = pick

    merged = _rebuild(local, remote, result, caps, region)
    hub_changed = units(merged, ALL_CAPS, region) != units(local, ALL_CAPS, region)
    push_needed = units(merged, caps, region) != r
    return MergeResult(card=merged, conflicts=conflicts, hub_changed=hub_changed,
                       push_needed=push_needed, held=held)


def _rebuild(local: Card, remote: Card, chosen: dict[str, Any], caps: Capabilities,
             region: str) -> Card:
    """The card whose supported units are `chosen` and whose unsupported ones
    are the hub's own, in the hub's order."""
    out = Card()
    # scalars
    for name in SCALARS:
        if name in caps.scalars:
            v = chosen.get(f"s:{name}")
            if v:
                out.scalars[name] = v
        elif local.get(name):
            out.scalars[name] = local.get(name)
    # fields: hub order first, remote-added after
    def supported(f: Field) -> bool:
        if f.kind not in caps.kinds:
            return False
        if f.kind == "date" and caps.date_labels is not None and _slug(f.label) not in caps.date_labels:
            return False
        return True

    kept_here = {"f:" + field_key(f, region) for f in _held_fields(local, caps, region)}
    taken: set[str] = set()
    for f in local.fields:
        # A field over the account's room stays as it is, unless the account itself has the value.
        if not supported(f) or ("f:" + field_key(f, region) not in kept_here
                                and "f:" + field_key(f, region) not in chosen):
            out.fields.append(Field(f.kind, f.value, f.label, dict(f.data), f.primary))
            continue
        key = "f:" + field_key(f, region)
        if key in taken or key not in chosen:
            continue
        taken.add(key)
        out.fields.append(_with_attr(f, chosen[key]))
    for f in remote.fields:
        if not supported(f):
            continue
        key = "f:" + field_key(f, region)
        if key in taken or key not in chosen:
            continue
        taken.add(key)
        out.fields.append(_with_attr(f, chosen[key]))
    # groups
    if caps.groups:
        names = {k: v for k, v in chosen.items() if k.startswith("g:")}
        for g in [*local.groups, *remote.groups]:
            k = f"g:{_slug(g)}"
            if k in names:
                out.groups.append(names.pop(k))
    else:
        out.groups = list(local.groups)
    out.favourite = bool(chosen.get("favourite")) if caps.favourite else local.favourite
    if caps.photo:
        want = chosen.get("photo")
        if want:
            if local.photo is not None and local.photo.sha == want:
                out.photo = local.photo
            elif remote.photo is not None and remote.photo.sha == want:
                out.photo = remote.photo
            else:
                out.photo = local.photo or remote.photo
    else:
        out.photo = local.photo
    return out


def _with_attr(f: Field, attr: tuple) -> Field:
    label, value, data, primary = attr
    return Field(f.kind, value, label, dict(data), bool(primary))


def fields_of(card: Card, kinds: Iterable[str]) -> list[Field]:
    wanted = set(kinds)
    return [f for f in card.fields if f.kind in wanted]
