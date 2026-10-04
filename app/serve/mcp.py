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

**Read-only.** Four tools - `search`, `find_files`, `read_text`,
`index_status` - and nothing that writes, moves, deletes or opens anything.
Every answer names the file it came from. **The privacy fact, stated once:**
Leasha itself sends nothing anywhere, but what a tool returns goes to the AI
program that asked, and one that runs in the cloud sends it there.

**One copy of the models.** Inside Leasha the server uses the window's own
embedder and reranker (`models` is a callable returning them); loading a
second copy measured 1.4 GB and 4.1 s on the owner's laptop, 2026-10-04.
The index is opened afresh for each call, as `app.cli search` opens it, so
the server sees what an index run has just written; WAL makes reading beside
a run safe.

**The bridge.** A program that can only start a command - Claude Desktop,
whose settings file is reported to lose every server when one is given by
address (anthropics/claude-code#37286) - runs `app.cli mcp`, which speaks
MCP on its stdin/stdout and passes each call to the running server
(`bridge_server`). With Leasha's server stopped, each tool answers that it
is stopped and where to start it.
"""

from __future__ import annotations

import hmac
import json
import secrets
import socket
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
TOOL_NAMES = ("search", "find_files", "read_text", "index_status")

#: What one search returns at most, and what one result's passage is cut to.
SEARCH_LIMIT_MAX = 50
SNIPPET_CHARS = 600
#: The most text `read_text` returns for one file.
TEXT_CHARS_MAX = 200_000

INSTRUCTIONS = (
    "Leasha is the owner's local search index of their own files and Outlook mail. "
    "Use `search` to find documents and messages by what they say, `find_files` to find a "
    "file by its name, and `read_text` for the whole indexed text of one result. Every answer "
    "names the file it came from - cite the path. Nothing here can change, move or open a file."
)

STOPPED = ("Leasha's AI access is stopped. Open Leasha, go to Settings > Models & AI > "
           "AI programs, and press Start.")


def endpoint(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/mcp"


def key_for(store: Any, *, create: bool = True) -> str:
    """The key AI programs send, made once and kept. `""` if none and not `create`."""
    key = str(store.get_state(KEY_STATE, "") or "")
    if not key and create:
        key = secrets.token_urlsafe(32)
        store.set_state(KEY_STATE, key)
    return key


# ---------------------------------------------------------------------------
# The four tools, over the index
# ---------------------------------------------------------------------------

class IndexTools:
    """What each tool does, against the index `settings` names.

    `models` returns `(embedder, reranker, clip_text_embedder)`; called only
    by `search`, so the other three never load a model.
    """

    def __init__(self, settings: Any, models: Callable[[], tuple]) -> None:
        self._settings = settings
        self._models = models

    def _missing(self) -> dict | None:
        if self._settings.fts_db.is_file():
            return None
        return {"error": "No index has been built yet. Open Leasha and index some folders first."}

    def _store(self) -> Any:
        from app.storage.sqlite_store import SqliteStore

        return SqliteStore(self._settings.fts_db)

    def search(self, query: str, limit: int = 10) -> dict:
        from app.search.commands import expand_slashes
        from app.search.engine import SearchEngine
        from app.storage.vector_store import ImageVectorStore, VectorStore

        raw = str(query or "").strip()
        if not raw:
            return {"error": "Give something to search for."}
        missing = self._missing()
        if missing:
            return missing
        limit = max(1, min(int(limit or 10), SEARCH_LIMIT_MAX))
        embedder, reranker, clip = self._models()
        settings = self._settings
        with self._store() as store, \
                VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
                ImageVectorStore(settings.vector_path) as image_vectors:
            engine = SearchEngine(store, vectors, embedder, reranker=reranker,
                                  image_vectors=image_vectors, clip_text_embedder=clip)
            try:
                engine.warm_up()
                # `expand_slashes` first, as every other entry point does - see
                # `cli.search.cmd_search` for the bug it prevents.
                response = engine.search(expand_slashes(raw), limit=limit)
            finally:
                engine.close()
            results = [self._result(store, r) for r in response.results]
        return {"query": raw, "results": results,
                "notices": [n.as_dict() for n in response.notices]}

    def find_files(self, name: str, type: str = "", limit: int = 25) -> dict:
        text = str(name or "").strip()
        if not text:
            return {"error": "Give part of a file name."}
        missing = self._missing()
        if missing:
            return missing
        limit = max(1, min(int(limit or 25), SEARCH_LIMIT_MAX * 4))
        kinds = [k.strip().lstrip(".") for k in str(type or "").split(",") if k.strip()]
        with self._store() as store:
            hits = store.search_files_by_name(text, limit=limit, ext=kinds or None)
        files = []
        for hit in hits:
            row = dict(hit)
            files.append({"path": row.get("path"), "name": row.get("name"),
                          "type": row.get("ext"), "size_bytes": row.get("size_bytes") or None,
                          "modified": _when((row.get("mtime_ns") or 0) / 1e9)})
        return {"name": text, "files": files}

    def read_text(self, path: str, max_chars: int = 20_000) -> dict:
        from app.ui.preview_loader import join_chunks

        key = str(path or "").strip()
        if not key:
            return {"error": "Give the path of a file, as search or find_files returned it."}
        missing = self._missing()
        if missing:
            return missing
        limit = max(1, min(int(max_chars or 20_000), TEXT_CHARS_MAX))
        with self._store() as store:
            record = store.get_file(key)
            if record is None:
                return {"error": f"'{key}' is not in the index. Use the path exactly as "
                                 "search returned it."}
            text = join_chunks(store.chunks_for_file(int(record.id)))
            mail = _mail_of(store, record.id, key)
        out: dict = {"path": key, "chars": len(text), "truncated": len(text) > limit,
                     "text": text[:limit]}
        if mail:
            out["mail"] = mail
        return out

    def index_status(self) -> dict:
        missing = self._missing()
        if missing:
            return missing
        with self._store() as store:
            def count(table: str) -> int:
                return int(store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

            return {"files": count("files"), "messages": count("messages"),
                    "passages": count("chunks")}

    @staticmethod
    def _result(store: Any, result: Any) -> dict:
        found = result.as_dict()
        text = " ".join(str(found.get("text") or "").split())
        out = {"rank": found["rank"], "path": found["path"], "page": found.get("page") or None,
               "score": found["score"],
               "passage": text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS - 1] + "…"}
        mail = _mail_of(store, found.get("file_id"), str(found["path"]))
        if mail:
            out["mail"] = mail
        return out


def _mail_of(store: Any, file_id: Any, path: str) -> dict | None:
    """Subject, sender and date for a message, or the message an attachment came on."""
    from app.ui.presenter.mail import attachment_of

    try:
        message = store.get_message(int(file_id or 0))
        attached = ""
        if not message:
            parent, attached = attachment_of(path)
            record = store.get_file(parent) if parent else None
            message = store.get_message(record.id) if record is not None else None
        if not message:
            return None
    except Exception as exc:                     # noqa: BLE001 - a result without its card
        _log.debug("no mail details for {}: {}", path, exc)
        return None
    mail = {"subject": message.get("subject") or "", "from": message.get("sender") or "",
            "sent": _when(message.get("sent_at"))}
    if attached:
        mail["attachment"] = attached
    return mail


def _when(seconds: Any) -> str:
    """A local date and time an AI program can read and compare, or `""`."""
    from datetime import datetime

    try:
        value = float(seconds or 0)
        return datetime.fromtimestamp(value).isoformat(timespec="minutes") if value > 0 else ""
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


# ---------------------------------------------------------------------------
# The server, and the bridge - the same four tools, defined once
# ---------------------------------------------------------------------------

def _register(server: Any, tools: Any) -> None:
    """The four tools, their wording and their read-only marks, on `server`.
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
    async def search(query: str, limit: int = 10) -> dict:
        """Search the owner's files and mail by what they say, as Leasha's search box does.

        Plain English works ("the boiler service quote from last spring"), and so do
        Leasha's own switches: `type:pdf`, `/newest`, `from:dave`. Returns the best
        matches, each with its path, a passage and, for mail, the subject, sender
        and date. Use `read_text` with a path for the whole text.
        """
        return await _invoke(tools.search, query, limit)

    @server.tool(annotations=read_only)
    async def find_files(name: str, type: str = "", limit: int = 25) -> dict:
        """Find files by part of their NAME (not their contents). `type` narrows
        to extensions, comma-separated: `pdf` or `xlsx,xlsm`."""
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


async def _invoke(method: Callable[..., Any], *args: Any) -> Any:
    import inspect

    import anyio

    if inspect.iscoroutinefunction(method):
        return await method(*args)
    return await anyio.to_thread.run_sync(lambda: method(*args))


def build_server(tools: Any) -> Any:
    """An MCP server with the four tools over `tools`."""
    from mcp.server import MCPServer

    from app.core.version import version

    server = MCPServer(name="leasha", title="Leasha", version=version(),
                       instructions=INSTRUCTIONS)
    _register(server, tools)
    return server


class _Forward:
    """The bridge's side: each call goes to Leasha's running server."""

    def __init__(self, url: str, key: str) -> None:
        self._url = url
        self._key = key

    async def _call(self, name: str, arguments: dict) -> dict:
        try:
            return await self._call_async(name, arguments)
        except Exception as exc:                 # noqa: BLE001 - said as an answer
            _log.debug("bridge call {} failed: {}", name, exc)
            return {"error": STOPPED}

    async def _call_async(self, name: str, arguments: dict) -> dict:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        headers = {"Authorization": f"Bearer {self._key}"}
        async with httpx2.AsyncClient(headers=headers, timeout=120) as http,                 Client(streamable_http_client(self._url, http_client=http)) as client:
            result = await client.call_tool(name, arguments)
        if result.structured_content is not None:
            content = result.structured_content
            return content.get("result", content) if isinstance(content, dict) else content
        text = "".join(getattr(block, "text", "") for block in result.content)
        try:
            return json.loads(text)
        except ValueError:
            return {"error": text or "Leasha gave no answer."}

    async def search(self, query: str, limit: int = 10) -> dict:
        return await self._call("search", {"query": query, "limit": limit})

    async def find_files(self, name: str, type: str = "", limit: int = 25) -> dict:
        return await self._call("find_files", {"name": name, "type": type, "limit": limit})

    async def read_text(self, path: str, max_chars: int = 20_000) -> dict:
        return await self._call("read_text", {"path": path, "max_chars": max_chars})

    async def index_status(self) -> dict:
        return await self._call("index_status", {})


def bridge_server(url: str, key: str) -> Any:
    """The stdio server `app.cli mcp` runs, forwarding to `url` with `key`."""
    return build_server(_Forward(url, key))


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
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                lifespan="on", access_log=False)
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
        server, thread = self._server, self._thread
        if server is not None:
            server.should_exit = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
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
