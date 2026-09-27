r"""Hand a file to the operating system: open it, or show it in its folder.

Layer: L0 (part of `app.core.osbridge`)

Two things a person does with a search result, and each operating system spells
them differently:

    what                     Windows                        macOS               Linux and others
    open with its own app    os.startfile(path)             open PATH           xdg-open PATH
    show it in its folder    explorer /select, PATH         open -R PATH        xdg-open FOLDER

**The Windows lines are copied, not rewritten.** They are exactly what
`app/core/media_open.py` and `app/ui/workers.open_in_explorer` have always run,
so nothing a Windows user sees changes. The Linux line is also what those two
already did, so a developer's machine behaves as before too. Only the macOS line
is new.

**Every command is a list and never goes through a shell.** A file called
`a; rm -rf b.pdf` is one argument to `open`, not two commands. That is the same
rule `app/extract/converter.py` keeps for the same reason: file names come from
the files being searched, which is exactly the input not to trust.

**Nothing here checks the file exists or catches errors.** The callers already
do both, in words the person can act on (`ERR_FILE_CORRUPT`, "Re-index this
folder..."), and they run this on a worker thread because starting a process
can take a few hundred milliseconds. Doing it again here would only give two
places that could disagree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Union

from app.core.osbridge._platform import is_macos, is_windows

__all__ = ["open_with_default_app", "show_in_file_manager"]

PathLike = Union[str, "os.PathLike[str]"]


def open_with_default_app(path: PathLike) -> None:
    """Open `path` with whatever program the system uses for that kind of file.

    The same as double-clicking it. Raises whatever the system raises (for
    example `OSError` when no program is registered for the file type); the
    caller turns that into a message.
    """
    target = str(path)
    if is_windows():
        # `os.startfile` exists only in Windows builds of Python. It asks
        # Windows' shell to open the file exactly as Explorer would.
        os.startfile(target)                        # type: ignore[attr-defined]
    elif is_macos():
        # `open` is macOS's own "double-click this" command-line tool: it hands
        # the file to Launch Services, which picks the default app.
        # (UNCONFIRMED on macOS: the behaviour is Apple's documented one, but it
        # has not been run on a real Mac from this code.)
        subprocess.Popen(["open", target])
    else:
        # Most Linux desktops provide `xdg-open`, which does the same job.
        subprocess.Popen(["xdg-open", target])


def show_in_file_manager(path: PathLike, *, select: bool = True) -> None:
    """Show `path` in Explorer / Finder, with the file picked out if `select`.

    With `select=False` the file itself is opened instead - which is what
    `workers.open_in_explorer(path, select=False)` has always meant, and is how
    the Settings page opens a *folder* (opening a folder shows its contents).

    Raises whatever starting the program raises; the caller reports it.
    """
    target = Path(path)
    if is_windows():
        if select:
            # The comma after `/select` is part of Explorer's own syntax, and it
            # must be its own list item exactly as it has always been sent.
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            os.startfile(str(target))               # type: ignore[attr-defined]
    elif is_macos():
        if select:
            # `open -R` means "reveal": Finder opens the enclosing folder with
            # this file highlighted - the Mac equivalent of `explorer /select,`.
            # (UNCONFIRMED on macOS.)
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["open", str(target)])     # (UNCONFIRMED on macOS)
    else:
        # `xdg-open` cannot highlight a file inside a folder, so for "show it"
        # the folder itself is opened. Unchanged from what workers.py did.
        subprocess.Popen(["xdg-open", str(target if not select else target.parent)])
