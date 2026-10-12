"""What the Contacts page decides, with no Qt in it.

Layer: L5

Order 1k section 6. The page's rules that are not drawing: which columns there are and how
each sorts, what the sidebar lists and which `ContactFilter` each entry means, the words for
"last contacted", "where it lives" and a journal entry, the labels a field may carry, and the
rules a smart group is made from. Kept apart so they run - and are tested - without a display.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from app.contacts.hub import ContactFilter, ContactRow, JournalRow

__all__ = [
    "STATE_VIEW", "STATE_SORT", "COLUMNS", "column_key", "sort_rows", "initials",
    "last_contacted_text", "where_it_lives", "Entry", "build_entries", "filter_for", "actor_words", "source_words", "merge_order",
    "account_glyph", "describe_entry", "KIND_INFO", "LABELS", "date_icon", "label_icon", "RULE_FIELDS",
    "RULE_OPS", "SMART_EXAMPLES", "rule_text", "TAG_COLOURS", "parse_sort", "hub_error_text",
    "unit_words", "value_words", "conflict_items",
]

#: `index_state` keys (namespaced `ui:` like every remembered choice).
STATE_VIEW = "ui:contacts_view"           # "list" | "cards"
STATE_SORT = "ui:contacts_sort"           # "<column>:<asc|desc>"

#: (key, heading). The order is the order on screen; 6b and 6t.
COLUMNS = (
    ("name", "Name"), ("company", "Company"), ("title", "Title"), ("email", "Email"),
    ("phone", "Phone"), ("accounts", "Accounts"), ("groups", "Groups"), ("tags", "Tags"),
    ("contacted", "Last contacted"), ("lives", "Where it lives"),
)

#: The app's tag palette - the same eight colours accounts are given in turn.
TAG_COLOURS = ("#4C8BF5", "#E8710A", "#188038", "#A142F4", "#D93025", "#129EAF", "#B06000", "#C2185B")


def column_key(index: int) -> str:
    return COLUMNS[index][0] if 0 <= index < len(COLUMNS) else "name"


def parse_sort(text: str) -> tuple[int, bool]:
    """`"company:desc"` -> (column index, descending). Anything unreadable is name, ascending."""
    key, _, order = (text or "").partition(":")
    for i, (name, _h) in enumerate(COLUMNS):
        if name == key:
            return i, order == "desc"
    return 0, False


def where_it_lives(row: ContactRow, names: dict[int, str]) -> tuple[str, str]:
    """(words, state) for 6t. State is `none`, `single` or `multi`."""
    n = len(row.accounts)
    if n == 0:
        return "Only in Leasha", "none"
    if n == 1:
        return f"Only in {names.get(row.accounts[0], 'one account')}", "single"
    return f"In {n} accounts", "multi"


def last_contacted_text(ms: Optional[int], now_s: Optional[float] = None) -> str:
    if not ms:
        return "Never"
    now_s = time.time() if now_s is None else now_s
    days = int((now_s - ms / 1000) // 86400)
    if days < 1:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    if days < 700:
        return f"{max(2, days // 30)} months ago"
    return f"{days // 365} years ago"


def initials(name: str) -> str:
    words = [w for w in (name or "").replace("-", " ").split() if w and w[0].isalnum()]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][0].upper()
    return (words[0][0] + words[-1][0]).upper()


def _sort_value(row: ContactRow, key: str, names: dict[int, str]) -> Any:
    if key == "name":
        return row.display_name.casefold()
    if key == "company":
        return row.company.casefold()
    if key == "title":
        return row.title.casefold()
    if key == "email":
        return row.email.casefold()
    if key == "phone":
        return row.phone
    if key == "accounts":
        return ", ".join(sorted(names.get(a, "") for a in row.accounts)).casefold()
    if key == "groups":
        return ", ".join(row.groups).casefold()
    if key == "tags":
        return ", ".join(row.tags).casefold()
    if key == "contacted":
        return row.last_contacted_at or 0
    if key == "lives":
        return (len(row.accounts), names.get(row.accounts[0], "") if row.accounts else "")
    return ""


def sort_rows(rows: list[ContactRow], key: str, descending: bool, names: dict[int, str]) -> list[ContactRow]:
    """A sorted copy. Empty values go last in either direction, so a blank company never leads."""
    def empty(r: ContactRow) -> bool:
        v = _sort_value(r, key, names)
        return v in ("", 0, (0, ""))

    filled = [r for r in rows if not empty(r)]
    blank = [r for r in rows if empty(r)]
    filled.sort(key=lambda r: (_sort_value(r, key, names), r.display_name.casefold(), r.id), reverse=descending)
    return filled + blank


# --------------------------------------------------------------------- sidebar
@dataclass
class Entry:
    """One line of the sidebar. `kind` says what picking it means."""

    key: str
    kind: str                       # all favourites group tag account conflict duplicates deleted_elsewhere
    label: str                      # single_account bin smart  (and "header")
    icon: str
    count: Optional[int] = None
    arg: Any = None
    colour: str = ""
    section: str = ""
    tip: str = ""


def account_glyph(account: Any) -> str:
    """6m.5: the account's own kind of thing, never a provider logo."""
    if getattr(account, "provider", "") in ("imap", "carddav"):
        return "server"
    return "contact" if getattr(account, "contacts_on", True) else "mail"


