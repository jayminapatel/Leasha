r"""The index, read-only, for AI programs on this computer, over MCP.

Layer: L6.

2026-10-04, the owner: "can this index act as a mcp server for other ai
programs", then "do both", then - asked whether MCP was the best way - the
recommended route: MCP, with the command line for tools that run commands.
MCP (Model Context Protocol) is the one standard AI programs from several
makers share for reaching a tool on the same computer.

**Leasha runs it** (the owner's choice, 2026-10-04): Settings > Models & AI >
AI programs has Start and Stop, and it runs only while Leasha is open. It
listens on `127.0.0.1` only - never another computer - on `MCP_PORT`, and
every request must carry the key Leasha made (`Authorization: Bearer <key>`),
so another program on this computer cannot read the index without being
given it. The library's DNS-rebinding guard stays on.

**Read-only.** Seven tools and nothing that writes, moves, deletes or opens
anything (the owner, 2026-10-11, order 1k D15: "the only read for mcp"):

* `search`, `find_files`, `read_text` - the files and mail in the index;
* `index_status` - the counts of files, messages and passages, and each
  account set up in Leasha: its provider, its name, whether mail and contact
  sync are on, the direction, the last sync and the last error. Never a
  token, a password, a client ID or anything held in Credential Manager;
* `find_contact`, `get_contact`, `contact_activity` - the contact hub: find a
  person by name, email, phone or company; every field of one person with its
  label and where it came from, groups, tags, favourite, the accounts it is
  "Kept in" (by name) and notes; and the latest mail, the files exchanged as
  attachments, the photos and the first and last contact dates for one person.

No tool creates, changes or deletes a contact, an account or an index row;
`tests/unit/test_mcp_contacts_readonly.py` fails if one can.
Every answer names the file or message it came from. **The privacy fact, stated once:**
Leasha itself sends nothing anywhere, but what a tool returns goes to the AI
program that asked, and one that runs in the cloud sends it there.

**One copy of the models.** Inside Leasha the server uses the window's own
embedder and reranker (`models` is a callable returning them); loading a
second copy measured 1.4 GB and 4.1 s on the owner's laptop, 2026-10-04.
The index is opened afresh for each call, as `app.cli search` opens it, so
the server sees what an index run has just written; WAL makes reading beside
a run safe.

*Note, 2026-10-04, code review: the paragraph above is no longer how it
works.* The index stays open between calls (`_OpenIndex`) and is reopened
when the database file or the paths change; SQLite's WAL and LanceDB's read
consistency still show each call what a run has just written, and the
engine's result cache keys on the index generation. Measured on a temporary
index: see `IndexTools`.

**The bridge.** A program that can only start a command - Claude Desktop,
whose settings file is reported to lose every server when one is given by
address (anthropics/claude-code#37286) - runs `app.cli mcp`, which speaks
MCP on its stdin/stdout and passes each call to the running server
(`bridge_server`). With Leasha's server stopped, each tool answers that it
is stopped and where to start it.

*Note, 2026-10-11, order 1k section 8:* three contact tools and the accounts
in `index_status` joined the four above. They read through `ContactHub`'s and
`AccountStore`'s read methods on the same open index, and the bridge
forwards them like the rest, so a stopped server answers each one with
`STOPPED`. `search` is unchanged.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import re
import secrets
import socket
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

__all__ = [
    "KEY_STATE",
    "TOOL_NAMES",
    "IndexTools",
    "McpHost",
    "bridge_server",
    "build_server",
    "endpoint",
    "key_for",
]

_log = logger.bind(component="serve.mcp")

#: Where Leasha keeps the key, in the index's own state table. The bridge, a
#: separate process, reads it there.
KEY_STATE = "mcp:key"
TOOL_NAMES = ("search", "find_files", "read_text", "index_status",
              "find_contact", "get_contact", "contact_activity")

#: What one search returns at most, and what one result's passage is cut to.
SEARCH_LIMIT_MAX = 50
SNIPPET_CHARS = 600
#: The most text `read_text` returns for one file.
TEXT_CHARS_MAX = 200_000
#: What `find_contact` and `contact_activity` return at most, per list.
CONTACT_LIMIT_MAX = 50
#: The longest recipients line a message is answered with.
RECIPIENTS_CHARS = 300

INSTRUCTIONS = (
    "Leasha is the owner's local search index of their own files and Outlook mail. "
    "Use `search` to find documents and messages by what they say, `find_files` to find a "
    "file by its name, and `read_text` for the whole indexed text of one result. Every answer "
    "names the file it came from - cite the path. Nothing here can change, move or open a file. "
    "Use `find_contact` and `get_contact` to look up the people in the owner's contacts and "
    "`contact_activity` for the mail, files and photos shared with one of them. "
    "`index_status` also lists the owner's connected accounts, never their sign-in details. "
    "`search` takes an optional `account` (an account's name or address) to look only at the mail "
    "fetched from it, and a message from an account says which account in its `mail`."
)

STOPPED = ("Leasha's AI access is stopped. Open Leasha, go to Settings > Models & AI > "
           "AI programs, and press Start.")
#: 2026-10-04, code review: the bridge said `STOPPED` for every failure, so a
#: changed key or a slow search sent the person to press a Start that was
#: already pressed. Each failure now says what it is.
KEY_CHANGED = ("Leasha's key for AI programs has changed since this program was connected. "
               "In Leasha, go to Settings > Models & AI > AI programs and connect this "
               "program again.")
TIMED_OUT = ("Leasha did not answer within {seconds} seconds. It may be busy indexing - "
             "try again in a moment.")
SERVER_ERROR = ("Leasha's AI access is running but could not answer this ({what}). Try again; "
                "if it keeps happening, press Stop and Start in Settings > Models & AI > "
                "AI programs.")
#: A call that arrives while Leasha is closing.
CLOSING = "Leasha is closing, so its AI access has stopped. Open Leasha again and ask again."
#: How long the bridge waits for one answer.
BRIDGE_TIMEOUT_S = 120
#: How long Stop lets open connections finish before closing them.
GRACEFUL_S = 2


def endpoint(port: int) -> str:
    """The address AI programs are given for a server on `port`."""
    return f"http://127.0.0.1:{int(port)}/mcp"


def key_for(store: Any, *, create: bool = True) -> str:
    """The key AI programs send, made once and kept. `""` if none and not `create`.

    2026-10-04, code review: **made once even when two workers ask at once.**
    Start and the status refresh both asked on workers; each found no key,
    made one and wrote it, and the last write won - so the server could be
    started with a key the box and the settings files no longer held. The
    key is now inserted only if absent, inside the store's one write
    transaction, and whatever is there afterwards is the answer.
    """
    key = str(store.get_state(KEY_STATE, "") or "")
    if key or not create:
        return key
    made = secrets.token_urlsafe(32)
    writer = getattr(store, "write", None)
    if not callable(writer):                     # a test double: no transactions
        store.set_state(KEY_STATE, made)
        return made
    with writer() as conn:
        conn.execute("INSERT OR IGNORE INTO index_state (key, value, updated_at) "
                     "VALUES (?, ?, ?)", (KEY_STATE, made, int(time.time())))
        row = conn.execute("SELECT value FROM index_state WHERE key = ?",
                           (KEY_STATE,)).fetchone()
    return str(row[0]) if row and row[0] else made


# ---------------------------------------------------------------------------
# The tools, over the index
# ---------------------------------------------------------------------------

class IndexTools:
    """What each tool does, against the index `settings` names.

    `models` returns `(embedder, reranker, clip_text_embedder)`; called only
    by `search`, so the other three never load a model.

    `preferences` returns the Settings search switches (`policy.preferences`
    shape); inside Leasha, the window's current ones, so a switch changed in
    Settings applies here at once. Left out, `settings`' own.

    **`search` and `find_files` search exactly as the Search and Files tabs
    do** (the owner's decision, 2026-10-04): `app.search.run.run_search` and
    `find_files`, the functions the window's steps are - saved searches,
    plain-English filters, the switches, one result per document with a
    `status` mark (`ok`, `missing`, `offline`). Neither writes anything: a
    saved search's run is not counted from here.
    """

    def __init__(self, settings: Any, models: Callable[[], tuple],
                 preferences: Callable[[], Any] | None = None) -> None:
        self._settings = settings
        self._models = models
        self._preferences = preferences or (lambda: _preferences_of(settings))
        # 2026-10-04, code review: **the index stays open between calls.**
        # Each search used to open the store and both vector stores, build an
        # engine with its own thread pool and an empty result cache, warm it
        # and close it all again. `_index` is reused while the index it opened
        # is still the one `settings` names; `_lock` guards it and the count
        # of calls in flight, which `close` waits for. Measured on this laptop,
        # a temporary 3,000-document index, keyword-only, 30 searches: a new
        # query 36-40 ms -> 8-12 ms median; a repeated one 42-66 ms -> 2 ms
        # (the cache now survives); `index_status` 4-9 ms -> 0.3 ms.
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._index: _OpenIndex | None = None
        self._calls = 0
        self._closed = False

    def _missing(self) -> dict | None:
        if self._settings.fts_db.is_file():
            return None
        return {"error": "No index has been built yet. Open Leasha and index some folders first."}

    # -- what stays open, and closing it --------------------------------------

    def _identity(self) -> tuple:
        """Which index `settings` names now: the paths, the vector size and the
        database file itself, so a reset that replaced the file reopens."""
        settings = self._settings
        try:
            found = settings.fts_db.stat()
            file_key: tuple = (found.st_ino, found.st_dev)
        except OSError:
            file_key = ()
        return (str(settings.fts_db), file_key, str(settings.vector_path),
                int(getattr(settings, "embed_dim", 0) or 0))

    @contextlib.contextmanager
    def _open(self) -> Any:
        """The open index for one call. Raises `_Closing` once `close` began."""
        with self._lock:
            if self._closed:
                raise _Closing()
            identity = self._identity()
            index = self._index
            if index is None or index.identity != identity or index.vectors_failed():
                if index is not None and index.users == 0:
                    index.close()                # else its last call closes it
                self._index = index = _OpenIndex(self._settings, identity)
            index.users += 1
            self._calls += 1
        try:
            yield index
        finally:
            with self._lock:
                index.users -= 1
                self._calls -= 1
                if index.users == 0 and index is not self._index:
                    index.close()                # replaced, or closed, while in use
                self._idle.notify_all()

    def refuse(self) -> None:
        """Answer every call from now on with `CLOSING`. Never blocks."""
        with self._lock:
            self._closed = True

    def close(self, *, wait_s: float = 2.0) -> None:
        """Refuse new calls, wait up to `wait_s` for those in flight, then close
        the index. A call still running after that closes it when it ends."""
        with self._lock:
            self._closed = True
            deadline = time.monotonic() + max(0.0, wait_s)
            while self._calls and (left := deadline - time.monotonic()) > 0:
                self._idle.wait(left)
            index, self._index = self._index, None
            if index is not None and index.users == 0:
                index.close()

    # -- the tools ----------------------------------------------------------

    def search(self, query: str, limit: int = 10, account: str = "") -> dict:
        """The `search` tool: `run_search` over the open index, `limit` capped at
        `SEARCH_LIMIT_MAX`. Every failure is answered as `{"error": ...}`, never raised.

        *Order 1k 8a:* `account` (a name, address or id) keeps only the mail fetched from
        that account. The search runs as usual and its best `SEARCH_LIMIT_MAX` documents
        are filtered, so a filtered search can come back shorter than `limit`.
        """
        from app.search.policy import SEARCH
        from app.search.run import run_search

        raw = str(query or "").strip()
        if not raw:
            return {"error": "Give something to search for."}
        missing = self._missing()
        if missing:
            return missing
        limit = max(1, min(int(limit or 10), SEARCH_LIMIT_MAX))
        wanted_account = str(account or "").strip()
        names: dict[int, str] = {}
        prefix = ""
        try:
            with self._open() as index:
                names = _account_names(index.store)
                if wanted_account:
                    found_id = _account_id_for(index.store, wanted_account)
                    if found_id is None:
                        return {"error": f"No account is called '{wanted_account}'. Use a name or "
                                         "address as `index_status` lists them."}
                    prefix = _account_prefix(found_id)
                engine = index.engine_for(self._models)
                # **The Search tab's search** (2026-10-04): slashes and saved
                # searches, the plain-English filters, the Settings switches,
                # the window's reranker as its Rerank box set it, history on a
                # history switch, one result per document with its mark. It
                # used to be a bare engine search, one row per passage.
                found = run_search(engine, raw, surface=SEARCH,
                                   preferences=self._preferences(),
                                   limit=SEARCH_LIMIT_MAX if prefix else limit)
        except _Closing:
            return {"error": CLOSING}
        response = found.response
        documents = found.documents
        if prefix:
            documents = [d for d in documents if str(d.best.path).startswith(prefix)][:limit]
        return {"query": raw, "read_as": [a.label for a in found.applied],
                "results": [self._result(document, names) for document in documents],
                "notices": [n.as_dict() for n in response.notices]}

    def find_files(self, name: str, type: str = "", limit: int = 25) -> dict:
        """The `find_files` tool: the Files tab's search, `type` read as a `type:` switch."""
        from app.search.run import find_files

        text = str(name or "").strip()
        if not text:
            return {"error": "Give part of a file name."}
        missing = self._missing()
        if missing:
            return missing
        limit = max(1, min(int(limit or 25), SEARCH_LIMIT_MAX * 4))
        kinds = [k.strip().lstrip(".") for k in str(type or "").split(",") if k.strip()]
        # **The Files tab's search** (2026-10-04), `type` as the switch the tab
        # would be typed with. It used to be a name-only lookup, so the same
        # words found something different here than on the tab.
        line = f"{text} type:{','.join(kinds)}" if kinds else text
        # All three from below the UI layer (2026-10-08 review): this module used
        # to import them from `app.ui`, the one backend package that did.
        from app.core.row_facts import has_own_size, own_size, shown_date_ns
        from app.search.marks import file_row_context

        try:
            with self._open() as index:
                store = index.store
                page = find_files(store, line, limit=limit, preferences=self._preferences())
                # The Files tab's own page context (2026-10-04): an attachment's
                # message, so its date is the message's - one statement a page.
                rows = file_row_context(store, page["rows"])
        except _Closing:
            return {"error": CLOSING}
        files = []
        for row in rows:
            path = str(row.get("path") or "")
            kind = row.get("source_kind")
            # The name from the path - the query selects none, which is why
            # this said `null` - and a real 0-byte size kept as 0 (2026-10-04).
            # Size and date by the same rules as every list (`row_facts`,
            # `facts.shown_date_ns`): no archive's size on a message, and an
            # attachment dated by its message.
            when = shown_date_ns(mtime_ns=row.get("mtime_ns"), taken_at_ns=row.get("taken_at_ns"),
                                 sent_at=row.get("message_sent_at"))
            files.append({"path": path,
                          "name": path.replace("\\", "/").rstrip("/").rpartition("/")[2],
                          "type": row.get("ext"),
                          "size_bytes": (own_size(row.get("size_bytes"), path, kind)
                                         if has_own_size(path, kind) else None),
                          "modified": _when(when / 1e9) if when else ""})
        out: dict = {"name": text, "files": files,
                     "read_as": [a.label for a in page["applied"]]}
        if page.get("spelling"):
            out["spelling"] = page["spelling"]
        return out

    def read_text(self, path: str, max_chars: int = 20_000) -> dict:
        """The `read_text` tool: one file's indexed text, cut at `TEXT_CHARS_MAX`."""
        from app.core.row_facts import join_chunks

        key = str(path or "").strip()
        if not key:
            return {"error": "Give the path of a file, as search or find_files returned it."}
        missing = self._missing()
        if missing:
            return missing
        limit = max(1, min(int(max_chars or 20_000), TEXT_CHARS_MAX))
        try:
            with self._open() as index:
                store = index.store
                record = store.get_file(key)
                if record is None and key.startswith(_ACCOUNT_SCHEME):
                    # Order 1k 8a: an account message quoted back with its account named by
                    # name or address, or its parts written out unescaped.
                    resolved = _resolve_account_path(store, key)
                    record = store.get_file(resolved) if resolved else None
                    key = resolved if record is not None else key
                if record is None:
                    return {"error": f"'{key}' is not in the index. Use the path exactly as "
                                     "search returned it."}
                text = join_chunks(store.chunks_for_file(int(record.id)))
                mail = _mail_of(store, record.id, key)
        except _Closing:
            return {"error": CLOSING}
        out: dict = {"path": key, "chars": len(text), "truncated": len(text) > limit,
                     "text": text[:limit]}
        if mail:
            out["mail"] = mail
        return out

    def index_status(self) -> dict:
        """The `index_status` tool: row counts of files, messages and passages."""
        missing = self._missing()
        if missing:
            return missing
        try:
            with self._open() as index:
                store = index.store

                def count(table: str) -> int:
                    return int(store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

                return {"files": count("files"), "messages": count("messages"),
                        "passages": count("chunks"), "accounts": _accounts_of(store)}
        except _Closing:
            return {"error": CLOSING}

    # -- the contact tools: reads of the hub and the index, nothing more ---------

    def find_contact(self, query: str, limit: int = 20) -> dict:
        """The `find_contact` tool: people by name, email, phone or company.

        An address or a phone number that matches a contact exactly comes
        first; then the contact search's own ranking. `limit` is capped at
        `CONTACT_LIMIT_MAX`. Every failure is answered as `{"error": ...}`.
        """
        text = str(query or "").strip()
        if not text:
            return {"error": "Give a name, an email address, a phone number or a company."}
        missing = self._missing()
        if missing:
            return missing
        limit = max(1, min(int(limit or 20), CONTACT_LIMIT_MAX))
        try:
            with self._open() as index:
                hub = _hub_of(index.store)
                exact: list[tuple[int, str]] = []
                if "@" in text and " " not in text:
                    exact = [(cid, "email") for cid in hub.find_by_email(text)]
                elif sum(c.isdigit() for c in text) >= 5 and not any(c.isalpha() for c in text):
                    exact = [(cid, "phone") for cid in hub.find_by_phone(text)]
                matched = dict(exact)
                ranked = [cid for cid, _ in exact]
                for hit in hub.search(text, limit=limit):
                    if hit.id not in matched:
                        matched[hit.id] = "text"
                        ranked.append(hit.id)
                contacts = []
                for cid in ranked[:limit]:
                    row = hub.get_row(cid)
                    if row is None or row.deleted_at is not None:
                        continue
                    contacts.append({"contact_id": cid, "name": row.display_name,
                                     "company": row.company, "title": row.title,
                                     "email": row.email, "phone": row.phone,
                                     "favourite": row.favourite, "matched": matched[cid]})
        except _Closing:
            return {"error": CLOSING}
        except sqlite3.Error as exc:
            return {"error": _CONTACTS_UNREADABLE.format(what=type(exc).__name__)}
        return {"query": text, "contacts": contacts}

    def get_contact(self, contact_id: int) -> dict:
        """The `get_contact` tool: everything held for one person, by the
        `contact_id` that `find_contact` returned."""
        missing = self._missing()
        if missing:
            return missing
        try:
            with self._open() as index:
                found = _contact_of(index.store, contact_id)
                if "error" in found:
                    return found
                hub, cid, row, card = found["hub"], found["id"], found["row"], found["card"]
                sources = hub.sources(cid)
                names = _account_names(index.store)
                kept = [names.get(a, "an account that is no longer set up")
                        for a in hub.kept_in(cid)]
                tags = hub.tags_of(cid)
        except _Closing:
            return {"error": CLOSING}
        except sqlite3.Error as exc:
            return {"error": _CONTACTS_UNREADABLE.format(what=type(exc).__name__)}
        from app.contacts.card import SCALARS, field_key

        def said(key: str) -> list[dict]:
            return [{"source": src, "ref": ref} for src, ref in sources.get(key, ())]

        details = [{"field": name, "value": card.get(name), "sources": said(f"s:{name}")}
                   for name in SCALARS if name != "notes" and card.get(name)]
        values = []
        for item in card.fields:
            entry: dict = {"kind": item.kind, "label": item.label, "value": item.value,
                           "primary": item.primary,
                           "sources": said("f:" + field_key(item, hub.region))}
            if item.data:
                entry["parts"] = dict(item.data)
            values.append(entry)
        return {"contact_id": cid, "name": row.display_name, "company": row.company,
                "title": row.title, "favourite": bool(card.favourite), "details": details,
                "fields": values, "groups": list(card.groups), "tags": tags,
                "kept_in": kept, "notes": card.get("notes"), "has_photo": row.has_photo}

    def contact_activity(self, contact_id: int, limit: int = 10) -> dict:
        """The `contact_activity` tool: the latest mail with one person, the
        files exchanged as attachments, the photos of them, and the first and
        last contact, each item naming the message or file it came from.

        Mail is matched on the contact's email addresses, in the message's
        sender and recipients; the photos are those of the Photo Tagger person
        the contact is confirmed as.
        """
        missing = self._missing()
        if missing:
            return missing
        limit = max(1, min(int(limit or 10), CONTACT_LIMIT_MAX))
        try:
            with self._open() as index:
                store = index.store
                found = _contact_of(store, contact_id)
                if "error" in found:
                    return found
                emails = [f.value.strip() for f in found["card"].values("email") if f.value.strip()]
                mail = _mail_with(store, emails, limit)
                files = _files_exchanged(store, emails, limit)
                link, photos = _photos_of(store, found["id"], limit)
        except _Closing:
            return {"error": CLOSING}
        except sqlite3.Error as exc:
            return {"error": _CONTACTS_UNREADABLE.format(what=type(exc).__name__)}
        return {"contact_id": found["id"], "name": found["row"].display_name,
                "emails": emails, **mail, "files_exchanged": files,
                "photo_match": link, "photos": photos}

    @staticmethod
    def _result(document: Any, accounts: dict | None = None) -> dict:
        """One document of a search: its best passage, how many passages
        matched, whether it can be opened now, and its mail card."""
        best = document.best
        text = " ".join(str(getattr(best, "text", "") or "").split())
        out = {"rank": document.rank, "path": best.path, "page": best.page or None,
               "score": round(float(best.score), 6), "matches": document.matches,
               "status": document.status,
               "passage": text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS - 1] + "…"}
        mail = _mail_card(document.mail, best.path, accounts)
        if mail:
            out["mail"] = mail
        return out


class _Closing(Exception):
    """A call that arrived after `IndexTools.close` began."""


class _OpenIndex:
    r"""The index as `IndexTools` keeps it open between calls. 2026-10-04, code review.

    One `SqliteStore` - a connection per thread, so the SDK's worker threads
    read side by side - and the two vector stores, opened **deferred**, as the
    window opens them: a vector store that cannot open no longer fails the
    whole call (it raised out of `__enter__`); the engine searches by keyword
    and says so (`NOTICE_NO_VECTORS`), and the next call tries to open it
    again. The engine is built on the first search, with the window's models,
    and rebuilt only if the window's models change (a moved index replaces
    its engine). `log_usage=False`: MCP is read-only, so its searches are not
    written to the usage log - they appeared among the window's recent
    searches, and each could wait 30 s on the write lock during an index run.
    """

    def __init__(self, settings: Any, identity: tuple) -> None:
        from app.storage.sqlite_store import SqliteStore
        from app.storage.vector_store import ImageVectorStore, VectorStore

        self.identity = identity
        self.users = 0
        self._settings = settings
        self._lock = threading.Lock()
        self._engine: Any = None
        self._models: tuple = ()
        self._spent: list = []
        self.store = SqliteStore(settings.fts_db).connect()
        self.vectors = VectorStore(settings.vector_path, dim=settings.embed_dim, deferred=True)
        self.image_vectors = ImageVectorStore(settings.vector_path, deferred=True)
        for vectors in (self.vectors, self.image_vectors):
            vectors.warm()

    def vectors_failed(self) -> bool:
        """A vector store failed to open, so the next call opens afresh."""
        return any(v.deferred_error() is not None for v in (self.vectors, self.image_vectors))

    def engine_for(self, models: Callable[[], tuple]) -> Any:
        """The engine over this index, rebuilt only when the window's models change."""
        from app.search.engine import SearchEngine

        embedder, reranker, clip = models()
        with self._lock:
            same = len(self._models) == 3 and all(
                a is b for a, b in zip(self._models, (embedder, reranker, clip)))
            if self._engine is not None and same:
                return self._engine
            if self._engine is not None:
                # Closed with the index: a search may still be running on it.
                self._spent.append(self._engine)
            engine = SearchEngine(self.store, self.vectors, embedder, reranker=reranker,
                                  image_vectors=self.image_vectors, clip_text_embedder=clip,
                                  log_usage=False)
            engine.warm_up()
            self._engine, self._models = engine, (embedder, reranker, clip)
            return engine

    def close(self) -> None:
        """Close every engine, both vector stores and the store, each regardless of the others."""
        for step in (*(e.close for e in (*self._spent, self._engine) if e is not None),
                     self.vectors.close, self.image_vectors.close, self.store.close):
            try:
                step()
            except Exception as exc:             # noqa: BLE001 - closing regardless
                _log.debug("closing the MCP server's index: {}", exc)
        self._engine, self._spent = None, []


def _mail_of(store: Any, file_id: Any, path: str) -> dict | None:
    """Subject, sender and date for a message, or the message an attachment came on.

    Through `app.search.marks.mail_details`, the batched read the results list
    and `search` use (2026-10-04), so one file reads its card the same way.
    """
    from types import SimpleNamespace

    from app.search.marks import mail_details

    details = mail_details(store, [SimpleNamespace(file_id=int(file_id or 0), path=path)])
    return _mail_card(details.get(int(file_id or 0)), path,
                      _account_names(store) if str(path).startswith(_ACCOUNT_SCHEME) else None)


def _mail_card(detail: Any, path: str, accounts: dict | None = None) -> dict | None:
    """A `mail_details` entry as the tools say it, or None for a non-message.

    *Order 1k 8a:* a message fetched from an account also says which one (`account`, its
    name), beside the sender and date. `accounts` is `{account id: name}`.
    """
    from app.search.marks import ATTACHMENT_MARKER

    if not detail:
        return None
    mail = {"subject": detail.get("subject") or "", "from": detail.get("sender") or "",
            "sent": _when(detail.get("sent_at"))}
    if detail.get("attachment_of"):
        text = str(path or "")
        mail["attachment"] = text[text.find(ATTACHMENT_MARKER) + len(ATTACHMENT_MARKER):]
    named = _account_of_path(path, accounts)
    if named:
        mail["account"] = named
    return mail


#: Where a message fetched from an account lives in the index (order 1k 5d).
_ACCOUNT_SCHEME = "account://"


def _account_prefix(account_id: int) -> str:
    return f"{_ACCOUNT_SCHEME}{account_id}/"


def _account_of_path(path: Any, accounts: dict | None) -> str:
    """The name of the account an `account://` path belongs to, or "" for any other path."""
    text = str(path or "")
    if not text.startswith(_ACCOUNT_SCHEME):
        return ""
    try:
        ident = _split_account_path(text)[0]
        return str((accounts or {}).get(int(ident)) or "an account that is no longer set up")
    except (ValueError, TypeError):
        return ""


def _split_account_path(path: str) -> tuple[str, str, str]:
    """(account, folder, remote id) of an `account://<account>/<folder>/<remote id>` path, each
    part unescaped. The same split `app.accounts.mail_ingest.split_account_path` makes; written
    here so this module imports nothing from the accounts but what it reads (D15)."""
    from urllib.parse import unquote

    parts = path[len(_ACCOUNT_SCHEME):].split("/")
    if not path.startswith(_ACCOUNT_SCHEME) or len(parts) != 3 or not parts[0] or not parts[2]:
        raise ValueError(f"not an account path: {path!r}")
    return unquote(parts[0]), unquote(parts[1]), unquote(parts[2])


def _account_id_for(store: Any, text: str) -> int | None:
    """The id of the account called `text` (id, name or address), or None."""
    from app.accounts.store import AccountStore

    try:
        found = AccountStore(store).find(text)
    except sqlite3.Error:
        return None
    return int(found.id) if found is not None else None


def _resolve_account_path(store: Any, text: str) -> str | None:
    """`text`, an `account://` path as an AI program may have quoted it, as the index keys it:
    the account by id, name or address, each part escaped as `account_path` escapes it."""
    from urllib.parse import quote

    try:
        ident, folder, remote = _split_account_path(text)
    except ValueError:
        return None
    found = _account_id_for(store, ident)
    if found is None:
        return None
    return f"{_ACCOUNT_SCHEME}{found}/{quote(folder, safe='')}/{quote(remote, safe='')}"


#: Said when the contact tables cannot be read (an index built before contacts).
_CONTACTS_UNREADABLE = ("Leasha could not read its contacts from this index ({what}). "
                        "Open Leasha once so it can bring the index up to date.")
_NO_CONTACT = "No contact has that contact_id. Use a contact_id exactly as find_contact returned it."


def _hub_of(store: Any) -> Any:
    """A `ContactHub` over the open index. Only its read methods are used here;
    `tests/unit/test_mcp_contacts_readonly.py` holds this module to that."""
    from app.contacts.hub import ContactHub

    try:
        from app.accounts.store import AccountStore

        region = AccountStore(store).settings().phone_region or "GB"
    except Exception:                            # noqa: BLE001 - the default region will do
        return ContactHub(store)
    return ContactHub(store, region=region)


def _contact_of(store: Any, contact_id: Any) -> dict:
    """`{hub, id, row, card}` for a live contact, or `{"error": ...}`."""
    try:
        cid = int(contact_id)
    except (TypeError, ValueError):
        return {"error": _NO_CONTACT}
    hub = _hub_of(store)
    row = hub.get_row(cid)
    card = hub.get_card(cid) if row is not None else None
    if row is None or card is None or row.deleted_at is not None:
        return {"error": _NO_CONTACT}
    return {"hub": hub, "id": cid, "row": row, "card": card}


def _account_names(store: Any) -> dict[int, str]:
    """`{account id: the name the person gave it}`. Names only - nothing that signs in."""
    from app.accounts.store import AccountStore

    try:
        return {int(a.id): str(a.name or a.address) for a in AccountStore(store).list()}
    except sqlite3.Error:
        return {}


def _accounts_of(store: Any) -> list[dict]:
    """Each account as `index_status` says it. **Never a token, a password, a
    client ID or a Credential Manager value**: only these named fields are
    read, and the error text goes through the secret scrubber as well."""
    from app.accounts.secrets import redact
    from app.accounts.store import AccountStore

    try:
        accounts = AccountStore(store).list()
    except sqlite3.Error:
        return []
    out = []
    for account in accounts:
        out.append({
            "name": str(account.name or ""), "provider": account.provider,
            "mail_sync": "on" if account.mail_on else "off",
            "contact_sync": "on" if account.syncs_contacts else "off",
            "contact_direction": account.contacts_direction,
            "last_sync": _when(account.last_synced_at),
            "last_error": redact(account.last_error) if account.last_error else "",
            "last_error_code": str(account.last_error_code or ""),
        })
    return out


def _is_address_in(text: Any, email: str) -> bool:
    """`email` as a whole address in `text` - `ann@x.com`, not `joann@x.com`."""
    pattern = r"(?<![\w.+%-])" + re.escape(email) + r"(?![\w-]|\.\w)"
    return re.search(pattern, str(text or ""), re.IGNORECASE) is not None


def _messages(store: Any, emails: list[str], limit: int, *, attachments: bool = False,
              oldest: bool = False) -> list[dict]:
    """Messages from or to any of `emails`, newest first (oldest first for
    `oldest`), through the Mail tab's own read (`browse_messages`)."""
    rows: dict[int, dict] = {}
    fetch = 25 if oldest else min(limit * 4, 200)
    for email in emails[:10]:
        for side in ("sender", "recipient"):
            found = store.browse_messages(
                **{side: email}, has_attachment=True if attachments else None,
                sort="oldest" if oldest else "", limit=fetch)
            for row in found:
                if _is_address_in(row.get(side if side == "sender" else "recipients"), email):
                    rows.setdefault(int(row["file_id"]), {**row, "_email": email, "_side": side})
    ordered = sorted(rows.values(), key=lambda r: (r.get("sent_at") is None, r.get("sent_at") or 0,
                                                   int(r["file_id"])), reverse=False)
    dated = [r for r in ordered if r.get("sent_at") is not None]
    undated = [r for r in ordered if r.get("sent_at") is None]
    dated = dated if oldest else dated[::-1]
    return (dated + undated)[:limit]


def _message_entry(row: dict) -> dict:
    """One message as `contact_activity` says it: the `_mail_card` fields, the
    path to give `read_text`, and whether it came from them or went to them."""
    card = _mail_card({"subject": row.get("subject"), "sender": row.get("sender"),
                       "sent_at": row.get("sent_at")}, str(row.get("path") or "")) or {}
    recipients = str(row.get("recipients") or "")
    return {"path": str(row.get("path") or ""), **card,
            "to": recipients if len(recipients) <= RECIPIENTS_CHARS
            else recipients[:RECIPIENTS_CHARS - 1] + "…",
            "direction": "from them" if row.get("_side") == "sender" else "to them"}


def _mail_with(store: Any, emails: list[str], limit: int) -> dict:
    """The latest mail with these addresses, and the first and last contact."""
    if not emails:
        return {"messages": [], "first_contact": "", "last_contact": "",
                "note": "This contact has no email address, so no mail can be matched."}
    latest = _messages(store, emails, limit)
    first = _messages(store, emails, 1, oldest=True)
    stamps = [r["sent_at"] for r in latest + first if r.get("sent_at")]
    return {"messages": [_message_entry(r) for r in latest],
            "first_contact": _when(min(stamps)) if stamps else "",
            "last_contact": _when(max(stamps)) if stamps else ""}


def _files_exchanged(store: Any, emails: list[str], limit: int) -> list[dict]:
    """Files attached to messages with these addresses: each with the message it came on."""
    from app.core.row_facts import attachment_of

    out: list[dict] = []
    if not emails:
        return out
    for row in _messages(store, emails, limit, attachments=True):
        message_path = str(row.get("path") or "")
        for file_id in store.file_ids_at_or_under(message_path):
            record = store.get_file_by_id(file_id)
            if record is None or attachment_of(record.path)[0] != message_path:
                continue
            out.append({"path": record.path, "name": attachment_of(record.path)[1],
                        "type": record.ext, "size_bytes": record.size_bytes,
                        "message": _message_entry(row)})
            if len(out) >= limit:
                return out
    return out


def _photos_of(store: Any, contact_id: int, limit: int) -> tuple[str, list[dict]]:
    """`(how the contact is matched to a Photo Tagger person, their photos)`.

    The match is `accepted`, `suggested` or `none`; only an accepted one lists
    photos - a suggestion is the program's guess, not the person's word.
    """
    from app.core.row_facts import shown_date_ns

    try:
        link = store.conn.execute(
            "SELECT l.pile_id, l.state, p.name FROM contact_pile_links l "
            "JOIN piles p ON p.id = l.pile_id WHERE l.contact_id = ?", (contact_id,)).fetchone()
    except sqlite3.Error:
        return "none", []
    if link is None or link["state"] == "declined":
        return "none", []
    if link["state"] != "accepted":
        return "suggested", []
    ids = [int(r[0]) for r in store.conn.execute(
        "SELECT DISTINCT file_id FROM faces WHERE pile_id = ? ORDER BY file_id DESC LIMIT ?",
        (int(link["pile_id"]), min(limit * 4, 100)))]
    photos = []
    for file_id in ids:
        record = store.get_file_by_id(file_id)
        if record is None:
            continue
        when = shown_date_ns(mtime_ns=record.mtime_ns, taken_at_ns=record.taken_at_ns, sent_at=None)
        photos.append((when or 0, {"path": record.path,
                                   "taken": _when(when / 1e9) if when else "",
                                   "person": link["name"] or ""}))
    photos.sort(key=lambda item: item[0], reverse=True)
    return "accepted", [item[1] for item in photos[:limit]]


def _preferences_of(settings: Any) -> dict:
    """The Settings search switches, from `settings` alone (no window)."""
    from app.search.policy import preferences

    return preferences(settings)


def _when(seconds: Any) -> str:
    """A local date and time an AI program can read and compare, or `""`."""
    from datetime import datetime

    try:
        value = float(seconds or 0)
        return datetime.fromtimestamp(value).isoformat(timespec="minutes") if value > 0 else ""
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


# ---------------------------------------------------------------------------
# The server, and the bridge - the same tools, defined once
# ---------------------------------------------------------------------------

def _register(server: Any, tools: Any) -> None:
    """The tools, their wording and their read-only marks, on `server`.
    `tools` is an `IndexTools`, or the bridge's forwarder with the same methods.

    Async, and an `IndexTools` call runs on a worker thread
    (`anyio.to_thread`): a search takes a second or more, and on the event
    loop it would hold every other request - the bridge's, or a second
    program's - until it finished.
    """
    from mcp_types import ToolAnnotations

    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                idempotent_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only)
    async def search(query: str, limit: int = 10, account: str = "") -> dict:
        """Search the owner's files and mail by what they say, as Leasha's search box does.

        Plain English works ("the boiler service quote from last spring"), and so do
        Leasha's own switches: `type:pdf`, `/newest`, `from:dave`. Returns the best
        matches, each with its path, a passage and, for mail, the subject, sender
        and date. Use `read_text` with a path for the whole text.

        2026-10-04: one result per document - its best passage, `matches` (how
        many passages matched) and `status`: `ok`, `missing` (gone from disk)
        or `offline` (on a drive not connected). `read_as` lists the filters
        the words were read as, and `saved:name` runs a saved search.

        2026-10-11: `account` (an account's name or address, as `index_status`
        lists them) looks only at the mail fetched from that account. Mail from an
        account says which one in `mail.account`, and its `path` (`account://...`)
        is given to `read_text` as it stands.
        """
        return await _invoke(tools.search, query, limit, account)

    @server.tool(annotations=read_only)
    async def find_files(name: str, type: str = "", limit: int = 25) -> dict:
        """Find files by part of their NAME (not their contents). `type` narrows
        to extensions, comma-separated: `pdf` or `xlsx,xlsm`.

        2026-10-04: searched as Leasha's Files tab searches - names rank first,
        and a folder or the file's text can match too; switches (`after:2019`)
        and plain English ("pdf from 2019") work."""
        return await _invoke(tools.find_files, name, type, limit)

    @server.tool(annotations=read_only)
    async def read_text(path: str, max_chars: int = 20_000) -> dict:
        """The text Leasha's index holds for one file or message, by its path as
        `search` or `find_files` returned it. Read from the index, never the file."""
        return await _invoke(tools.read_text, path, max_chars)

    @server.tool(annotations=read_only)
    async def index_status() -> dict:
        """How much is in the index: files, messages and passages. An empty index
        answers every search with nothing."""
        return await _invoke(tools.index_status)

    @server.tool(annotations=read_only)
    async def find_contact(query: str, limit: int = 20) -> dict:
        """Find people in the owner's contacts by name, email address, phone number
        or company. An exact email or phone match comes first, then the best name and
        company matches (at most 50). Each answer has a `contact_id` for `get_contact`
        and `contact_activity`."""
        return await _invoke(tools.find_contact, query, limit)

    @server.tool(annotations=read_only)
    async def get_contact(contact_id: int) -> dict:
        """Everything Leasha holds for one contact, by the `contact_id` `find_contact`
        returned: each field with its label and the `sources` it came from, groups,
        tags, favourite, the accounts it is kept in (by name) and notes."""
        return await _invoke(tools.get_contact, contact_id)

    @server.tool(annotations=read_only)
    async def contact_activity(contact_id: int, limit: int = 10) -> dict:
        """The latest mail with one contact, the files exchanged with them as email
        attachments, photos of them (when Leasha knows who they are in photos) and
        the first and last contact dates. Each item names the message or file it came
        from; give a message's `path` to `read_text` for its text."""
        return await _invoke(tools.contact_activity, contact_id, limit)


