r"""Open a recording at the moment a search result is about.

Layer: L5 support (pure logic; no Qt, no store)

Work order 202626270515: a transcript hit says *at 12:41*, and the point of that
is that one click takes a person to 12:41. Windows' own "open" does not - the
default player starts at the beginning and takes no argument - so this looks for
a player that does, and asks it to start there.

**Four players, each with the one argument it documents** for "begin at this
time": VLC (`--start-time=SECONDS`), mpv (`--start=SECONDS`), MPC-HC
(`/start MILLISECONDS`) and PotPlayer (`/seek=HH:MM:SS`). They are looked for by
name on `PATH` and in the folders each installs into, in the order a person is
most likely to have meant. **Nothing is downloaded and nothing is installed**;
Leasha never chooses a player for somebody, it uses one that is already there.

**No player found is not an error.** The file is opened with the system's own
choice, exactly as before, and the note that comes back says which moment the
words are at and that VLC (or another of the four) would let Leasha go there
itself. A search result that cannot seek still tells the person where to look.

**A list in code, not a setting** (non-negotiable 11: a value nobody will ever
change is a constant). If someone wants a fifth player, it is a commit with a
test, not a text box - and every launch is `shell=False` with the file path as
one argument, so a file called `a; del b.mp4` stays a file name.

Everything here is testable with no player installed and nothing launched: the
finder and the launcher are parameters.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.core.errors import AppError, make_error
from app.core.logging import logger
# `Player` and `KNOWN_PLAYERS` moved to the platform package with the install
# search (their `.exe` names and Program Files folders are Windows install
# knowledge); they are imported back under the same names, so
# `media_open.Player` and `media_open.KNOWN_PLAYERS` work as they always did.
from app.core.osbridge import launch as _launch_os
from app.core.osbridge import programs as _programs
from app.core.osbridge.programs import KNOWN_PLAYERS, Player
from app.extract.timecode import format_timecode, parse_timecode

__all__ = [
    "Player",
    "KNOWN_PLAYERS",
    "OpenOutcome",
    "find_player",
    "command_for",
    "open_at",
    "seconds_for_result",
]

log = logger.bind(component="core.media_open")


@dataclass(frozen=True)
class OpenOutcome:
    """What `open_at` did, in words the interface can show as they are."""

    opened: bool
    #: True only when a player was asked to start at the moment.
    seeked: bool = False
    player: str = ""
    #: A sentence for the toast: empty when the player went to the moment by
    #: itself and there is nothing more to say.
    note: str = ""
    error: Optional[AppError] = None


def seconds_for_result(label: object) -> Optional[int]:
    """Whole seconds from a result's locator (`12:41`), or None if it is not a time.

    The one place a result row becomes a number to seek to, so a spreadsheet's
    `Q3!D14` or a page number can never be mistaken for one.
    """
    return parse_timecode(label)


def _executable_in_folders(player: Player) -> Optional[str]:
    """A known Windows install of `player`, or None. Never raises.

    2026-09-27 (work order 0x §1b): the search itself moved to
    `app.core.osbridge.programs`, unchanged. On a Mac it also looks in
    `/Applications` and Homebrew's folders (UNCONFIRMED on macOS); on Windows
    that second look is skipped at once, so nothing changes there.
    """
    return (_programs.find_player_on_windows(player)
            or _programs.find_player_on_macos(player))


def find_player(
    *, which: Callable[[str], Optional[str]] = shutil.which,
    installed: Callable[[Player], Optional[str]] = _executable_in_folders,
) -> Optional[tuple[Player, str]]:
    """The first known player that is installed, as `(player, absolute path)`.

    `PATH` first (somebody who put a build there means that one), then the
    folders it installs into. `None` when there is none - which is the ordinary
    answer, not a failure.
    """
    for player in KNOWN_PLAYERS:
        for name in player.executables:
            found = which(name)
            if found:
                return player, found
        found = installed(player)
        if found:
            return player, found
    return None


def command_for(player: Player, executable: str, path: str, seconds: int) -> list[str]:
    """The argument list that starts `player` at `seconds`. Pure.

    Every placeholder is substituted inside one list element, so a path with a
    space, a semicolon or a quote reaches the player as one argument.
    """
    seconds = max(0, int(seconds))
    hms = format_timecode(seconds)
    if hms.count(":") == 1:
        hms = "0:" + hms
    parts = hms.split(":")
    hms = f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
    values = {"{file}": str(path), "{s}": str(seconds), "{ms}": str(seconds * 1000),
              "{hms}": hms}
    command = [executable]
    for argument in player.args:
        value = argument
        for placeholder, replacement in values.items():
            value = value.replace(placeholder, replacement)
        command.append(value)
    return command


def _launch(command: list[str]) -> None:
    subprocess.Popen(command, shell=False,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _system_open(path: str) -> None:
    # "Open it with whatever the system uses for this kind of file". The
    # per-system commands (Windows' `os.startfile`, the Mac's `open`, Linux's
    # `xdg-open`) live in `app.core.osbridge.launch` since work order 0x §1b;
    # Windows and Linux run exactly what they ran here before.
    _launch_os.open_with_default_app(path)


def open_at(
    path: str, seconds: Optional[float], *,
    finder: Callable[[], Optional[tuple[Player, str]]] = find_player,
    launch: Callable[[list[str]], None] = _launch,
    system_open: Callable[[str], None] = _system_open,
) -> OpenOutcome:
    """Open `path`; at `seconds` if a player that can is installed. Never raises.

    **Blocking (stat calls, a process start): run it on a worker**, like
    `workers.open_in_explorer`, whose contract this shares.
    """
    target = Path(path)
    try:
        exists = target.exists()
    except OSError:
        exists = False
    if not exists:
        return OpenOutcome(opened=False, error=make_error(
            "ERR_FILE_CORRUPT", "ui.open", path=str(target),
            suggestion="The file has moved or been deleted since it was indexed. "
                       "Re-index this folder to update the results.",
            details="Not found on disk."))

    moment = None if seconds is None else max(0, int(seconds))
    if moment is not None:
        try:
            found = finder()
        except Exception as exc:                    # noqa: BLE001 - a lookup, not the open
            log.debug("player lookup failed: {}: {}", type(exc).__name__, exc)
            found = None
        if found is not None:
            player, executable = found
            try:
                launch(command_for(player, executable, str(target), moment))
                return OpenOutcome(opened=True, seeked=True, player=player.name)
            except Exception as exc:                # noqa: BLE001 - fall back to a plain open
                log.warning("{} would not start ({}: {}); opening plainly",
                            player.name, type(exc).__name__, exc)

    try:
        system_open(str(target))
    except Exception as exc:                        # noqa: BLE001
        from app.core.errors import to_app_error

        return OpenOutcome(opened=False, error=to_app_error(
            exc, "ui.open", path=str(target)))

    note = ""
    if moment is not None:
        note = (f"Opened. What you searched for is at {format_timecode(moment)} - "
                f"with VLC, mpv, MPC-HC or PotPlayer installed Leasha opens "
                f"recordings at that moment by itself.")
    return OpenOutcome(opened=True, seeked=False, note=note)
