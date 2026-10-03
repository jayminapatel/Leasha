"""Finding the files worth indexing, and noticing which have changed.

Layer: L3

The walker's job is to be *cheap* and *correct about change*. Cheap, because on
100GB it runs on every incremental pass and must not read file contents to
decide there is nothing to do. Correct about change, because everything
downstream trusts its answer: a file it wrongly calls unchanged is a file whose
edits never reach the index, and nothing anywhere would report that.

**Two-tier change detection.** `mtime_ns` plus `size_bytes` is the cheap tier and
settles almost every file from a `stat()` alone. The hash is the expensive tier
and runs only when the cheap tier says something moved. This matters more than it
sounds: re-running over an unchanged 100GB corpus should cost a directory walk,
not a read of every byte.

The reverse - trusting mtime alone - is the classic trap. Restoring from backup,
`robocopy`, cloud sync and archive extraction all reset mtime while leaving
content identical, and some editors write a file with its old timestamp. So the
hash is what actually decides, and mtime only decides whether to bother hashing.

**Exclusions prune during the walk, never filter after it.** Descending into
`node_modules` or `AppData` and discarding the results afterwards costs the whole
subtree. On a mounted or network drive that is the difference between seconds and
minutes - a lesson this project has already learned once, in a test that hung.

**Cloud placeholders are checked from the stat we already have.** Reading a
OneDrive placeholder is what triggers its download, so the check must happen
before anything opens the file, and it must not cost a second `stat()` per file.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat as stat_module
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from app.core.osbridge.cloudfs import attributes_say_placeholder, is_dataless
from app.core.osbridge.pathnames import case_sensitive, path_key

__all__ = [
    "Candidate",
    "WalkConfig",
    "RECENT_EDIT_WINDOW_S",
    "walk",
    "content_hash",
    "has_changed",
    "PathRules",
    "enclosing_repo",
    "repo_kind_at",
    "own_paths",
    "DEFAULT_EXCLUDE_DIRS",
    "DEFAULT_EXCLUDE_GLOBS",
    "HASH_CHUNK_BYTES",
]

#: Directory names never worth descending into. Pruned by name at every level,
#: which is what makes the walk cheap rather than merely selective.
DEFAULT_EXCLUDE_DIRS = frozenset({
    # ours
    "venv", ".venv", "__pycache__", ".pytest_tmp", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".git", ".svn", ".hg", "build", "dist", ".tox",
    # Windows
    "$Recycle.Bin", "System Volume Information", "Windows", "Program Files",
    "Program Files (x86)", "ProgramData", "AppData", "$WinREAgent",
    # caches that hold copies of things already indexed elsewhere
    "Temp", "Temporary Internet Files", ".cache", "Cache",
})

#: Filename patterns to skip wherever they appear.
DEFAULT_EXCLUDE_GLOBS = (
    "~$*",            # Office lock files: a live copy of a document already indexed
    ".~lock.*",       # LibreOffice equivalent
    "*.tmp", "*.temp", "*.partial", "*.crdownload", "*.part",
    "Thumbs.db", "desktop.ini", ".DS_Store",
)

#: Read size for hashing. Large enough that a 5GB PST is not a million syscalls,
#: small enough not to hold a meaningful buffer per worker.
HASH_CHUNK_BYTES = 1024 * 1024

#: A file modified within this many seconds of now is always hashed, whatever
#: its mtime and size say.
#:
#: Filesystem timestamps are not infinitely precise, and two writes inside one
#: tick produce identical mtimes. If such an edit also preserves the file's size
#: - an overtype, a corrected figure, a swapped word - the cheap tier reports
#: "unchanged" and the new contents never reach the index. Nothing errors and
#: nothing warns; the document simply stops matching what it now says.
#:
#: Windows found this and Linux did not, which is the point: NTFS and the local
#: clock are coarser than ext4's, so the window is real on the target platform
#: and invisible on the development one. Two seconds covers FAT's notorious
#: granularity as well, and the cost is hashing only files touched in the last
#: two seconds - which during an index run is approximately none of them.
RECENT_EDIT_WINDOW_S = 2.0

#: 202626270514 (0l) §2b: the whole run's cloud-content download budget, when
#: nothing else has been configured. 1GB - big enough that a handful of
#: opted-in documents clears it without a second thought, small enough that
#: forgetting to change it never quietly pulls down a whole synced library.
#: A number to override, not a promise about what is reasonable for every
#: machine - `Settings.cloud_content_cap_mb` is where a person actually
#: changes it.
DEFAULT_CLOUD_CONTENT_CAP_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class Candidate:
    """One file worth considering, described without opening it."""

    path: Path
    size_bytes: int
    mtime_ns: int
    #: Lower sorts first. Set from `WalkConfig.priority_roots` so the folders the
    #: user cares about are indexed first and search becomes useful in minutes
    #: rather than after the whole corpus.
    priority: int = 100
    #: Set only when this file was found under one of `WalkConfig.volume_roots`
    #: - a root the caller has identified as a catalogued Offline Media source.
    #: Both travel together and both are None for an ordinary file. See
    #: `app.storage.sqlite_store.volume_synthetic_path`: the pipeline uses
    #: these, not `path`, to build the row's identity - `path` still holds
    #: the letter it happened to be found at *this walk*, which is exactly
    #: what must never become the row's key (1c).
    volume_id: Optional[int] = None
    relative_path: Optional[str] = None
    #: Raw Windows attribute bits from the stat already performed, so a
    #: placeholder check costs nothing extra. None off Windows.
    attributes: Optional[int] = None
    #: Order 0x section 1 (2026-09-27): the BSD file flags from that same stat,
    #: which is where macOS marks an iCloud file that is not downloaded
    #: (`SF_DATALESS`). None on Windows and Linux, whose stat has no such field,
    #: so nothing changes there. (UNCONFIRMED on macOS.)
    flags: Optional[int] = None
    #: Whether anything can read this file's *contents*.
    #:
    #: **False is not a failure and not a skip.** Extension routing decides
    #: what is read; it does not decide what exists. A `.mp4` gets a row with
    #: its name, path, size and date so it is findable - see
    #: `WalkConfig.name_only` - and nothing opens it.
    readable: bool = True
    #: This file was **deliberately** put back in the queue, so its settled row
    #: must not send it home again.
    #:
    #: A skipped file whose date and size have not moved is settled: whatever
    #: could not read it last time cannot read it now, and re-parsing 100k
    #: scanned PDFs on every incremental run to rediscover known failures is
    #: hours per night for nothing. Two passes contradict that on purpose -
    #: `_locked_candidates`, because the program holding the file may have
    #: closed, and `_no_text_layer_candidates`, because the OCR pass exists to
    #: read exactly the files the text pass could not. Both set this.
    #:
    #: **A flag rather than an allow-list of skip codes**, because the code
    #: alone cannot answer it: `ERR_NO_TEXT_LAYER` is settled during the text
    #: pass and precisely the work during the images pass. The pass that
    #: re-queues the file is the only thing that knows, so it is the thing that
    #: says.
    retry: bool = False

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    @property
    def parent_dir(self) -> str:
        return str(self.path.parent)

    @property
    def is_cloud_placeholder(self) -> bool:
        """True if reading this file would pull it down from the cloud."""
        # `attributes_say_placeholder`, not `& CLOUD_PLACEHOLDER_MASK` (2026-09-30):
        # the mask called every checked `.exe` a cloud file - see cloudfs.
        if attributes_say_placeholder(self.attributes):
            return True
        # A Mac's iCloud placeholder: the same question, asked of the flags the
        # stat already returned - reading the file to find out would download it.
        return is_dataless(self.flags)

    def sort_key(self) -> tuple[int, str]:
        """Priority first, then path. Deterministic, which is what lets a
        resumed run pick up where the last one stopped.

        **Still plain `.lower()`, on every system (order 0x section 7b).** This
        only decides the *order* files are worked through, never whether two
        of them are the same file, so a case-sensitive Mac folder holding both
        `A.txt` and `a.txt` loses nothing: the two sort next to each other and
        both are indexed. Keeping it unchanged keeps the Windows order - and
        so every resumed run - exactly as it was.
        """
        return (self.priority, str(self.path).lower())


@dataclass
class WalkConfig:
    """What to walk and what to ignore."""

    roots: Sequence[Path]
    #: None means "every registered extractor's extensions", resolved at walk
    #: time so adding an extractor does not require touching config.
    extensions: Optional[frozenset[str]] = None
    priority_roots: Sequence[Path] = ()
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS
    exclude_globs: Sequence[str] = DEFAULT_EXCLUDE_GLOBS
    #: Files above this are skipped outright. A 20GB disk image has no text and
    #: hashing it would cost minutes.
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    #: Off by default: reading a placeholder downloads it, and pointing this at
    #: a synced library would quietly pull down the entire thing.
    #:
    #: **Superseded by the three fields below (202626270514 §2b) - kept only
    #: so a caller that still sets it gets the old, whole-run behaviour**:
    #: every root opted in, at the default cap. New callers should set
    #: `cloud_content_roots` directly; `__post_init__` folds this in once,
    #: at construction, never checked again during the walk itself.
    include_cloud: bool = False
    #: `{root, normalised: opted in}` - a root not present here is names-only
    #: for cloud placeholders under it, whatever `include_cloud` says. The
    #: opt-in half of the trap `is_cloud_placeholder` guards: a folder is
    #: never included in the download budget just because the *run* included
    #: cloud content somewhere else in it.
    cloud_content_roots: frozenset[str] = field(default_factory=frozenset)
    #: The whole run's cumulative byte budget for content actually hydrated
    #: from a cloud placeholder - **never per folder**. "A scan must never
    #: silently pull 500GB onto a 512GB laptop" is a statement about the
    #: machine, not about any one folder, so the cap has to be a session
    #: total: two opted-in folders must not each get their own full budget.
    cloud_content_cap_bytes: int = DEFAULT_CLOUD_CONTENT_CAP_BYTES
    #: **Output, like `stat_failures`.** Bytes actually spent hydrating
    #: cloud placeholders so far this run - `walk()` increments it in place;
    #: nothing else writes to it. Read after the run to report what was
    #: actually pulled, and checked during the run so the cap is enforced
    #: against real cumulative spend, not estimated up front.
    cloud_bytes_spent: int = 0

    def __post_init__(self) -> None:
        if self.include_cloud and not self.cloud_content_roots:
            self.cloud_content_roots = frozenset(
                str(root).rstrip("\\/").lower() for root in self.roots)
    follow_symlinks: bool = False
    #: Excluded directories still descended into, by absolute path. Lets a user
    #: index one folder that happens to live under an excluded name.
    force_include: frozenset[str] = field(default_factory=frozenset)
    #: Absolute directory paths never descended into, whatever they are called.
    #:
    #: **This is how the application avoids indexing itself.** `LOG_PATH`
    #: defaults to `<project>\\logs`, so pointing an indexed root at the project
    #: folder had the indexer reading its own log file - while writing to it.
    #: Excluding by *name* would have been wrong twice over: it would miss a log
    #: directory anywhere else, and it would hide a `logs` folder that genuinely
    #: belongs to the person.
    exclude_paths: frozenset[str] = field(default_factory=frozenset)
    #: Yield files nothing can read, so they are indexed by name.
    #:
    #: **Asked for**: *"the files search should include all files, not just the
    #: ones we have read the content of"*. Before this the walk simply skipped
    #: them, so a `.zip`, a `.mp4` or an `.exe` produced no row at all - not a
    #: name, not a skip-ledger line, nothing anywhere saying it had been passed
    #: over. Invisible is the worst of the three possible answers.
    #:
    #: Costs one `stat` the walk already performs and one INSERT. Nothing is
    #: opened, hashed or extracted - see `Candidate.readable`.
    name_only: bool = True
    #: Files the walk could not `stat`, counted by reason. **A sink, written by
    #: the walk and read by the pipeline at the end of the run**, on the same
    #: pattern as `Pipeline._repo_roots`.
    #:
    #: It exists because `except OSError: continue` made a file disappear
    #: completely - no row, no skip, no count, nothing in any log. That is fine
    #: for the common case, a file deleted between the listing and the stat, and
    #: it is not fine for the other one: on stock Windows every path longer than
    #: 260 characters fails here, so a deep tree can lose thousands of files
    #: with no number moving anywhere. A count is the difference between "this
    #: corpus has no such files" and "nobody ever looked".
    stat_failures: dict[str, int] = field(default_factory=dict)
    #: Extension -> how many files were dropped for being over
    #: `max_file_bytes`. The same sink pattern as `stat_failures`: before this
    #: existed, a file too big to read and not covered by `size_exempt` hit a
    #: bare `continue` with no row, no skip code and no log line - which is
    #: exactly how 17 of 20 `.pst` files in a 128GB Outlook archive vanished
    #: from a run on 2026-09-23, all of them over 2GB, none of them reported
    #: anywhere. `.pst`/`.ost` are now in `size_exempt` (see below) so that
    #: specific case no longer drops files at all; this counter is what keeps
    #: any *other* oversized type visible instead of silently absent.
    oversize_dropped: dict[str, int] = field(default_factory=dict)
    #: Roots the walk did not use at all, as `path -> reason`. The same sink
    #: pattern as `stat_failures`, and it exists for the same reason at a much
    #: larger scale.
    #:
    #: **A root that does not exist was skipped by a bare `continue`.** No log
    #: line, no counter, no notice - so a folder on a drive that had not
    #: mounted, or one renamed since it was added, made the entire corpus
    #: vanish from the run while the run reported success. On 2026-08-27 that
    #: produced an index run over a 30GB mail corpus with `seen: 0` and no
    #: explanation anywhere, which reads from the outside as "it did not index
    #: my mail" - because it did not.
    #:
    #: The reason matters as much as the count: "not found" is a folder to fix,
    #: "excluded" is a setting to change, and they are not the same
    #: conversation.
    root_problems: dict[str, str] = field(default_factory=dict)
    #: Repository roots found during the walk, written here as they are seen,
    #: as `root_path -> kind`.
    #:
    #: A mutable output parameter, which is not the shape this module prefers -
    #: but `walk()` is a generator and a second return value is not available.
    #: The alternative, a second pass over the tree purely to find `.git`, costs
    #: a full walk of a 100GB corpus to learn something the first walk already
    #: had in its hands.
    repo_sink: Optional[dict[str, str]] = None
    #: `{root path, lowercased and without a trailing separator: volume_id}`.
    #: A root in `roots` that is also a key here is a catalogued Offline Media
    #: source's current mount point - resolved by the caller (§1b: "at the
    #: last moment") immediately before the walk starts, never inside `walk()`
    #: itself, which has no business knowing about Windows volumes. Every
    #: candidate found under it gets `relative_path` set and `volume_id`
    #: carried straight through.
    volume_roots: dict[str, int] = field(default_factory=dict)

    def excluded_paths_lower(self) -> frozenset[str]:
        """`exclude_paths`, normalised once rather than per directory entry."""
        return frozenset(
            str(Path(p)).rstrip("\\/").lower() for p in self.exclude_paths
        )

    def resolved_names(self) -> frozenset[str]:
        """Whole filenames worth indexing - `Makefile`, `Dockerfile`, dotfiles.

        **A second question, asked separately on purpose.** The walk filters on
        `path.suffix`, and every one of these has none: `Path("Makefile").suffix`
        is `""`, and so is `Path(".gitignore").suffix`, because Python reads a
        leading dot as the start of the stem. Folding them into the extension
        set would mean comparing `""` against a set containing `""`, which
        admits every extensionless file on the disk - a `.tmp` scratch file, a
        Unix binary, a lock file.
        """
        from app.extract.base import supported_names

        return supported_names()

    def resolved_extensions(self) -> frozenset[str]:
        r"""Every extension the application can index, from all three tiers.

        **This used to return the code registry alone, and that quietly
        disabled two entire tiers of the format system.**

        `config/extractors.toml` describes three ways a file gets read: an
        extension routed to a registered extractor (tier 1), an external
        converter (tier 2), and a parser written in Python (tier 3). Only tier 3
        puts an extension in `REGISTRY`. So the walker - which skips any file
        whose suffix is not in this set - never offered a single file to tiers 1
        or 2, and both were configured, reported as ready by `doctor`, shown as
        enabled in Settings, and dead.

        That was 27 text and code types (`.vb`, `.kt`, `.swift`, `.tex`,
        `.conf`, `.ics`...) and **every converter format**: `.doc`, `.ppt`,
        `.dwg`, `.pub`, `.wpd` and the iWork three. Installing LibreOffice could
        never have made any difference, because no `.doc` file ever reached the
        converter.

        `is_enabled` is applied last so a route switched off in configuration
        stays off - the whole point of the switch.
        """
        if self.extensions is not None:
            return self.extensions

        from app.core.formats import load_rules
        from app.extract import media, supported_extensions

        # **Video and audio are off unless their own switch is on**
        # (`VIDEO_INDEXING_ENABLED`, `AUDIO_TRANSCRIPTION_ENABLED`), whatever
        # else is true. Subtracted last and on every path out, so the extensions
        # a switched-off feature owns can never leak in through configuration -
        # and so "off" means the walk is exactly what it was before video
        # existed: a `.mp4` is found by name and nothing reads it.
        switched_off = media.disabled_extensions()

        known = set(supported_extensions())
        try:
            rules = load_rules()
        except Exception:                        # noqa: BLE001
            # A broken config must not stop the walk finding the file types the
            # code itself knows about.
            return frozenset(known) - switched_off

        known |= set(rules.extensions)           # tier 1: routed by config
        known |= set(rules.converters)           # tier 2: external converters
        return rules.enabled_extensions(known) - switched_off


def own_paths(settings: object) -> frozenset[str]:
    """Directories this application writes to, and must never index.

    **The indexer was reading its own log file while writing to it.**
    `LOG_PATH` defaults to `<project>\\logs`, so an indexed root pointed at the
    project folder swept it up - along with the SQLite index, the vector store
    and the model cache if `DATA_PATH` happens to sit under a root too.

    Every field is read defensively: this is called with a `Settings`, but it
    must not become the reason a run cannot start if one of them is absent.
    """
    names = (
        "data_path", "log_path", "state_path", "cache_path",
        "model_cache", "vector_path",
    )
    found: set[str] = set()
    for name in names:
        value = getattr(settings, name, None)
        if value:
            found.add(str(value))
    # `fts_db` is a file; its directory is what must not be walked.
    #
    # **Split on both separators, not `Path.parent`.** These paths are written
    # on Windows and read back anywhere, and `PurePosixPath` treats the whole
    # of `D:\Data\fts\knowledge.db` as one filename - so the parent came out as
    # `.`, which excludes nothing. The third time this project has been caught
    # by that; `sqlite_store._basename` exists for the same reason.
    database = getattr(settings, "fts_db", None)
    if database:
        text = str(database).replace("\\", "/").rstrip("/")
        parent = text.rpartition("/")[0]
        if parent:
            found.add(str(Path(parent)))
    return frozenset(found)


def _matches_any(name: str, globs: Iterable[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in globs)


#: How much of a `.git` *file* to read. It holds one short `gitdir:` line; a
#: larger read only matters when the file is not what it claims to be, and then
#: the answer is "not a repository" either way.
GITDIR_READ_BYTES = 4096


def _git_file_kind(path: Path) -> Optional[str]:
    """`submodule`, `worktree`, or None if this is not a `.git` pointer file.

    **`.git` is not always a directory.** In a submodule or a linked worktree
    it is a *file* holding `gitdir: ../.git/modules/foo`. Anything that looks
    only at the subdirectory list walks straight past both.

    Unreadable, empty, binary or unparseable all mean **not a repository**,
    never an error. Detection is a convenience laid on top of a walk that has
    real work to do; the posture is the one `walk()` already states for a
    directory it cannot read - a permission error on one folder is not a reason
    to abandon a walk.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(GITDIR_READ_BYTES)
    except OSError:
        return None

    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None                      # binary: not a gitdir pointer

    if not text.startswith("gitdir:"):
        return None

    target = text[len("gitdir:"):].strip().replace("\\", "/")
    if not target:
        return None
    # A submodule's real git directory lives under the parent's
    # `.git/modules/`; a linked worktree's points anywhere else.
    return "submodule" if "/modules/" in target else "worktree"


