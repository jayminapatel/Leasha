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
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from app.core.winfs import CLOUD_PLACEHOLDER_MASK

__all__ = [
    "Candidate",
    "WalkConfig",
    "walk",
    "content_hash",
    "has_changed",
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
    #: Raw Windows attribute bits from the stat already performed, so a
    #: placeholder check costs nothing extra. None off Windows.
    attributes: Optional[int] = None

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    @property
    def parent_dir(self) -> str:
        return str(self.path.parent)

    @property
    def is_cloud_placeholder(self) -> bool:
        """True if reading this file would pull it down from the cloud."""
        return bool(self.attributes or 0) and bool(self.attributes & CLOUD_PLACEHOLDER_MASK)

    def sort_key(self) -> tuple[int, str]:
        """Priority first, then path. Deterministic, which is what lets a
        resumed run pick up where the last one stopped."""
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
    include_cloud: bool = False
    follow_symlinks: bool = False
    #: Excluded directories still descended into, by absolute path. Lets a user
    #: index one folder that happens to live under an excluded name.
    force_include: frozenset[str] = field(default_factory=frozenset)

    def resolved_extensions(self) -> frozenset[str]:
        if self.extensions is not None:
            return self.extensions
        from app.extract import supported_extensions

        return supported_extensions()


def _matches_any(name: str, globs: Iterable[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in globs)


def _priority_for(path: Path, priority_roots: Sequence[Path]) -> int:
    """0 for the first nominated root, 1 for the second, and so on; 100 otherwise.

    Ordering the nominated roots by their position lets someone say "my current
    project first, then the archive" and have it mean that.
    """
    text = str(path).lower()
    for index, root in enumerate(priority_roots):
        if text.startswith(str(root).lower()):
            return index
    return 100


def walk(config: WalkConfig) -> Iterator[Candidate]:
    """Yield every indexable file under `config.roots`.

    Lazy by design: a 100GB tree must start producing work immediately rather
    than after a full enumeration, or the pipeline sits idle while the disk is
    read and the first checkpoint is minutes away.

    Directories that cannot be read are skipped silently. A permission error on
    one folder is not a reason to abandon a walk, and the file-level errors that
    matter are raised where they can be attributed to a file.
    """
    extensions = config.resolved_extensions()
    seen: set[str] = set()

    for root in config.roots:
        root = Path(root)
        if not root.exists():
            continue

        for directory, subdirectories, filenames in os.walk(
            root, topdown=True, followlinks=config.follow_symlinks
        ):
            # Pruned here, in place - never filtered afterwards. Descending into
            # a 40,000-file node_modules and discarding it costs the subtree.
            subdirectories[:] = [
                name for name in subdirectories
                if str(Path(directory, name)) in config.force_include
                or (name not in config.exclude_dirs and not _matches_any(name, config.exclude_globs))
            ]

            for filename in filenames:
                if _matches_any(filename, config.exclude_globs):
                    continue

                path = Path(directory) / filename
                if path.suffix.lower() not in extensions:
                    continue

                key = str(path).lower()
                if key in seen:            # overlapping roots must not double-index
                    continue

                try:
                    stat = path.stat()
                except OSError:
                    continue               # vanished or unreadable between listing and stat

                if stat.st_size > config.max_file_bytes or stat.st_size == 0:
                    continue

                attributes = getattr(stat, "st_file_attributes", None)
                candidate = Candidate(
                    path=path,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    priority=_priority_for(path, config.priority_roots),
                    attributes=attributes,
                )

                if candidate.is_cloud_placeholder and not config.include_cloud:
                    continue

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
        return True, (content_hash(candidate.path) if verify_hash else None)

    if candidate.mtime_ns == known_mtime_ns and candidate.size_bytes == known_size:
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
