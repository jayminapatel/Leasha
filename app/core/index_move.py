r"""Moving the index to a new location, safely.

Layer: L0

**This module exists because the promise was already being made and nothing
kept it.** Settings offered "Move or change index location...", the dialog
recorded the decision as `index:pending_move`, and the status bar said *"the app
will move the index the next time it starts"*. Nothing read that key. Nothing
moved anything. `.env` had already been repointed at the new path, so the next
start opened an empty index and a hundred gigabytes appeared to have vanished -
still on disk, still intact, and no longer referenced by anything.

So the rule this module is built on: **the `.env` write and the file move are
one operation or they are a data-loss bug.** They happen here, together, in that
order, with the old location left alone until the new one is verified.

## Why the derived keys have to be unpinned

`install.ps1` writes all five subpaths absolutely:

    DATA_PATH=D:\KnowledgeGraphData
    VECTOR_PATH=D:\KnowledgeGraphData\vectors
    FTS_DB=D:\KnowledgeGraphData\fts\knowledge.db
    ...

`config.path_of` prefers the explicit key over `data_path / subdir`, and `.env`
always beats a default. So **changing `DATA_PATH` on its own changes nothing**:
the vectors, the database, the cache and the models all keep resolving to the
old drive, and the only visible effect is that Settings now displays a path the
application is not using.

Every move therefore removes those keys - `env_writer` treats `None` as "delete
this line" - so they fall back to being derived from `DATA_PATH`, which is what
makes `DATA_PATH` mean something for the next move too.

## Why it does not run while the application is up

Copying a SQLite database out from under an open connection produces a file
that opens, reports no error, and is missing whatever was in the write-ahead log
at the moment of the copy. That is worse than a failure - it is a half-copied
index that looks fine. The stores are closed here by construction: this runs
from the CLI, or from startup before anything opens.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from app.core.env_writer import apply_values
from app.core.errors import AppErrorException, make_error, raise_error
from app.core.logging import logger

__all__ = [
    "MOVE", "ADOPT", "FRESH",
    "INDEX_SUBDIRS", "DERIVED_KEYS", "PENDING_FILE",
    "MoveReport", "plan_move", "perform_move", "looks_like_an_index",
    "write_pending", "read_pending", "clear_pending",
]

log = logger.bind(component="core.index_move")

MOVE = "move"
ADOPT = "adopt"
FRESH = "fresh"

#: The five directories that make up an index, in `config._INDEX_SUBDIRS` order.
INDEX_SUBDIRS = ("vectors", "fts", "cache", "models", "state")

#: The `.env` keys that must be removed so they derive from `DATA_PATH` again.
#: See the module docstring - leaving any one of them behind silently keeps that
#: part of the index on the old drive.
DERIVED_KEYS = ("VECTOR_PATH", "FTS_DB", "CACHE_PATH", "MODEL_CACHE", "STATE_PATH")

#: Below this, a "move" is really a copy that leaves the source behind. Used
#: only to decide the wording of the report, never the behaviour.
_SAME_VOLUME_HINT = "instant (same drive)"


@dataclass(frozen=True, slots=True)
class MoveReport:
    """What a move did, or would do. Returned by both plan and perform."""

    action: str
    source: Path
    destination: Path
    moved: tuple[str, ...] = ()
    bytes_moved: int = 0
    same_volume: bool = False
    env_keys_removed: tuple[str, ...] = ()
    performed: bool = False

    @property
    def summary(self) -> str:
        """One sentence for the dialog and the CLI, in the past or the
        conditional tense according to `performed`."""
        if self.action == ADOPT:
            return f"Using the index already at {self.destination}."
        if self.action == FRESH:
            return f"Starting a new, empty index at {self.destination}."
        what = ", ".join(self.moved) if self.moved else "nothing"
        size = f"{self.bytes_moved / 1e9:.1f}GB" if self.bytes_moved else "0 bytes"
        speed = _SAME_VOLUME_HINT if self.same_volume else "a full copy"
        return (f"{'Moved' if self.performed else 'Would move'} {what} "
                f"({size}, {speed}) from {self.source} to {self.destination}.")


#: Where a decision made in Settings waits until the next start.
#:
#: **A file beside `.env`, deliberately not a row in the index.** The first
#: version recorded it with `store.set_state("index:pending_move", ...)` - inside
#: the very database about to be moved. Reading it back at startup means opening
#: the old store, closing it, moving it, and hoping nothing kept a handle; and
#: after an adopt, the flag is in whichever database happens to be there. A
#: sidecar in the project folder is read before anything opens and survives the
#: move because it is not part of what moves.
PENDING_FILE = "pending-move.json"


def write_pending(project_path: Path, action: str, destination: Path) -> Path:
    """Record a decision for the next start. Overwrites any earlier one."""
    import json

    path = Path(project_path) / PENDING_FILE
    path.write_text(
        json.dumps({"action": action, "destination": str(destination)}, indent=2),
        encoding="utf-8",
    )
    return path


def read_pending(project_path: Path) -> Optional[tuple[str, Path]]:
    """`(action, destination)` if a move is waiting, else None. Never raises.

    A corrupt or half-written file is treated as no pending move: the cost of
    ignoring one is that somebody clicks the button again, and the cost of
    acting on garbage is moving an index somewhere nobody asked for.
    """
    import json

    path = Path(project_path) / PENDING_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        action = str(data["action"])
        destination = Path(str(data["destination"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if action not in {MOVE, ADOPT, FRESH} or not str(destination).strip():
        return None
    return action, destination


def clear_pending(project_path: Path) -> None:
    """Remove the record. Safe to call when there is none."""
    try:
        (Path(project_path) / PENDING_FILE).unlink(missing_ok=True)
    except OSError as exc:                                   # noqa: BLE001
        # Not fatal, but it must be visible: a pending file that cannot be
        # deleted would replay the move on every start.
        log.warning("could not clear the pending move file: {}", exc)


def looks_like_an_index(path: Path) -> bool:
    """True if `path` already holds something this application wrote.

    Deliberately generous - any one of the five subdirectories counts. A
    half-built index is still an index, and telling somebody their populated
    folder is empty is the more dangerous mistake.
    """
    try:
        return any((path / name).is_dir() for name in INDEX_SUBDIRS)
    except OSError:
        return False


def _folder_bytes(path: Path) -> int:
    """Size on disk, never raising. Used for reporting only."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        # A folder that vanishes or refuses listing mid-walk: the size is only
        # for the wording of the report, so a short answer beats no move.
        pass
    return total


