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
        assert "stopped" in _answer(wrong)["error"]
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