async def _invoke(method: Callable[..., Any], *args: Any) -> Any:
    import inspect

    import anyio

    if inspect.iscoroutinefunction(method):
        return await method(*args)
    return await anyio.to_thread.run_sync(lambda: method(*args))


def build_server(tools: Any) -> Any:
    """An MCP server with the tools over `tools`."""
    from mcp.server import MCPServer

    from app.core.version import version

    server = MCPServer(name="leasha", title="Leasha", version=version(),
                       instructions=INSTRUCTIONS)
    _register(server, tools)
    return server


class _Forward:
    """The bridge's side: each call goes to Leasha's running server.

    `read_key` reads the key from the index again (`app.cli mcp` passes one):
    2026-10-04, code review - when Leasha's answer is "wrong key" the key is
    read once more and the call retried, so a key made after the bridge
    started is picked up without restarting the AI program.
    """

    def __init__(self, url: str, key: str,
                 read_key: Callable[[], str] | None = None) -> None:
        self._url = url
        self._key = key
        self._read_key = read_key
        #: The last HTTP status Leasha answered with - the SDK turns a 401
        #: into a generic error, so the bridge watches the responses itself.
        self._status = 0

    async def _call(self, name: str, arguments: dict) -> dict:
        retried = False
        while True:
            self._status = 0
            try:
                return await self._call_async(name, arguments)
            except Exception as exc:             # noqa: BLE001 - said as an answer
                why = _failure_of(exc, self._status)
                _log.debug("bridge call {} failed ({}): {}", name, why, exc)
            if why == "key" and not retried and self._fresh_key():
                retried = True
                continue
            return {"error": _FAILURE_WORDS[why].format(seconds=BRIDGE_TIMEOUT_S,
                                                        what=f"HTTP {self._status}"
                                                        if self._status else "no answer")}

    def _fresh_key(self) -> bool:
        """Read the key again; True if it changed, so a retry can succeed."""
        if self._read_key is None:
            return False
        try:
            key = str(self._read_key() or "")
        except Exception as exc:                 # noqa: BLE001 - the answer says why
            _log.debug("could not read Leasha's key again: {}", exc)
            return False
        if not key or key == self._key:
            return False
        self._key = key
        return True

    async def _call_async(self, name: str, arguments: dict) -> dict:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        async def noted(response: Any) -> None:
            if response.status_code >= 400:
                self._status = int(response.status_code)

        headers = {"Authorization": f"Bearer {self._key}"}
        async with httpx2.AsyncClient(headers=headers, timeout=BRIDGE_TIMEOUT_S,
                                      event_hooks={"response": [noted]}) as http, \
                Client(streamable_http_client(self._url, http_client=http)) as client:
            result = await client.call_tool(name, arguments)
        if result.structured_content is not None:
            content = result.structured_content
            return content.get("result", content) if isinstance(content, dict) else content
        text = "".join(getattr(block, "text", "") for block in result.content)
        try:
            return json.loads(text)
        except ValueError:
            return {"error": text or "Leasha gave no answer."}

    async def search(self, query: str, limit: int = 10, account: str = "") -> dict:
        arguments = {"query": query, "limit": limit}
        if account:
            arguments["account"] = account
        return await self._call("search", arguments)

    async def find_files(self, name: str, type: str = "", limit: int = 25) -> dict:
        return await self._call("find_files", {"name": name, "type": type, "limit": limit})

    async def read_text(self, path: str, max_chars: int = 20_000) -> dict:
        return await self._call("read_text", {"path": path, "max_chars": max_chars})

    async def index_status(self) -> dict:
        return await self._call("index_status", {})

    async def find_contact(self, query: str, limit: int = 20) -> dict:
        return await self._call("find_contact", {"query": query, "limit": limit})

    async def get_contact(self, contact_id: int) -> dict:
        return await self._call("get_contact", {"contact_id": contact_id})

    async def contact_activity(self, contact_id: int, limit: int = 10) -> dict:
        return await self._call("contact_activity", {"contact_id": contact_id, "limit": limit})