def _detect_repo(directory: str, subdirectories: list[str],
                 filenames: list[str]) -> Optional[str]:
    """The `kind` of repository rooted at `directory`, or None.

    Both lists are the ones `os.walk` has already built, so this costs a
    membership test and, at most, one 4KB read per directory that holds a
    `.git` file.
    """
    if ".git" in subdirectories:
        return "work"
    if ".git" in filenames:
        return _git_file_kind(Path(directory) / ".git")
    return None


def enclosing_repo(start: Path, *, ceiling: Optional[Path] = None) -> Optional[Path]:
    """The nearest ancestor of `start` holding a `.git`, or None.

    An indexed root may sit *below* a repository root. `D:\\SearchProject\\app`
    has no `.git` beneath it and every file under it is still in a repository,
    so a walk that only looks downwards attributes none of them.

    Pure and cheap: a `stat` per ancestor, up to the drive root. `ceiling`
    exists so the tests can bound it, not for production.
    """
    start = Path(start)
    ceiling = Path(ceiling) if ceiling is not None else None

    for candidate in [start, *start.parents]:
        marker = candidate / ".git"
        try:
            if marker.is_dir() or (marker.is_file() and _git_file_kind(marker)):
                return candidate
        except OSError:
            pass                         # unreadable: keep climbing
        if ceiling is not None and candidate == ceiling:
            break
    return None


