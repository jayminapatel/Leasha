"""What the Contacts tool dialogs and the command line do to the book, with no Qt in it.

Layer: L7

Order 1k 6g, 6i, 6n, 6o, 6p. The dialogs ask and show; this module reads and writes,
so every claim the order makes about them is a function a test can call without a
window, and a worker thread can run (rule 5).

**One batch, one undo.** Each writer takes or makes a `batch_id` and hands it to every
hub call, so the journal holds the whole action under one id and `hub.undo_batch(id)`
puts all of it back (6n.2, 6o, 6p, 6i). A writer that changes nothing returns no batch
id, so a button is never offered for an undo of nothing.

**Safe means safe (D9).** `plan_safe_merges` takes `dupes.safe_groups` and then checks
each group again against its reasons; a name-only group cannot get through even if a
caller passes one in by mistake.
"""

from __future__ import annotations

import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from app.contacts import dupes
from app.contacts.card import (ALL_CAPS, SCALARS, Capabilities, Card, display_name, field_key, project,
                               units)

__all__ = [
    "book_version", "ScanResult", "scan_book", "MergeStep", "pick_survivor", "plan_safe_merges",
    "merge_safe", "reject_group", "rejected_rows", "suggest_again", "MergeModel", "caps_for",
    "describe_unit", "projection", "commit_merge", "ImportRow", "build_import_rows", "ImportResult",
    "commit_import", "CleanupResult", "apply_changes", "remove_copies", "bin_contacts", "new_batch",
    "SAFE_REASONS",
]

SAFE_REASONS = frozenset({"same email", "same phone"})


def new_batch() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------
# Duplicates (6n)
# --------------------------------------------------------------------------

def book_version(hub: Any) -> tuple:
    """Something that changes whenever the book does: how many live contacts, the sum of
    their edit counters (an edit to any one raises it), the newest change time and the
    number of rejected pairs. The duplicate scan is kept until this differs."""
    row = hub.conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(rev), 0), COALESCE(MAX(updated_at), 0), COALESCE(MAX(id), 0) "
        "FROM contacts WHERE deleted_at IS NULL").fetchone()
    rejected = hub.conn.execute("SELECT COUNT(*) FROM contact_rejected").fetchone()[0]
    return (tuple(int(x) for x in row), int(rejected), len(hub.shared_values()))


@dataclass
class ScanResult:
    groups: list
    version: tuple
    cards: dict[int, Card] = field(default_factory=dict)       # every member of every group
    cached: bool = False

    @property
    def contacts(self) -> int:
        return len({i for g in self.groups for i in g.ids})


_SCAN_CACHE: "weakref.WeakKeyDictionary[Any, ScanResult]" = weakref.WeakKeyDictionary()


def scan_book(hub: Any, *, use_cache: bool = True) -> ScanResult:
    """Scan the whole book for people held twice. The result is kept (per hub) until
    `book_version` changes, so reopening the screen is instant and a rescan only happens
    when something moved."""
    version = book_version(hub)
    held = _SCAN_CACHE.get(hub)
    if use_cache and held is not None and held.version == version:
        return ScanResult(held.groups, held.version, held.cards, cached=True)
    items = hub.all_cards()
    groups = dupes.scan_duplicates(items, hub.rejected_pairs(), hub.region, hub.shared_values())
    wanted = {i for g in groups for i in g.ids}
    cards = {i: c for i, c in items if i in wanted}
    result = ScanResult(groups, version, cards)
    _SCAN_CACHE[hub] = result
    return result


def forget_scan(hub: Any) -> None:
    _SCAN_CACHE.pop(hub, None)


def pick_survivor(ids: Sequence[int], cards: dict[int, Card]) -> int:
    """The contact that keeps its id when a group is folded into one: the one holding the
    most information, the first listed on a tie."""
    best, best_n = ids[0], -1
    for cid in ids:
        card = cards.get(cid)
        n = len(units(card)) if card is not None else 0
        if n > best_n:
            best, best_n = cid, n
    return best


@dataclass
class MergeStep:
    ids: tuple
    survivor: int
    others: list[int]
    card: Card
    reasons: tuple
    names: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Changed in two places: review and accept
# --------------------------------------------------------------------------

def _unit_value(raw: Any) -> Any:
    """The stored JSON text of one side of a clash, as the value `card.units` makes (lists back to tuples)."""
    import json

    value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    if isinstance(value, list) and len(value) == 4:
        label, text, data, primary = value
        return (label, text, tuple(sorted((k, v) for k, v in (data or []))), bool(primary))
    return value


