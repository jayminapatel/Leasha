r"""Runs the server for AI programs from the window, and connects programs to it.

Layer: L5 controller. 2026-10-04, the owner: Start and Stop in Settings, run by
Leasha while it is open (`app/serve/mcp.py`), for Claude and other programs
(`app/serve/clients.py`).

**Every action is a worker.** Start binds a port and waits for it; Connect
rewrites another program's settings file; the status reads those files and
the key from the store. None of it may run on the interface thread, so each
is handed to a `CallableWorker` here and the box is told the outcome.

**One copy of the models.** The server is given the window's own embedder,
reranker and picture-text model, read from the engine at each search, so it
follows an engine the window replaces (a moved index) and never loads a
second 1.4 GB copy.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThreadPool

from app.core.logging import logger
from app.ui.workers import CallableWorker, run

__all__ = ["McpController"]

_log = logger.bind(component="ui.mcp")


class McpController(QObject):
    def __init__(self, window: Any) -> None:
        super().__init__(window)
        from app.serve.mcp import McpHost

        self._w = window
        self.host = McpHost()
        #: The `IndexTools` the running server answers with - kept so Stop and
        #: closing the window can close the index it holds open (2026-10-04).
        self.tools: Any = None
        box = window.settings_view.mcp_box
        self._box = box
        box.start_requested.connect(self.start)
        box.stop_requested.connect(self.stop)
        box.connect_requested.connect(self.connect_program)
        box.refresh_requested.connect(self.refresh)

    # -- the engine's models, for the server's searches -------------------------

    def _models(self) -> tuple:
        engine = self._w._engine
        return (engine.embedder, engine.reranker, getattr(engine, "clip_text_embedder", None))

    # -- start, stop -----------------------------------------------------------

    def start(self, port: int) -> None:
        from app.serve.mcp import IndexTools, key_for

        settings, store, host, models = self._w._settings, self._w._store, self.host, self._models
        window = self._w

        def switches() -> Any:
            # The window's current Settings search switches, read per call, so
            # AI programs search as the Search tab does now (2026-10-04).
            return getattr(window, "search_preferences", None)

        controller = self

        def start_server() -> Any:
            if host.running:
                return host.start(controller.tools, int(port), "")
            tools = IndexTools(settings, models, switches)
            url = host.start(tools, int(port), key_for(store))
            old, controller.tools = controller.tools, tools
            if old is not None:
                old.close(wait_s=0)
            return url

        self._box.show_busy("Starting…")
        self._run(start_server, lambda url: (self._box.show_running(str(url)), self.refresh()),
                  "ui.mcp.start")

    def stop(self) -> None:
        host, controller = self.host, self

        def stop_server() -> str:
            host.stop()
            # 2026-10-04, code review: and the index the server held open.
            tools, controller.tools = controller.tools, None
            if tools is not None:
                tools.close()
            return ""

        self._box.show_busy("Stopping…")
        self._run(stop_server, lambda _r: self._box.show_running(""), "ui.mcp.stop")

    def start_if_wanted(self) -> None:
        """At window start: only when the person turned "Start AI access when
        Leasha opens" on."""
        if bool(getattr(self._w._settings, "mcp_autostart", False)):
            self.start(int(getattr(self._w._settings, "mcp_port", 8737) or 8737))
        else:
            self.refresh()

    def shutdown(self) -> None:
        """On close. Stops the server thread; short, bounded wait.

        2026-10-04, code review: **new calls are refused first**, then the
        server stops, then a call already running is given a moment to finish
        before the index it reads is closed - all before the window closes its
        own engine and store, whose models a running search is using.
        """
        tools, self.tools = self.tools, None
        if tools is not None:
            tools.refuse()
        try:
            self.host.stop(timeout_s=2.0)
        except Exception as exc:                 # noqa: BLE001 - closing regardless
            _log.debug("stopping the MCP server on close: {}", exc)
        if tools is not None:
            try:
                tools.close(wait_s=2.0)
            except Exception as exc:             # noqa: BLE001 - closing regardless
                _log.debug("closing the MCP server's index on close: {}", exc)

    # -- programs ----------------------------------------------------------------

    def refresh(self) -> None:
        """Which programs are connected, and the key - read on a worker."""
        from app.serve.clients import PROGRAMS, is_connected
        from app.serve.mcp import key_for

        store = self._w._store

        def read_states() -> tuple:
            states = {}
            for program in PROGRAMS:
                states[program.key] = ("not installed" if not program.installed()
                                       else "connected" if is_connected(program)
                                       else "not connected")
            # 2026-10-04, code review: read, not made - a refresh beside Start
            # could make a second key. Only when there is none yet is one made
            # (so "Copy address and key" has one), and `key_for` inserts it
            # only if absent, so a Start at the same moment gets the same key.
            return states, key_for(store, create=False) or key_for(store)

        self._run(read_states, lambda found: self._box.show_programs(*found), "ui.mcp.status")

    def connect_program(self, key: str, join: bool) -> None:
        from app.serve.clients import PROGRAMS, connect, disconnect
        from app.serve.mcp import endpoint, key_for

        program = next((p for p in PROGRAMS if p.key == key), None)
        if program is None:
            return
        store, port = self._w._store, self._box.port.value()

        def change_settings() -> Any:
            if join:
                return connect(program, endpoint(port), key_for(store))
            return disconnect(program)

        def done(_backup: Any) -> None:
            verb = "connected to" if join else "disconnected from"
            self._w.notify(f"Leasha is {verb} {program.name}. {program.note}", 8_000)
            self.refresh()

        self._run(change_settings, done, "ui.mcp.connect")

    # -- the one way work is run ----------------------------------------------------

    def _run(self, body: Any, on_done: Any, component: str) -> None:
        worker = CallableWorker(body, component=component)
        worker.signals.finished.connect(on_done)
        worker.signals.failed.connect(self._failed)
        run(QThreadPool.globalInstance(), worker)

    def _failed(self, error: Any) -> None:
        self._box.show_running(self._box._url if self.host.running else "")
        self._w._show_error(error)