def _same_volume(a: Path, b: Path) -> bool:
    """Whether a rename would suffice rather than a copy.

    On Windows the drive letter is the test. `os.path.splitdrive` is used rather
    than comparing `st_dev`, because the destination usually does not exist yet.
    """
    return os.path.splitdrive(str(a))[0].lower() == os.path.splitdrive(str(b))[0].lower()


def models_stay_put(source: Path, model_cache: Optional[Path]) -> bool:
    """True when the models folder was chosen in Settings, away from the index.

    Such a folder is not part of the index: it is downloads, rebuilt on demand,
    and somebody who sent it to another drive meant that. So a move leaves it
    where it is and keeps its `.env` line. The default - the models inside the
    index folder - is not a choice and moves with the index as before.
    """
    if model_cache is None or not str(model_cache).strip():
        return False
    try:
        return Path(model_cache).resolve() != (Path(source) / "models").resolve()
    except OSError:
        # Cannot tell where it is: leave it alone rather than move something
        # somebody may have put on purpose.
        return True


def _pinned_model_cache(env_file: Path) -> Optional[Path]:
    """The `MODEL_CACHE` value in `.env`, or None. Never raises."""
    try:
        text = Path(env_file).read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "MODEL_CACHE" and value.strip().strip('"\'').strip():
            return Path(value.strip().strip('"\''))
    return None


def plan_move(
    source: Path, destination: Path, action: str, *, keep_models: bool = False,
) -> MoveReport:
    """What `perform_move` would do, without touching anything.

    Raises `ERR_CONFIG_INVALID` for a request that cannot be honoured, so the
    caller finds out before any file has been touched rather than half way
    through.

    `keep_models` means the models folder is a separate choice (see
    `models_stay_put`): its subdirectory is not moved and its `.env` line is kept.
    """
    source = Path(source)
    destination = Path(destination)

    if action not in {MOVE, ADOPT, FRESH}:
        raise_error("ERR_CONFIG_INVALID", "core.index_move",
                    key="action", reason=f"{action!r} is not move, adopt or fresh")

    if destination == source:
        raise_error(
            "ERR_CONFIG_INVALID", "core.index_move",
            key="DATA_PATH", reason="the destination is the current location",
            suggestion="Choose a different folder, or cancel - there is nothing to do.",
        )

    # A destination inside the source cannot work: moving the parent into its
    # own child is either an infinite recursion or a destroyed index, depending
    # on which tool does it. Caught here rather than discovered at 40GB.
    try:
        if destination.resolve().is_relative_to(source.resolve()):
            raise_error(
                "ERR_CONFIG_INVALID", "core.index_move",
                key="DATA_PATH",
                reason=f"{destination} is inside the current index folder",
                suggestion="Choose a folder that is not inside the index itself.",
            )
    except (OSError, ValueError):
        pass                                     # unresolvable path: let the move fail honestly

    if action == ADOPT and not looks_like_an_index(destination):
        raise_error(
            "ERR_CONFIG_INVALID", "core.index_move",
            key="DATA_PATH",
            reason=f"{destination} does not contain an index",
            suggestion="Pick 'start a new index here' instead, or choose the "
                       "folder that holds vectors, fts and state.",
        )

    if action == MOVE and looks_like_an_index(destination):
        raise_error(
            "ERR_CONFIG_INVALID", "core.index_move",
            key="DATA_PATH",
            reason=f"{destination} already contains an index",
            suggestion="Moving onto it would overwrite it. Choose 'use the "
                       "index already there', or pick an empty folder.",
        )

    present: tuple[str, ...] = ()
    size = 0
    if action == MOVE:
        present = tuple(
            name for name in INDEX_SUBDIRS
            if (source / name).is_dir() and not (keep_models and name == "models")
        )
        if not present:
            raise_error(
                "ERR_CONFIG_INVALID", "core.index_move",
                key="DATA_PATH", reason=f"there is no index at {source} to move",
                suggestion="Pick 'start a new index here' instead.",
            )
        size = _folder_bytes(source)

    return MoveReport(
        action=action,
        source=source,
        destination=destination,
        moved=present,
        bytes_moved=size,
        same_volume=_same_volume(source, destination),
        env_keys_removed=_keys_removed(keep_models),
        performed=False,
    )