def _put_unit(hub: Any, card: Card, unit: str, value: Any) -> bool:
    """Set `unit` on `card` to `value` (None removes it). False when the unit is not one this can set."""
    from app.contacts.card import Field  # noqa: PLC0415

    kind, _, rest = unit.partition(":")
    if kind == "s":
        if rest not in SCALARS:
            return False
        if value:
            card.scalars[rest] = str(value)
        else:
            card.scalars.pop(rest, None)
        return True
    if kind == "f":
        for f in list(card.fields):
            if "f:" + field_key(f, hub.region) == unit:
                card.fields.remove(f)
        if value is not None:
            label, text, data, primary = value
            card.fields.append(Field(rest.partition(":")[0], text, label, dict(data), bool(primary)))
        return True
    return False


def _agree_base(hub: Any, cid: int, account_id: int, unit: str, remote_value: Any) -> None:
    """A held clash is settled: the account's value of `unit` becomes part of what Leasha and the account last
    agreed (the link's merge base), so the next sync sees only Leasha's side as changed - and sends it, or finds
    nothing to do - instead of finding both sides changed and holding the same clash again (review CS-6).
    A held clash leaves the base alone (`ContactSyncEngine._inbound`), so without this it was asked again."""
    link = hub.link(cid, account_id)
    if link is None or link.snapshot is None:
        return
    base = link.snapshot.copy()
    if _put_unit(hub, base, unit, remote_value):
        hub.set_link(cid, account_id, remote_id=link.remote_id, version_tag=link.version_tag, snapshot=base,
                     hub_rev=link.hub_rev, state=link.state)


def open_conflict_rows(hub: Any) -> list[dict]:
    """Every clash still open, each with its contact's name: what the review window lists."""
    rows = hub.open_conflicts()
    names: dict[int, str] = {}
    for r in rows:
        cid = int(r["contact_id"])
        if cid not in names:
            card = hub.get_card(cid)
            names[cid] = display_name(card) if card is not None else f"Contact {cid}"
        r["contact_name"] = names[cid]
    return rows


def accept_conflicts(hub: Any, picks: Sequence[tuple[int, str]]) -> MergeSummary:
    """Settle clashes: `picks` is `(conflict id, "local" | "remote")`, the side to keep.

    The hub already holds the winner's value, so keeping the winner only clears the clash; keeping the other
    side writes that value to the contact (which the next sync sends on). One batch, so one undo."""
    batch = new_batch()
    open_by_id = {int(r["id"]): r for r in hub.open_conflicts()}
    switched = 0
    for conflict_id, side in picks:
        row = open_by_id.get(int(conflict_id))
        if row is None or side not in ("local", "remote"):
            continue
        current = "remote" if row.get("winner") == "remote" else "local"
        if side != current:
            cid = int(row["contact_id"])
            card = hub.get_card(cid)
            value = _unit_value(row["remote_value" if side == "remote" else "local_value"])
            if card is not None and _put_unit(hub, card, str(row["unit"]), value):
                hub.update(cid, card, action="conflict-choice", batch_id=batch)
                switched += 1
        if row.get("winner") == "held" and row.get("account_id") is not None:
            _agree_base(hub, int(row["contact_id"]), int(row["account_id"]), str(row["unit"]),
                        _unit_value(row["remote_value"]))
        hub.resolve_conflict_row(int(conflict_id))
    forget_scan(hub)
    return MergeSummary(batch if switched else None, len(picks), switched)


def plan_chosen_merges(groups: Iterable, cards: dict[int, Card], region: str = "GB") -> list[MergeStep]:
    """What merging the groups the person ticked will do. Unlike `plan_safe_merges` it asks nothing of
    the reasons: the person looked and chose. A group of fewer than two live cards is left out."""
    steps: list[MergeStep] = []
    for g in groups:
        members = [cards[i] for i in g.ids if i in cards]
        if len(members) < 2:
            continue
        survivor = pick_survivor(list(g.ids), cards)
        others = [i for i in g.ids if i != survivor]
        ordered = [cards[survivor]] + [cards[i] for i in others]
        merged = dupes.merge_cards(ordered, region=region).card
        steps.append(MergeStep(tuple(g.ids), survivor, others, merged, tuple(g.reasons),
                               [display_name(cards[i]) for i in g.ids]))
    return steps


