r"""Connect and Disconnect: Leasha's entry in an AI program's own settings file.

Layer: L6.

2026-10-04, the owner, asked whether Leasha may edit those files: "Yes, with a
backup" - and "this will not be only for claude but other platforms too".
Each program keeps its MCP servers in a JSON file of its own, each in a
slightly different shape; `PROGRAMS` holds the shape per program, as each
maker documents it (checked 2026-10-04; marked where not).

**What Connect does to a file, and nothing else:** reads it (and refuses to
touch one that is not valid JSON - `ERR_MCP_CONFIG`), copies it beside
itself as `<name>.leasha-backup-<when>`, sets the one entry named `leasha`
under the program's servers key, and writes it back through a temporary
file, so a failure half-way leaves the original. Disconnect removes only that
entry. Every other server and setting in the file is left as it was.

**Claude Desktop is given the bridge, not the address.** Its settings file
is reported to lose every MCP server when one is given by `url`
(anthropics/claude-code#37286), so its entry starts `app.cli mcp`, which
passes each call to Leasha's running server. Any program that can only start
a command can use the same entry - `bridge_entry`, under "Other programs".
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.errors import AppErrorException, make_error

__all__ = ["ENTRY_NAME", "PROGRAMS", "Program", "bridge_entry", "connect", "disconnect",
           "http_entry", "is_connected"]

ENTRY_NAME = "leasha"


@dataclass(frozen=True)
class Program:
    """One AI program: where its settings file is, and the shape it wants."""

    key: str
    name: str
    #: The file, with `%APPDATA%` / `~` left to expand on this computer.
    file: str
    #: The object that holds the servers: `mcpServers` for most, `servers` for VS Code.
    servers_key: str
    #: "bridge" (a command), "http" (`type: http` + url + headers) or
    #: "url" (url + headers, no type).
    style: str
    #: Shown under the name; says what is not confirmed.
    note: str = ""
    #: The program's own command for adding a server, used when it is on
    #: PATH: Claude Code rewrites `~/.claude.json` while it runs, so an edit
    #: made beside it could be written over a moment later.
    command: str = ""

    def path(self) -> Path:
        return Path(os.path.expandvars(self.file)).expanduser()

    def installed(self) -> bool:
        """The program's settings folder exists - it is installed, or has been."""
        return self.path().parent.is_dir()


PROGRAMS: tuple[Program, ...] = (
    Program("claude-desktop", "Claude Desktop", r"%APPDATA%\Claude\claude_desktop_config.json",
            "mcpServers", "bridge",
            "Uses the bridge command. Restart Claude Desktop after connecting."),
    Program("claude-code", "Claude Code", r"~\.claude.json", "mcpServers", "http",
            "For every project. Start a new Claude Code session after connecting.",
            command="claude"),
    Program("cursor", "Cursor", r"~\.cursor\mcp.json", "mcpServers", "url",
            "Restart Cursor after connecting."),
    Program("vscode", "VS Code", r"%APPDATA%\Code\User\mcp.json", "servers", "http",
            "Your default VS Code profile. The key header is not in VS Code's own "
            "example (UNCONFIRMED)."),
)


def http_entry(style: str, url: str, key: str) -> dict:
    """The address form: `url` and the key in a header."""
    entry: dict[str, Any] = {"url": url, "headers": {"Authorization": f"Bearer {key}"}}
    if style == "http":
        entry = {"type": "http", **entry}
    return entry


def bridge_entry() -> dict:
    """The command form: the venv's `python.exe -m app.cli mcp`, from anywhere.

    `python.exe`, not `pythonw.exe` - the bridge talks on stdin/stdout, which
    a windowless Python does not have. `PYTHONPATH` is the project, so `-m`
    finds `app` whatever folder the AI program starts it in.
    """
    from app.core.version import PROJECT_ROOT

    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    return {"command": str(python), "args": ["-m", "app.cli", "mcp"],
            "env": {"PYTHONPATH": str(PROJECT_ROOT)}}


def entry_for(program: Program, url: str, key: str) -> dict:
    return bridge_entry() if program.style == "bridge" else http_entry(program.style, url, key)


def is_connected(program: Program) -> bool:
    """Leasha's entry is in the file. Never raises; reads only."""
    try:
        data = json.loads(program.path().read_text(encoding="utf-8"))
        return ENTRY_NAME in (data.get(program.servers_key) or {})
    except (OSError, ValueError, AttributeError):
        return False


def connect(program: Program, url: str, key: str) -> Path | None:
    """Put Leasha's entry in `program`'s settings file. Returns the backup's
    path (None when there was no file before). **Worker thread.**"""
    if _own_command(program):
        # Remove first: an entry left from an earlier port or key is replaced, not kept.
        backup = _by_command(program, ["remove", "--scope", "user", ENTRY_NAME], check=False)
        _by_command(program, ["add", "--scope", "user", "--transport", "http", ENTRY_NAME,
                              url, "--header", f"Authorization: Bearer {key}"], backup=False)
        return backup

    def change(data: dict) -> None:
        servers = data.setdefault(program.servers_key, {})
        if not isinstance(servers, dict):
            raise ValueError(f"'{program.servers_key}' is not an object")
        servers[ENTRY_NAME] = entry_for(program, url, key)

    return _rewrite(program.path(), change)


def disconnect(program: Program) -> Path | None:
    """Take Leasha's entry out, and nothing else. **Worker thread.**"""
    def change(data: dict) -> None:
        servers = data.get(program.servers_key)
        if isinstance(servers, dict):
            servers.pop(ENTRY_NAME, None)

    if not program.path().is_file():
        return None
    if _own_command(program):
        return _by_command(program, ["remove", "--scope", "user", ENTRY_NAME], check=False)
    return _rewrite(program.path(), change)


def _own_command(program: Program) -> str:
    return (shutil.which(program.command) or "") if program.command else ""


def _by_command(program: Program, arguments: list[str], *, check: bool = True,
                backup: bool = True) -> Path | None:
    """Through the program's own `mcp` command, after the same backup."""
    import subprocess

    from app.core.osbridge import hidden_console_flags

    path = program.path()
    saved = _backup(path) if backup else None
    try:
        done = subprocess.run([_own_command(program), "mcp", *arguments], capture_output=True,
                              text=True, timeout=60, creationflags=hidden_console_flags())
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppErrorException(make_error(
            "ERR_MCP_CONFIG", "serve.clients", path=str(path),
            details=f"{type(exc).__name__}: {exc}")) from exc
    if check and done.returncode != 0:
        raise AppErrorException(make_error(
            "ERR_MCP_CONFIG", "serve.clients", path=str(path),
            details=(done.stderr or done.stdout).strip()[:400]))
    return saved


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    backup = path.with_name(f"{path.name}.leasha-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    return backup


def _rewrite(path: Path, change: Any) -> Path | None:
    try:
        existed = path.is_file()
        text = path.read_text(encoding="utf-8") if existed else ""
        data = json.loads(text) if text.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("the file is not a JSON object")
        change(data)
        backup = _backup(path) if existed else None
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.leasha-writing")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        return backup
    except (OSError, ValueError) as exc:
        raise AppErrorException(make_error(
            "ERR_MCP_CONFIG", "serve.clients", path=str(path),
            details=f"{type(exc).__name__}: {exc}")) from exc