def repo_kind_at(root: Path) -> Optional[str]:
    """The `kind` of the repository rooted exactly at `root`, or None."""
    marker = Path(root) / ".git"
    try:
        if marker.is_dir():
            return "work"
        if marker.is_file():
            return _git_file_kind(marker)
    except OSError:
        pass
    return None


def _priority_for(path: Path, priority_roots: Sequence[Path]) -> int:
    """0 for the first nominated root, 1 for the second, and so on; 100 otherwise.

    Ordering the nominated roots by their position lets someone say "my current
    project first, then the archive" and have it mean that.
    """
    text = str(path).lower()
    for index, root in enumerate(priority_roots):
        # **At a folder boundary** (2026-09-29): a bare prefix test put
        # `C:\Docs2\a.txt` inside a first folder `C:\Docs`. Harmless while the
        # priority only reordered a 256-file window; wrong once the whole run
        # is sorted by it.
        prefix = str(root).lower().rstrip("\\/")
        if text == prefix or (text.startswith(prefix)
                              and text[len(prefix):len(prefix) + 1] in ("\\", "/")):
            return index
    return 100


#: Windows' classic path ceiling. A path at or over this that will not `stat`
#: is almost certainly refused for its length rather than because it is gone -
#: `ERROR_PATH_NOT_FOUND` is what Windows returns for both, which is why the
#: length has to be checked rather than the error code.
_WINDOWS_PATH_LIMIT = 260