def build_entries(counts: dict[str, int], groups: Iterable, tags: Iterable, accounts: Iterable,
                  smarts: Iterable, account_counts: Optional[dict[int, int]] = None) -> list[Entry]:
    """6c and 6t, in sidebar order. `groups` are (id, name, n), `tags` (id, name, colour, n)."""
    account_counts = account_counts or {}
    out = [
        Entry("all", "all", "All", "contact", counts.get("all", 0), section="Contacts"),
        Entry("favourites", "favourites", "Favourites", "star", counts.get("favourites", 0),
              section="Contacts"),
    ]
    for gid, name, n in groups:
        out.append(Entry(f"group:{gid}", "group", name, "users", n, gid, section="Groups"))
    for tid, name, colour, n in tags:
        out.append(Entry(f"tag:{tid}", "tag", name, "tag", n, tid, colour, section="Tags"))
    for a in accounts:
        out.append(Entry(f"account:{a.id}", "account", a.name or a.address, account_glyph(a),
                         account_counts.get(a.id), a.id, a.colour, section="Accounts", tip=a.address))
    out += [
        Entry("conflict", "conflict", "Changed in two places", "triangle-alert",
              counts.get("conflict", 0), section="Needs a look"),
        Entry("duplicates", "duplicates", "Possible duplicates", "merge", None, section="Needs a look"),
        Entry("deleted_elsewhere", "deleted_elsewhere", "Deleted elsewhere", "unlink",
              counts.get("deleted_elsewhere", 0), section="Needs a look"),
        Entry("single_account", "single_account", "Single account", "cloud",
              counts.get("single_account", 0), section="Needs a look",
              tip="Contacts kept in only one account - what an account closing would lose"),
    ]
    for s in smarts:
        out.append(Entry(f"smart:{s.id}", "smart", s.name, s.icon or "sliders-horizontal", None, s.id,
                         section="Smart groups"))
    out.append(Entry("bin", "bin", "Bin", "trash-2", counts.get("bin", 0), section="Bin"))
    return out


def filter_for(entry: Entry, text: str = "", sort: str = "name") -> ContactFilter:
    view = entry.kind if entry.kind not in ("duplicates",) else "all"
    return ContactFilter(view=view, arg=entry.arg, text=text, sort=sort)


# --------------------------------------------------------------------- history
_ACTIONS = {
    "create": ("user-plus", "Created"), "update": ("pencil", "Edited"),
    "delete": ("trash-2", "Moved to the bin"), "restore": ("archive-restore", "Restored from the bin"),
    "kept_in": ("cloud", "Changed where it is kept"), "group": ("users", "Changed its groups"),
    "tag": ("tag", "Changed its tags"), "favourite": ("star", "Changed favourite"),
    "merge": ("merge", "Merged"), "split": ("split", "Split"), "undo": ("undo-2", "Undone"),
    "conflict": ("triangle-alert", "Changed in two places"),
    "sync": ("refresh-cw", "Changed by a sync"), "restore_to_account": ("archive-restore", "Written back to an account"),
}


