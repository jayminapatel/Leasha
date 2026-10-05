"""A path's own name is found however the path is written, on any system.

2026-10-05, the first whole-suite run on macOS: nine tests failed because
`Path(r"D:\\Docs\\a.bin").name` is the whole string off Windows. An index copied
from a Windows machine is full of such paths.
"""

from __future__ import annotations

import pytest

from app.core.osbridge import pathnames


@pytest.mark.parametrize("on_windows", [True, False])
@pytest.mark.parametrize("path, name", [
    (r"D:\Docs\alpha.bin", "alpha.bin"),
    (r"D:\Docs", "Docs"),
    ("D:" + chr(92) + "Docs" + chr(92), "Docs"),
    ("D:/Docs/zeta.bin", "zeta.bin"),
    (r"\\server\share\plans\site.dwg", "site.dwg"),
    (r"D:\SearchData\2007.pst", "2007.pst"),
])
def test_a_windows_shaped_path_has_the_same_name_everywhere(monkeypatch, on_windows, path, name):
    import os

    if on_windows and os.name != "nt":
        pytest.skip("the Windows branch is `Path(text).name`, which only Windows splits")
    monkeypatch.setattr(pathnames, "is_windows", lambda: on_windows)
    assert pathnames.name_of(path) == name


def test_a_drive_on_its_own_has_no_name(monkeypatch):
    monkeypatch.setattr(pathnames, "is_windows", lambda: False)
    assert pathnames.name_of("D:") == ""
    assert pathnames.name_of("D:" + chr(92)) == ""
    assert pathnames.name_of("") == ""
    assert pathnames.name_of(None) == ""


def test_a_path_written_this_systems_way_is_left_to_pathlib(monkeypatch, tmp_path):
    monkeypatch.setattr(pathnames, "is_windows", lambda: False)
    assert pathnames.name_of("/Users/me/Photos/beach.jpg") == "beach.jpg"
    assert pathnames.name_of(tmp_path / "a.pdf") == "a.pdf"


def test_a_folder_written_with_a_backslash_is_two_folders(tmp_path):
    """`VideoLAN\\VLC` in the players' table: found on every system."""
    from app.core.osbridge.programs import find_in_install_folders

    target = tmp_path / "VideoLAN" / "VLC" / "vlc.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    assert find_in_install_folders([tmp_path], ["VideoLAN" + chr(92) + "VLC"], ["vlc.exe"]) == str(target)
    assert find_in_install_folders([tmp_path], ["VideoLAN/VLC"], ["vlc.exe"]) == str(target)