def _record_stat_failure(config: WalkConfig, path: Path, exc: OSError) -> None:
    """Count one unreadable file, classified so the number means something.

    "4,812 files could not be read" is a fact nobody can act on. "4,812 files
    have paths too long for Windows" names the setting to change.
    """
    text = str(path)
    if len(text) >= _WINDOWS_PATH_LIMIT:
        reason = "path over 260 characters"
    elif isinstance(exc, PermissionError):
        reason = "permission denied"
    elif isinstance(exc, FileNotFoundError):
        reason = "vanished during the walk"
    else:
        reason = type(exc).__name__
    config.stat_failures[reason] = config.stat_failures.get(reason, 0) + 1


#: `{root, normalised: True}` - which folders the owner has explicitly opted
#: in to cloud-content indexing. The same home `ui:root_modes`
#: (`app/index/archives.py`) uses for a per-root preference: read once at
#: startup, written back whenever the folders panel changes, never touched
#: mid-walk.
CLOUD_CONTENT_STATE_KEY = "ui:cloud_content_roots"


def load_cloud_content_roots(raw: str) -> frozenset[str]:
    """`{normalised root}` from the stored JSON. Never raises.

    An unreadable record means "nothing is opted in", which is the safe
    answer - the same reasoning `archives.load_modes` gives for defaulting
    to Live rather than failing the run.
    """
    if not raw:
        return frozenset()
    try:
        record = json.loads(raw)
    except Exception:                              # noqa: BLE001
        return frozenset()
    if not isinstance(record, list):
        return frozenset()
    return frozenset(str(root).rstrip("\\/").lower() for root in record)


