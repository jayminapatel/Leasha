"""The Start-menu shortcut: what it holds, that it can be undone, and that the
installer writes it. `app/core/osbridge/startmenu.py`.

Layer: L9
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.osbridge import startmenu
from app.ui.tray import APP_USER_MODEL_ID

ROOT = Path(__file__).resolve().parents[2]


def test_the_shortcut_runs_the_window_without_a_console(tmp_path):
    """pythonw.exe, not python.exe or leasha.cmd: either of those flashes a
    console on every launch."""
    spec = startmenu.shortcut_spec(tmp_path)

    assert spec["target"] == str(tmp_path / "venv" / "Scripts" / "pythonw.exe")
    assert spec["arguments"] == "-m app.main"
    assert spec["working_directory"] == str(tmp_path), \
        "the window finds .env beside app/, but a relative path must still resolve"


def test_the_shortcut_carries_the_windows_own_taskbar_id(tmp_path):
    """Without the same ID the running window is a second, unrelated button
    beside the Start entry."""
    assert startmenu.shortcut_spec(tmp_path)["app_user_model_id"] == APP_USER_MODEL_ID


def test_the_icon_it_names_is_in_the_repository():
    assert Path(startmenu.shortcut_spec(ROOT)["icon"]).is_file()


def test_the_shortcut_is_per_user(monkeypatch, tmp_path):
    """%APPDATA%, never ProgramData: no administrator rights, nothing changed
    for anybody else on the computer."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert startmenu.shortcut_path() == (
        tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Leasha.lnk")


def test_create_refuses_an_installation_without_its_interpreter(tmp_path):
    """Never a shortcut to a pythonw.exe that is not there."""
    assert startmenu.create(tmp_path, destination=tmp_path / "Leasha.lnk") is False
    assert not (tmp_path / "Leasha.lnk").exists()


def test_remove_says_when_there_was_nothing_to_remove(tmp_path):
    assert startmenu.remove(tmp_path / "Leasha.lnk") is False


@pytest.mark.skipif(sys.platform != "win32", reason="shell links are Windows-only")
def test_create_then_remove_round_trip(tmp_path):
    """Written, readable back with the same contents, and gone after remove."""
    pythoncom = pytest.importorskip("pythoncom")
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    if not (ROOT / "venv" / "Scripts" / "pythonw.exe").is_file():
        pytest.skip("no venv in this checkout")
    lnk = tmp_path / "Leasha.lnk"

    assert startmenu.create(ROOT, destination=lnk) is True
    assert startmenu.exists(lnk)

    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                      pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(lnk))
    spec = startmenu.shortcut_spec(ROOT)
    assert Path(link.GetPath(0)[0]) == Path(spec["target"])
    assert link.GetArguments() == spec["arguments"]
    assert Path(link.GetWorkingDirectory()) == ROOT
    assert Path(link.GetIconLocation()[0]) == Path(spec["icon"])
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    assert store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue() == APP_USER_MODEL_ID
    del store, link

    assert startmenu.remove(lnk) is True
    assert not lnk.exists()


def test_the_installer_adds_the_shortcut_through_the_cli():
    """One implementation: the installer calls the same command a person can
    run, rather than a second copy of the shortcut in PowerShell."""
    text = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert "-m app.cli shortcut create" in text
    assert "Start Menu\\Programs\\Leasha.lnk" in text


def test_the_installer_is_still_ascii_only():
    """PowerShell 5.1 and a BOM-less or non-ASCII script: see test_launcher."""
    raw = (ROOT / "install.ps1").read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"
    raw[3:].decode("ascii")


def test_the_cli_command_can_undo_itself():
    from app.cli import build_parser

    parser = build_parser()
    for action in ("create", "remove", "show"):
        assert parser.parse_args(["shortcut", action]).action == action
    assert parser.parse_args(["shortcut"]).action == "create"
