r"""Every branch of `app/core/osbridge/`, on the machine the tests run on (0x §1d).

**Why fake the operating system.** The tests run on Linux (and on the Windows
CI). The Mac branches would otherwise never run until somebody owned a Mac and
tried it - by which time a typo in `"open", "-R"` would be a bug report. So
each test sets `sys.platform` to the system it is about, for that test only
(pytest's `monkeypatch` puts it back afterwards), and replaces whatever would
really touch the system - starting a program, reading a file's flags, calling
`kernel32` - with a fake that records what it was asked to do.

What this proves: **the code asks the operating system for the right thing.**
What it cannot prove: that the operating system then does what Apple's (or
Microsoft's) documentation says. Those are the "(UNCONFIRMED on macOS)" notes
in the package, to be checked on a real Mac.

**The Windows branches are faked too**, because they cannot run here either.
They were moved from their old modules unchanged; these tests pin the exact
calls and arguments so that a later edit cannot quietly change them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import osbridge
from app.core.osbridge import cloudfs, launch, paths, programs
from app.core.osbridge import priority as bridge_priority

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def on(monkeypatch):
    """`on("darwin")` makes this test believe it runs on that system."""
    def _on(platform: str) -> None:
        monkeypatch.setattr(sys, "platform", platform)
    return _on


class Recorder:
    """Stands in for `subprocess.Popen` or `os.startfile`: remembers each call."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture()
def spawned(monkeypatch):
    """Replace the programs `launch` could start, and return what was asked for.

    The module's own `subprocess` and `os` names are swapped for small fakes,
    so nothing else in the test process is affected.
    """
    popen, startfile = Recorder(), Recorder()
    monkeypatch.setattr(launch, "subprocess", SimpleNamespace(Popen=popen))
    monkeypatch.setattr(launch, "os", SimpleNamespace(startfile=startfile))
    return SimpleNamespace(popen=popen, startfile=startfile)


# ---------------------------------------------------------------------------
# Which system is this?
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform,windows,mac", [
    ("win32", True, False), ("darwin", False, True), ("linux", False, False)])
def test_the_platform_is_read_when_asked_not_at_import(on, platform, windows, mac):
    on(platform)
    assert osbridge.is_windows() is windows
    assert osbridge.is_macos() is mac


# ---------------------------------------------------------------------------
# Opening and revealing files
# ---------------------------------------------------------------------------

def test_mac_opens_with_open(on, spawned):
    on("darwin")
    launch.open_with_default_app("/Users/a/Report 2024.pdf")
    assert spawned.popen.calls == [((["open", "/Users/a/Report 2024.pdf"],), {})]
    assert spawned.startfile.calls == []


def test_mac_reveals_with_open_dash_r(on, spawned):
    on("darwin")
    launch.show_in_file_manager("/Users/a/Report.pdf")
    assert spawned.popen.calls == [((["open", "-R", "/Users/a/Report.pdf"],), {})]


def test_mac_without_select_opens_the_item_itself(on, spawned):
    on("darwin")
    launch.show_in_file_manager("/Users/a/Folder", select=False)
    assert spawned.popen.calls == [((["open", "/Users/a/Folder"],), {})]


def test_a_hostile_file_name_stays_one_argument(on, spawned):
    """`open` is given a list, never a shell line, so `;` is just a character."""
    on("darwin")
    launch.open_with_default_app("/tmp/a; rm -rf b.pdf")
    (argv,), _ = spawned.popen.calls[0]
    assert argv == ["open", "/tmp/a; rm -rf b.pdf"]


def test_windows_opens_with_startfile_exactly_as_before(on, spawned):
    on("win32")
    launch.open_with_default_app(r"C:\Users\a\Report.pdf")
    assert spawned.startfile.calls == [((r"C:\Users\a\Report.pdf",), {})]
    assert spawned.popen.calls == []


def test_windows_reveals_with_explorer_select_exactly_as_before(on, spawned):
    on("win32")
    target = "/data/Report.pdf"          # any path; only the argv shape matters
    launch.show_in_file_manager(target)
    assert spawned.popen.calls == [((["explorer", "/select,", str(Path(target))],), {})]


