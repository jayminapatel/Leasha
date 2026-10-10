"""The index for AI programs, over MCP: read-only, local, keyed. 2026-10-04.

Layer: L6

The owner: "can this index act as a mcp server for other ai programs", "do
both", "in the settings there should be a method to manage start stop etc",
"this will not be only for claude but other platforms too". Leasha runs the
server on 127.0.0.1 while it is open, every request carries Leasha's key,
four read-only tools answer from the index, and a bridge command serves
programs that can only start a command. Connect and Disconnect change only
Leasha's own entry in a program's settings file, after a backup.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from tests.unit.test_cli_wiring import env_file

MESSAGE = "pst://2024/2097188"


@pytest.fixture()
def settings(tmp_path):
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore

    loaded = load_settings(Path(env_file(tmp_path)))
    loaded.fts_db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteStore(loaded.fts_db) as store:
        report = store.upsert_file("C:/work/boiler quote.pdf", parent_dir="C:/work", ext="pdf",
                                   size_bytes=2048, mtime_ns=1_700_000_000 * 10**9,
                                   status="INDEXED", source_kind="file")
        store.replace_chunks(report, [{"ordinal": 0, "text": "Boiler service quote, spring."}])
        message = store.upsert_file(MESSAGE, size_bytes=1, mtime_ns=1, ext="pst",
                                    source_kind="pst_message", status="INDEXED")
        store.set_message(message, store_path="D:/a.pst", entry_id="2097188",
                          subject="The boiler", sender="Dave", sent_at=1_700_000_000)
        store.replace_chunks(message, [{"ordinal": 0, "text": "Dave sent the boiler quote."}])
    return loaded


def _tools(settings):
    from app.serve.mcp import IndexTools
    from tests.unit.conftest import _NoModel

    return IndexTools(settings, lambda: (_NoModel(), None, None))


def _call(server, name, arguments=None):
    from mcp import Client

    async def go():
        async with Client(server) as client:
            listed = await client.list_tools()
            result = await client.call_tool(name, arguments or {})
            return listed, result

    return asyncio.run(go())


def _answer(result):
    content = result.structured_content
    if isinstance(content, dict) and set(content) == {"result"}:
        return content["result"]
    return content if content is not None else json.loads(result.content[0].text)


# -- the tools ----------------------------------------------------------------

def test_four_tools_all_marked_read_only(settings):
    from app.serve.mcp import TOOL_NAMES, build_server

    listed, _ = _call(build_server(_tools(settings)), "index_status")
    assert sorted(t.name for t in listed.tools) == sorted(TOOL_NAMES)
    for tool in listed.tools:
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


def test_search_finds_by_what_a_file_says_and_names_the_message(settings):
    from app.serve.mcp import build_server

    _, result = _call(build_server(_tools(settings)), "search", {"query": "boiler quote"})
    found = _answer(result)
    paths = [r["path"] for r in found["results"]]
    assert "C:/work/boiler quote.pdf" in paths and MESSAGE in paths
    mail = next(r for r in found["results"] if r["path"] == MESSAGE)["mail"]
    assert mail["subject"] == "The boiler" and mail["from"] == "Dave" and mail["sent"]


def test_find_files_read_text_and_status(settings):
    from app.serve.mcp import build_server

    server = build_server(_tools(settings))
    files = _answer(_call(server, "find_files", {"name": "boiler"})[1])["files"]
    assert files[0]["path"] == "C:/work/boiler quote.pdf" and files[0]["type"] == "pdf"
    text = _answer(_call(server, "read_text", {"path": MESSAGE})[1])
    assert "Dave sent the boiler quote." in text["text"] and text["mail"]["subject"] == "The boiler"
    assert "not in the index" in _answer(_call(server, "read_text", {"path": "C:/nope"})[1])["error"]
    status = _answer(_call(server, "index_status")[1])
    assert status["files"] == 2 and status["messages"] == 1


def test_find_files_and_status_never_load_a_model(settings):
    from app.serve.mcp import IndexTools

    tools = IndexTools(settings, lambda: pytest.fail("loaded a model for a name search"))
    assert tools.find_files("boiler")["files"]
    assert tools.index_status()["files"] == 2


def test_with_no_index_every_tool_says_so(tmp_path):
    from app.core.config import load_settings
    from app.serve.mcp import IndexTools

    tools = IndexTools(load_settings(Path(env_file(tmp_path))), lambda: pytest.fail("model"))
    for answer in (tools.search("x"), tools.find_files("x"), tools.read_text("x"),
                   tools.index_status()):
        assert "No index has been built" in answer["error"]


# -- running it: local, keyed, start and stop ---------------------------------

def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_server_runs_on_this_computer_only_and_needs_its_key(settings):
    import httpx2

    from app.serve.mcp import McpHost, bridge_server

    host, port = McpHost(), _free_port()
    url = host.start(_tools(settings), port, "the-key")
    try:
        assert url == f"http://127.0.0.1:{port}/mcp" and host.running
        refused = httpx2.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert refused.status_code == 401
        # The bridge, with the key, reaches it - which is how Claude Desktop will.
        _, result = _call(bridge_server(url, "the-key"), "index_status")
        assert _answer(result)["files"] == 2
        _, wrong = _call(bridge_server(url, "not-the-key"), "index_status")
        # 2026-10-04, code review: a wrong key says so, not "stopped" (it is running).
        assert "key for AI programs has changed" in _answer(wrong)["error"]
        # And a key read again after that answer is used, once.
        _, again = _call(bridge_server(url, "not-the-key", lambda: "the-key"), "index_status")
        assert _answer(again)["files"] == 2
    finally:
        host.stop()
    assert not host.running
    _, after = _call(bridge_server(url, "the-key"), "index_status")
    assert "Settings > Models & AI > AI programs" in _answer(after)["error"]


def test_a_port_in_use_says_so_with_the_way_out(settings):
    import socket

    from app.serve.mcp import McpHost

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        with pytest.raises(AppErrorException) as raised:
            McpHost().start(_tools(settings), port, "k")
    assert raised.value.error.code == "ERR_MCP_START" and "Port for AI programs" in raised.value.error.suggestion


def test_the_key_is_made_once_and_kept(settings):
    from app.serve.mcp import key_for
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(settings.fts_db) as store:
        assert key_for(store, create=False) == ""
        first = key_for(store)
        assert len(first) >= 40 and key_for(store) == first


# -- Connect and Disconnect ----------------------------------------------------

def _program(tmp_path, style, servers_key="mcpServers"):
    from app.serve.clients import Program

    return Program("test", "Test", str(tmp_path / "app" / "settings.json"), servers_key, style)


def test_connect_adds_only_leashas_entry_after_a_backup_and_disconnect_takes_it_out(tmp_path):
    from app.serve.clients import connect, disconnect, is_connected

    program = _program(tmp_path, "url")
    program.path().parent.mkdir()
    original = {"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}
    program.path().write_text(json.dumps(original), encoding="utf-8")

    backup = connect(program, "http://127.0.0.1:8737/mcp", "k")
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    data = json.loads(program.path().read_text(encoding="utf-8"))
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["leasha"] == {"url": "http://127.0.0.1:8737/mcp",
                                            "headers": {"Authorization": "Bearer k"}}
    assert is_connected(program)

    disconnect(program)
    assert json.loads(program.path().read_text(encoding="utf-8")) == original
    assert not is_connected(program)


def test_each_program_gets_its_own_shape(tmp_path):
    from app.serve.clients import PROGRAMS, bridge_entry, connect

    vscode = _program(tmp_path, "http", servers_key="servers")
    connect(vscode, "http://127.0.0.1:1/mcp", "k")
    entry = json.loads(vscode.path().read_text(encoding="utf-8"))["servers"]["leasha"]
    assert entry["type"] == "http" and entry["headers"]["Authorization"] == "Bearer k"

    bridge = bridge_entry()
    assert bridge["args"] == ["-m", "app.cli", "mcp"]
    assert Path(bridge["command"]).name.lower() != "pythonw.exe"
    assert Path(bridge["env"]["PYTHONPATH"], "app", "cli", "mcp_server.py").is_file()
    desktop = next(p for p in PROGRAMS if p.key == "claude-desktop")
    assert desktop.style == "bridge", "Claude Desktop loses its servers when given an address"


def test_a_file_that_is_not_json_is_never_overwritten(tmp_path):
    from app.serve.clients import connect

    program = _program(tmp_path, "url")
    program.path().parent.mkdir()
    program.path().write_text("{ not json", encoding="utf-8")
    with pytest.raises(AppErrorException) as raised:
        connect(program, "http://127.0.0.1:1/mcp", "k")
    assert raised.value.error.code == "ERR_MCP_CONFIG"
    assert program.path().read_text(encoding="utf-8") == "{ not json"
    assert not list(program.path().parent.glob("*.leasha-backup-*"))


def test_claude_code_goes_through_its_own_command_when_it_is_installed(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from app.serve import clients

    program = clients.Program("claude-code", "Claude Code", str(tmp_path / ".claude.json"),
                              "mcpServers", "http", command="claude")
    program.path().write_text("{}", encoding="utf-8")
    ran = []
    monkeypatch.setattr(clients.shutil, "which", lambda name: "C:/bin/claude.exe")
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: ran.append(argv)
                        or SimpleNamespace(returncode=0, stdout="", stderr=""))
    clients.connect(program, "http://127.0.0.1:8737/mcp", "k")
    assert ran[0][1:] == ["mcp", "remove", "--scope", "user", "leasha"]
    assert ran[1][1:] == ["mcp", "add", "--scope", "user", "--transport", "http", "leasha",
                          "http://127.0.0.1:8737/mcp", "--header", "Authorization: Bearer k"]
    assert program.path().read_text(encoding="utf-8") == "{}", "the file itself is not edited"


def test_the_command_line_has_the_bridge():
    from app import cli

    parsed = cli.build_parser().parse_args(["mcp"])
    assert parsed.func.__name__ == "cmd_mcp"


def test_the_bridge_as_its_own_process_speaks_clean_mcp_on_stdout(settings, tmp_path):
    """As Claude Desktop starts it: a separate process on stdin/stdout. Anything
    printed to stdout would break the protocol, so this is the test that would see it."""
    import sys

    from mcp import Client, StdioServerParameters

    from app.serve.mcp import KEY_STATE, McpHost
    from app.storage.sqlite_store import SqliteStore

    port = _free_port()
    env = Path(env_file(tmp_path))                         # the same index as `settings`
    env.write_text(env.read_text(encoding="utf-8") + f"MCP_PORT={port}\n", encoding="utf-8")
    with SqliteStore(settings.fts_db) as store:
        store.set_state(KEY_STATE, "bridge-key")
    host = McpHost()
    host.start(_tools(settings), port, "bridge-key")
    try:
        root = Path(__file__).resolve().parents[2]
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "app.cli", "--env", str(env), "mcp"],
                                       env={"PYTHONPATH": str(root)}, cwd=str(tmp_path))

        async def go():
            async with Client(params) as client:
                return await client.call_tool("index_status", {})

        assert _answer(asyncio.run(go()))["files"] == 2
    finally:
        host.stop()


# -- Settings: Start, Stop, Connect ---------------------------------------------

@pytest.mark.gui
def test_settings_starts_and_stops_it_and_connects_a_program(gui_mainwindow, qtbot, tmp_path,
                                                             monkeypatch):
    from app.serve import clients

    _app, window, _store, _engine = gui_mainwindow
    program = clients.Program("test", "Test program", str(tmp_path / "t" / "mcp.json"),
                              "mcpServers", "url", "Restart it.")
    program.path().parent.mkdir()
    monkeypatch.setattr(clients, "PROGRAMS", (program,))
    box, ctl = window.settings_view.mcp_box, window.mcp_ctl
    box._rows = {"test": box._rows[next(iter(box._rows))]}       # one row, for the fake program

    box.port.setValue(_free_port())
    box.start.click()
    qtbot.waitUntil(lambda: box.status.text().startswith("Running at http://127.0.0.1:"),
                    timeout=15_000)
    assert ctl.host.running and box.stop.isEnabled() and not box.start.isEnabled()

    ctl.connect_program("test", True)
    qtbot.waitUntil(lambda: clients.is_connected(program), timeout=5_000)
    entry = json.loads(program.path().read_text(encoding="utf-8"))["mcpServers"]["leasha"]
    assert entry["url"] == box.status.text().removeprefix("Running at ")
    assert '"Authorization": "Bearer ' in box.address_entry()

    box.stop.click()
    qtbot.waitUntil(lambda: box.status.text() == "Stopped.", timeout=10_000)
    assert not ctl.host.running and box.start.isEnabled()


@pytest.mark.gui
def test_the_box_carries_both_settings_and_says_where_answers_go(gui_mainwindow):
    from app.ui.widgets.mcp_box import PRIVACY_NOTE

    _app, window, _store, _engine = gui_mainwindow
    box = window.settings_view.mcp_box
    assert box.port.objectName() == "MCP_PORT" and box.autostart.objectName() == "MCP_AUTOSTART"
    assert not box.autostart.isChecked(), "off by default"
    assert "goes to the AI program that asked" in PRIVACY_NOTE
    assert json.loads(box.bridge_text())["leasha"]["args"] == ["-m", "app.cli", "mcp"]


# -- 2026-10-04, code review ----------------------------------------------------

def test_the_index_stays_open_between_calls_and_searches_are_not_logged(settings):
    r"""Finding 4 and 2: one store and one engine for every call (it was a new
    store, two vector stores, an engine, a thread pool and a warm-up per
    search), and MCP's searches never reach the usage log - they showed in the
    window's recent searches and could wait on the write lock."""
    from app.serve.mcp import IndexTools
    from app.storage.sqlite_store import SqliteStore
    from tests.unit.conftest import _NoModel

    models = (_NoModel(), None, None)            # the window's: the same objects each call
    tools = IndexTools(settings, lambda: models)
    try:
        assert tools.search("boiler quote")["results"]
        index = tools._index
        engine = index._engine
        assert engine.log_usage is False
        assert tools.find_files("boiler")["files"] and tools.index_status()["files"] == 2
        assert tools.search("boiler")["results"]
        assert tools._index is index and index._engine is engine, "reopened per call"
    finally:
        tools.close()
    with SqliteStore(settings.fts_db) as store:
        assert store.recent_searches() == [], "an MCP search was logged as the owner's"