_FAILURE_WORDS = {"stopped": STOPPED, "key": KEY_CHANGED, "timeout": TIMED_OUT,
                  "server": SERVER_ERROR}


def _failure_of(exc: BaseException, status: int) -> str:
    """`stopped`, `key`, `timeout` or `server` for a failed bridge call.

    Connection refused is the one case that means the server is not running;
    a 401 is a key that no longer matches; a read that ran out of time is a
    busy Leasha; anything else - a 5xx, a broken answer - is an error from a
    server that is there. The SDK wraps each in task-group exception groups.
    """
    import httpx2

    if status == 401:
        return "key"
    found: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        found.append(current)
        pending.extend(getattr(current, "exceptions", ()) or ())
        for linked in (current.__cause__, current.__context__):
            if linked is not None and linked not in found and len(found) < 50:
                pending.append(linked)
    if any(isinstance(e, (httpx2.ConnectError, httpx2.ConnectTimeout, ConnectionRefusedError))
           for e in found):
        return "stopped"
    if any(isinstance(e, (httpx2.TimeoutException, TimeoutError)) for e in found):
        return "timeout"
    return "server"


def bridge_server(url: str, key: str, read_key: Callable[[], str] | None = None) -> Any:
    """The stdio server `app.cli mcp` runs, forwarding to `url` with `key`.
    `read_key` reads the key again after a "wrong key" answer (2026-10-04)."""
    return build_server(_Forward(url, key, read_key))


