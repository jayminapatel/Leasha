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

import glob
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
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
        """Where the file is on this computer. **Never raises.**"""
        text = settings_file_text(self.file, windows=os.name == "nt")
        try:
            return Path(text).expanduser()
        except (RuntimeError, ValueError, OSError):
            return Path(text)

    def installed(self) -> bool:
        """The program's settings folder exists - it is installed, or has been."""
        try:
            path = self.path()
            # A `%NAME%` still in it is a Windows folder this computer does
            # not have, not a folder beside wherever Leasha was started.
            return "%" not in str(path) and path.parent.is_dir()
        except OSError:
            return False


def settings_file_text(file: str, *, windows: bool) -> str:
    r"""`file` with its variables filled in, written the way this system reads paths.

    The four files in `PROGRAMS` are written the Windows way - `~\.claude.json`.
    Off Windows a backslash is an ordinary character, so `expanduser` read
    `~\.claude.json` as "the home folder of a user called `\.claude.json`" and
    raised `RuntimeError: Could not determine home directory`. That reached the
    window as an error box the moment it opened - and an error box waits for a
    click, so every test that builds the window stopped there: forty minutes on
    Linux, and the whole ninety allowed on macOS (2026-10-05, the first
    whole-suite runs off Windows). Off Windows the separators are turned round,
    which also makes `~/.claude.json` the right file on a Mac.
    """
    text = os.path.expandvars(file)
    return text if windows else text.replace("\\", "/")


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
    # 3.2 (2026-10-10). Gemini CLI reads ~/.gemini/settings.json; a remote server is
    # given as `httpUrl` with headers. Not installed on the owner's laptop when this was
    # written, so the shape is UNVERIFIED until it is connected to a real Gemini CLI.
    #
    # 2026-10-10, A7: the shape is CONFIRMED against Gemini CLI's own documentation,
    # https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md
    # (read that day): servers live under `mcpServers` in `settings.json`, user scope
    # `~/.gemini/settings.json`; `httpUrl` is the streamable-HTTP endpoint (which is
    # what Leasha's /mcp serves - `url` there would mean SSE, the wrong transport);
    # `headers` is an object of header name to string, and the documentation's own
    # example is `"Authorization": "Bearer your-api-token"`. What is still unverified
    # is a real Gemini CLI reading it on this computer - which is what the note shown
    # under the name says, so the note is left as it is.
    Program("gemini", "Gemini CLI", r"~\.gemini\settings.json", "mcpServers", "gemini",
            "Restart Gemini CLI after connecting. Shape per its documentation (UNVERIFIED "
            "on this computer)."),
)


def http_entry(style: str, url: str, key: str) -> dict:
    """The address form: `url` and the key in a header."""
    if style == "gemini":
        return {"httpUrl": url, "headers": {"Authorization": f"Bearer {key}"}}
    entry: dict[str, Any] = {"url": url, "headers": {"Authorization": f"Bearer {key}"}}
    if style == "http":
        entry = {"type": "http", **entry}
    return entry


def started_before(process_name: str, when: float) -> bool:
    """Whether a process called `process_name` was already running at `when` (epoch
    seconds). Never raises: an unknown answer is False, and nothing is said.

    **Worker thread** - it walks every process on the computer.

    2026-10-10, A5: only the *name* is asked of every process; the start time
    is asked only of a process with the right name. On Windows psutil answers
    `create_time` through `proc_info`, which opens each process in turn - the
    stack the lag monitor caught on the interface thread at 08:56:20 that day
    (731 ms, `logs/runs/run-20261010-085501-window.log`) was inside exactly
    that call, for a computer's worth of processes, to find one program. The
    caller has also been moved to a worker (`McpController.connect_program`).
    """
    try:
        import psutil

        wanted = process_name.lower()
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() != wanted:
                continue
            try:
                if float(proc.create_time() or 0) < when:
                    return True
            except Exception:                          # noqa: BLE001 - gone, or not ours to ask
                continue
    except Exception:                                  # noqa: BLE001 - a hint, not a verdict
        return False
    return False


def bridge_entry() -> dict:
    """The command form: the venv's `python.exe -m app.cli mcp`, from anywhere.

    The console Python (`osbridge.stdio.console_python`) - the bridge talks
    on stdin/stdout, which the window's windowless Python does not have. `PYTHONPATH` is the project, so `-m`
    finds `app` whatever folder the AI program starts it in.
    """
    from app.core.version import PROJECT_ROOT

    from app.core.osbridge.stdio import console_python

    return {"command": console_python(sys.executable), "args": ["-m", "app.cli", "mcp"],
            "env": {"PYTHONPATH": str(PROJECT_ROOT)}}


