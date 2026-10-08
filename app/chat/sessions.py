"""Saving and reopening conversations.

Layer: L8b - no Qt. Work order section 3d.

The rows live in `chat_sessions` (schema v26, `_v26_chat_sessions`) and the three
store methods `save_session`, `load_sessions` and `delete_session` handle plain
JSON-able dicts, so the storage layer knows nothing about chat. This module is the
other half: `ChatTurn` and `Receipt` to and from those dicts, and a `Session`
value the tab can hold.

    session = Session(id=None, title="", turns=[...], shelf=[...])
    session = save_session(store, session)         # id assigned, title made
    sessions = load_sessions(store)                # newest first
    delete_session(store, session.id)

**What is kept.** The turns exactly as displayed - text, receipts (with the quoted
words themselves), the kind, the notes - and the shelf. A `result_set` of search
results is kept in a compact form (the fields the result delegate draws), so a
reopened FIND answer still shows its rows. `debug` is deliberately not kept: it is
about how one answer was made, not part of the conversation.

**The sensitivity sentence extends to this table**: a session holds quotes from
indexed files, so it is exactly as sensitive as the index, lives beside it, and
never leaves the machine.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Optional

from app.chat.types import ChatTurn, Receipt

__all__ = [
    "Session",
    "save_session",
    "load_sessions",
    "delete_session",
    "turn_to_dict",
    "turn_from_dict",
    "receipt_to_dict",
    "receipt_from_dict",
    "title_for",
    "details_to_json",
    "details_from_json",
]


@dataclass
class Session:
    """A conversation, as the tab holds it."""

    id: Optional[int] = None
    title: str = ""
    turns: list[ChatTurn] = field(default_factory=list)
    #: The context shelf: documents the conversation has touched.
    shelf: list[Receipt] = field(default_factory=list)
    model: str = ""
    created_at: int = 0
    updated_at: int = 0


def receipt_to_dict(receipt: Receipt) -> dict[str, Any]:
    return dataclasses.asdict(receipt)


def receipt_from_dict(data: dict[str, Any]) -> Receipt:
    return Receipt(
        file_id=data.get("file_id"), path=str(data.get("path", "")),
        name=str(data.get("name", "")), quote=str(data.get("quote", "")),
        locator=str(data.get("locator", "")), chunk_id=data.get("chunk_id"),
        mtime_ns=int(data.get("mtime_ns") or 0))


def _result_to_dict(result: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(result):
        return dataclasses.asdict(result)
    return dict(getattr(result, "__dict__", {}))


def _result_from_dict(data: dict[str, Any]) -> Any:
    from app.search.engine import SearchResult

    known = {f.name for f in dataclasses.fields(SearchResult)}
    values = {k: v for k, v in data.items() if k in known}
    if "sources" in values:
        values["sources"] = tuple(values["sources"])
    return SearchResult(**values)


def turn_to_dict(turn: ChatTurn) -> dict[str, Any]:
    return {
        "role": turn.role, "text": turn.text, "kind": turn.kind,
        "model": turn.model, "partial": bool(turn.partial),
        "receipts": [receipt_to_dict(r) for r in turn.receipts],
        "notes": list(turn.notes),
        "result_set": (None if turn.result_set is None
                       else [_result_to_dict(r) for r in turn.result_set]),
        "details": details_to_json(turn.details),
    }


def details_to_json(details: Any) -> dict[str, dict]:
    """`ChatTurn.details` as JSON can hold it: the file ids as text keys."""
    return {str(k): dict(v) for k, v in dict(details or {}).items() if isinstance(v, dict)}


def details_from_json(data: Any) -> dict[int, dict]:
    """`details_to_json` back again. A key that is not a number is dropped."""
    out: dict[int, dict] = {}
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        try:
            out[int(key)] = dict(value)
        except (TypeError, ValueError):
            continue
    return out


def turn_from_dict(data: dict[str, Any]) -> ChatTurn:
    results = data.get("result_set")
    restored: Optional[list] = None
    if results is not None:
        restored = []
        for item in results:
            try:
                restored.append(_result_from_dict(item))
            except Exception:                           # noqa: BLE001 - one bad row never hides the rest
                continue
    return ChatTurn(
        role=str(data.get("role", "assistant")), text=str(data.get("text", "")),
        receipts=[receipt_from_dict(r) for r in data.get("receipts", [])],
        result_set=restored, kind=str(data.get("kind", "answer")),
        notes=[str(n) for n in data.get("notes", [])],
        model=str(data.get("model", "") or ""), partial=bool(data.get("partial", False)),
        details=details_from_json(data.get("details")))


def title_for(turns: list[ChatTurn], limit: int = 60) -> str:
    """A conversation's title: its first question, trimmed at a word."""
    first = next((t.text for t in turns if t.role == "user" and t.text.strip()), "")
    text = " ".join(first.split())
    if len(text) <= limit:
        return text or "New conversation"
    return text[:limit].rsplit(" ", 1)[0] + "..."


def save_session(store: Any, session: Session) -> Session:
    """Write `session`, returning it with its id (and a title, if it had none)."""
    if not session.title.strip():
        session.title = title_for(session.turns)
    session.id = store.save_session(
        session.id, session.title,
        [turn_to_dict(t) for t in session.turns],
        [receipt_to_dict(r) for r in session.shelf], session.model)
    return session


def load_sessions(store: Any, *, with_turns: bool = True) -> list[Session]:
    """Every saved conversation, most recently used first."""
    out: list[Session] = []
    for row in store.load_sessions(with_turns=with_turns):
        out.append(Session(
            id=row["id"], title=row["title"], model=row.get("model", ""),
            turns=[turn_from_dict(t) for t in row.get("turns", [])],
            shelf=[receipt_from_dict(r) for r in row.get("shelf", [])],
            created_at=row.get("created_at", 0), updated_at=row.get("updated_at", 0)))
    return out


def delete_session(store: Any, session_id: int) -> bool:
    return bool(store.delete_session(session_id))