def test_a_replaced_index_file_is_reopened_and_the_old_one_closed(settings, monkeypatch):
    tools = _tools(settings)
    try:
        tools.index_status()
        first = tools._index
        monkeypatch.setattr(tools, "_identity", lambda: ("another index",))
        assert tools.index_status()["files"] == 2
        assert tools._index is not first and first.store._closed
    finally:
        tools.close()


def test_a_vector_store_that_cannot_open_costs_the_meaning_half_not_the_call(settings, tmp_path):
    r"""Finding 6: opened deferred, as the window opens it. A vector folder that
    is a file used to fail the whole search out of `__enter__`."""
    blocked = tmp_path / "not a folder"
    blocked.write_text("x", encoding="utf-8")
    tools = _tools(settings.model_copy(update={"vector_path": blocked}))
    try:
        found = tools.search("boiler quote")
        assert "error" not in found and found["results"]
    finally:
        tools.close()


def test_closing_refuses_new_calls_and_waits_for_one_in_flight(settings):
    import threading

    from app.serve.mcp import CLOSING

    tools = _tools(settings)
    tools.index_status()
    index = tools._index
    entered, release = threading.Event(), threading.Event()

    def busy():
        with tools._open():
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=busy)
    worker.start()
    assert entered.wait(5)
    tools.close(wait_s=0.2)                       # the call is still running
    assert tools.search("boiler")["error"] == CLOSING
    assert tools.index_status()["error"] == CLOSING
    assert not index.store._closed, "closed under a call still running"
    release.set()
    worker.join(5)
    assert index.store._closed, "the last call out did not close it"