def entry_for(program: Program, url: str, key: str) -> dict:
    """The entry `program` wants: the bridge command, or the address with the key."""
    return bridge_entry() if program.style == "bridge" else http_entry(program.style, url, key)


def is_connected(program: Program) -> bool:
    """Leasha's entry is in the file. Never raises; reads only."""
    try:
        data = json.loads(program.path().read_text(encoding="utf-8"))
        return ENTRY_NAME in (data.get(program.servers_key) or {})
    except (OSError, ValueError, AttributeError):
        return False


#: Backups of one settings file kept: the oldest (the file as it was before
#: Leasha first touched it) and this many of the newest.
KEEP_BACKUPS = 3


def _refuse_without_command(program: Program) -> None:
    r"""A program that rewrites its own file while it runs (`command` set) is
    never edited beside it. 2026-10-04, code review: without the `claude`
    command on PATH, Connect fell back to rewriting `~/.claude.json` - which
    Claude Code may be rewriting at that moment, so either write could be lost.
    """
    if program.command and not _own_command(program):
        raise AppErrorException(make_error(
            "ERR_MCP_CONFIG", "serve.clients", path=str(program.path()),
            details=f"{program.name} changes this file itself while it runs, and its "
                    f"'{program.command}' command was not found on this computer's PATH, "
                    "so Leasha did not edit it.",
            suggestion=(f"Use \"Copy address and key\" below and add Leasha in "
                        f"{program.name} yourself, or make its '{program.command}' "
                        "command available and press Connect again. Nothing in the file "
                        "was changed.")))


def connect(program: Program, url: str, key: str) -> Path | None:
    """Put Leasha's entry in `program`'s settings file. Returns the backup's
    path (None when there was no file before). **Worker thread.**"""
    _refuse_without_command(program)
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
    _refuse_without_command(program)
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
            # A timeout's message quotes the command, header and all.
            details=_redact(f"{type(exc).__name__}: {exc}"))) from None
    if check and done.returncode != 0:
        # The key never reaches an error message or the log (2026-10-04, code
        # review): the program's own output may echo the header it was given.
        raise AppErrorException(make_error(
            "ERR_MCP_CONFIG", "serve.clients", path=str(path),
            details=_redact((done.stderr or done.stdout).strip())[:400]))
    return saved


def _redact(text: str) -> str:
    """`Bearer <key>` as `Bearer ...`, wherever it appears."""
    return re.sub(r"(?i)(Bearer\s+)\S+", r"\1...", str(text or ""))


def _backup(path: Path) -> Path | None:
    r"""A copy beside `path`, named to the microsecond, then old copies pruned.

    2026-10-04, code review: named to the second, two Connects in one second
    wrote the second backup over the first; and none was ever removed, so
    every Connect and Disconnect left one more. `KEEP_BACKUPS` of the newest
    are kept, and always the oldest - the file before Leasha first changed it.
    """
    if not path.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.leasha-backup-{stamp}")
    numbered = list(path.parent.glob(f"{glob.escape(backup.name)}-*"))
    if backup.exists() or numbered:
        # 2026-10-05: one past the highest number this stamp has had, and
        # padded. Windows' clock gave 5 distinct readings in 2,000 calls, so
        # backups close together share a stamp. "The first free number"
        # reused a name pruning had just freed, and "-10" sorted before "-2",
        # so the pruning kept the wrong ones.
        # The un-numbered one counts as 0, and may itself have been pruned.
        taken = [found.name.rsplit("-", 1)[-1] for found in numbered]
        number = max((int(n) for n in taken if n.isdigit()), default=0) + 1
        backup = path.with_name(f"{backup.name}-{number:04d}")
    shutil.copy2(path, backup)
    _prune_backups(path)
    return backup


def _prune_backups(path: Path) -> None:
    """Keep the oldest backup and the `KEEP_BACKUPS` newest. Never raises."""
    # Ordered by name - the time it was made, oldest first (an older
    # to-the-second name sorts before the same second's newer ones). Not by
    # the file's modified time: `copy2` keeps the settings file's own.
    try:
        backups = sorted(path.parent.glob(f"{glob.escape(path.name)}.leasha-backup-*"),
                         key=lambda found: found.name)
    except OSError:
        return
    for stale in backups[1:-KEEP_BACKUPS] if len(backups) > KEEP_BACKUPS + 1 else ():
        try:
            stale.unlink()
        except OSError:
            continue


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