def plan_safe_merges(groups: Iterable, cards: dict[int, Card], region: str = "GB") -> list[MergeStep]:
    """What 'Merge all safe matches' will do, group by group, before it does anything.

    Only groups `dupes` flags safe, and only if every reason is an exact email or an exact
    phone: a group that is safe by accident of a caller's list is refused here again.
    """
    steps: list[MergeStep] = []
    for g in dupes.safe_groups(groups):
        if not g.safe or not set(g.reasons) or not set(g.reasons) <= SAFE_REASONS:
            continue
        members = [cards[i] for i in g.ids if i in cards]
        if len(members) < 2:
            continue
        survivor = pick_survivor(list(g.ids), cards)
        others = [i for i in g.ids if i != survivor]
        ordered = [cards[survivor]] + [cards[i] for i in others]
        merged = dupes.merge_cards(ordered, region=region).card
        steps.append(MergeStep(tuple(g.ids), survivor, others, merged, tuple(g.reasons),
                               [display_name(cards[i]) for i in g.ids]))
    return steps


@dataclass
class MergeSummary:
    batch_id: Optional[str]
    groups: int
    contacts: int


def merge_safe(hub: Any, steps: Sequence[MergeStep], batch_id: Optional[str] = None) -> MergeSummary:
    """Merge every planned group as ONE journal batch."""
    if not steps:
        return MergeSummary(None, 0, 0)
    batch = batch_id or new_batch()
    folded = 0
    for step in steps:
        hub.merge(step.survivor, step.others, step.card, batch_id=batch)
        folded += len(step.others)
    forget_scan(hub)
    return MergeSummary(batch, len(steps), folded + len(steps))


def reject_group(hub: Any, ids: Iterable[int]) -> int:
    """'Not a duplicate': every pair in the group is remembered and never suggested again."""
    pairs = dupes.pairs_of(list(ids))
    for a, b in pairs:
        hub.reject_pair(a, b)
    return len(pairs)


def rejected_rows(hub: Any) -> list[tuple[int, int, str, str]]:
    """The rejected pairs for the 'Rejected' list: (a, b, name of a, name of b)."""
    rows = []
    for a, b in sorted(hub.rejected_pairs()):
        ca, cb = hub.get_card(a), hub.get_card(b)
        rows.append((a, b, display_name(ca) if ca else f"Contact {a}", display_name(cb) if cb else f"Contact {b}"))
    return rows


def suggest_again(hub: Any, pairs: Iterable[tuple[int, int]]) -> int:
    n = 0
    for a, b in pairs:
        hub.unreject_pair(a, b)
        n += 1
    return n


# --------------------------------------------------------------------------
# The merge dialog (6g, 6o)
# --------------------------------------------------------------------------

def caps_for(ctx: Any, account: Any) -> Capabilities:
    """What an account can hold. The host's own answer when it has one (`ctx.caps_for`),
    else the account's provider through the service, else everything (the full card)."""
    custom = getattr(ctx, "caps_for", None)
    if callable(custom):
        try:
            got = custom(account)
            if isinstance(got, Capabilities):
                return got
        except Exception:                                    # noqa: BLE001 - fall through to the next source
            pass
    service = getattr(ctx, "service", None)
    if service is not None and hasattr(service, "contact_provider"):
        try:
            caps = getattr(service.contact_provider(account), "caps", None)
            if isinstance(caps, Capabilities):
                return caps
        except Exception:                                    # noqa: BLE001 - no sign-in yet: show the full card
            pass
    return ALL_CAPS


def projection(card: Card, caps: Capabilities) -> Card:
    """The card as the account would be sent it (D10)."""
    return project(card, caps)


def describe_unit(key: str, card: Card) -> str:
    """A unit key of `card.units` in words."""
    if key.startswith("s:"):
        name = key[2:]
        return f"{name}: {card.get(name)}"
    if key.startswith("f:"):
        for f in card.fields:
            if "f:" + field_key(f) == key:
                return f"{f.kind}: {f.value or ', '.join(v for v in f.data.values() if v)}"
        return key[2:]
    if key.startswith("g:"):
        return f"group: {key[2:]}"
    return key


def unsent(card: Card, caps: Capabilities) -> list[str]:
    """What the account will not be sent, in words."""
    full, held = units(card, ALL_CAPS), units(project(card, caps), ALL_CAPS)
    return [describe_unit(k, card) for k in full if k not in held]


