r"""AI programs: Start and Stop Leasha's server for them, and Connect each one.

Layer: L5 view - thin. 2026-10-04, the owner: "in the settings there should be
a method to manage start stop etc the mcp server", and "this will not be only
for claude but other platforms too". What runs it is `app/serve/mcp.py`; what
connects a program is `app/serve/clients.py`; this box only shows their state
and asks `controllers/mcp_controller.py` to act. **No I/O here** - the
controller does every file read and port bind on a worker and calls back.

New controls, so new wording (the standing rule binds existing labels only).
The privacy sentence is in the box itself, beside Start, because it is the
one thing someone deciding to press it needs to know.
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

__all__ = ["CLI_NOTE", "PRIVACY_NOTE", "McpBox"]

PRIVACY_NOTE = (
    "Lets AI programs on this computer - Claude, Cursor, VS Code and others - search "
    "your index. Leasha answers only while AI access is started and Leasha is open, "
    "only to programs on this computer that have its key, and can never change a file. "
    "What it finds goes to the AI program that asked; one that runs in the cloud "
    "sends it there."
)
CLI_NOTE = (
    "Coding tools that can run commands can also search without this: "
    "venv\\Scripts\\python.exe -m app.cli search \"what you want\" --json"
)


class McpBox(QGroupBox):
    """The AI programs group. Signals out; `show_*` in."""

    #: `{registry key: value}` - the port and the autostart switch.
    changed = Signal(dict)
    start_requested = Signal(int)
    stop_requested = Signal()
    #: `(program key, connect?)`.
    connect_requested = Signal(str, bool)
    refresh_requested = Signal()

    def __init__(self, settings: Any, parent: QWidget | None = None) -> None:
        super().__init__("AI programs", parent)
        from app.serve.clients import PROGRAMS

        self._key = ""
        self._url = ""
        layout = QVBoxLayout(self)
        intro = QLabel(PRIVACY_NOTE)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.status = QLabel("Stopped.")
        self.status.setObjectName("mcpStatus")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.start = QPushButton("Start")
        self.start.setToolTip("Start answering AI programs on this computer.")
        self.stop = QPushButton("Stop")
        self.stop.setToolTip("Stop answering. Connected programs are told it is stopped.")
        self.stop.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.status, stretch=1)
        row.addWidget(self.start)
        row.addWidget(self.stop)
        layout.addLayout(row)

        form = QFormLayout()
        self.port = QSpinBox()
        self.port.setObjectName("MCP_PORT")
        self.port.setRange(1024, 65535)
        self.port.setValue(int(getattr(settings, "mcp_port", 8737) or 8737))
        from app.ui.widgets.number_field import fit

        fit(self.port)
        self.port.setToolTip(
            "The port on this computer that AI programs connect to. Only this computer "
            "can reach it. After changing it, press Stop, Start, and Connect again.")
        form.addRow("Port for AI programs", self.port)
        self.autostart = QCheckBox("Start AI access when Leasha opens")
        self.autostart.setObjectName("MCP_AUTOSTART")
        self.autostart.setChecked(bool(getattr(settings, "mcp_autostart", False)))
        self.autostart.setToolTip(
            "Starts it every time Leasha opens, so a connected AI program can search "
            "without you pressing Start. Off by default.")
        form.addRow(self.autostart)
        layout.addLayout(form)

        grid = QGridLayout()
        self._rows: dict[str, tuple[QLabel, QPushButton, QPushButton]] = {}
        for line, program in enumerate(PROGRAMS):
            name = QLabel(program.name)
            name.setToolTip(program.note)
            state = QLabel("Checking…")
            connect = QPushButton("Connect")
            connect.setToolTip(
                f"Add Leasha to {program.name}'s settings. The file is backed up first, "
                f"and only Leasha's own entry is added. {program.note}")
            disconnect = QPushButton("Disconnect")
            disconnect.setToolTip(f"Take Leasha out of {program.name}'s settings, "
                                  "and nothing else.")
            connect.clicked.connect(lambda _c=False, k=program.key: self.connect_requested.emit(k, True))
            disconnect.clicked.connect(lambda _c=False, k=program.key: self.connect_requested.emit(k, False))
            grid.addWidget(name, line, 0)
            grid.addWidget(state, line, 1)
            grid.addWidget(connect, line, 2)
            grid.addWidget(disconnect, line, 3)
            self._rows[program.key] = (state, connect, disconnect)
        layout.addLayout(grid)

        other = QHBoxLayout()
        other.addWidget(QLabel("Other programs:"))
        self.copy_address = QPushButton("Copy address and key")
        self.copy_address.setToolTip(
            "Copies the settings entry for a program that connects by address - most of "
            "them. Paste it into that program's MCP settings. The key is in it: keep it "
            "to this computer.")
        self.copy_bridge = QPushButton("Copy bridge command")
        self.copy_bridge.setToolTip(
            "Copies the settings entry for a program that can only start a command, as "
            "Claude Desktop does. Leasha still has to be open with AI access started.")
        other.addWidget(self.copy_address)
        other.addWidget(self.copy_bridge)
        other.addStretch(1)
        layout.addLayout(other)

        cli = QLabel(CLI_NOTE)
        cli.setWordWrap(True)
        cli.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(cli)

        self.start.clicked.connect(lambda _c=False: self.start_requested.emit(self.port.value()))
        self.stop.clicked.connect(lambda _c=False: self.stop_requested.emit())
        self.port.editingFinished.connect(
            lambda: self.changed.emit({"MCP_PORT": self.port.value()}))
        self.autostart.toggled.connect(lambda on: self.changed.emit({"MCP_AUTOSTART": on}))
        self.copy_address.clicked.connect(self._copy_address)
        self.copy_bridge.clicked.connect(self._copy_bridge)

    # -- what the controller tells it --------------------------------------------

    def show_running(self, url: str) -> None:
        self._url = url
        self.status.setText(f"Running at {url}" if url else "Stopped.")
        self.start.setEnabled(not url)
        self.stop.setEnabled(bool(url))
        self.port.setEnabled(not url)

    def show_busy(self, text: str) -> None:
        self.status.setText(text)
        self.start.setEnabled(False)
        self.stop.setEnabled(False)

    def show_programs(self, states: dict[str, str], key: str) -> None:
        """`{program key: "connected" | "not connected" | "not installed"}`."""
        self._key = key
        words = {"connected": "Connected", "not connected": "Not connected",
                 "not installed": "Not found on this computer"}
        for program, (label, connect, disconnect) in self._rows.items():
            state = states.get(program, "not connected")
            label.setText(words.get(state, state))
            connect.setEnabled(state != "not installed")
            disconnect.setEnabled(state == "connected")

    # -- copying -------------------------------------------------------------------

    def _address(self) -> str:
        from app.serve.mcp import endpoint

        return self._url or endpoint(self.port.value())

    def address_entry(self) -> str:
        from app.serve.clients import ENTRY_NAME, http_entry

        return json.dumps({ENTRY_NAME: http_entry("http", self._address(), self._key)}, indent=2)

    def bridge_text(self) -> str:
        from app.serve.clients import ENTRY_NAME, bridge_entry

        return json.dumps({ENTRY_NAME: bridge_entry()}, indent=2)

    def _copy_address(self) -> None:
        QApplication.clipboard().setText(self.address_entry())

    def _copy_bridge(self) -> None:
        QApplication.clipboard().setText(self.bridge_text())