def test_windows_without_select_uses_startfile(on, spawned):
    on("win32")
    launch.show_in_file_manager("/data/Folder", select=False)
    assert spawned.startfile.calls == [((str(Path("/data/Folder")),), {})]


def test_linux_keeps_xdg_open(on, spawned):
    on("linux")
    launch.open_with_default_app("/home/a/r.pdf")
    launch.show_in_file_manager("/home/a/r.pdf")
    assert [args[0] for args, _ in spawned.popen.calls] == [
        ["xdg-open", "/home/a/r.pdf"], ["xdg-open", "/home/a"]]


def test_media_open_hands_the_plain_open_to_the_bridge(on, spawned):
    """`media_open._system_open` - the fallback when no player can seek."""
    from app.core import media_open

    on("darwin")
    media_open._system_open("/Users/a/talk.mp4")
    assert spawned.popen.calls == [((["open", "/Users/a/talk.mp4"],), {})]


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------

class FakeKernel32:
    """Enough of `kernel32` for the three thread calls, recording each one."""

    def __init__(self, current: int = 0, set_ok: bool = True) -> None:
        self.current = current
        self.set_ok = set_ok
        self.sets: list = []

    def GetCurrentThread(self):
        return "HANDLE"

    def GetThreadPriority(self, handle):
        assert handle == "HANDLE"
        return self.current

    def SetThreadPriority(self, handle, value):
        self.sets.append((handle, value))
        if self.set_ok:
            self.current = value
        return 1 if self.set_ok else 0


@pytest.fixture()
def kernel32(monkeypatch, on):
    """Pretend to be Windows with a fake `kernel32` already loaded.

    `_api()` keeps the library it loaded in `_kernel32`, so putting a fake
    there means `ctypes.WinDLL` (which does not exist here) is never reached.
    """
    on("win32")
    fake = FakeKernel32()
    monkeypatch.setattr(bridge_priority, "_kernel32", fake)
    return fake


def test_mac_thread_priority_is_left_alone_and_says_so(on):
    """(UNCONFIRMED on macOS) - no Mac thread priority is built; None = unchanged."""
    on("darwin")
    assert bridge_priority.lower_this_thread() is None
    assert bridge_priority.current_thread_priority() is None
    bridge_priority.restore_this_thread(None)
    bridge_priority.restore_this_thread(0)       # must not raise either


def test_windows_lowers_to_lowest_and_returns_what_it_was(kernel32):
    assert bridge_priority.lower_this_thread() == 0
    assert kernel32.sets == [("HANDLE", bridge_priority.THREAD_PRIORITY_LOWEST)]
    assert bridge_priority.current_thread_priority() == -2


def test_windows_restores_the_previous_value(kernel32):
    previous = bridge_priority.lower_this_thread()
    bridge_priority.restore_this_thread(previous)
    assert kernel32.sets[-1] == ("HANDLE", 0)


def test_windows_error_return_means_nothing_changed(kernel32):
    kernel32.current = 0x7FFFFFFF
    assert bridge_priority.lower_this_thread() is None
    assert kernel32.sets == []
    assert bridge_priority.current_thread_priority() is None


def test_windows_refused_set_means_nothing_to_restore(kernel32):
    kernel32.set_ok = False
    assert bridge_priority.lower_this_thread() is None


def test_windows_failure_is_a_courtesy_lost_not_an_exception(kernel32, monkeypatch):
    def explode():
        raise OSError("access denied")
    monkeypatch.setattr(kernel32, "GetCurrentThread", explode)
    assert bridge_priority.lower_this_thread() is None
    bridge_priority.restore_this_thread(0)
    assert bridge_priority.current_thread_priority() is None


def test_the_old_module_names_are_the_bridge(kernel32):
    """`app.core.priority` still offers the same functions and constants."""
    from app.core import priority

    assert priority.lower_this_thread is bridge_priority.lower_this_thread
    assert priority.THREAD_PRIORITY_LOWEST == -2
    assert priority.BELOW_NORMAL_PRIORITY_CLASS == 0x00004000
    with priority.background_thread(True) as lowered:
        assert lowered is True
        assert priority.child_creationflags() == priority.BELOW_NORMAL_PRIORITY_CLASS
    assert priority.child_creationflags() == 0
    assert kernel32.sets == [("HANDLE", -2), ("HANDLE", 0)]