class MergeModel:
    """The state of a merge dialog: the cards, each choice, and the card that results.

    A scalar's source is a position in `cards` (default: the first card that has it, as
    `dupes.merge_cards` decides); a repeatable value is kept unless its key is in
    `dropped`. `result()` is exactly the card `commit_merge` writes.
    """

    def __init__(self, ids: Sequence[int], cards: Sequence[Card], region: str = "GB") -> None:
        if len(ids) != len(cards) or len(cards) < 2:
            raise ValueError("A merge needs at least two contacts.")
        self.ids = list(ids)
        self.cards = list(cards)
        self.region = region
        self.choices: dict[str, int] = {}
        self.dropped: set[str] = set()
        self.survivor_index = 0

    def choose(self, key: str, index: Optional[int]) -> None:
        if index is None:
            self.choices.pop(key, None)
        else:
            self.choices[key] = index

    def keep(self, key: str, on: bool) -> None:
        (self.dropped.discard if on else self.dropped.add)(key)

    def preview(self) -> "dupes.MergePreview":
        return dupes.merge_cards(self.cards, self.choices, self.region)

    def result(self) -> Card:
        card = self.preview().card
        if self.dropped:
            card.fields = [f for f in card.fields if field_key(f, self.region) not in self.dropped]
        return card

    @property
    def survivor(self) -> int:
        return self.ids[self.survivor_index]

    @property
    def others(self) -> list[int]:
        return [i for n, i in enumerate(self.ids) if n != self.survivor_index]

    def repeatable_keys(self) -> list[tuple[str, Any, list[int]]]:
        """Each distinct repeatable value once: (key, first Field holding it, positions of the
        cards that hold it)."""
        order: dict[str, tuple[Any, list[int]]] = {}
        for n, card in enumerate(self.cards):
            for f in card.fields:
                if not f.value.strip() and not any(f.data.values()):
                    continue
                key = field_key(f, self.region)
                if key not in order:
                    order[key] = (f, [])
                if n not in order[key][1]:
                    order[key][1].append(n)
        return [(k, f, held) for k, (f, held) in order.items()]


def commit_merge(hub: Any, model: MergeModel, batch_id: Optional[str] = None) -> str:
    """Write the merge: the survivor takes the result, the others are folded in. One batch;
    `hub.undo_batch(batch)` splits them again."""
    batch = batch_id or new_batch()
    result = model.result()
    hub.merge(model.survivor, model.others, result, batch_id=batch)
    forget_scan(hub)
    return batch


# --------------------------------------------------------------------------
# Import (6i)
# --------------------------------------------------------------------------

@dataclass
class ImportRow:
    index: int
    card: Card
    source_row: int = 0
    warnings: list[str] = field(default_factory=list)
    matches: list[int] = field(default_factory=list)          # contacts already in the book
    match_names: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    same_as: Optional[int] = None                              # an earlier row of the same file
    choice: str = "new"                                        # new | merge | skip
    target: Optional[int] = None                               # the existing contact to merge into
    source: str = ""                                           # the file this row came from


def build_import_rows(hub: Any, preview: Any) -> list[ImportRow]:
    """Every card of the file with the copies already in the book found by email and phone
    (`find_by_email`, `find_by_phone`) and the copies inside the file itself. Reads only."""
    from app.contacts import importers                           # noqa: PLC0415 - optional until built

    rows: list[ImportRow] = []
    for n, item in enumerate(preview.contacts):
        row = ImportRow(n, item.card, getattr(item, "source_row", n + 1), list(getattr(item, "warnings", [])))
        found: dict[int, list[str]] = {}
        for f in item.card.fields:
            text = f.value.strip()
            if not text:
                continue
            if f.kind == "email":
                for cid in hub.find_by_email(text):
                    found.setdefault(cid, []).append(f"same email {text}")
            elif f.kind == "phone":
                for cid in hub.find_by_phone(text):
                    found.setdefault(cid, []).append(f"same phone {text}")
        for cid, why in found.items():
            existing = hub.get_card(cid)
            row.matches.append(cid)
            row.match_names.append(display_name(existing) if existing else f"Contact {cid}")
            row.reasons.extend(r for r in why if r not in row.reasons)
        if row.matches:
            row.choice, row.target = "merge", row.matches[0]
        rows.append(row)
    for group in importers.find_internal_duplicates(preview.contacts, hub.region):
        first = group.indices[0]
        for later in group.indices[1:]:
            row = rows[later]
            row.same_as = first
            row.reasons.append(f"also in this file (row {rows[first].source_row})")
            if row.target is None:
                row.choice = "merge"
    return rows


