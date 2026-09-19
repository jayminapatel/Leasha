"""Saying "nothing found" honestly: the ABSENCE protocol.

Layer: L8b - no Qt, no network, no model. Work order section 1e.

When retrieval comes back empty the tempting answers are both wrong. "That
doesn't exist" is a claim about the world, made from a search of one index.
"Here's my best guess" is a guess dressed as an answer. This answers the question
that was actually asked - *what does Leasha know?* - and shows its working:

1. **what was searched** - the queries, visibly, including what was dropped when
   the first search was too narrow;
2. **the honest scope sentence** - nothing in *Leasha's index* matches, and what
   the index currently holds, so "no" can be weighed against how much there is to
   have said yes to;
3. **the obvious next steps** - other words, and the drive or folder that may not
   have been scanned yet.

The text is the system's own, from a template: it makes no claim about what any
document says, so it carries no receipts and needs none. A test pins that it never
says a thing does not exist (it says the index does not hold it).
"""

from __future__ import annotations

from typing import Any, Sequence

from app.core.logging import logger

__all__ = ["absence_text", "index_summary", "NEXT_STEPS", "WORLD_CLAIMS"]

log = logger.bind(component="chat.absence")

_COUNTED = ("INDEXED", "PARTIAL", "NAME_ONLY")

NEXT_STEPS = (
    "Try different words, or a name or date it would have been written with. "
    "If it lives on a drive or in a folder Leasha has not scanned yet, add it under Indexing "
    "and ask again."
)

#: Phrases the protocol must never use: each asserts non-existence in the world
#: rather than absence from the index. Tested against every absence answer.
WORLD_CLAIMS = (
    "does not exist", "doesn't exist", "do not exist", "don't exist", "no such",
    "never existed", "never had", "there is no ", "there isn't", "you don't have",
    "you do not have", "you never",
)


def index_summary(store: Any) -> str:
    """What the index holds, in a clause: "12,340 files (3,201 of them emails),
    including drives that are offline: Backup 2019". Never raises."""
    try:
        marks = ", ".join("?" for _ in _COUNTED)
        rows = store.conn.execute(
            f"SELECT source_kind, COUNT(*) FROM files WHERE status IN ({marks}) "
            "GROUP BY source_kind", _COUNTED).fetchall()
        total = sum(int(r[1]) for r in rows)
        mail = sum(int(r[1]) for r in rows if r[0] in ("eml", "pst_message"))
    except Exception as exc:                            # noqa: BLE001 - a summary only
        log.debug("index summary unavailable: {}", exc)
        return "the files and emails Leasha has indexed"
    if not total:
        return "nothing yet - no folder has been indexed"
    text = f"{total:,} file{'s' if total != 1 else ''}"
    if mail:
        text += f" ({mail:,} of them email{'s' if mail != 1 else ''})"
    try:
        offline = [v.get("name", "") for v in store.list_volumes()
                   if str(v.get("status", "")).upper() != "ONLINE" and v.get("name")]
        if offline:
            text += f", including drives that are offline: {', '.join(offline[:4])}"
    except Exception:                                   # noqa: BLE001
        pass
    return text


def absence_text(what: str, queries: Sequence[str], store: Any, *,
                 dropped_filters: bool = False,
                 found_something_unhelpful: bool = False) -> tuple[str, list[str]]:
    """`(text, notes)` for a question the index could not answer.

    `found_something_unhelpful` distinguishes "nothing matched" from "documents
    matched but none of them states an answer I could verify" - both end the same
    way, and the wording says which, because they suggest different next steps.
    """
    what = (what or "that").strip().rstrip("?.! ")
    searched = list(dict.fromkeys(q for q in queries if q))
    if found_something_unhelpful:
        lead = (f"Leasha found documents that mention {what}, but none of them states an "
                "answer I can point to.")
    else:
        lead = f"Nothing in Leasha's index matches {what}."
    parts = [lead]
    if searched:
        parts.append("I searched for: " + "; ".join(f'"{q}"' for q in searched) + ".")
    if dropped_filters:
        parts.append("That included a search with the names and dates left out.")
    parts.append(f"Sources currently indexed: {index_summary(store)}.")
    parts.append("That is all I can say - it only means the index does not hold it, not that it "
                 "is not out there.")
    parts.append(NEXT_STEPS)
    notes = [f"Searched for: {q}" for q in searched]
    notes.append(f"Sources currently indexed: {index_summary(store)}")
    return " ".join(parts), notes
