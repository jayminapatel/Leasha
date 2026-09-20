"""Chat sessions on disk: a thin adapter over whatever the store offers.

Layer: L5 (no Qt; **every call here blocks - call it from a worker only**)

Work order 202626270611 section 3d: conversations persist locally, are listed
in a sidebar, are deletable, and reopen with their shelf intact.

The store's own `save_session` / `load_sessions` / `delete_session` (schema v26)
take *positional pieces* - `save_session(id, title, turns, shelf, model)` - and give
back rows with integer ids, so a session is **translated** on the way in and out:
the tab's own id (a uuid), its `titled` / `auto_titled` / `web` flags and the
shelf's removed list ride in a `_meta` first entry of the stored turns, and the
shelf's items are the stored shelf. **Before 2026-09-20 this module called the
store's method with one dict**, which raised `TypeError` on every save against the
real store - conversations never persisted outside the tests' in-memory double
(found by looking at the real window, not by a test, because no test used the real
store). A backend that takes the whole record (`save_session(record)`, the tests'
double) is still supported, and a store with none of the three methods falls back
to one keyed-state entry - which every store already has and which needs no
schema. Either way the rest of the tab sees only `ChatSession`.

**The methods are looked up by name and called through a local**, never as
`store.save_session(...)` in a view: `test_ui_never_blocks` reads the store's
public methods and would (rightly) flag a direct call from a slot. Nothing in
this file is a slot.
"""

from __future__ import annotations

import dataclasses
import inspect
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
    #: The assistant named it (a short title from the fast model, after the first
    #: answer). It is asked for once; a rename by the person wins over it.
    auto_titled: bool = False
    #: The Web switch for this conversation (off unless the person turned it on here).
    web: bool = False


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
        "model": turn.model, "partial": bool(turn.partial),
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
        kind=str(data.get("kind", "answer")), notes=list(data.get("notes", ())),
        model=str(data.get("model", "") or ""), partial=bool(data.get("partial", False)))


def session_to_dict(session: ChatSession) -> dict:
    return {
        "id": session.id, "title": session.title, "created": session.created,
        "updated": session.updated, "titled": session.titled,
        "auto_titled": session.auto_titled, "web": session.web,
        "shelf": session.shelf.to_dict(),
        "turns": [turn_to_dict(t) for t in session.turns],
    }


def session_from_dict(data: dict) -> ChatSession:
    return ChatSession(
        id=str(data["id"]), title=str(data.get("title") or "New chat"),
        created=float(data.get("created") or 0.0),
        updated=float(data.get("updated") or 0.0),
        titled=bool(data.get("titled", False)),
        auto_titled=bool(data.get("auto_titled", False)), web=bool(data.get("web", False)),
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


def _takes_pieces(saver: Any) -> bool:
    """Is this the store's `save_session(session_id, title, turns, ...)`, rather than a
    backend that takes the whole record?"""
    try:
        return "title" in inspect.signature(saver).parameters
    except (TypeError, ValueError):
        return False


#: The first entry of a stored session's turns: what the store's own columns have no
#: place for.
META = "_meta"


class ChatSessions:
    """Load, save and delete sessions. **Blocking - workers only.**"""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        #: The tab's id (a uuid) -> the store's integer id, for the sessions seen so far.
        self._store_ids: dict[str, int] = {}

    def _method(self, name: str) -> Any:
        return getattr(self._backend, name, None)

    # -- the store's own shape -------------------------------------------------
    def _from_store_row(self, row: dict) -> Optional[dict]:
        """A row of the store's `chat_sessions` as the tab's record."""
        turns = list(row.get("turns") or [])
        meta = {}
        if turns and isinstance(turns[0], dict) and META in turns[0]:
            meta = dict(turns.pop(0)[META] or {})
        uid = str(meta.get("id") or f"store-{row['id']}")
        self._store_ids[uid] = int(row["id"])
        shelf = row.get("shelf") or []
        return {
            "id": uid, "title": row.get("title") or "New chat",
            "created": float(row.get("created_at") or 0), "updated": float(row.get("updated_at") or 0),
            "titled": bool(meta.get("titled", False)), "auto_titled": bool(meta.get("auto_titled", False)),
            "web": bool(meta.get("web", False)),
            "shelf": {"items": list(shelf) if isinstance(shelf, list) else [],
                      "removed": list(meta.get("removed", []))},
            "turns": turns,
        }

    def _to_store_args(self, record: dict) -> tuple:
        """`(store id or None, title, turns, shelf)` for the store's `save_session`."""
        shelf = record.get("shelf") or {}
        meta = {"id": record["id"], "titled": bool(record.get("titled")),
                "auto_titled": bool(record.get("auto_titled")), "web": bool(record.get("web")),
                "removed": list(shelf.get("removed", []))}
        turns = [{META: meta}] + list(record.get("turns", []))
        return (self._store_ids.get(str(record["id"])), str(record.get("title") or ""),
                turns, list(shelf.get("items", [])))

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
        saver = self._method("save_session")
        try:
            raws = list(loader()) if loader is not None \
                else list(self._state_read().values())
            if loader is not None and saver is not None and _takes_pieces(saver):
                raws = [self._from_store_row(row) for row in raws if isinstance(row, dict)]
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
            if _takes_pieces(saver):
                store_id, title, turns, shelf = self._to_store_args(record)
                self._store_ids[str(record["id"])] = int(saver(store_id, title, turns, shelf))
                return
            saver(record)
            return
        records = self._state_read()
        records[str(record["id"])] = record
        self._state_write(records)

    def delete(self, session_id: str) -> None:
        remover = self._method("delete_session")
        if remover is not None:
            saver = self._method("save_session")
            if saver is not None and _takes_pieces(saver):
                store_id = self._store_ids.pop(str(session_id), None)
                if store_id is not None:
                    remover(store_id)
                return
            remover(session_id)
            return
        records = self._state_read()
        if records.pop(str(session_id), None) is not None:
            self._state_write(records)