def test_children_are_never_lowered_on_a_mac(on):
    """`creationflags` means nothing off Windows, so it stays 0."""
    from app.core import priority

    on("darwin")
    with priority.background_thread(True) as lowered:
        assert lowered is False
        assert priority.child_creationflags() == 0


class FakeProcess:
    def __init__(self, ionice_fails: bool = False) -> None:
        self.calls: list = []
        self.ionice_fails = ionice_fails

    def nice(self, value):
        self.calls.append(("nice", value))

    def ionice(self, value):
        self.calls.append(("ionice", value))
        if self.ionice_fails:
            raise OSError("not on this build")


def test_process_priority_on_windows_uses_the_class_and_low_io():
    process = FakeProcess()
    psutil = SimpleNamespace(Process=lambda: process, BELOW_NORMAL_PRIORITY_CLASS=0x4000,
                             IOPRIO_LOW=1)
    bridge_priority.lower_process_priority(psutil)
    assert process.calls == [("nice", 0x4000), ("ionice", 1)]


def test_process_priority_ignores_an_io_refusal():
    process = FakeProcess(ionice_fails=True)
    psutil = SimpleNamespace(Process=lambda: process, BELOW_NORMAL_PRIORITY_CLASS=0x4000,
                             IOPRIO_LOW=1)
    bridge_priority.lower_process_priority(psutil)
    assert process.calls[0] == ("nice", 0x4000)


def test_process_priority_on_a_mac_is_nice_ten():
    """psutil on macOS/Linux has no priority classes, only the nice number."""
    process = FakeProcess()
    bridge_priority.lower_process_priority(SimpleNamespace(Process=lambda: process))
    assert process.calls == [("nice", 10)]


def test_the_governor_still_reports_failure_in_its_own_words():
    """`SystemProbe.lower_priority` catches what the bridge raises and says False."""
    from app.index.resources import SystemProbe

    def broken():
        raise PermissionError("no")

    probe = SystemProbe()
    probe._psutil = lambda: SimpleNamespace(Process=broken)
    assert probe.lower_priority() is False


# ---------------------------------------------------------------------------
# Finding installed programs
# ---------------------------------------------------------------------------

@pytest.fixture()
def mac_disk(monkeypatch, tmp_path, on):
    """A pretend Mac: `/Applications` and Homebrew's bin folders under tmp_path."""
    on("darwin")
    apps, brew, local = tmp_path / "Applications", tmp_path / "homebrew", tmp_path / "local"
    for folder in (apps, brew, local):
        folder.mkdir()
    monkeypatch.setattr(programs, "MAC_APPLICATION_FOLDERS", (str(apps),))
    monkeypatch.setattr(programs, "MAC_EXTRA_BIN_FOLDERS", (str(brew), str(local)))

    def make(relative: str, base: Path = apps) -> Path:
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    return SimpleNamespace(apps=apps, brew=brew, local=local, make=make)


def test_mac_finds_libreoffice_inside_its_app_bundle(mac_disk):
    soffice = mac_disk.make("LibreOffice.app/Contents/MacOS/soffice")
    assert programs.find_converter_on_macos("soffice") == str(soffice)
    assert programs.find_converter_on_macos("libreoffice") == str(soffice)


def test_mac_finds_libreoffices_own_python(mac_disk):
    python = mac_disk.make("LibreOffice.app/Contents/Resources/python")
    assert programs.find_converter_on_macos("libreoffice-python") == str(python)


def test_a_homebrew_python_is_never_mistaken_for_libreoffices(mac_disk):
    """It would lack LibreOffice's `uno` bridge, so it is not looked for."""
    mac_disk.make("python", mac_disk.brew)
    assert programs.find_converter_on_macos("libreoffice-python") is None


def test_mac_finds_homebrew_tools_on_apple_silicon_first(mac_disk):
    arm = mac_disk.make("tesseract", mac_disk.brew)
    mac_disk.make("tesseract", mac_disk.local)
    assert programs.find_converter_on_macos("tesseract") == str(arm)