def describe_entry(j: JournalRow, account_names: Optional[dict[int, str]] = None) -> tuple[str, str]:
    """(icon, words) for one journal entry."""
    icon_name, words = _ACTIONS.get(j.action, ("history", j.action.replace("_", " ").capitalize()))
    names = account_names or {}
    detail = j.detail or {}
    extra = []
    for key, verb in (("added", "added to"), ("removed", "removed from")):
        ids = detail.get(key) if isinstance(detail, dict) else None
        if ids:
            extra.append(f"{verb} " + ", ".join(names.get(int(i), f"account {i}") for i in ids))
    if extra:
        words += " (" + "; ".join(extra) + ")"
    return icon_name, f"{words} by {actor_words(j.actor, names)}"


def actor_words(actor: str, names: Optional[dict[int, str]] = None) -> str:
    """'you', 'an undo', or the account a change came from."""
    if actor.startswith("account:") and actor[8:].isdigit():
        return (names or {}).get(int(actor[8:]), "an account")
    return {"user": "you", "undo": "an undo", "sync": "a sync"}.get(actor, actor)


def source_words(sources: list, names: Optional[dict[int, str]] = None) -> str:
    """Hover text for a field: where the hub learnt it (2a's sources). '' when it was typed here."""
    if not sources:
        return ""
    parts = []
    for source, ref in sources:
        if source.startswith("account:"):
            parts.append(f"synced from {actor_words(source, names)}")
        elif source:
            parts.append(f"imported ({source}{': ' + ref if ref else ''})")
    return ("Came from: " + "; ".join(dict.fromkeys(parts))) if parts else ""


def hub_error_text(exc: Any) -> str:
    return str(getattr(exc, "message", None) or exc)


# ---------------------------------------------------------------------- fields
#: kind -> (heading, icon, placeholder for the value)
KIND_INFO = {
    "email": ("Email", "mail", "name@example.com"),
    "phone": ("Phone", "phone", "+44 7700 900123"),
    "address": ("Address", "map-pin", "Street, town, postcode"),
    "url": ("Website", "globe", "https://"),
    "date": ("Dates", "cake", "YYYY-MM-DD"),
    "related": ("Related people", "users", "Name of the person"),
    "social": ("Social handle", "at-sign", "@name"),
    "im": ("Messaging", "message-square", "Account name"),
    "custom": ("Other", "link", "Anything else"),
}

_BASIC = ("work", "home", "mobile", "other")
LABELS = {
    "email": ("work", "home", "other"),
    "phone": ("mobile", "work", "home", "fax", "other"),
    "address": ("home", "work", "other"),
    "url": ("work", "home", "blog", "profile", "other"),
    "date": ("birthday", "anniversary", "other"),
    "related": ("spouse", "partner", "child", "parent", "sibling", "friend", "manager", "assistant", "other"),
    "social": ("linkedin", "x", "facebook", "instagram", "other"),
    "im": ("whatsapp", "signal", "skype", "other"),
    "custom": ("custom",),
}


def date_icon(label: str) -> str:
    return "cake" if (label or "").strip().casefold() == "birthday" else "calendar"


#: What each label of a phone, email or address looks like. A label not listed (or "other") keeps the kind's own.
_LABEL_ICONS = {
    "phone": {"mobile": "smartphone", "cell": "smartphone", "work": "briefcase", "home": "house", "fax": "printer"},
    "email": {"work": "briefcase", "home": "house"},
    "address": {"home": "house", "work": "building-2"},
}


def label_icon(kind: str, label: str, default: str = "") -> str:
    """The picture for one row: a house for home, a briefcase for work, a handset for mobile, a printer for fax."""
    if kind == "date":
        return date_icon(label)
    text = (label or "").strip().casefold()
    return _LABEL_ICONS.get(kind, {}).get(text) or default or KIND_INFO.get(kind, ("", "tag"))[1]


