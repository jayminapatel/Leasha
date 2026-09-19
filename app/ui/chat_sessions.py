"""Chat sessions on disk: a thin adapter over whatever the store offers.

Layer: L5 (no Qt; **every call here blocks - call it from a worker only**)

Work order 202626270611 section 3d: conversations persist locally, are listed
in a sidebar, are deletable, and reopen with their shelf intact.

The store's own `save_session` / `load_sessions` / `delete_session` arrive with
the engine's migration; this tab must not add a migration of its own. So this
module asks for those three methods with `getattr` and, until they exist,
keeps the same records under one keyed-state entry - which every store already
has and which needs no schema. Either way the rest of the tab sees only
`ChatSession`.

**The methods are looked up by name and called through a local**, never as
`store.save_session(...)` in a view: `test_ui_never_blocks` reads the store's
public methods and would (rightly) flag a direct call from a slot. Nothing in
this file is a slot.
"""

from __future__ import annotations

import dataclasses
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from app.chat.types import ChatTurn, Receipt
from app.core.logging import logger
from app.ui.presenter.chat import Shelf

__all__ = ["ChatSession", "ChatSessions", "new_session", "STATE_KEY"]

_log = logger.bind(component="ui.chat.sessions")

#: The fallback home, until the store has a table of its own for these.
STATE_KEY = "chat:sessions"


@dataclass
class ChatSession:
    id: str
    title: str = "New chat"
    created: float = 0.0
    updated: float = 0.0
    turns: list[ChatTurn] = field(default_factory=list)
    shelf: Shelf = field(default_factory=Shelf)
    #: The person renamed it, so the first question no longer names it.
    titled: bool = False


def new_session() -> ChatSession:
    now = time.time()
    return ChatSession(id=uuid.uuid4().hex, created=now, updated=now)


# -- records <-> plain dicts -------------------------------------------------

def _result_to_dict(result: Any) -> dict:
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return dataclasses.asdict(result)
    return dict(result) if isinstance(result, dict) else {}


def _result_from_dict(data: dict) -> Any:
    from app.search.engine import SearchResult

    known = {f.name for f in dataclasses.fields(SearchResult)}
    try:
        return SearchResult(**{k: v for k, v in data.items() if k in known})
    except TypeError:
        return None


def turn_to_dict(turn: ChatTurn) -> dict:
    return {
        "role": turn.role, "text": turn.text, "kind": turn.kind,
        "notes": list(turn.notes),
        "receipts": [dataclasses.asdict(r) for r in turn.receipts],
        "result_set": (None if turn.result_set is None
                       else [_result_to_dict(r) for r in turn.result_set]),
    }


def turn_from_dict(data: dict) -> ChatTurn:
    receipt_fields = {f.name for f in dataclasses.fields(Receipt)}
    receipts = [Receipt(**{k: v for k, v in r.items() if k in receipt_fields})
                for r in data.get("receipts", ())]
    raw = data.get("result_set")
    results = None if raw is None else [
        r for r in (_result_from_dict(d) for d in raw) if r is not None]
    return ChatTurn(
        role=str(data.get("role", "assistant")), text=str(data.get("text", "")),
        receipts=receipts, result_set=results,
        kind=str(data.get("kind", "answer")), notes=list(data.get("notes", ())))


def session_to_dict(session: ChatSession) -> dict:
    return {
        "id": session.id, "title": session.title, "created": session.created,
        "updated": session.updated, "titled": session.titled,
        "shelf": session.shelf.to_dict(),
        "turns": [turn_to_dict(t) for t in session.turns],
    }


def session_from_dict(data: dict) -> ChatSession:
    return ChatSession(
        id=str(data["id"]), title=str(data.get("title") or "New chat"),
        created=float(data.get("created") or 0.0),
        updated=float(data.get("updated") or 0.0),
        titled=bool(data.get("titled", False)),
        shelf=Shelf.from_dict(data.get("shelf")),
        turns=[turn_from_dict(t) for t in data.get("turns", ())])


def _as_dict(raw: Any) -> Optional[dict]:
    """A stored record in any of the shapes a store might hand back."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    payload = raw.get("payload")
    if payload is not None and "turns" not in raw:
        inner = _as_dict(payload)
        if inner is not None:
            inner.setdefault("id", raw.get("id"))
            inner.setdefault("title", raw.get("title"))
            return inner
    return raw if raw.get("id") else None


class ChatSessions:
    """Load, save and delete sessions. **Blocking - workers only.**"""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def _method(self, name: str) -> Any:
        return getattr(self._backend, name, None)

    # -- the keyed-state fallback -----------------------------------------
    def _state_read(self) -> dict[str, dict]:
        getter = self._method("get_state")
        if getter is None:
            return {}
        try:
            data = json.loads(getter(STATE_KEY, "") or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _state_write(self, records: dict[str, dict]) -> None:
        setter = self._method("set_state")
        if setter is not None:
            setter(STATE_KEY, json.dumps(records))

    # -- the three operations ---------------------------------------------
    def load(self) -> list[ChatSession]:
        """Every saved session, most recently used first. Never raises."""
        loader = self._method("load_sessions")
        try:
            raws = list(loader()) if loader is not None \
                else list(self._state_read().values())
        except Exception as exc:                          # noqa: BLE001
            _log.warning("could not read chat sessions: {}", exc)
            return []
        found: list[ChatSession] = []
        for raw in raws:
            data = _as_dict(raw)
            if data is None:
                continue
            try:
                found.append(session_from_dict(data))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                _log.warning("skipping an unreadable chat session: {}", exc)
        found.sort(key=lambda s: s.updated, reverse=True)
        return found

    def save(self, record: dict) -> None:
        """Write one session (already turned into a dict on the UI thread)."""
        saver = self._method("save_session")
        if saver is not None:
            saver(record)
            return
        records = self._state_read()
        records[str(record["id"])] = record
        self._state_write(records)

    def delete(self, session_id: str) -> None:
        remover = self._method("delete_session")
        if remover is not None:
            remover(session_id)
            return
        records = self._state_read()
        if records.pop(str(session_id), None) is not None:
            self._state_write(records)