def test_stop_returns_with_an_event_stream_still_open(settings):
    r"""Finding 5: an AI program holding its stream open kept uvicorn waiting
    forever, so each Stop leaked a loop and a thread."""
    import threading
    import time

    import httpx2

    from app.serve.mcp import McpHost

    host, port = McpHost(), _free_port()
    url = host.start(_tools(settings), port, "k")
    connected = threading.Event()

    def hold_open():
        # A session, then the GET event stream a program keeps open for
        # messages from the server. Measured before the fix: Stop took its full
        # 5 s and the thread was still alive; after, 2.4 s and gone.
        async def go():
            headers = {"Authorization": "Bearer k", "Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream"}
            async with httpx2.AsyncClient(headers=headers, timeout=60) as http:
                started = await http.post(url, json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "test", "version": "1"}}})
                session = {"mcp-session-id": started.headers.get("mcp-session-id", "")}
                await http.post(url, headers=session,
                                json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                async with http.stream("GET", url, headers={
                        **session, "Accept": "text/event-stream"}) as stream:
                    assert stream.status_code == 200
                    connected.set()
                    async for _ in stream.aiter_bytes():
                        pass

        try:
            asyncio.run(go())
        except BaseException:                    # noqa: BLE001 - the server went away
            pass

    threading.Thread(target=hold_open, daemon=True).start()
    assert connected.wait(15)
    thread = host._thread
    began = time.monotonic()
    host.stop(timeout_s=5.0)
    assert time.monotonic() - began < 8 and not thread.is_alive()


def test_each_bridge_failure_says_what_it_is():
    import httpx2

    from app.serve.mcp import _failure_of

    def grouped(exc):
        return ExceptionGroup("task group", [ExceptionGroup("inner", [exc])])

    assert _failure_of(grouped(httpx2.ConnectError("refused")), 0) == "stopped"
    assert _failure_of(grouped(RuntimeError("Server returned an error response")), 401) == "key"
    assert _failure_of(grouped(httpx2.ReadTimeout("slow")), 0) == "timeout"
    assert _failure_of(grouped(RuntimeError("Server returned an error response")), 500) == "server"


def test_two_workers_asking_for_a_key_at_once_get_the_same_one(settings):
    r"""Finding 11: Start and the status refresh both made a key when none was
    there, and the last write won."""
    import threading

    from app.serve.mcp import key_for
    from app.storage.sqlite_store import SqliteStore

    keys: list = []
    barrier = threading.Barrier(6)

    def ask():
        with SqliteStore(settings.fts_db) as store:
            barrier.wait(5)
            keys.append(key_for(store))

    threads = [threading.Thread(target=ask) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(keys) == 6 and len(set(keys)) == 1
    with SqliteStore(settings.fts_db) as store:
        assert key_for(store, create=False) == keys[0]
    source = (Path(__file__).resolve().parents[2] / "app" / "ui" / "controllers"
              / "mcp_controller.py").read_text(encoding="utf-8")
    assert "key_for(store, create=False)" in source, "the refresh makes a key again"


def test_claude_code_without_its_command_is_never_edited_beside_it(tmp_path, monkeypatch):
    r"""Finding 10a: without `claude` on PATH, Connect rewrote `~/.claude.json`,
    which Claude Code may be rewriting at that moment."""
    from app.serve import clients

    program = clients.Program("claude-code", "Claude Code", str(tmp_path / ".claude.json"),
                              "mcpServers", "http", command="claude")
    program.path().write_text('{"projects": {}}', encoding="utf-8")
    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    for action in (lambda: clients.connect(program, "http://127.0.0.1:1/mcp", "k"),
                   lambda: clients.disconnect(program)):
        with pytest.raises(AppErrorException) as raised:
            action()
        assert "Copy address and key" in raised.value.error.suggestion
    assert program.path().read_text(encoding="utf-8") == '{"projects": {}}'
    assert not list(tmp_path.glob("*.leasha-backup-*"))


def test_backups_are_never_overwritten_and_the_original_is_always_kept(tmp_path):
    from app.serve.clients import KEEP_BACKUPS, connect, disconnect

    program = _program(tmp_path, "url")
    program.path().parent.mkdir()
    program.path().write_text('{"theme": "original"}', encoding="utf-8")
    made = []
    for turn in range(KEEP_BACKUPS + 4):
        made.append(connect(program, f"http://127.0.0.1:{turn + 1}/mcp", "k"))
        made.append(disconnect(program))
    assert len(set(made)) == len(made), "two backups shared a name"
    left = sorted(program.path().parent.glob("*.leasha-backup-*"), key=lambda p: p.name)
    assert len(left) == KEEP_BACKUPS + 1
    assert json.loads(left[0].read_text(encoding="utf-8")) == {"theme": "original"}
    assert left[-1] == made[-1]


def test_a_key_never_reaches_an_error_message(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from app.serve import clients

    program = clients.Program("claude-code", "Claude Code", str(tmp_path / ".claude.json"),
                              "mcpServers", "http", command="claude")
    program.path().write_text("{}", encoding="utf-8")
    monkeypatch.setattr(clients.shutil, "which", lambda name: "C:/bin/claude.exe")
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: SimpleNamespace(
        returncode=1, stdout="", stderr=f"bad header: {argv[-1]}"))
    with pytest.raises(AppErrorException) as raised:
        clients.connect(program, "http://127.0.0.1:8737/mcp", "s3cret-key")
    assert "s3cret-key" not in str(raised.value.error.details)
    assert "Bearer ..." in str(raised.value.error.details)


def test_backups_made_in_one_clock_tick_never_reuse_a_name(tmp_path, monkeypatch):
    """2026-10-05. Windows' clock gave 5 distinct readings in 2,000 calls, so
    backups made close together share a stamp and were told apart by "-1",
    "-2". Pruning removed a middle one, and the next backup took its freed
    name: "two backups shared a name", whenever the machine was fast enough.
    The clock is held still here, so it fails every time on the old code."""
    from datetime import datetime as real

    from app.serve import clients

    class Still:
        @staticmethod
        def now():
            return real(2026, 10, 5, 20, 0, 0, 123456)

    monkeypatch.setattr(clients, "datetime", Still)
    program = _program(tmp_path, "url")
    program.path().parent.mkdir()
    program.path().write_text('{"theme": "original"}', encoding="utf-8")
    # An earlier day's backup is the one kept as the original, so this tick's
    # own first backup - the one without a number - is pruned like any other.
    first = program.path().with_name(program.path().name + ".leasha-backup-20260101-000000-000000")
    first.write_text('{"theme": "original"}', encoding="utf-8")
    made = []
    for turn in range(clients.KEEP_BACKUPS + 8):
        made.append(clients.connect(program, f"http://127.0.0.1:{turn + 1}/mcp", "k"))
        made.append(clients.disconnect(program))
    assert len(set(made)) == len(made), "two backups shared a name"
    kept = sorted(p.name for p in program.path().parent.glob("*.leasha-backup-*"))
    assert len(kept) == clients.KEEP_BACKUPS + 1
    assert kept[0] == first.name, "the oldest, the file before Leasha changed it, is kept"
    assert kept[1:] == [p.name for p in made[-clients.KEEP_BACKUPS:]], "the newest are kept"


def test_the_server_starts_with_no_console_as_under_pythonw(settings, monkeypatch):
    """2026-10-10. The window runs under pythonw.exe, where sys.stdout is None.
    Uvicorn's default logging config asks sys.stdout.isatty() and raised
    AttributeError, so the server never started ("could not connect"). The
    server must start with no standard output at all."""
    import sys

    from app.serve.mcp import McpHost

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    host, port = McpHost(), _free_port()
    try:
        url = host.start(_tools(settings), port, "pythonw-key")
        assert url.endswith("/mcp")
        assert host.running
    finally:
        host.stop()


def test_connect_is_offered_only_when_a_program_is_not_yet_connected(gui_mainwindow):
    """2026-10-10: the screen showed Claude Desktop as Connected with Connect still enabled.
    Uses whatever programs the box holds, so it does not depend on the order of other tests."""
    import importlib

    import app.serve.clients as clients
    from app.core.config import load_settings
    from app.ui.widgets.mcp_box import McpBox

    importlib.reload(clients)                   # other tests may have swapped the list
    box = McpBox(load_settings(create_dirs=False, check_writable=False))
    keys = list(box._rows)
    assert len(keys) >= 3, keys
    connected, not_connected, not_installed = keys[0], keys[1], keys[2]
    box.show_programs({connected: "connected", not_connected: "not connected",
                       not_installed: "not installed"}, "k")
    _label, connect, disconnect = box._rows[connected]
    assert connect.isEnabled() is False and disconnect.isEnabled() is True
    _label, connect, disconnect = box._rows[not_connected]
    assert connect.isEnabled() is True and disconnect.isEnabled() is False
    _label, connect, disconnect = box._rows[not_installed]
    assert connect.isEnabled() is False and disconnect.isEnabled() is False


def test_the_program_states_are_re_read_while_the_box_is_on_screen(gui_mainwindow, qtbot):
    """3.1 (2026-10-10): a removed entry must show as Not connected without an action."""
    from app.ui.controllers import mcp_controller

    _app, window, _store, _engine = gui_mainwindow
    controller = window.mcp_ctl
    assert controller._recheck.interval() == mcp_controller.RECHECK_MS
    assert controller._recheck.isActive()
    seen = []
    controller.refresh = lambda: seen.append(1)
    box = controller._box
    box.isVisible = lambda: False               # the window is not shown in this test
    controller._recheck_if_shown()
    assert seen == [], "a box nobody can see is not re-read"
    box.isVisible = lambda: True
    controller._recheck_if_shown()
    assert seen == [1]


def test_gemini_is_a_local_program_with_its_own_entry_shape(tmp_path):
    """3.2 (2026-10-10). The entry uses `httpUrl` and a header; UNVERIFIED on a real Gemini CLI."""
    from app.serve import clients

    keys = [p.key for p in clients.PROGRAMS]
    assert "gemini" in keys
    gemini = clients.Program("gemini", "Gemini CLI", str(tmp_path / "settings.json"),
                             "mcpServers", "gemini")
    backup = clients.connect(gemini, "http://127.0.0.1:8737/mcp", "k3")
    assert backup is None, "no file before: nothing to back up"
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["leasha"] == {"httpUrl": "http://127.0.0.1:8737/mcp",
                                            "headers": {"Authorization": "Bearer k3"}}
    assert clients.is_connected(gemini) is True
    clients.disconnect(gemini)
    assert clients.is_connected(gemini) is False


def test_a_program_started_before_a_connect_is_named(monkeypatch):
    """3.1 (2026-10-10): Claude Desktop keeps the settings it read at start-up."""
    import time

    import psutil

    from app.serve import clients

    me = psutil.Process().name()
    assert clients.started_before(me, time.time()) is True
    assert clients.started_before(me, 0) is False
    assert clients.started_before("no-such-program-xyz.exe", time.time()) is False


def test_only_a_process_with_the_right_name_is_asked_its_start_time(monkeypatch):
    """2026-10-10, A5: asking every process its start time held the window 731 ms
    (lag monitor, 08:56:20). Only the name is read of every process now."""
    import psutil

    from app.serve import clients

    asked = []

    class _Proc:
        def __init__(self, name, created):
            self.info = {"name": name}
            self._created = created

        def create_time(self):
            asked.append(self.info["name"])
            return self._created

    procs = [_Proc("explorer.exe", 1.0), _Proc("Claude.exe", 5.0), _Proc("svchost.exe", 1.0)]
    seen_attrs = []

    def fake_iter(attrs=None, *a, **k):
        seen_attrs.append(list(attrs or []))
        return iter(procs)

    monkeypatch.setattr(psutil, "process_iter", fake_iter)
    assert clients.started_before("claude.exe", 10.0) is True
    assert clients.started_before("claude.exe", 1.0) is False
    assert asked == ["Claude.exe", "Claude.exe"], "no other process is asked its start time"
    assert all("create_time" not in attrs for attrs in seen_attrs)


@pytest.mark.gui
def test_connect_asks_whether_claude_desktop_was_running_on_the_worker(
        gui_mainwindow, qtbot, tmp_path, monkeypatch):
    """2026-10-10, A5: `started_before` walks every process; it ran in `done`, on the
    interface thread, and held the window 731 ms. It must run on the worker, and the
    window must still be told when Claude Desktop was already running."""
    import threading

    from app.serve import clients
    from app.ui.controllers import mcp_controller

    _app, window, _store, _engine = gui_mainwindow
    program = clients.Program("claude-desktop", "Claude Desktop",
                              str(tmp_path / "c" / "claude_desktop_config.json"),
                              "mcpServers", "url", "Restart it.")
    program.path().parent.mkdir()
    monkeypatch.setattr(clients, "PROGRAMS", (program,))
    ui_thread = threading.get_ident()
    asked_on = []

    def fake_started_before(_name, _when):
        asked_on.append(threading.get_ident())
        return True

    monkeypatch.setattr(mcp_controller, "started_before", fake_started_before)
    told = []
    monkeypatch.setattr(window, "notify", lambda text, *_a, **_k: told.append(text))
    ctl = window.mcp_ctl
    monkeypatch.setattr(ctl, "refresh", lambda: None)
    ctl.connect_program("claude-desktop", True)
    qtbot.waitUntil(lambda: len(told) >= 2, timeout=5_000)
    assert asked_on and all(ident != ui_thread for ident in asked_on), \
        "the process walk ran on the interface thread"
    assert any("already running" in text for text in told)