def dump_cloud_content_roots(roots: Iterable[str]) -> str:
    """Only the opted-in roots are stored, sorted for a stable diff - the
    same shape `archives.dump_modes` writes for archive-mode roots."""
    return json.dumps(
        sorted({str(root).rstrip("\\/").lower() for root in roots}))


def walk(config: WalkConfig, seen: Optional[set[str]] = None) -> Iterator[Candidate]:
    """Yield every indexable file under `config.roots`.

    Lazy by design: a 100GB tree must start producing work immediately rather
    than after a full enumeration, or the pipeline sits idle while the disk is
    read and the first checkpoint is minutes away.

    Directories that cannot be read are skipped silently. A permission error on
    one folder is not a reason to abandon a walk, and the file-level errors that
    matter are raised where they can be attributed to a file.

    **`seen` is shared, not private, and that is a memory fix.** Every path the
    walk yields was being held three times over: here, to stop overlapping roots
    double-indexing; in `Pipeline._candidates`, to stop a re-queued file being
    yielded twice; and in `Pipeline._produce`, for the prune pass to know what
    this run covered. Three sets of the same several million lowercased strings,
    roughly a gigabyte each at five million files, all for one question.

    Passing one set in answers it once. Callers that do not care keep their own
    private set by omitting the argument, so `walk(config)` behaves exactly as
    it always did.
    """
    if seen is None:
        seen = set()
    extensions = config.resolved_extensions()
    names = config.resolved_names()
    # Normalised once for the whole walk, not per directory entry.
    blocked = config.excluded_paths_lower()
    # **A film is not "too big to read"** the way a disk image is. Reading one
    # means PyAV reading its header and decoding only its keyframes - neither
    # touches most of its bytes - and a family archive is exactly where the
    # multi-gigabyte files are. Only extensions whose switch is on are exempt.
    from app.extract.media import media_extensions
    # **Nor is a `.pst` "too big to read" the way a disk image is.** It is
    # read message by message through libpff or MAPI, never loaded whole into
    # memory, so the generic per-file ceiling - sized for a monolithic file
    # read in one gulp - never applied to it in spirit. It did apply in code:
    # a `.pst` over `max_file_bytes` (2GB by default) hit the same bare
    # `continue` as an oversized disk image, and 17 of a 20-file archive went
    # missing this way with no skip code and no log line. `.ost` gets the
    # same exemption for the same reason, even though it is read through
    # Outlook alone. `.olm` (an Outlook for Mac export, work order 0x §8b)
    # joins them: it is read one message at a time out of the zip by
    # `app/extract/email_olm.py`, never whole, and a real mailbox export is
    # routinely larger than the ceiling. `.mbox` too (2026-09-27, found while
    # building `.olm`): `email_mbox.py` reads one message at a time and its own
    # docstring promises a 10GB Google Takeout mbox is indexed - but until now a
    # Takeout export over the ceiling was dropped here before it was ever read.
    STREAMED_ARCHIVE_EXTENSIONS = frozenset({".pst", ".ost", ".olm", ".mbox"})
    size_exempt = (
        (media_extensions() & extensions)
        | (STREAMED_ARCHIVE_EXTENSIONS & extensions)
    )

    for root in config.roots:
        root = Path(root)
        root_volume_id = config.volume_roots.get(str(root).rstrip("\\/").lower())
        if not root.exists():
            # **Recorded, not merely skipped.** See `root_problems`: this
            # `continue` used to be silent, and a single mistyped or
            # disconnected folder took the whole run with it without leaving a
            # mark anywhere.
            config.root_problems[str(root)] = "not found"
            continue
        if str(root).rstrip("\\/").lower() in blocked:
            # The root itself is excluded. Pruning only filters subdirectories,
            # so without this an indexed root pointed straight at the log or
            # index directory would still be walked in full.
            config.root_problems[str(root)] = "excluded by a setting"
            continue
        # **Ask this folder's disk whether letter case counts, before listing
        # a single file in it** (order 0x section 7b). The answer is
        # remembered, and `path_key` below - and the pipeline's clean-up pass,
        # which looks paths up in the same `seen` set - both read it. On
        # Windows this returns at once without touching the disk: NTFS ignores
        # case, and the keys stay exactly as they always were.
        case_sensitive(root)

        # **A root may be one file** (2026-10-03, the owner: the list of
        # folders to index should take a file too - one archive out of a
        # folder of them). It is walked as a listing of its own folder that
        # names only it, so every rule below - the exclusions, the extension
        # table, the size ceiling, name-only, placeholders - applies to it
        # exactly as it would had it been met inside a folder. A file root
        # that does not exist is "not found" above, like a folder.
        if root.is_file():
            steps: Iterable = [(str(root.parent), [], [root.name])]
        else:
            steps = os.walk(root, topdown=True, followlinks=config.follow_symlinks)

        for directory, subdirectories, filenames in steps:
            # **Detection happens here, and the position is load-bearing.**
            #
            # Before the prune, because `.git` is in `DEFAULT_EXCLUDE_DIRS` and
            # the line below is about to remove it from `subdirectories` - the
            # walker has stood next to this evidence on every pass and thrown
            # it away.
            #
            # And before the `for filename in filenames` loop, because
            # `os.walk(topdown=True)` yields a repository root together with
            # the files sitting directly in it. Detecting afterwards leaves
            # exactly those files unattributed while everything in
            # subdirectories is attributed correctly - which reads as
            # flakiness and gets blamed on the pipeline. `test_repos_
            # acceptance.py::T3` is that case.
            if config.repo_sink is not None:
                kind = _detect_repo(directory, subdirectories, filenames)
                if kind is not None:
                    config.repo_sink.setdefault(str(Path(directory)), kind)

            # Pruned here, in place - never filtered afterwards. Descending into
            # a 40,000-file node_modules and discarding it costs the subtree.
            #
            # `exclude_paths` is checked case-insensitively: these are absolute
            # paths from configuration, compared against paths from the
            # filesystem, and on Windows the same directory routinely appears
            # with different casing in the two.
            #
            # **Left case-insensitive on a Mac and Linux too (order 0x 7b).**
            # These are the application's own folders (its logs, its index).
            # The only thing a case-sensitive disk could change is a *second*
            # folder whose name differs from one of those only by case - and
            # leaving that out errs on the safe side: the indexer never reads
            # its own files, which is what this list exists for.
            subdirectories[:] = [
                name for name in subdirectories
                if str(Path(directory, name)).lower() not in blocked
                and (
                    str(Path(directory, name)) in config.force_include
                    or (name not in config.exclude_dirs
                        and not _matches_any(name, config.exclude_globs))
                )
            ]

            for filename in filenames:
                if _matches_any(filename, config.exclude_globs):
                    continue

                path = Path(directory) / filename
                readable = (path.suffix.lower() in extensions
                            or filename.lower() in names)
                if not readable and not config.name_only:
                    continue

                # **`path_key`, not `.lower()`** (order 0x section 7b). On
                # Windows it *is* `str(path).lower()`, byte for byte. On a
                # case-sensitive Mac or Linux folder it keeps the case, so
                # `Report.docx` and `report.docx` - two real files there - are
                # two keys and both are indexed, instead of the second being
                # dropped here as a "duplicate" of the first.
                key = path_key(path)
                if key in seen:            # overlapping roots must not double-index
                    continue

                try:
                    stat = path.stat()
                except OSError as exc:
                    # **Counted, because this is how a file becomes invisible.**
                    #
                    # `continue` alone produced no row, no skip, no count and no
                    # log line - the exact silent absence `NAME_ONLY` was built
                    # to eliminate, arriving through a different door. Most of
                    # these are genuinely files that vanished between the
                    # listing and the stat, which is ordinary; but on stock
                    # Windows **every path over 260 characters lands here too**,
                    # and a deep folder can lose thousands of files without a
                    # single number moving.
                    #
                    # `doctor` probes the long-path policy separately. This is
                    # the count that says whether it matters on this corpus.
                    _record_stat_failure(config, path, exc)
                    continue

                # **Too big or empty means "do not read it", not "pretend it
                # is not there".** A 40GB disk image and a zero-byte marker are
                # both real files somebody may go looking for; what they are
                # not is files worth opening. In name-only mode they become
                # name-only rows; otherwise they are dropped here, and
                # `oversize_dropped` is what keeps that drop a counted,
                # logged fact rather than a silent one - see its docstring.
                too_big = (stat.st_size > config.max_file_bytes
                           and path.suffix.lower() not in size_exempt)
                if (too_big or stat.st_size == 0) and not config.name_only:
                    if too_big:
                        ext = path.suffix.lower() or "(none)"
                        config.oversize_dropped[ext] = (
                            config.oversize_dropped.get(ext, 0) + 1)
                    continue

                attributes = getattr(stat, "st_file_attributes", None)
                flags = getattr(stat, "st_flags", None)
                relative_path = None
                if root_volume_id is not None:
                    # **Relative to the root actually being walked**, not to
                    # any other member of `config.roots` - a volume scan is
                    # always one source's current mount point on its own, but
                    # this stays correct even if a caller ever mixes one in
                    # alongside ordinary roots.
                    try:
                        relative_path = str(path.relative_to(root)).replace("\\", "/")
                    except ValueError:
                        relative_path = str(path).replace("\\", "/")
                candidate = Candidate(
                    path=path,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    priority=_priority_for(path, config.priority_roots),
                    attributes=attributes,
                    flags=flags,
                    readable=readable and not too_big and stat.st_size > 0,
                    volume_id=root_volume_id,
                    relative_path=relative_path,
                )

                if candidate.is_cloud_placeholder:
                    # **202626270514 §2b: opted in AND inside the budget, or
                    # names-only - never one without the other.** A root not
                    # in `cloud_content_roots` is names-only regardless of
                    # the cap; an opted-in root is names-only too, the
                    # moment the *run's* cumulative spend would cross
                    # `cloud_content_cap_bytes` - the trap this item names:
                    # two opted-in folders sharing one budget, not each
                    # getting their own.
                    # A stored setting's key (`CLOUD_CONTENT_STATE_KEY`), in
                    # the Windows format on every system on purpose (order 0x
                    # 7b): it is compared with the saved list, which was
                    # written lower-cased, and it only decides whether a
                    # cloud file's *content* is downloaded - never whether
                    # the file is indexed at all.
                    root_key = str(root).rstrip("\\/").lower()
                    opted_in = root_key in config.cloud_content_roots
                    within_cap = (config.cloud_bytes_spent + candidate.size_bytes
                                 <= config.cloud_content_cap_bytes)
                    if opted_in and within_cap:
                        config.cloud_bytes_spent += candidate.size_bytes
                    else:
                        # **202626270514 3a: never read placeholder bytes, but
                        # never make the file invisible either.** `continue`
                        # here used to drop the candidate entirely - no row, no
                        # skip code, no count, the exact "invisible is the
                        # worst of the three possible answers" bug `name_only`
                        # was built to eliminate (see `WalkConfig.name_only`'s
                        # own docstring), arriving through a different door: a
                        # name-only OneDrive or Google Drive library indexed as
                        # if it were empty. `LOCAL_KNOWLEDGE_GRAPH_V2.md`'s own
                        # architecture section already documented the intended
                        # shape - SKIPPED with `ERR_CLOUD_ONLY`, findable by
                        # name, reported and actionable - `app.cli extract`
                        # already raises it; the real walk never did. Forcing
                        # `readable=False` here is what makes `Pipeline.
                        # _extract_worker` take that branch (see there) without
                        # opening the file - the check stays exactly where it
                        # was, on the stat already performed.
                        candidate = replace(candidate, readable=False)

                seen.add(key)
                yield candidate


