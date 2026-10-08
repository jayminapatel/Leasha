r"""Retry with a longer time limit: read a group of timed-out files again.

Layer: L3

Work order 0z, item F3. Lane B (`app/index/file_watch.py`) gives every file a
time limit, and a file past it is recorded as `ERR_FILE_TIMEOUT` - status
TimedOut - and **settled**: an unchanged file is not read again, because
reading it again under the same limit would end the same way. That leaves one
case with nothing to press: a file that was not damaged, only large. Raising
"Time limit per file" for every file, for good, to get forty big PDFs read is
the wrong trade; this is the narrow one.

## What a retry is

An index run that **walks nothing**. Its only candidates are the timed-out
files of one group, read back from the ledger with one indexed query
(`SqliteStore.timed_out_files`), in the way the pictures pass reads its
scanned PDFs back and the media tail reads its queue
(`app/index/media_backlog.py` - the same shape, a `Pipeline` whose candidates
come from the ledger). So nothing outside the group is read, and nothing is
pruned, because a run that saw forty files has no opinion about the rest.

A **group** is a file type - `files.ext`, as `SqliteStore.timed_out_groups`
lists them - or every timed-out file. By type, because the limit is by type:
text and code get one limit, documents ten times it, mailboxes and archives a
limit on no progress.

## The longer limit

`factor` times the usual limit - whichever of the two settings applies to the
file - **for this run only and for these files only**. Nothing is saved: the
settings on the Tuning shelf are not touched, and the next ordinary run reads
with them. It is a multiple, not a number of seconds, because one retry can
hold a text file, a PDF and a mailbox, whose limits differ by design; "four
times as long" means the right thing for each. A limit that is switched off
(0) stays off.

* A file that **times out again** stays TimedOut, and its sentence says what
  it was given: "... on this retry (4 times the usual 20 min)".
* A file that **has changed** since it timed out (its size or date differ from
  the row) is not the file that timed out. It is read as any changed file is,
  with the usual limit.
* A file that is **no longer there** is left to the ordinary clean-up.

A retry reads each file whatever pass the settings would give it to - a
picture held for the pictures pass, a recording queued for the tail - because
the person asked for these files, now. The catch-up work an ordinary run does
first (missing vectors, captions, faces) and the media tail are left to the
next ordinary run for the same reason.
"""

from __future__ import annotations

import dataclasses
import functools
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from app.core.logging import logger
from app.core.osbridge.pathnames import path_key
from app.index.walker import Candidate

__all__ = [
    "ALL_GROUPS",
    "DEFAULT_FACTOR",
    "MAX_FACTOR",
    "MIN_FACTOR",
    "RESOLVED_KEY",
    "RetryPlan",
    "RetryTimedOut",
    "child_arguments",
    "clamp_factor",
    "group_words",
    "normalise_group",
    "plan",
    "retry_pipeline",
    "said",
    "was_retry",
]

log = logger.bind(component="index.timed_out_retry")

#: How many times the usual limit a retry gives, unless the person says
#: otherwise. A constant default, not a saved setting (non-negotiable 11): it
#: is asked for at each retry - the box beside the button, `--time-limit-factor`
#: - and four is "clearly longer" without being "all night" (a 20-minute
#: document limit becomes 80 minutes).
DEFAULT_FACTOR = 4
#: 1 is allowed from the command line: "read them again with the limit as it
#: is now", for somebody who has just raised the setting itself.
MIN_FACTOR = 1
#: A bound, so a slip of the hand is not a run with no limit in all but name.
#: For no limit at all there is a setting: 0 on the Tuning shelf.
MAX_FACTOR = 100

#: What the command line and the child process write for "every group".
ALL_GROUPS = "*"

#: The key a retry leaves in `IndexStats.resolved` - "what this run was
#: configured with" - so whoever is handed the finished stats can tell. It
#: travels with the stats from the separate indexing process like every other
#: field (`run_events.StatsRebuilder`). See `was_retry`.
RESOLVED_KEY = "retry_timed_out"


