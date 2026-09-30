r"""Opening a code result in an editor, at the line.

Layer: L5 presenter — pure. Builds a command line and finds installed editors.
Nothing here runs anything; the caller does that, on a worker.

**A code result is a place, not a document.** Reveal-in-Explorer is the right
action for a spreadsheet and the wrong one for line 512 of `engine.py`:
somebody who found that line wants to be *at* it, and giving them a folder
instead makes them repeat the search inside their editor.

**Detection follows the converters, and for the same reason.** `shutil.which`
alone is not enough on Windows: several editors do not put themselves on
`PATH`, so a machine with VS Code installed and working would report nothing
found and offer to install software that is already there. See
`extract/converter.py`, where that exact bug is written up - this is the same
lookup with an editor table.

**The command is a Setting**, so an editor nobody here has heard of is a line
of configuration rather than a feature request. `{path}` and `{line}` are the
placeholders; a command with neither still opens the file, which is better
than refusing.
"""

from __future__ import annotations

import shutil
from typing import Optional, Sequence

from app.core.osbridge import programs as _programs

__all__ = [
    "EDITORS", "command_for", "detect", "installed", "copyable", "AUTO",
    "NONE", "TERMINAL_EDITORS", "label_for",
]

#: The value meaning "work it out from what is installed".
AUTO = "auto"

#: Editors this knows how to open at a line, best-guess order [TUNE].
#:
#: `(setting value, human name, executable, argument template)`.
#:
#: **Ordered by how likely somebody searching a code index is to have it**, not
#: alphabetically: the first one found wins, and a machine with both VS Code
#: and Notepad++ almost certainly wants the first.
EDITORS: tuple = (
    ("vscode", "Visual Studio Code", "code", "-g {path}:{line}"),
    ("cursor", "Cursor", "cursor", "-g {path}:{line}"),
    ("vscodium", "VSCodium", "codium", "-g {path}:{line}"),
    # Order 0y §2c names the order: VS Code, then Notepad++, then Sublime.
    ("notepadpp", "Notepad++", "notepad++", "-n{line} {path}"),
    ("sublime", "Sublime Text", "subl", "{path}:{line}"),
    ("idea", "IntelliJ IDEA", "idea", "--line {line} {path}"),
    ("pycharm", "PyCharm", "pycharm", "--line {line} {path}"),
    ("vim", "Vim", "vim", "+{line} {path}"),
    ("nvim", "Neovim", "nvim", "+{line} {path}"),
    ("emacs", "Emacs", "emacs", "+{line} {path}"),
)

#: The value meaning "do not use an editor": the file opens in its usual program.
NONE = "none"

#: Editors that run *inside* a terminal. Started from a window with no console
#: they need one of their own, or they run where nobody can see them (order 0y
#: §2c; `workers.editor_creationflags` reads this).
TERMINAL_EDITORS = frozenset({"vim", "nvim"})


def label_for(command: Sequence[str]) -> str:
    """The editor's name in words for a command line, or its program's name.

    Pure. Used for the one sentence said when an editor would not start.
    """
    program = str(command[0]) if command else ""
    stem = program.replace("\\", "/").rpartition("/")[2].lower()
    stem = stem.rpartition(".")[0] if "." in stem else stem
    for _value, label, executable, _template in EDITORS:
        if stem == executable.lower():
            return label
    return stem or "The editor"


#: Where these install on Windows when they are not on `PATH`, relative to a
#: program-files root. Same shape and same reason as the converters' table.
#:
#: **Moved to `app/core/osbridge/programs.py` (work order 0x §1b)**, with the
#: search below; these are the same objects under their old names, so
#: `editors._WINDOWS_LOCATIONS` reads exactly as it did.
_WINDOWS_LOCATIONS: dict = _programs.EDITOR_WINDOWS_LOCATIONS

#: Where an executable sits inside its install folder. `""` is the folder
#: itself; VS Code puts its CLI shim in `bin`.
_WINDOWS_SUBDIRS = _programs.EDITOR_WINDOWS_SUBDIRS


def _program_roots() -> list:
    """The folders Windows installs programs into, most-specific first."""
    return _programs.editor_program_roots()


def _installed_on_windows(executable: str) -> Optional[str]:
    """The path of an editor that did not put itself on `PATH`. Never raises."""
    return _programs.find_editor_on_windows(executable)


def _installed_on_macos(executable: str) -> Optional[str]:
    """Where a Mac keeps this editor when it is not on `PATH`, or None.

    Always None off a Mac. On a Mac it looks inside `/Applications` app bundles
    and Homebrew's folders, which Leasha - started from Finder, not a terminal -
    does not have on its `PATH`. (UNCONFIRMED on macOS.)
    """
    return _programs.find_editor_on_macos(executable)


def installed(name: str) -> Optional[str]:
    """Where this editor's executable is, or None. **Never raises.**

    `PATH` first - somebody who deliberately put a build on it means that one -
    then the standard Windows locations.
    """
    executable = str(name or "").strip()
    if not executable:
        return None
    try:
        # 2026-09-27 (work order 0x §1a): the Mac look comes last and returns
        # None at once anywhere else, so Windows answers exactly as before.
        return (shutil.which(executable) or _installed_on_windows(executable)
                or _installed_on_macos(executable))
    except Exception:                              # noqa: BLE001 - a lookup
        return None


def detect() -> tuple:
    """Every known editor that is actually here, in `EDITORS` order.

    `(value, human name, path)` each. **Settings offers exactly these**, so
    nobody chooses an editor that will then fail on every click - the same rule
    the converter list follows.
    """
    found = []
    for value, label, executable, _template in EDITORS:
        where = installed(executable)
        if where:
            found.append((value, label, where))
    return tuple(found)


def command_for(choice: str, path: str, line: Optional[int] = None,
                *, custom: str = "") -> Optional[Sequence[str]]:
    r"""The argument list to open `path` at `line`, or None if there is none.

    `choice` is the setting: an editor key, `auto`, or empty. `custom` is the
    free-text command, which wins when it is set - **an editor nobody here has
    heard of is a line of configuration, not a feature request.**

    Returns a list rather than a string, because a path with a space in it is
    the normal case on Windows and joining by hand is how that breaks.

    **A command with no `{line}` still opens the file.** Landing on line one of
    the right file is worth having; refusing because the template is simple is
    not.
    """
    target = str(path or "").strip()
    if not target:
        return None
    where = str(line or 1)

    if custom.strip():
        parts = [piece.replace("{path}", target).replace("{line}", where)
                 for piece in custom.split()]
        # No placeholder at all: append the path, so a bare `notepad` works.
        if "{path}" not in custom:
            parts.append(target)
        return parts

    wanted = str(choice or AUTO).strip().lower()
    table = {value: (executable, template)
             for value, _label, executable, template in EDITORS}

    if wanted in ("", AUTO):
        for value, _label, _where in detect():
            wanted = value
            break
        else:
            return None

    entry = table.get(wanted)
    if entry is None:
        return None
    executable, template = entry
    resolved = installed(executable) or executable
    arguments = [piece.replace("{path}", target).replace("{line}", where)
                 for piece in template.split()]
    return [resolved, *arguments]


def copyable(path: str, line: Optional[int] = None) -> str:
    """`path:line`, for the copy action beside "open in editor".

    **The universal currency between developers.** It pastes into a terminal,
    a chat message, another editor and half the tools on the machine, and it
    is what somebody asks a colleague to look at.
    """
    target = str(path or "").strip()
    if not target:
        return ""
    return f"{target}:{int(line)}" if line else target