def test_mac_finds_intel_homebrew_tools(mac_disk):
    intel = mac_disk.make("dwg2dxf", mac_disk.local)
    assert programs.find_converter_on_macos("dwg2dxf") == str(intel)


def test_mac_app_bundle_wins_over_homebrew(mac_disk):
    bundle = mac_disk.make("LibreOffice.app/Contents/MacOS/soffice")
    mac_disk.make("soffice", mac_disk.brew)
    assert programs.find_converter_on_macos("soffice") == str(bundle)


def test_mac_finds_nothing_that_is_not_there(mac_disk):
    assert programs.find_converter_on_macos("soffice") is None
    assert programs.find_converter_on_macos("xstexporter") is None
    assert programs.find_converter_on_macos("curl") is None


def test_the_per_user_applications_folder_is_searched(monkeypatch, tmp_path, on):
    """`~/Applications`, the Mac's per-user install place, with `~` expanded."""
    on("darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(programs, "MAC_EXTRA_BIN_FOLDERS", ())
    cursor = tmp_path / "Applications" / "Cursor.app" / "Contents" / "Resources" / "app" / "bin" / "cursor"
    cursor.parent.mkdir(parents=True)
    cursor.write_bytes(b"")
    monkeypatch.setattr(programs, "MAC_APPLICATION_FOLDERS",
                        ("/nonexistent-applications", "~/Applications"))
    assert programs.find_editor_on_macos("cursor") == str(cursor)


def test_the_mac_search_does_nothing_off_a_mac(mac_disk, on):
    mac_disk.make("LibreOffice.app/Contents/MacOS/soffice")
    for platform in ("win32", "linux"):
        on(platform)
        assert programs.find_converter_on_macos("soffice") is None


def test_resolve_binary_finds_the_mac_install(mac_disk, monkeypatch):
    """End to end through `converter.resolve_binary`, PATH empty (Finder's case)."""
    from app.extract import converter

    soffice = mac_disk.make("LibreOffice.app/Contents/MacOS/soffice")
    monkeypatch.setattr(converter.shutil, "which", lambda name: None)
    monkeypatch.setattr(converter, "_is_windows", lambda: False)
    assert converter.resolve_binary("soffice") == str(soffice)
    assert converter.available_binaries()["soffice"] == str(soffice)
    # The allow-list still decides first: a blocked name is never looked for.
    mac_disk.make("curl", mac_disk.brew)
    assert converter.resolve_binary("curl") is None


def test_editors_find_vs_code_on_a_mac(mac_disk, monkeypatch):
    from app.ui import editors

    code = mac_disk.make("Visual Studio Code.app/Contents/Resources/app/bin/code")
    monkeypatch.setattr(editors.shutil, "which", lambda name: None)
    assert editors.installed("code") == str(code)
    assert ("vscode", "Visual Studio Code", str(code)) in editors.detect()


def test_media_players_are_found_on_a_mac(mac_disk):
    from app.core import media_open

    vlc = mac_disk.make("VLC.app/Contents/MacOS/VLC")
    by_name = {player.name: player for player in media_open.KNOWN_PLAYERS}
    assert media_open._executable_in_folders(by_name["VLC"]) == str(vlc)
    assert media_open._executable_in_folders(by_name["MPC-HC"]) is None
    assert programs.find_player_on_macos(by_name["PotPlayer"]) is None


def test_every_mac_converter_is_an_allowed_program():
    """Same rule as the Windows table: a location for a name that cannot run
    is dead weight, and one for a name added later is a way to run it."""
    from app.extract.converter import ALLOWED_BINARIES

    assert set(programs.CONVERTER_MAC_LOCATIONS) <= set(ALLOWED_BINARIES)


def test_every_mac_editor_and_player_is_a_known_one():
    from app.core.media_open import KNOWN_PLAYERS
    from app.ui.editors import EDITORS

    assert set(programs.EDITOR_MAC_LOCATIONS) <= {entry[2] for entry in EDITORS}
    assert set(programs.PLAYER_MAC_LOCATIONS) <= {player.name for player in KNOWN_PLAYERS}


# --- The Windows search, faked with folders under tmp_path -----------------

def test_the_shared_search_walks_roots_then_folders_then_subdirs(tmp_path):
    """The order each old copy of the loop used, so the same file wins."""
    first, second = tmp_path / "one", tmp_path / "two"
    (second / "App").mkdir(parents=True)
    (second / "App" / "tool.exe").write_bytes(b"")
    (first / "App" / "bin").mkdir(parents=True)
    (first / "App" / "bin" / "tool.exe").write_bytes(b"")
    found = programs.find_in_install_folders(
        [None, "", str(first), str(second)], ("App",), ("tool.exe",), ("", "bin"))
    assert found == str(first / "App" / "bin" / "tool.exe")


def test_program_files_roots_keep_their_order(monkeypatch):
    for key in (*programs.WINDOWS_PROGRAM_ROOT_VARIABLES, "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ProgramFiles(x86)", "/pf86")
    monkeypatch.setenv("LOCALAPPDATA", "/local")
    import os
    assert programs.program_files_roots() == [None, "/pf86", None,
                                              os.path.join("/local", "Programs")]


def test_editor_roots_put_the_per_user_install_first(monkeypatch):
    for key in (*programs.WINDOWS_PROGRAM_ROOT_VARIABLES, "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ProgramFiles", "/pf")
    monkeypatch.setenv("LOCALAPPDATA", "/local")
    assert programs.editor_program_roots() == [Path("/local"), Path("/local/Programs"),
                                               Path("/pf")]


def test_windows_editor_search_finds_a_per_user_vs_code(monkeypatch, tmp_path):
    from app.ui import editors

    for key in (*programs.WINDOWS_PROGRAM_ROOT_VARIABLES, "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    exe = tmp_path / "Programs" / "Microsoft VS Code" / "bin" / "Code.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert editors._installed_on_windows("code") == str(exe)


def test_the_old_table_names_are_the_moved_tables():
    """Callers and tests that read the old private names see the same objects."""
    from app.core import media_open
    from app.extract import converter
    from app.ui import editors

    assert converter._WINDOWS_LOCATIONS is programs.CONVERTER_WINDOWS_LOCATIONS
    assert converter._WINDOWS_SUBDIRS == ("", "bin", "program")
    assert converter._WINDOWS_ROOTS == ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
    assert editors._WINDOWS_LOCATIONS is programs.EDITOR_WINDOWS_LOCATIONS
    assert editors._WINDOWS_SUBDIRS == ("", "bin")
    assert media_open.KNOWN_PLAYERS is programs.KNOWN_PLAYERS
    assert media_open.Player is programs.Player


# ---------------------------------------------------------------------------
# The default data folder and the interpreter shown in messages
# ---------------------------------------------------------------------------

def test_windows_data_folder_is_what_the_installer_proposes(on, monkeypatch):
    on("win32")
    monkeypatch.setenv("LOCALAPPDATA", "/Users/a/AppData/Local")
    assert paths.default_data_folder() == Path("/Users/a/AppData/Local") / "Leasha"


def test_windows_data_folder_without_the_variable(on, monkeypatch, tmp_path):
    on("win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.default_data_folder() == tmp_path / "AppData" / "Local" / "Leasha"


def test_mac_data_folder_is_application_support(on, monkeypatch, tmp_path):
    """(UNCONFIRMED on macOS) - Apple's documented place for app data."""
    on("darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.default_data_folder() == (
        tmp_path / "Library" / "Application Support" / "Leasha")


def test_linux_data_folder_follows_xdg(on, monkeypatch, tmp_path):
    on("linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.default_data_folder() == tmp_path / "xdg" / "Leasha"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.default_data_folder() == tmp_path / ".local" / "share" / "Leasha"


def test_the_interpreter_text_on_windows_is_exactly_the_old_text(on):
    on("win32")
    assert paths.venv_python_display() == "venv\\Scripts\\python.exe"
    assert paths.venv_pip_display() == "venv\\Scripts\\pip"


def test_the_windows_text_matches_what_the_messages_already_say(on):
    """So switching a message to the helper later cannot change a character."""
    on("win32")
    logging_source = (ROOT / "app" / "core" / "logging.py").read_text(encoding="utf-8")
    assert f"{paths.venv_python_display()} -m app.cli diagnose" in logging_source.replace(
        "\\\\", "\\")
    format_health = (ROOT / "app" / "core" / "format_health.py").read_text(encoding="utf-8")
    assert f"{paths.venv_pip_display()} install" in format_health.replace("\\\\", "\\")


def test_the_interpreter_text_on_a_mac(on):
    on("darwin")
    assert paths.venv_python_display() == "venv/bin/python"
    assert paths.venv_pip_display() == "venv/bin/pip"


# ---------------------------------------------------------------------------
# Cloud placeholders
# ---------------------------------------------------------------------------

def _fake_stat(monkeypatch, **fields):
    """Make `cloudfs`'s `os.stat` return an object with just these fields."""
    monkeypatch.setattr(cloudfs, "os", SimpleNamespace(stat=lambda _p: SimpleNamespace(**fields)))


def _failing_stat(monkeypatch):
    def fail(_path):
        raise FileNotFoundError("gone")
    monkeypatch.setattr(cloudfs, "os", SimpleNamespace(stat=fail))


def test_mac_dataless_file_is_a_placeholder(on, monkeypatch):
    """(UNCONFIRMED on macOS) - iCloud's 'Optimise Mac Storage' stand-in."""
    on("darwin")
    _fake_stat(monkeypatch, st_flags=cloudfs.SF_DATALESS)
    assert cloudfs.file_flags(Path("/x")) == 0x40000000
    assert cloudfs.is_cloud_placeholder(Path("/Users/a/iCloud/big.mov")) is True


def test_mac_ordinary_file_is_not_a_placeholder(on, monkeypatch):
    on("darwin")
    _fake_stat(monkeypatch, st_flags=0x20)          # UF_HIDDEN-ish, not dataless
    assert cloudfs.is_cloud_placeholder(Path("/Users/a/doc.txt")) is False


def test_mac_unreadable_file_is_indexed_not_skipped(on, monkeypatch):
    """Not knowing never causes a skip: a false skip loses a document."""
    on("darwin")
    _failing_stat(monkeypatch)
    assert cloudfs.file_flags(Path("/x")) is None
    assert cloudfs.is_cloud_placeholder(Path("/x")) is False


def test_mac_given_windows_attributes_keeps_their_meaning(on, monkeypatch):
    """`attributes`, when passed, are Windows bits on every system - as always."""
    on("darwin")
    _fake_stat(monkeypatch, st_flags=cloudfs.SF_DATALESS)
    assert cloudfs.is_cloud_placeholder(Path("/x"), 0x20) is False
    assert cloudfs.is_cloud_placeholder(
        Path("/x"), cloudfs.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS) is True


def test_windows_reads_the_attribute_bits(on, monkeypatch):
    on("win32")
    _fake_stat(monkeypatch, st_file_attributes=cloudfs.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | 0x20)
    assert cloudfs.file_attributes(Path("C:/x")) == 0x00400020
    assert cloudfs.is_cloud_placeholder(Path("C:/x")) is True
    assert cloudfs.file_flags(Path("C:/x")) is None


def test_windows_failed_stat_is_not_a_placeholder(on, monkeypatch):
    on("win32")
    _failing_stat(monkeypatch)
    assert cloudfs.file_attributes(Path("C:/x")) is None
    assert cloudfs.is_cloud_placeholder(Path("C:/x")) is False


def test_linux_has_no_placeholders(on, monkeypatch):
    on("linux")
    _fake_stat(monkeypatch, st_flags=cloudfs.SF_DATALESS, st_file_attributes=0x00400000)
    assert cloudfs.file_attributes(Path("/x")) is None
    assert cloudfs.file_flags(Path("/x")) is None
    assert cloudfs.is_cloud_placeholder(Path("/x")) is False


def test_winfs_still_offers_every_name():
    """The walker, the command line and the window import `winfs` unchanged."""
    from app.core import winfs

    for name in winfs.__all__:
        assert getattr(winfs, name) is getattr(cloudfs, name)
    assert winfs.__all__ == [
        "FILE_ATTRIBUTE_OFFLINE", "FILE_ATTRIBUTE_RECALL_ON_OPEN",
        "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS", "CLOUD_PLACEHOLDER_MASK",
        "file_attributes", "is_cloud_placeholder", "describe_placeholder"]
