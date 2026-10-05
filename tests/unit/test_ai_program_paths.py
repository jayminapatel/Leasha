"""Where an AI program keeps its settings is found on any system, and asking never raises.

2026-10-05, the first whole-suite runs off Windows. `Program.path()` expanded
`~\.claude.json`; off Windows that backslash is part of a user name, and
`expanduser` raised "Could not determine home directory". The window showed it
as an error box when it opened, an error box waits for a click, and every test
that builds the window hung - forty minutes on Linux, ninety on macOS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.serve import clients


def test_off_windows_the_separators_are_turned_round():
    assert clients.settings_file_text("~\.claude.json", windows=False) == "~/.claude.json"
    assert clients.settings_file_text("~\.cursor\mcp.json", windows=False) == "~/.cursor/mcp.json"
    # On Windows they are left exactly as written.
    assert clients.settings_file_text("~\.claude.json", windows=True) == "~\.claude.json"


def test_what_is_turned_round_expands_to_the_home_folder():
    """The form `path()` hands to `expanduser` off Windows is one it can read."""
    expanded = Path(clients.settings_file_text("~\.claude.json", windows=False)).expanduser()
    assert expanded == Path.home() / ".claude.json"


@pytest.mark.parametrize("program", clients.PROGRAMS, ids=lambda p: p.key)
def test_asking_where_a_program_is_never_raises(program, monkeypatch):
    assert isinstance(program.path(), Path)
    assert program.installed() in (True, False)
    # As on a system with none of the Windows folders: still an answer, and "no".
    for name in ("APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(clients.os, "name", "posix")
    assert isinstance(program.path(), Path)
    if "%" in program.file:
        assert program.installed() is False, "a Windows-only folder is not 'installed' elsewhere"


def test_a_home_folder_that_cannot_be_found_is_not_an_error(monkeypatch):
    def no_home(self):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", no_home)
    for program in clients.PROGRAMS:
        assert isinstance(program.path(), Path)
        assert program.installed() in (True, False)
