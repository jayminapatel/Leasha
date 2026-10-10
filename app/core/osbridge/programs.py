r"""Finding a program that is installed but not on `PATH`.

Layer: L0 (part of `app.core.osbridge`)

**Why `PATH` alone is not enough.** `PATH` is the list of folders the system
searches when you type a program's name. Python's `shutil.which("soffice")`
searches it too. But plenty of programs never add themselves to it:

- On **Windows**, LibreOffice, VS Code (for some install types), VLC and many
  others install under `C:\Program Files\...` and stop there. A machine with
  LibreOffice installed and working used to report it missing - see the long
  note on `CONVERTER_WINDOWS_LOCATIONS` below, where that bug is written up.
- On a **Mac**, a program dragged into `/Applications` lives inside an "app
  bundle" (a folder named `Something.app`) and its actual executable is buried
  at `Something.app/Contents/MacOS/<name>`. And a program started from the Dock
  or Finder - which is how Leasha is started - gets a very short `PATH`
  (`/usr/bin:/bin:/usr/sbin:/sbin`) that does **not** include Homebrew's
  folders (`/opt/homebrew/bin` on Apple Silicon, `/usr/local/bin` on Intel
  Macs). So `tesseract` installed with Homebrew is invisible to `which` from
  the app, even though it works in Terminal. That is the Mac version of the
  same bug, and it gets the same fix: after `PATH`, look where the program is
  known to install. (UNCONFIRMED on macOS: the folders below are the
  documented or conventional ones; none has been checked on a real Mac.)

**What callers do.** Each caller still asks `PATH` first - somebody who has
deliberately put a build there means that one - and only then asks this module.
The three callers, whose names and behaviour are unchanged:

- `app/extract/converter.py` (`resolve_binary`): LibreOffice, its Python,
  Tesseract, the DWG converters.
- `app/ui/editors.py` (`installed`): code editors.
- `app/core/media_open.py` (`find_player`): media players that can start at a
  given time.

**The Windows search was moved here unchanged** (work order 0x §1b): the same
tables, the same environment variables in the same order, the same
sub-folders, the same `is_file()` checks. Each caller used to carry its own
copy of the loop; they now share `find_in_install_folders`, which walks the
folders in exactly the order each copy did.

**Nothing is ever guessed.** A path comes back only when the file is really
there. Every function here returns `None` rather than raising, because a
lookup that fails must never stop an index run or a click.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from app.core.osbridge._platform import is_macos, is_windows

__all__ = [
    "WINDOWS_PROGRAM_ROOT_VARIABLES",
    "CONVERTER_WINDOWS_LOCATIONS", "CONVERTER_WINDOWS_SUBDIRS",
    "EDITOR_WINDOWS_LOCATIONS", "EDITOR_WINDOWS_SUBDIRS",
    "MAC_APPLICATION_FOLDERS", "MAC_EXTRA_BIN_FOLDERS",
    "CONVERTER_MAC_LOCATIONS", "EDITOR_MAC_LOCATIONS", "PLAYER_MAC_LOCATIONS",
    "Player", "KNOWN_PLAYERS",
    "find_in_install_folders", "program_files_roots", "editor_program_roots",
    "find_converter_on_windows", "find_editor_on_windows", "find_player_on_windows",
    "find_on_macos",
    "find_converter_on_macos", "find_editor_on_macos", "find_player_on_macos",
    "git_program", "claude_desktop_process",
]


# ===========================================================================
# Windows: the tables
# ===========================================================================

#: The roots `CONVERTER_WINDOWS_LOCATIONS` is resolved against, in order of
#: preference. `LOCALAPPDATA\Programs` catches a per-user install, which is what
#: somebody without administrator rights ends up with.
#:
#: These are the *names* of environment variables that Windows fills in with
#: folder paths - `ProgramFiles` is usually `C:\Program Files` - so nothing
#: here assumes a drive letter.
WINDOWS_PROGRAM_ROOT_VARIABLES = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")

#: Where these programs actually install on Windows, relative to a program-files
#: root. (Moved from `app/extract/converter.py`, where it was
#: `_WINDOWS_LOCATIONS`; that name still works there.)
#:
#: **`shutil.which` alone was not enough, and this is the bug it caused.**
#: LibreOffice does not put itself on `PATH` on Windows - it never has - so a
#: machine with LibreOffice installed and working reported "needs attention",
#: offered an install link for software already present, and refused to enable
#: the `.doc` and `.ppt` routes. The person is then told to fix something that
#: is not broken, which is worse than saying nothing.
#:
#: PATH is still checked first: somebody who has deliberately put a build on it
#: means that one.
#: name -> (the folders it installs into, the executable to look for)
#:
#: **Folders, not full paths, because the layout inside them varies.**
#: The first version hardcoded `LibreDWG\bin\dwg2dxf.exe`, and a real install
#: turned out to be `C:\Program Files\libredwg` with the executable somewhere
#: else inside it. Guessing the exact layout for every project is how this table
#: goes stale; searching the two or three arrangements that actually exist -
#: the folder itself, `bin\`, `program\` - costs three `is_file()` calls and
#: covers all of them.
CONVERTER_WINDOWS_LOCATIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "soffice": (("LibreOffice",), "soffice.exe"),
    "libreoffice": (("LibreOffice",), "soffice.exe"),
    "libreoffice-python": (("LibreOffice",), "python.exe"),
    "tesseract": (("Tesseract-OCR",), "tesseract.exe"),
    # No `pandoc` entry: it came off ALLOWED_BINARIES when `.epub` and `.fb2`
    # moved in-process. A location for a name that cannot run is dead weight,
    # and `test_only_allowed_names_have_locations` is what caught it here.
    "dwg2dxf": (("libredwg", "LibreDWG"), "dwg2dxf.exe"),
    "ODAFileConverter": (("ODA",), "ODAFileConverter.exe"),
    # Same install, second program - so the same folders, and the same three
    # arrangements `_WINDOWS_SUBDIRS` already covers.
    "dwg2SVG": (("libredwg", "LibreDWG"), "dwg2SVG.exe"),
}

#: Where an executable sits inside its install folder. `""` is the folder
#: itself, which is how a zip extracted by hand usually looks.
CONVERTER_WINDOWS_SUBDIRS = ("", "bin", "program")

#: Where these install on Windows when they are not on `PATH`, relative to a
#: program-files root. Same shape and same reason as the converters' table.
#: (Moved from `app/ui/editors.py`, where it was `_WINDOWS_LOCATIONS`; that
#: name still works there.)
EDITOR_WINDOWS_LOCATIONS: dict = {
    "code": (("Microsoft VS Code",), "Code.exe"),
    "cursor": (("Cursor",), "Cursor.exe"),
    "codium": (("VSCodium",), "VSCodium.exe"),
    "subl": (("Sublime Text", "Sublime Text 3"), "subl.exe"),
    "notepad++": (("Notepad++",), "notepad++.exe"),
    "idea": (("JetBrains",), "idea64.exe"),
    "pycharm": (("JetBrains",), "pycharm64.exe"),
}

#: Where an executable sits inside its install folder. `""` is the folder
#: itself; VS Code puts its CLI shim in `bin`.
EDITOR_WINDOWS_SUBDIRS = ("", "bin")


@dataclass(frozen=True)
class Player:
    """A player that can be told where to start, and how to say so."""

    name: str
    #: Executable names to look for on `PATH`.
    executables: tuple[str, ...]
    #: Folders under a Program Files root that it installs into.
    folders: tuple[str, ...]
    #: Arguments. `{file}`, `{s}` (whole seconds), `{ms}` and `{hms}` are filled
    #: in; each stays ONE argument, which is the property a shell would destroy.
    args: tuple[str, ...]


#: The four players `app/core/media_open.py` knows how to start at a moment.
#: (Moved from there with `Player`, because the `.exe` names and Program Files
#: folders are Windows install knowledge; `media_open` still offers both names.)
#: Why these four and what each argument means is explained at the top of
#: `media_open.py`.
KNOWN_PLAYERS: tuple[Player, ...] = (
    Player("VLC", ("vlc", "vlc.exe"), ("VideoLAN\\VLC",),
           ("--start-time={s}", "{file}")),
    Player("mpv", ("mpv", "mpv.exe"), ("mpv",), ("--start={s}", "{file}")),
    Player("MPC-HC", ("mpc-hc64", "mpc-hc64.exe", "mpc-hc", "mpc-hc.exe"),
           ("MPC-HC", "K-Lite Codec Pack\\MPC-HC64"), ("{file}", "/start", "{ms}")),
    Player("PotPlayer", ("PotPlayerMini64.exe", "PotPlayerMini.exe"),
           ("DAUM\\PotPlayer", "PotPlayer"), ("{file}", "/seek={hms}")),
)


# ===========================================================================
# Windows: the search
# ===========================================================================

def _folders_in(text: str) -> list:
    """`text` as its folders, whichever slash divides them."""
    return [part for part in str(text).replace("\\", "/").split("/") if part]


def find_in_install_folders(
    roots: Iterable[object],
    folders: Sequence[str],
    executables: Sequence[str],
    subdirs: Sequence[str] = ("",),
) -> Optional[str]:
    """The first `root/folder/[subdir/]executable` that is a real file, or None.

    The one search loop the three callers used to write out separately. The
    order of the four loops - roots, then folders, then sub-folders, then
    names - is the order each of them used, so the first match found is the
    same one found before.

    `roots` may hold `None` or empty entries (an environment variable that is
    not set); they are skipped. A drive that is not there raises `OSError`
    from `is_file()` on some systems; that candidate is skipped too. Never
    raises.
    """
    for root in roots:
        if not root:
            continue
        for folder in folders:
            for sub in subdirs:
                for name in executables:
                    # `""` means "the folder itself", so it adds no part.
                    # 2026-10-05: split on either slash. The tables write
                    # `VideoLAN\VLC`; off Windows that was one folder with
                    # a backslash in its name, so nothing was ever found in it.
                    parts = ([root] + _folders_in(folder)
                             + (_folders_in(sub) if sub else []) + [name])
                    try:
                        candidate = Path(*parts)
                        if candidate.is_file():
                            return str(candidate)
                    except OSError:                # a drive that is not there
                        continue
    return None


def program_files_roots() -> list:
    """The Program Files roots, then the per-user `LOCALAPPDATA\\Programs`.

    The order the converters and the media players have always searched in.
    Unset variables come back as `None`, which the search skips.
    """
    roots = [os.environ.get(key) for key in WINDOWS_PROGRAM_ROOT_VARIABLES]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(os.path.join(local, "Programs"))
    return roots


def editor_program_roots() -> list:
    """The folders Windows installs programs into, most-specific first.

    The editors' order, which differs from the converters' on purpose and is
    kept: editors are very often per-user installs, so `LOCALAPPDATA` (and its
    `Programs` folder) is tried before the machine-wide Program Files.
    """
    found = []
    for name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)",
                 "ProgramW6432"):
        value = os.environ.get(name)
        if not value:
            continue
        found.append(Path(value))
        if name == "LOCALAPPDATA":
            found.append(Path(value) / "Programs")
    return found


def find_converter_on_windows(name: str) -> Optional[str]:
    """A known Windows install location for converter `name`, or None.

    **Does not check that this is Windows** - `converter._installed_on_windows`
    does that first, through its own `_is_windows()`, which its tests replace.
    Never raises and never guesses.
    """
    entry = CONVERTER_WINDOWS_LOCATIONS.get(name)
    if entry is None:
        return None
    folders, executable = entry
    return find_in_install_folders(program_files_roots(), folders, (executable,),
                                   CONVERTER_WINDOWS_SUBDIRS)


def find_editor_on_windows(executable: str) -> Optional[str]:
    """The path of an editor that did not put itself on `PATH`. Never raises.

    Like the code it came from, this does not check the platform: off Windows
    the variables it reads are normally unset, so it finds nothing.
    """
    entry = EDITOR_WINDOWS_LOCATIONS.get(executable)
    if not entry:
        return None
    folders, filename = entry
    return find_in_install_folders(editor_program_roots(), folders, (filename,),
                                   EDITOR_WINDOWS_SUBDIRS)


def find_player_on_windows(player: Player) -> Optional[str]:
    """A known Windows install of `player`, or None. Never raises.

    Only the `.exe` names are looked for inside the install folders (the bare
    names are for `PATH`), lower-cased and in sorted order, as before.
    """
    wanted = {name.lower() for name in player.executables if name.lower().endswith(".exe")}
    return find_in_install_folders(program_files_roots(), player.folders,
                                   sorted(wanted))


# ===========================================================================
# macOS: the tables (all UNCONFIRMED on macOS)
# ===========================================================================

#: Where Mac apps live: the shared `/Applications`, then the per-user
#: `~/Applications` (the Mac counterpart of `LOCALAPPDATA\Programs`).
MAC_APPLICATION_FOLDERS = ("/Applications", "~/Applications")

#: Homebrew's folders, which a program started from Finder does not have on its
#: `PATH`: Apple Silicon first (what the Mac CI runner and new Macs are), then
#: Intel. (UNCONFIRMED on macOS.)
MAC_EXTRA_BIN_FOLDERS = ("/opt/homebrew/bin", "/usr/local/bin")

#: name -> (paths inside an application folder, names to try in the bin folders).
#:
#: An empty tuple means "not installed that way". LibreOffice's own Python is
#: only ever inside its app bundle, so it has no bin-folder name: a `python`
#: found in Homebrew's folder would be a different interpreter without
#: LibreOffice's `uno` bridge. `xstexporter` (Lotus Notes) has no Mac entry.
#: Every path here is (UNCONFIRMED on macOS).
CONVERTER_MAC_LOCATIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "soffice": (("LibreOffice.app/Contents/MacOS/soffice",), ("soffice",)),
    "libreoffice": (("LibreOffice.app/Contents/MacOS/soffice",), ("soffice",)),
    "libreoffice-python": (("LibreOffice.app/Contents/Resources/python",), ()),
    "tesseract": ((), ("tesseract",)),
    "dwg2dxf": ((), ("dwg2dxf",)),
    "dwg2SVG": ((), ("dwg2SVG",)),
    "ODAFileConverter": (("ODAFileConverter.app/Contents/MacOS/ODAFileConverter",), ()),
}

#: The editors' Mac locations, same shape. VS Code and its relatives keep their
#: command-line launcher (the one that understands `-g file:line`) at
#: `Contents/Resources/app/bin/`, not in `Contents/MacOS/`, whose executable is
#: the window process itself. Notepad++ is Windows-only. (UNCONFIRMED on macOS.)
EDITOR_MAC_LOCATIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "code": (("Visual Studio Code.app/Contents/Resources/app/bin/code",), ("code",)),
    "cursor": (("Cursor.app/Contents/Resources/app/bin/cursor",), ("cursor",)),
    "codium": (("VSCodium.app/Contents/Resources/app/bin/codium",), ("codium",)),
    "subl": (("Sublime Text.app/Contents/SharedSupport/bin/subl",), ("subl",)),
    "idea": (("IntelliJ IDEA.app/Contents/MacOS/idea",
              "IntelliJ IDEA CE.app/Contents/MacOS/idea"), ("idea",)),
    "pycharm": (("PyCharm.app/Contents/MacOS/pycharm",
                 "PyCharm CE.app/Contents/MacOS/pycharm"), ("pycharm",)),
    "vim": ((), ("vim",)),
    "nvim": ((), ("nvim",)),
    "emacs": (("Emacs.app/Contents/MacOS/Emacs",), ("emacs",)),
}

#: The media players' Mac locations, by `Player.name`. MPC-HC and PotPlayer are
#: Windows-only. Whether VLC's and mpv's bundled executables accept the same
#: start-time arguments as on Windows is (UNCONFIRMED on macOS).
PLAYER_MAC_LOCATIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "VLC": (("VLC.app/Contents/MacOS/VLC",), ("vlc",)),
    "mpv": (("mpv.app/Contents/MacOS/mpv",), ("mpv",)),
}


# ===========================================================================
# macOS: the search
# ===========================================================================

def find_on_macos(bundle_paths: Sequence[str], bin_names: Sequence[str]) -> Optional[str]:
    """The first Mac install found, or None. **None on anything but a Mac.**

    Tries each application folder with each path inside it (an app bundle),
    then each Homebrew folder with each bare name. App bundles first, the way
    the Windows search tries install folders: a program installed as an app is
    the ordinary case. (UNCONFIRMED on macOS.)

    Checked with `is_file()`, like the Windows search: only a real file counts.
    Never raises.
    """
    if not is_macos():
        return None
    candidates = []
    for folder in MAC_APPLICATION_FOLDERS:
        for inside in bundle_paths:
            # `~` is the person's home folder; `expanduser` turns it into the
            # real path, which `is_file` needs.
            candidates.append(Path(os.path.expanduser(folder)) / inside)
    for folder in MAC_EXTRA_BIN_FOLDERS:
        for name in bin_names:
            candidates.append(Path(folder) / name)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def find_converter_on_macos(name: str) -> Optional[str]:
    """A known Mac install of converter `name`, or None (always None off a Mac)."""
    entry = CONVERTER_MAC_LOCATIONS.get(name)
    if entry is None:
        return None
    return find_on_macos(*entry)


def find_editor_on_macos(executable: str) -> Optional[str]:
    """A known Mac install of an editor, or None (always None off a Mac)."""
    entry = EDITOR_MAC_LOCATIONS.get(executable)
    if entry is None:
        return None
    return find_on_macos(*entry)


def find_player_on_macos(player: Player) -> Optional[str]:
    """A known Mac install of `player`, or None (always None off a Mac)."""
    entry = PLAYER_MAC_LOCATIONS.get(player.name)
    if entry is None:
        return None
    return find_on_macos(*entry)


# ===========================================================================
# git: the program behind the launcher (order 0y section 3, 2026-09-30)
# ===========================================================================

#: Where Git for Windows keeps the real git, relative to its install folder.
GIT_WINDOWS_REAL = (("mingw64", "bin"), ("clangarm64", "bin"), ("mingw32", "bin"))

_git_program_found: Optional[str] = None


def git_program() -> str:
    """The git to start: the real program, not the launcher in front of it.

    Git for Windows puts a small launcher on `PATH` (`cmd/git.exe` in its
    install folder) which starts the real git as a child process. Ending the
    launcher does **not** end that child: the real git reads history to the
    end, holding the output pipe open, so a history search that was stopped
    kept its caller waiting. Measured here on 2026-09-30: `kill()` on the
    launcher closed the pipe 0.8 s to 1.5 s later (when git had finished by
    itself); `kill()` on the real git closed it at once. The two print the same
    thing - `--version`, `config --list`, `log -S`, `grep` and `show` were
    compared byte for byte.

    Off Windows, and for an install laid out some other way, this is whatever
    `git` is on `PATH`; with no git at all it is the bare word, so the caller's
    own "git was not found" still happens. Found once and kept, because it is
    stat calls - **worker thread only**, like everything that starts git.
    """
    global _git_program_found
    if _git_program_found:
        return _git_program_found
    found = shutil.which("git")
    if not found:
        return "git"
    if is_windows():
        launcher = Path(found)
        if launcher.parent.name.lower() == "cmd":
            for parts in GIT_WINDOWS_REAL:
                real = launcher.parent.parent.joinpath(*parts, "git.exe")
                try:
                    if real.is_file():
                        found = str(real)
                        break
                except OSError:
                    continue
    _git_program_found = found
    return found


def claude_desktop_process() -> str:
    """The name Claude Desktop's own process runs under, as `psutil` reports it.

    Moved here 2026-10-10 from `mcp_controller.py`, which named `claude.exe`
    outside this package (`test_no_windows_only_call_outside_osbridge`). On a Mac
    the app bundle's executable is `Claude` (UNCONFIRMED on macOS: the bundle was
    not looked at on a real Mac).
    """
    return "claude.exe" if is_windows() else "Claude"