def content_hash(path: Path, *, chunk_bytes: int = HASH_CHUNK_BYTES) -> str:
    """blake2b of the file's bytes, streamed.

    blake2b rather than SHA-256 because it is faster on the same hardware and
    nothing here is a security boundary - this answers "are these the same
    bytes?", not "did an adversary tamper with them?". Streamed because the
    corpus contains multi-gigabyte archives and reading one into memory to hash
    it would be its own outage.
    """
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_bytes)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _modified_recently(candidate: Candidate, *, now: Optional[float] = None) -> bool:
    """True if the file was written within the timestamp resolution window."""
    current = now if now is not None else time.time()
    return (current - candidate.mtime_ns / 1_000_000_000) < RECENT_EDIT_WINDOW_S


def has_changed(
    candidate: Candidate,
    *,
    known_mtime_ns: Optional[int],
    known_size: Optional[int],
    known_hash: Optional[str] = None,
    verify_hash: bool = True,
) -> tuple[bool, Optional[str]]:
    """Has this file changed since it was indexed? Returns `(changed, new_hash)`.

    The cheap tier decides whether to pay for the expensive one:

    * never seen -> changed, and hashed so the record starts out complete;
    * `mtime_ns` and `size_bytes` both match -> unchanged, **no read at all**;
    * either moved -> hash it, and compare against the stored hash.

    That last step is what makes `robocopy`, a restore from backup, cloud sync
    and archive extraction cheap: they all reset mtime while leaving content
    identical, so the hash matches and nothing downstream re-runs. Trusting
    mtime alone would re-index the whole corpus every time one of those happened.

    `verify_hash=False` skips the read and trusts the cheap tier, for a fast pass
    that accepts the risk of missing a same-size, same-mtime edit.
    """
    if known_mtime_ns is None or known_size is None:
        if not verify_hash:
            return True, None
        try:
            return True, content_hash(candidate.path)
        except OSError:
            # Unreadable right now - locked by the program that owns it, or gone.
            # Changed, unhashed: the pipeline attempts it and produces a proper
            # per-file AppError. Letting this escape took down an entire index
            # run on the first real use, because it is raised in the walker
            # thread where one exception ends the walk for every remaining file.
            return True, None

    if candidate.mtime_ns == known_mtime_ns and candidate.size_bytes == known_size:
        if _modified_recently(candidate):
            # Too new to trust the cheap tier - see RECENT_EDIT_WINDOW_S. Pay
            # for the hash rather than risk missing an edit permanently.
            if not verify_hash:
                return True, None
            try:
                fresh = content_hash(candidate.path)
            except OSError:
                return True, None
            return fresh != known_hash, fresh
        return False, known_hash

    if not verify_hash or known_hash is None:
        return True, None

    try:
        fresh = content_hash(candidate.path)
    except OSError:
        # Unreadable right now - treat as changed so the pipeline attempts it
        # and produces a proper per-file AppError rather than silently skipping.
        return True, None

    return fresh != known_hash, fresh


