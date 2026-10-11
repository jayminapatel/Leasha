"""The `/` switches of the Contacts box, as filter rules. Qt-free.

Layer: L7. The box takes `/company acme`, `/email gmail` or the `company:acme` the menu inserts; what is
left after the switches is the free text the full-text search takes. A switch means a rule of
`ContactHub.ids_for_rules`, so the box, the smart groups and the duplicates tools share one reading.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = ["SWITCHES", "HAS_VALUES", "split"]

#: switch -> (rule field, rule op). `has:` and `favourite` are handled in `split`.
SWITCHES: dict[str, tuple[str, str]] = {
    "company": ("company", "contains"),
    "title": ("title", "contains"),
    "email": ("email", "contains"),
    "phone": ("phone", "contains"),
    "name": ("name", "contains"),
    "tag": ("tag", "is"),
    "group": ("group", "is"),
    "quiet": ("last_contacted", "older_than_days"),
}
#: What `/has` can ask for: the fields a rule can test for being filled in.
HAS_VALUES: tuple[str, ...] = ("email", "phone", "company", "title")
_VALUELESS = ("favourite",)

_TOKEN = re.compile(r'[^\s"]*"[^"]*"|\S+')


def _bare(token: str) -> str:
    return token[1:-1] if len(token) > 1 and token[0] == token[-1] == '"' else token


def split(text: str) -> tuple[str, list[dict]]:
    """`(free text, rules)` for what was typed. Anything that is not a known switch stays in the text,
    exactly as typed - a path like /var/log or a date like 12/03 is somebody's search, not a command."""
    tokens = _TOKEN.findall(text or "")
    free: list[str] = []
    rules: list[dict] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        name, value, used = "", "", 1
        if token.startswith("/") and token[1:].lower() in (*SWITCHES, "has", *_VALUELESS):
            name = token[1:].lower()
            if name not in _VALUELESS and i + 1 < len(tokens):
                value, used = _bare(tokens[i + 1]), 2
        elif ":" in token and not token.startswith('"'):
            head, _, tail = token.partition(":")
            if head.lower() in (*SWITCHES, "has", *_VALUELESS):
                name, value = head.lower(), _bare(tail)
                if not value and name not in _VALUELESS and i + 1 < len(tokens):
                    value, used = _bare(tokens[i + 1]), 2
        if not name:
            free.append(token)
            i += 1
            continue
        i += used
        if name == "favourite":
            rules.append({"field": "favourite", "op": "is", "value": "yes"})
        elif not value:
            continue                                  # a switch with nothing after it yet: no rule
        elif name == "has":
            if value.lower() in HAS_VALUES:
                rules.append({"field": value.lower(), "op": "not_empty", "value": ""})
        else:
            field, op = SWITCHES[name]
            rules.append({"field": field, "op": op, "value": value})
    return " ".join(free), rules
