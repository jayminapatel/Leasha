"""The `/` switches in a chat question: `/from dave what did he say about the contract`.

Layer: L4. The same grammar as every other box - `app.search.commands.expand_slashes` turns `/from dave` into
`from:dave` and `app.search.query` knows the fields - so a switch means the same thing in Chat as in Search, Mail
and Files. What is left after the switches are taken out is the question, read as it always was; the switches
become filters on the search that answers it.
"""

from __future__ import annotations

from app.search.commands import expand_slashes

__all__ = ["split_switches"]


def split_switches(text: str) -> tuple[str, list[str]]:
    """`(the question without its switches, the switches as `field:value` filters)`.

    A question with no switch comes back unchanged. When the switches were all there was, the question is
    returned whole, so there is still something to ask."""
    from app.search.query import _OPERATOR

    raw = str(text or "")
    if "/" not in raw and ":" not in raw:
        return raw, []
    expanded = expand_slashes(raw)
    filters = [m.group(0).strip() for m in _OPERATOR.finditer(expanded)]
    if not filters:
        return raw, []
    rest = " ".join(_OPERATOR.sub(" ", expanded).split())
    return (rest or raw), filters