# ---------------------------------------------------------------------------
# One path at a time (work order 0z, F1: the folder watch)
# ---------------------------------------------------------------------------

#: Mailboxes read message by message. `walk` names the same four when it
#: exempts them from the size ceiling; kept in step by `test_folder_watch.py`.
STREAMED_MAILBOXES = frozenset({".pst", ".ost", ".olm", ".mbox"})


class PathRules:
    r"""`walk`'s decisions, asked about one path instead of a whole tree.

    The folder watch (`app/index/folder_watch.py`) is told "this file changed"
    and must answer the questions `walk` answers while it lists a folder: is
    this inside a folder the walk never enters, is the name one it skips, can
    anything read it, is it a cloud placeholder. **The rules are `walk`'s,
    restated, not new ones** - `test_folder_watch.py` walks a tree both ways
    and fails if the two ever disagree, which is what stops this drifting
    from the loop above.

    Built once from a `WalkConfig` so the per-path cost is string tests and,
    for `candidate`, the one `stat` the walk also pays.

    **A cloud placeholder is never read here, whatever the folder's opt-in.**
    `walk` may download an opted-in folder's files within a budget the person
    set for a run they started; a watch runs all day with nobody asking, so it
    records a placeholder by name only and leaves its contents to that run.
    """

    def __init__(self, config: WalkConfig) -> None:
        from app.extract.media import media_extensions

        self.config = config
        self.extensions = config.resolved_extensions()
        self.names = config.resolved_names()
        self.blocked = config.excluded_paths_lower()
        self.size_exempt = (
            (media_extensions() & self.extensions)
            | (STREAMED_MAILBOXES & self.extensions))

    def _folder_excluded(self, folder: Path) -> bool:
        """Would `walk` refuse to enter this one folder? (Not its parents.)"""
        if str(folder).rstrip("\\/").lower() in self.blocked:
            return True
        if str(folder) in self.config.force_include:
            return False
        name = folder.name
        return (name in self.config.exclude_dirs
                or _matches_any(name, self.config.exclude_globs))

    def excluded(self, root: Path, path: Path, *, is_dir: bool = False) -> bool:
        r"""True if `walk` over `root` would never reach `path`.

        Every folder between `root` and `path` is asked about in turn, because
        `walk` prunes as it descends: a file is out of reach the moment any
        folder above it is. `is_dir` says `path` itself is a folder; for a
        path that has gone (so nobody can tell) leave it False, which only
        applies the file-name patterns - patterns `walk` applies to folder
        names as well.

        A path not under `root` at all is excluded.
        """
        root, path = Path(root), Path(path)
        if str(root).rstrip("\\/").lower() in self.blocked:
            return True
        try:
            relative = path.relative_to(root)
        except ValueError:
            return True
        parts = relative.parts
        if not parts:
            return False                     # the root itself
        folder = root
        for part in parts[:-1]:
            folder = folder / part
            if self._folder_excluded(folder):
                return True
        if is_dir:
            return self._folder_excluded(path)
        return _matches_any(parts[-1], self.config.exclude_globs)

    def candidate(self, root: Path, path: Path) -> Optional[Candidate]:
        """The `Candidate` `walk` would yield for this file, or None.

        None when the walk would not reach it, would skip it, or it cannot be
        looked at right now (gone again, or not an ordinary file).
        """
        root, path = Path(root), Path(path)
        if self.excluded(root, path):
            return None
        config = self.config
        readable = (path.suffix.lower() in self.extensions
                    or path.name.lower() in self.names)
        if not readable and not config.name_only:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        if not stat_module.S_ISREG(stat.st_mode):
            return None
        too_big = (stat.st_size > config.max_file_bytes
                   and path.suffix.lower() not in self.size_exempt)
        if (too_big or stat.st_size == 0) and not config.name_only:
            return None
        candidate = Candidate(
            path=path,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            priority=_priority_for(path, config.priority_roots),
            attributes=getattr(stat, "st_file_attributes", None),
            flags=getattr(stat, "st_flags", None),
            readable=readable and not too_big and stat.st_size > 0,
        )
        if candidate.is_cloud_placeholder:
            candidate = replace(candidate, readable=False)
        return candidate
