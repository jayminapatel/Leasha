r"""`mcp`: the bridge an AI program starts, to reach Leasha's running server.

2026-10-04. Leasha runs the server for AI programs itself, from Settings >
Models & AI > AI programs, on `127.0.0.1` (`app/serve/mcp.py`). A program that
connects by address needs nothing else. A program that can only start a
command - Claude Desktop - starts this instead:

    venv\Scripts\python.exe -m app.cli mcp

It speaks MCP on its own stdin and stdout and passes each call to the running
server with Leasha's key. It holds no index and loads no model. With the
server stopped, every tool answers that it is stopped and where to start it.

**stdout is the protocol.** Nothing in this command prints there; logging
goes to the log file and stderr.
"""

from __future__ import annotations

import argparse

from app.cli._common import EXIT_OK, _load
from app.core.logging import logger, setup_logging

__all__ = ["add_mcp_parser", "cmd_mcp"]


def cmd_mcp(args: argparse.Namespace) -> int:
    """Run the bridge until the AI program closes it."""
    from app.serve.mcp import KEY_STATE, bridge_server, endpoint
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)

    def read_key() -> str:
        # Read again after a "wrong key" answer (2026-10-04, code review), so
        # a key Leasha made after this bridge started is used without a restart.
        if not settings.fts_db.is_file():
            return ""
        with SqliteStore(settings.fts_db) as store:
            return str(store.get_state(KEY_STATE, "") or "")

    logger.bind(component="cli.mcp").info("MCP bridge to {}", endpoint(settings.mcp_port))
    bridge_server(endpoint(settings.mcp_port), read_key(), read_key).run("stdio")
    return EXIT_OK


def add_mcp_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_mcp = sub.add_parser(
        "mcp", parents=[common],
        help="bridge an AI program to Leasha's running AI access (MCP over stdin/stdout)")
    p_mcp.set_defaults(func=cmd_mcp)