@dataclasses.dataclass(frozen=True)
class RetryTimedOut:
    """One retry: which group, and how many times the usual limit."""

    #: A file type as `files.ext` holds it (`"pdf"`; `""` for files with no
    #: extension), or None for every timed-out file.
    group: Optional[str] = None
    factor: float = DEFAULT_FACTOR


@dataclasses.dataclass
class RetryPlan:
    """What a retry will read, worked out from the ledger and the disk."""

    candidates: list[Candidate] = dataclasses.field(default_factory=list)
    #: `path_key` of each file unchanged since it timed out: the ones given
    #: the longer limit.
    longer: frozenset[str] = frozenset()
    #: Files whose size or date differ from the row - read with the usual limit.
    changed: int = 0
    #: Rows whose file is not where it was.
    missing: int = 0


def clamp_factor(value: Any) -> float:
    """`value` as a usable factor, within `MIN_FACTOR`..`MAX_FACTOR`."""
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_FACTOR)
    if factor != factor:                            # NaN
        return float(DEFAULT_FACTOR)
    return max(float(MIN_FACTOR), min(float(MAX_FACTOR), factor))


def normalise_group(text: Any) -> Optional[str]:
    """A group as typed (`.PDF`, `pdf`, `*`) as the store names it, or None
    for every group. `""` stays `""`: files with no extension."""
    if text is None:
        return None
    value = str(text).strip()
    if value.lower() in (ALL_GROUPS, "all"):
        return None
    return value.lstrip(".").lower()


def child_arguments(retry: Optional[RetryTimedOut]) -> list[str]:
    """The `app.cli index` flags that ask for this retry, for the window's
    separate indexing process (`child_run.child_command(extra=...)`). None for
    no retry: no flags.

    Written with `=` so an empty group - files with no extension - is still
    an argument, and so nothing after it can be taken for the group.
    """
    if retry is None:
        return []
    group = ALL_GROUPS if retry.group is None else retry.group
    return [f"--retry-timed-out={group}",
            f"--time-limit-factor={clamp_factor(retry.factor):g}"]


def was_retry(stats: Any) -> bool:
    """Was this finished run a retry of timed-out files?

    For what follows a run in the window. A retry reads a handful of the
    slowest files in the index, by construction: it is not a measurement of
    the machine (so the tuner must not learn from it), and it is not a text
    pass (so nothing should be said about the pictures pass after it).
    """
    resolved = getattr(stats, "resolved", None)
    return bool(isinstance(resolved, dict) and resolved.get(RESOLVED_KEY))


def group_words(group: Optional[str]) -> str:
    """The group in a sentence: `.pdf`, `no-extension`, or nothing for all."""
    if group is None:
        return ""
    return f".{group}" if group else "no-extension"


def plan(store: Any, group: Optional[str]) -> RetryPlan:
    """Read the group back from the ledger and look at each file once.

    One indexed query and one `stat` per row - run on the run's own thread,
    never the window's.
    """
    out = RetryPlan()
    longer: set[str] = set()
    for row in store.timed_out_files(group):
        path = Path(row["path"])
        try:
            stat = path.stat()
        except OSError:
            out.missing += 1
            continue
        unchanged = (stat.st_size == row["size_bytes"]
                     and stat.st_mtime_ns == row["mtime_ns"])
        if unchanged:
            longer.add(path_key(path))
        else:
            out.changed += 1
        # `retry=True`: an unchanged timed-out row is a settled skip to
        # `Pipeline._classify`, and this is the run that reads it anyway.
        out.candidates.append(Candidate(
            path=path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
            priority=0, retry=True))
    out.longer = frozenset(longer)
    return out


