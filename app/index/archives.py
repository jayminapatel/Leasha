r"""Folders the owner says do not change, and what that lets a run skip.

Layer: L3

**The recurring cost of a big corpus is not indexing it. It is re-walking it.**
Indexing 1.5TB is paid once, because the corpus is historic: fifteen years of
project files, mail archives and scans that were finished before this
application existed. What is paid *every* time is the walk that discovers,
again, that a fifteen-year archive is still fifteen years old - millions of
`stat()` calls that find nothing, plus a prune pass that stats every row in the
database to confirm that no file has been deleted.

At 100GB that is a few minutes and nobody notices. At 1.5TB it is the reason
background indexing has to be scheduled carefully instead of just running.

**A root the owner marks as an archive is walked once, then left alone** -
until one of four things happens, each of them cheap to check:

* the interval elapses (`ARCHIVE_RECHECK_DAYS`);
* a rescan is asked for, from the command line or the Indexing panel;
* the root has never completed a full pass, so there is nothing to trust;
* **the top-level directory's mtime has moved** - one `stat()` per root, which
  catches the ordinary "somebody dropped a folder in" case for the cost of a
  single syscall.

**Per root, never globally.** A corpus is nearly always both: the mail folder
that changes hourly sits beside twelve years of project files that do not. A
global switch would be turned off by whoever owns the first of those and would
then protect nothing.

**Skip cheaply, but never silently.** An archive that is skipped is an archive
nobody is checking, so the failure mode is a file that changed and never got
re-read. That is why every skip carries the file count and the date of the last
full pass, and why `RootPlan.describe` exists: a skipped root that looks
identical to an empty one is how somebody concludes their archive was never
indexed at all.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

__all__ = [
    "LIVE",
    "ARCHIVE",
    "MODES",
    "MODE_STATE_KEY",
    "RECORD_STATE_KEY",
    "DEFAULT_RECHECK_DAYS",
    "ArchiveRecord",
    "RootPlan",
    "load_modes",
    "dump_modes",
    "load_records",
    "dump_records",
    "plan_roots",
    "record_pass",
    "directory_mtime",
    "files_under",
    "normalise",
]

#: Walked on every run, like everything always has been. The default, because
#: it is the answer that is never wrong - only slow.
LIVE = "live"
#: Walked once, then checked cheaply. Faster, and requires a claim from the
#: owner that this application cannot verify for itself.
ARCHIVE = "archive"
MODES = (LIVE, ARCHIVE)

#: `{root: mode}` - what the owner declared, in the Folders-to-index panel.
MODE_STATE_KEY = "ui:root_modes"
#: `{root: ArchiveRecord}` - what the last completed full pass observed.
#: Deliberately a second key: one is a preference and one is evidence, and
#: clearing a preference must not silently discard the evidence behind it.
RECORD_STATE_KEY = "index:archives"

#: How long an archive is trusted without any evidence at all. A month, not a
#: week: the mtime tripwire below is what actually catches change, and this is
#: only the backstop for a change it cannot see - a file edited in place, deep
#: in the tree, leaving every directory above it untouched.
DEFAULT_RECHECK_DAYS = 30


def normalise(root: Any) -> str:
    r"""The key a root is stored under.

    Lower-cased and stripped of a trailing separator, because these are Windows
    paths written by the window and read back by the command line, and the same
    folder routinely appears as `D:\Archive`, `D:\Archive\` and `d:\archive` in
    the two. A mode keyed under one of those and looked up under another is a
    setting that silently does nothing - the failure this project has already
    had three times with Windows paths.

    **A stored key, so it keeps this exact format on every system** (order 0x
    section 7, decided 2026-09-27). The result is written into the index's
    state (`RECORD_STATE_KEY`, and the archive-mode setting beside it) and
    looked up again on the next run; changing it would orphan every
    archive-mode choice already saved on Windows. It is only ever compared
    with other results of this same function, never opened as a path, so on a
    Mac `/Users/me/Archive` simply becomes the key `\users\me\archive` -
    odd to look at, but consistent, and nothing is ever joined or opened with
    it. Folding letter case is kept too: two indexed folders whose names
    differ *only* by case, on a case-sensitive disk, would share one setting -
    a corner too rare to justify a second key format on a Mac.
    """
    return str(root).replace("/", "\\").rstrip("\\").lower()


@dataclass(frozen=True)
class ArchiveRecord:
    """What a completed full pass observed about an archival root."""

    root: str
    #: Unix seconds. The date shown beside every skip.
    archived_at: int
    #: Files the walk found. Shown so a skipped root is plainly not an empty
    #: one - the whole point of saying anything at all.
    files: int
    #: The root directory's own mtime at that moment. The tripwire.
    mtime_ns: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"root": self.root, "archived_at": self.archived_at,
                "files": self.files, "mtime_ns": self.mtime_ns}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Optional["ArchiveRecord"]:
        try:
            return cls(
                root=str(raw["root"]),
                archived_at=int(raw.get("archived_at", 0) or 0),
                files=int(raw.get("files", 0) or 0),
                mtime_ns=int(raw.get("mtime_ns", 0) or 0),
            )
        except Exception:                          # noqa: BLE001 - a bad record is no record
            return None


@dataclass(frozen=True)
class RootPlan:
    """One root, and whether this run is going to walk it.

    `reason` is never empty, for either answer. A skip with no reason is the
    thing this whole module is trying not to be.
    """

    root: str
    mode: str
    walk: bool
    reason: str
    record: Optional[ArchiveRecord] = None

    @property
    def files(self) -> int:
        return self.record.files if self.record else 0

    @property
    def archived_at(self) -> int:
        return self.record.archived_at if self.record else 0

    def describe(self) -> str:
        """One line, for the Indexing panel and the command line alike.

        Says the count and the date on every skip. *"A skipped root that looks
        identical to an empty one is how somebody concludes their archive was
        never indexed."*
        """
        if self.walk:
            return f"{self.root} - {self.reason}"
        when = (
            time.strftime("%Y-%m-%d", time.localtime(self.archived_at))
            if self.archived_at else "an unknown date"
        )
        return (f"{self.root} - skipped: {self.reason}. "
                f"{self.files:,} file(s), fully indexed on {when}.")

    def as_dict(self) -> dict[str, Any]:
        return {"root": self.root, "mode": self.mode, "walk": self.walk,
                "reason": self.reason, "files": self.files,
                "archived_at": self.archived_at}


# ---------------------------------------------------------------------------
# Reading and writing the two state keys
# ---------------------------------------------------------------------------

def load_modes(raw: str) -> dict[str, str]:
    """`{normalised root: mode}` from the stored JSON. Never raises.

    An unreadable record means "everything is live", which is the slow answer
    and never the wrong one. Failing a run because a preference could not be
    parsed would be a poor trade.
    """
    if not raw:
        return {}
    try:
        record = json.loads(raw)
    except Exception:                              # noqa: BLE001
        return {}
    if not isinstance(record, dict):
        return {}
    return {
        normalise(root): str(mode)
        for root, mode in record.items()
        if str(mode) in MODES
    }


def dump_modes(modes: Mapping[str, str]) -> str:
    """The stored form of `MODE_STATE_KEY`: archive roots only, keys sorted."""
    return json.dumps({
        normalise(root): str(mode)
        for root, mode in modes.items()
        # Only archives are stored. `live` is the default, so writing it would
        # leave a file full of entries that mean "no change", and a root
        # removed from the list would keep a mode for ever.
        if str(mode) == ARCHIVE
    }, sort_keys=True)


def load_records(raw: str) -> dict[str, ArchiveRecord]:
    """`{normalised root: ArchiveRecord}` from the stored JSON. Never raises:
    an unreadable record means no pass is trusted, so every root is walked."""
    if not raw:
        return {}
    try:
        found = json.loads(raw)
    except Exception:                              # noqa: BLE001
        return {}
    if not isinstance(found, dict):
        return {}
    records: dict[str, ArchiveRecord] = {}
    for key, value in found.items():
        if not isinstance(value, dict):
            continue
        record = ArchiveRecord.from_dict({"root": value.get("root", key), **value})
        if record is not None:
            records[normalise(key)] = record
    return records


def dump_records(records: Mapping[str, ArchiveRecord]) -> str:
    """The stored form of `RECORD_STATE_KEY`, keys normalised and sorted."""
    return json.dumps(
        {normalise(key): record.as_dict() for key, record in records.items()},
        sort_keys=True,
    )


def directory_mtime(root: Any, *, stat: Optional[Callable[[Any], Any]] = None) -> int:
    """The root directory's own mtime in nanoseconds, or 0 if it cannot be read.

    **One syscall per root, and it is the whole tripwire.** Creating, deleting
    or renaming an entry in a directory updates that directory's mtime on NTFS
    and on every filesystem this runs on - so a folder dropped into an archive
    moves the root's mtime and the next run walks it in full. Editing a file
    that already exists deep inside does not, which is what
    `ARCHIVE_RECHECK_DAYS` is the backstop for.

    0 on failure, and 0 never matches a stored mtime, so an unreadable root is
    walked rather than skipped. The safe direction.
    """
    reader = stat or os.stat
    try:
        return int(reader(str(root)).st_mtime_ns)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def plan_roots(
    roots: Sequence[Any],
    *,
    modes: Mapping[str, str],
    records: Mapping[str, ArchiveRecord],
    now: Optional[float] = None,
    recheck: bool = False,
    recheck_days: int = DEFAULT_RECHECK_DAYS,
    stat: Optional[Callable[[Any], Any]] = None,
) -> tuple[RootPlan, ...]:
    """Which roots this run walks, and why - one entry per root, in order.

    Pure: it takes the two stored dictionaries and a `stat` seam and returns a
    decision. Everything about archival roots that could be wrong is decided
    here, where it can be tested without a filesystem, a database or a
    fortnight.
    """
    current = now if now is not None else time.time()
    plans: list[RootPlan] = []

    for root in roots:
        key = normalise(root)
        mode = modes.get(key, LIVE)
        record = records.get(key)

        if mode != ARCHIVE:
            plans.append(RootPlan(str(root), LIVE, True, "a live folder"))
            continue

        if record is None or not record.archived_at:
            # **Never trusted before it has been earned.** Marking a folder as
            # an archive must not be a way to make it never index at all - the
            # first pass always happens.
            plans.append(RootPlan(
                str(root), ARCHIVE, True,
                "marked as an archive, but it has not had a full pass yet"))
            continue

        if recheck:
            plans.append(RootPlan(
                str(root), ARCHIVE, True, "a rescan was asked for", record))
            continue

        if recheck_days > 0:
            days = (current - record.archived_at) / 86_400
            if days >= recheck_days:
                plans.append(RootPlan(
                    str(root), ARCHIVE, True,
                    f"{days:,.0f} days since the last full pass", record))
                continue

        live_mtime = directory_mtime(root, stat=stat)
        if live_mtime != record.mtime_ns:
            # Something was added, removed or renamed at the top level. One
            # `stat`, and it catches the case that actually happens.
            plans.append(RootPlan(
                str(root), ARCHIVE, True,
                "the folder itself has changed since the last pass", record))
            continue

        plans.append(RootPlan(
            str(root), ARCHIVE, False, "an archive, and nothing has changed", record))

    return tuple(plans)


def record_pass(
    records: Mapping[str, ArchiveRecord],
    plans: Iterable[RootPlan],
    counts: Mapping[str, int],
    *,
    now: Optional[float] = None,
    stat: Optional[Callable[[Any], Any]] = None,
) -> dict[str, ArchiveRecord]:
    r"""Records updated for every archival root this run walked in full.

    **Only roots that were walked, and only after a run that finished.** The
    caller must not call this for an interrupted run: recording a pass that
    stopped a third of the way through would mark an archive as fully indexed
    when two thirds of it has never been read, and nothing afterwards would
    ever look at it again. That is the one failure in this module that is
    silent *and* permanent, so it is stated here and enforced by the caller
    (`Pipeline.run` checks `_interrupted`).

    A live root gets no record. Its mode may change later, and a stale count
    from whenever it happened to be marked as an archive is worse than none.
    """
    updated = dict(records)
    for plan in plans:
        if plan.mode != ARCHIVE or not plan.walk:
            continue
        key = normalise(plan.root)
        updated[key] = ArchiveRecord(
            root=str(plan.root),
            archived_at=int(now if now is not None else time.time()),
            files=int(counts.get(key, 0)),
            mtime_ns=directory_mtime(plan.root, stat=stat),
        )
    return updated


def files_under(path: Any, roots: Sequence[Any]) -> Optional[str]:
    r"""The normalised root containing `path`, longest match first, or None.

    Longest first for the same reason `Pipeline._repo_id_for` does it: one
    indexed root routinely sits inside another, and a first match over an
    unordered list attributes files to whichever was seen first.

    **Prefix comparison on the normalised string, never `Path.parent`.**
    `PurePosixPath(r"D:\Archive\a\b.txt").parent` is `.` - the whole path is one
    filename off Windows - and this project has been caught by that four times.
    """
    text = normalise(path)
    best: Optional[str] = None
    for root in roots:
        key = normalise(root)
        # `text == key`: a root that is one file (2026-10-03) contains itself.
        if key and (text == key or text.startswith(key + "\\")):
            if best is None or len(key) > len(best):
                best = key
    return best