@dataclass
class ImportResult:
    batch_id: Optional[str]
    added: int = 0
    merged: int = 0
    skipped: int = 0
    ids: list[int] = field(default_factory=list)


def commit_import(hub: Any, rows: Sequence[ImportRow], source_name: str, *,
                  kept_in: Iterable[int] = (), batch_id: Optional[str] = None) -> ImportResult:
    """Add what the person confirmed, as ONE journal batch with source='import' and the file
    name as the receipt. Nothing is read from or written to the source file here.

    'new' creates a contact; 'merge' folds the card into the existing contact it names (or,
    for a copy inside the file, into the contact its earlier row became); 'skip' does nothing.
    """
    batch = batch_id or new_batch()
    accounts = [int(a) for a in kept_in]
    holder: dict[int, int] = {}                                 # row index -> the contact it ended up in
    out = ImportResult(None)
    for row in sorted(rows, key=lambda r: r.index):
        if row.choice == "skip":
            out.skipped += 1
            continue
        if row.choice == "merge":
            target = row.target if row.target is not None else holder.get(row.same_as if row.same_as is not None else -1)
            if target is not None and hub.exists(target):
                existing = hub.get_card(target)
                merged = dupes.merge_cards([existing, row.card], region=hub.region).card
                hub.update(target, merged, action="merge", batch_id=batch, source="import", source_ref=row.source or source_name)
                holder[row.index] = target
                out.merged += 1
                if target not in out.ids:
                    out.ids.append(target)
                continue
        cid = hub.create(row.card, kept_in=accounts, batch_id=batch, source="import",
                         source_ref=row.source or source_name)
        holder[row.index] = cid
        out.added += 1
        out.ids.append(cid)
    if out.added or out.merged:
        out.batch_id = batch
        forget_scan(hub)
    return out


# --------------------------------------------------------------------------
# Clean-up (6p)
# --------------------------------------------------------------------------

@dataclass
class CleanupResult:
    batch_id: Optional[str]
    contacts: int = 0
    changes: int = 0


def apply_changes(hub: Any, changes: Iterable[Any], *, batch_id: Optional[str] = None,
                  action: str = "cleanup") -> CleanupResult:
    """Apply `cleanup.Change` objects the person accepted: one `hub.update` per contact, all
    under one batch. A change whose 'before' text is no longer on the card is skipped, so a
    stale preview cannot overwrite an edit made since."""
    by_contact: dict[Any, list[Any]] = {}
    for c in changes:
        by_contact.setdefault(c.id, []).append(c)
    batch = batch_id or new_batch()
    out = CleanupResult(None)
    for cid, items in by_contact.items():
        card = hub.get_card(cid)
        if card is None:
            continue
        done = 0
        for c in items:
            if c.field == "phone":
                for f in card.fields:
                    if f.kind == "phone" and f.value == c.before:
                        f.value = c.after
                        done += 1
            elif c.field.startswith("s:"):
                name = c.field[2:]
                if name in SCALARS and card.get(name) == c.before:
                    card.scalars[name] = c.after
                    done += 1
        if done and hub.update(cid, card, action=action, batch_id=batch):
            out.contacts += 1
            out.changes += done
    if out.contacts:
        out.batch_id = batch
        forget_scan(hub)
    return out


def remove_copies(hub: Any, ids: Iterable[int], *, batch_id: Optional[str] = None) -> CleanupResult:
    """Drop the second and later copies of a value inside each contact."""
    from app.contacts import cleanup                              # noqa: PLC0415

    batch = batch_id or new_batch()
    out = CleanupResult(None)
    for cid in ids:
        card = hub.get_card(cid)
        if card is None:
            continue
        fixed = cleanup.remove_field_copies(card, hub.region)
        if hub.update(cid, fixed, action="cleanup", batch_id=batch):
            out.contacts += 1
            out.changes += len(card.fields) - len(fixed.fields)
    if out.contacts:
        out.batch_id = batch
    return out


def bin_contacts(hub: Any, ids: Iterable[int], *, batch_id: Optional[str] = None) -> CleanupResult:
    """Send contacts to the bin (30 days) as one batch."""
    batch = batch_id or new_batch()
    out = CleanupResult(None)
    for cid in ids:
        if hub.delete(cid, batch_id=batch):
            out.contacts += 1
            out.changes += 1
    if out.contacts:
        out.batch_id = batch
        forget_scan(hub)
    return out