def said(retry: RetryTimedOut, found: RetryPlan) -> str:
    """The run's opening sentence: what is being read again, and how."""
    kind = group_words(retry.group)
    kind = f"{kind} " if kind else ""
    total = len(found.candidates)
    gone = ""
    if found.missing:
        gone = (f" {found.missing:,} could not be found where "
                f"{'it was' if found.missing == 1 else 'they were'} - a drive "
                "may be disconnected.")
    if not total:
        return f"No timed-out {kind}files were found to read again.{gone}"
    text = (f"Reading {total:,} timed-out {kind}file{'' if total == 1 else 's'} "
            f"again, with {retry.factor:g} times the usual time limit.")
    if found.changed:
        text += (f" {found.changed:,} "
                 f"{'has' if found.changed == 1 else 'have'} changed since, and "
                 f"{'is' if found.changed == 1 else 'are'} read with the usual limit.")
    return text + gone


def _retry_pipeline_class() -> type:
    """The `Pipeline` subclass for a retry, built on demand for the same reason
    `media_backlog._backlog_pipeline_class` is: `pipeline` imports this module."""
    from app.index.pipeline import Pipeline

    class RetryPipeline(Pipeline):
        """`Pipeline` whose candidates are one group of timed-out files."""

        def __init__(self, *args: Any, retry: RetryTimedOut, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.retry = dataclasses.replace(
                retry, factor=clamp_factor(retry.factor))
            self._retry_plan = RetryPlan()
            # Nothing is walked, so nothing may be concluded about the rest of
            # the index: no pruning, no archive record, no second look at
            # locked files. `found`: there is nothing to sort a scan of.
            self.config = dataclasses.replace(
                self.config,
                archives=False,
                prune_missing=False,
                retry_locked=False,
                read_order="found",
                walk=dataclasses.replace(self.config.walk, roots=[], extensions=None),
            )

        def run(self, *, on_progress: Any = None) -> Any:
            stats = super().run(on_progress=on_progress)
            # Marked, for whoever is handed the stats - see `was_retry`.
            kind = group_words(self.retry.group) or "every type"
            stats.resolved = dict(
                stats.resolved or {},
                **{RESOLVED_KEY: f"{kind}, {self.retry.factor:g} times the usual limit"})
            return stats

        def _plan_roots(self, stats: Any) -> None:
            # The run's thread, before the producer starts: the one place a
            # run decides what it will look at, and may say so.
            super()._plan_roots(stats)
            self._retry_plan = plan(self.store, self.retry.group)
            sentence = said(self.retry, self._retry_plan)
            stats.add_notice(sentence)
            self._log.info("{}", sentence)

        def _candidates(self) -> Iterator[Candidate]:
            seen = self._seen_paths
            for candidate in self._retry_plan.candidates:
                key = path_key(candidate.path)
                if key not in seen:
                    seen.add(key)
                    yield candidate

        def _time_limit_factor(self, candidate: Candidate) -> float:
            if path_key(candidate.path) in self._retry_plan.longer:
                return float(self.retry.factor)
            return 1.0

        def _ocr_gate(self, candidate: Candidate) -> None:
            # The person asked for these files now; none is handed to another
            # pass. See the module docstring.
            return None

        def _run_enrichment_drains(self, stats: Any) -> None:
            return None

        def _drain_media_backlog(self, stats: Any, on_progress: Any = None) -> None:
            return None

        def _say_if_nothing_was_walked(self, stats: Any) -> None:
            # "No folders are set up" would be untrue: `said` has already
            # told the person there was nothing to read again.
            return None

    return RetryPipeline


def retry_pipeline(retry: RetryTimedOut) -> Callable[..., Any]:
    """What to call instead of `Pipeline(...)` for a retry: same arguments.

    The window and the command line each build their `PipelineConfig` exactly
    as for an ordinary run and hand it to this, so a retry cannot drift from
    a run in anything but what it reads and for how long.
    """
    return functools.partial(_retry_pipeline_class(), retry=retry)