# ----------------------------------------------------------------- smart groups
RULE_FIELDS = (
    ("company", "Company"), ("title", "Title"), ("name", "Name"), ("email", "Email"),
    ("phone", "Phone number"), ("group", "Group"), ("tag", "Tag"), ("account", "Account"),
    ("favourite", "Favourite"), ("last_contacted", "Last contacted"),
)
RULE_OPS = (
    ("is", "is"), ("is_not", "is not"), ("contains", "contains"), ("empty", "is empty"),
    ("not_empty", "is not empty"), ("older_than_days", "is older than (days)"),
)
#: What the order names: "company is X", "no phone number", "not contacted in a year".
SMART_EXAMPLES = (
    ("No phone number", [{"field": "phone", "op": "empty", "value": ""}]),
    ("Not contacted in a year", [{"field": "last_contacted", "op": "older_than_days", "value": "365"}]),
)


def rule_text(rule: dict, account_names: Optional[dict[int, str]] = None) -> str:
    fields, ops = dict(RULE_FIELDS), dict(RULE_OPS)
    value = str(rule.get("value", "") or "")
    if rule.get("field") == "account" and value.isdigit() and account_names:
        value = account_names.get(int(value), value)
    op = ops.get(rule.get("op", "is"), rule.get("op", ""))
    tail = "" if rule.get("op") in ("empty", "not_empty") else f" {value}"
    return f"{fields.get(rule.get('field', ''), rule.get('field', ''))} {op}{tail}".strip()


def merge_order(original: list, per_kind: dict[str, list]) -> list:
    """The card's fields in one list, the way the card had them.

    The editor shows one list per kind, so it hands back a list per kind. Putting them back in
    the sequence of kinds the card started with means a card nobody edited compares equal to
    itself (no journal entry for a Save that changed nothing); kinds the card did not have, and
    values added at the end of a kind, follow in the editor's order.
    """
    queues = {k: list(v) for k, v in per_kind.items()}
    out = []
    for f in original:
        q = queues.get(f.kind)
        if q:
            out.append(q.pop(0))
    for kind in KIND_INFO:
        out += queues.get(kind, [])
    return out


# ----------------------------------------------------------------- conflicts (D6, 10k)
_SCALAR_WORDS = {"prefix": "Title", "given": "First name", "additional": "Middle name", "family": "Last name",
                 "suffix": "Suffix", "nickname": "Nickname", "company": "Company", "department": "Department",
                 "title": "Job title", "notes": "Notes"}


def unit_words(unit: str) -> str:
    """A conflict's unit key in plain words: `s:company` is "Company", `f:phone:mobile:0771..` is
    "Phone (mobile)"."""
    kind, _, rest = unit.partition(":")
    if kind == "s":
        return _SCALAR_WORDS.get(rest, rest.replace("_", " ").capitalize())
    if kind == "f":
        field_kind, _, more = rest.partition(":")
        name = KIND_INFO.get(field_kind, (field_kind.capitalize(),))[0]
        label = more.partition(":")[0] if ":" in more else ""
        return f"{name} ({label})" if label else name
    if kind == "g":
        return "Group"
    return {"favourite": "Favourite", "photo": "Photo"}.get(unit, unit)


def value_words(unit: str, raw: Any) -> str:
    """One side of a conflict as it reads to a person. `raw` is the JSON text the hub stored."""
    import json

    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        value = raw
    if value is None:
        return "nothing (removed)"
    if unit == "photo":
        return "a different photo"
    if unit == "favourite":
        return "a favourite" if value else "not a favourite"
    if isinstance(value, list) and len(value) >= 2 and unit.startswith("f:"):
        data = value[2] if len(value) > 2 and isinstance(value[2], list) else []
        parts = [str(v) for _k, v in data if v] if unit.startswith("f:address") else []
        text = ", ".join(parts) if parts else str(value[1])
        label = str(value[0] or "")
        return f"{text} ({label})" if label else text
    return str(value)


def conflict_items(conflicts: list[dict], names: dict[int, str]) -> list[dict]:
    """The open clashes of one contact, ready to show: unit and both values in words, who won
    (`Leasha` or the account's name), and the account. `held` winners wait for the person."""
    out = []
    for c in conflicts:
        unit = str(c.get("unit") or "")
        aid = c.get("account_id")
        account = names.get(aid, "the account") if aid is not None else "the account"
        out.append({"id": c.get("id"), "account_id": aid, "account": account, "unit": unit,
                    "what": unit_words(unit), "winner": str(c.get("winner") or ""),
                    "local": value_words(unit, c.get("local_value")),
                    "remote": value_words(unit, c.get("remote_value"))})
    return out
