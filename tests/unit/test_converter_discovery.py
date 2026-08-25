r"""Finding LibreOffice on a machine that has it.

**LibreOffice does not add itself to `PATH` on Windows.** Detection was
`shutil.which` alone, so an installed, working LibreOffice reported "needs
attention", Settings offered a download link for software already present, and
the `.doc` and `.ppt` routes stayed off. Being told to fix something that is not
broken is worse than being told nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.extract import converter


def test_path_is_still_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    """Somebody who has deliberately put a build on PATH means that one."""
    monkeypatch.setattr(converter.shutil, "which", lambda name: r"C:\chosen\soffice.exe")
    assert converter.resolve_binary("soffice") == r"C:\chosen\soffice.exe"


def test_a_windows_install_is_found_when_path_has_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point: installed but not on PATH must read as installed."""
    root = tmp_path / "Program Files"
    exe = root / "LibreOffice" / "program" / "soffice.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(converter, "_is_windows", lambda: True)
    monkeypatch.setattr(converter.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(root))

    assert converter.resolve_binary("soffice") == str(exe)


def test_nothing_is_invented_when_it_is_genuinely_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path is returned only when the executable is really there.

    Reporting a converter that is not installed is the same bug pointing the
    other way: every file of that type then fails at conversion time instead.
    """
    monkeypatch.setattr(converter, "_is_windows", lambda: True)
    monkeypatch.setattr(converter.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "nothing here"))

    assert converter.resolve_binary("soffice") is None


def test_the_allow_list_still_governs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked name is refused before anything is looked up."""
    monkeypatch.setattr(converter.shutil, "which", lambda name: r"C:\windows\curl.exe")
    assert converter.resolve_binary("curl") is None


def test_only_allowed_names_have_locations() -> None:
    """A location for a name that cannot run is dead weight, and a location for
    a name somebody later adds to the map is a way to run it."""
    unknown = set(converter._WINDOWS_LOCATIONS) - set(converter.ALLOWED_BINARIES)
    assert not unknown, f"{unknown} have install locations but are not allowed"


def test_settings_and_the_converter_agree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """**They used to disagree.**

    `available_binaries` called `shutil.which` itself, so a conversion could
    find LibreOffice and run while Settings and `doctor` reported it missing.
    """
    root = tmp_path / "Program Files"
    exe = root / "LibreOffice" / "program" / "soffice.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(converter, "_is_windows", lambda: True)
    monkeypatch.setattr(converter.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(root))

    assert converter.available_binaries()["soffice"] == converter.resolve_binary("soffice")
    assert converter.available_binaries()["soffice"] is not None


def test_a_per_user_install_is_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """What somebody without administrator rights ends up with."""
    programs = tmp_path / "AppData" / "Local" / "Programs"
    exe = programs / "LibreOffice" / "program" / "soffice.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(converter, "_is_windows", lambda: True)
    monkeypatch.setattr(converter.shutil, "which", lambda name: None)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

    assert converter.resolve_binary("soffice") == str(exe)


def test_nothing_is_searched_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """`C:\\Program Files` means nothing on Linux, and a backslash is a legal
    filename character there - the same trap `config._refuse_foreign_path`
    exists for."""
    monkeypatch.setattr(converter, "_is_windows", lambda: False)
    monkeypatch.setattr(converter.shutil, "which", lambda name: None)
    assert converter.resolve_binary("soffice") is None