def _keys_removed(keep_models: bool) -> tuple[str, ...]:
    """The derived keys a move removes from `.env`; MODEL_CACHE only if not kept."""
    return tuple(key for key in DERIVED_KEYS if not (keep_models and key == "MODEL_CACHE"))


def perform_move(
    source: Path,
    destination: Path,
    action: str,
    env_file: Path,
    on_progress: Optional[Callable[[str], None]] = None,
) -> MoveReport:
    """Move (or adopt, or start fresh), then repoint `.env`. Returns what it did.

    **Order matters and is not negotiable.** Files first, `.env` last: if the
    copy fails half way, `.env` still points at the old location and the old
    location is still intact, so the application starts normally and nothing has
    been lost. Writing `.env` first and then failing is the case that orphans an
    index, which is the bug this module was written to fix.
    """
    keep = models_stay_put(source, _pinned_model_cache(env_file))
    plan = plan_move(source, destination, action, keep_models=keep)
    say = on_progress or (lambda message: None)

    if action == MOVE:
        say(f"Moving {plan.bytes_moved / 1e9:.1f}GB from {source}...")
        _move_tree(source, destination, plan.moved, say)
        say("Move complete. Verifying...")
        _verify(destination, plan.moved)
    else:
        destination.mkdir(parents=True, exist_ok=True)
        if action == FRESH:
            for name in INDEX_SUBDIRS:
                (destination / name).mkdir(parents=True, exist_ok=True)

    # **`.env` is written last, and the derived keys are removed rather than
    # rewritten.** Removing makes them derive from DATA_PATH, so this is the
    # last time anybody has to think about them. See the module docstring.
    values: dict[str, object] = {"DATA_PATH": str(destination)}
    for key in _keys_removed(keep):
        values[key] = None
    apply_values(Path(env_file), values)
    say(f"Configuration updated: DATA_PATH={destination}")

    log.info("index location changed", action=action,
             source=str(source), destination=str(destination))

    return MoveReport(
        action=plan.action,
        source=plan.source,
        destination=plan.destination,
        moved=plan.moved,
        bytes_moved=plan.bytes_moved,
        same_volume=plan.same_volume,
        env_keys_removed=_keys_removed(keep),
        performed=True,
    )


def _move_tree(
    source: Path, destination: Path, names: Iterable[str],
    say: Callable[[str], None],
) -> None:
    """Move each subdirectory, one at a time.

    Per-subdirectory rather than one `shutil.move` of the whole folder, for two
    reasons. It reports progress on something that can take an hour across
    drives; and a destination that already exists for an unrelated reason - a
    `Data` folder holding something else - does not cause the whole move to
    refuse before it starts.

    `shutil.move` renames when it can and copies when it cannot, which is what
    makes a same-drive move instant and a cross-drive move honest about being a
    copy.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = destination / name
        if target.exists():
            raise AppErrorException(make_error(
                "ERR_CONFIG_INVALID", "core.index_move",
                key=str(target), reason="already exists",
                suggestion="Empty the destination folder, or choose another one. "
                           "Nothing has been moved.",
            ))
        say(f"  {name}...")
        try:
            shutil.move(str(source / name), str(target))
        except OSError as exc:
            # Whatever has already moved stays where it went. `.env` has not
            # been touched yet, so the application still starts against the old
            # location - and the message says exactly what to do about it.
            raise AppErrorException(make_error(
                "ERR_CONFIG_INVALID", "core.index_move",
                key=name, reason=f"could not be moved ({exc.__class__.__name__})",
                details=str(exc),
                suggestion=f"The index has not been repointed, so the application "
                           f"still works. Some folders may now be under "
                           f"{destination} - move them back beside the others, or "
                           f"re-run the move once the cause is fixed.",
            )) from exc


def _verify(destination: Path, names: Iterable[str]) -> None:
    """Every moved subdirectory arrived. Cheap, and the whole point of a move.

    Not a checksum: `shutil.move` either renames (atomic) or copies and raises.
    This catches the case where it silently landed somewhere else, which is what
    a wrong destination looks like from the outside.
    """
    missing = [name for name in names if not (destination / name).exists()]
    if missing:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.index_move",
            key=str(destination),
            reason=f"{', '.join(missing)} did not arrive",
            suggestion="The configuration has NOT been changed, so the "
                       "application still points at the old location. Check the "
                       "destination drive and try again.",
        ))