# ---------------------------------------------------------------------------
# Running it, inside Leasha
# ---------------------------------------------------------------------------

class _KeyRequired:
    """Refuses any HTTP request that does not carry the key. ASGI middleware."""

    def __init__(self, app: Any, key: str) -> None:
        self._app = app
        self._expected = f"Bearer {key}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            given = dict(scope.get("headers") or ()).get(b"authorization", b"")
            # Constant-time, so a wrong key cannot be narrowed down byte by byte
            # from how long the comparison takes.
            if not hmac.compare_digest(given, self._expected):
                body = b'{"error": "Leasha needs its key for this. Copy it from Settings."}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self._app(scope, receive, send)


class McpHost:
    """Starts and stops the server on a thread of its own. **Never the UI thread**:
    `start` binds a port and waits for it, so the window calls it on a worker."""

    def __init__(self) -> None:
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self.port = 0

    @property
    def running(self) -> bool:
        """Whether the server thread is alive and uvicorn reports it has started."""
        return bool(self._thread and self._thread.is_alive() and self._server
                    and self._server.started)

    def start(self, tools: Any, port: int, key: str, *, timeout_s: float = 15.0) -> str:
        """Serve `tools` on `127.0.0.1:port`. Returns the address; raises
        `AppErrorException` (`ERR_MCP_START`) with the way out."""
        import uvicorn

        if self.running:
            return endpoint(self.port)
        port = int(port)
        _check_port_free(port)
        app = _KeyRequired(build_server(tools).streamable_http_app(host="127.0.0.1"), key)
        # `timeout_graceful_shutdown` (2026-10-04, code review): an AI program
        # holding its event stream open kept uvicorn waiting for it forever, so
        # Stop left a loop and a thread behind on every Stop/Start.
        # `log_config=None` (2026-10-10): uvicorn's default logging setup asks
        # `sys.stdout.isatty()`, and under pythonw.exe - the window - there is no
        # stdout, so the server never started and every Connect failed with
        # "Unable to configure formatter 'default'". Leasha logs through its own
        # logger, so uvicorn need not configure any.
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None,
                                lifespan="on", access_log=False,
                                timeout_graceful_shutdown=GRACEFUL_S)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="leasha-mcp", daemon=True)
        self._server, self._thread, self.port = server, thread, port
        thread.start()
        deadline = time.monotonic() + timeout_s
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            self.stop()
            raise AppErrorException(make_error(
                "ERR_MCP_START", "serve.mcp", port=port,
                details="it did not start listening"))
        _log.info("MCP server listening on {}", endpoint(port))
        return endpoint(port)

    def stop(self, *, timeout_s: float = 5.0) -> None:
        """Ask uvicorn to exit, wait `timeout_s`, then force it. Never raises."""
        server, thread = self._server, self._thread
        if server is not None:
            server.should_exit = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
            if thread.is_alive() and server is not None:
                # Still serving after the graceful window (2026-10-04, code
                # review): uvicorn's own "stop now", then a short last wait.
                server.force_exit = True
                thread.join(1.0)
            if thread.is_alive():
                _log.warning("the MCP server's thread did not stop within {}s", timeout_s + 1)
        self._server, self._thread = None, None
        _log.info("MCP server stopped")


def _check_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise AppErrorException(make_error(
                "ERR_MCP_START", "serve.mcp", port=port,
                details=f"port {port} is already in use ({exc.strerror or exc})")) from exc
