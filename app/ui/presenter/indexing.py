"""Indexing progress, runs started elsewhere, skips and the index summary.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from app.ui.presenter.formatting import (
    format_count,
    format_eta,
    format_size,
    format_when,
)
from app.ui.presenter.rows import MAIL_TOTAL_CAP, capped_total


@dataclass
class SkipGroup:
    """One reason files were skipped, with its count and its fix."""

    code: str
    count: int
    message: str = ""
    suggestion: str = ""
    action_type: str = ""
    action_payload: Optional[str] = None
    examples: list[str] = field(default_factory=list)

    @property
    def retryable(self) -> bool:
        """Worth offering a retry button for.

        A locked file is worth retrying - the program holding it has probably
        closed. A scanned PDF is not: it will still have no text layer, and a
        retry button that cannot possibly help is worse than none.
        """
        from app.core.file_state import RETRY_CODES   # 2026-10-04, code review: one list

        return self.code in RETRY_CODES


#: What a group row says where one file's row would name its own detail.
#:
#: **2026-09-30.** `group_skips` filled only `path`, `ext` and `folder`, so 22
#: of the 55 registered sentences reached the "N files skipped - review" panel
#: with their placeholders showing: "'these files' was skipped after {took}:
#: {reason}." A group has no single time, reason or member - each file's own
#: is on its row - so these stand in for them. `_GROUP_ANY` covers a field
#: added to the registry later, so a brace can never reach the page again.
GROUP_WORDS: dict[str, str] = {
    "took": "the time allowed",
    "reason": "each file's own reason is on its row",
    "member": "a file",
    "binary": "a converter",
    "model": "chosen in Settings",
    "allowed": "the programs Leasha lists",
    "depth": "a set number of",
}
_GROUP_ANY = "…"
_PLACEHOLDER = re.compile(r"\{\w+\}")


def _no_placeholders(text: str) -> str:
    return _PLACEHOLDER.sub(_GROUP_ANY, text or "")


def group_skips(
    summary: dict[str, int],
    *,
    examples: Optional[dict[str, list[str]]] = None,
) -> list[SkipGroup]:
    """Turn a skip tally into rows for the "N files skipped — review" panel.

    Sorted by count, because on a 100GB run the panel's job is to answer "what
    is the biggest thing I am missing?" - and 4,000 scanned PDFs matters more
    than one locked spreadsheet however recently the spreadsheet failed.

    The message and fix come from the error registry, so the panel says exactly
    what every other surface says about the same code.
    """
    from app.core.errors import make_error

    groups: list[SkipGroup] = []
    for code, count in summary.items():
        if not count:
            continue
        error = make_error(code, "ui", path="these files", ext="?", folder="?",
                           **GROUP_WORDS)
        groups.append(SkipGroup(
            code=code,
            count=int(count),
            message=_no_placeholders(error.message),
            suggestion=_no_placeholders(error.suggestion),
            action_type=error.action_type.value,
            action_payload=error.action_payload,
            examples=list((examples or {}).get(code, []))[:5],
        ))

    groups.sort(key=lambda group: (-group.count, group.code))
    return groups

# ---------------------------------------------------------------------------
# The progress bar
#
# Its own function because the arithmetic was wrong and nothing could have
# caught it: a bar that moves too slowly still moves, and "the progress does not
# feel right" is the only symptom anybody can report.
# ---------------------------------------------------------------------------

#: What the detail line says while the run is in a stretch it cannot count,
#: keyed by `pipeline.PHASE_*`. A phase absent from here - `reading`, or none
#: yet - is drawn from the counts as usual.
#:
#: **Every one of these used to be a bar that did not move.** Loading the
#: model, catching up on a previous run, building the vector index at the end:
#: minutes each on a large index, with no tick at all, so the page showed
#: whatever it showed last and the run was taken for a hang. A busy bar with a
#: sentence beside it is the honest picture - something is happening, this is
#: what, and nobody can say how long.
PHASE_WORDS: dict[str, str] = {
    "model": "Getting the search model ready…",
    "word_index_check": "Checking the word index…",
    "catch_up": "Finishing what the last run left undone…",
    "planning": "Working out which folders to read…",
    # 2026-09-29, `read_order` "newest": the whole walk before the first read.
    # The count is the headline's "files seen", climbing as it goes.
    "scanning": "Finding files, to read the newest first…",
    "media": "Reading videos and recordings…",
    "tidying": "Tidying up the index…",
    "vector_index": "Organising the index so searches stay quick…",
    "word_index": "Tidying the word index so searches stay quick…",
}

#: Said the moment Start is pressed, before any run exists. The window works
#: out the tuning numbers first, which on a cold cache means asking Windows
#: about the disk and the graphics card - seconds, sometimes more.
PREPARING_WORDS = "Getting ready to index…"


def phase_words(stats: Any) -> str:
    """The sentence for the phase `stats` is in, or "" when it is counting."""
    return PHASE_WORDS.get(str(getattr(stats, "phase", "") or ""), "")


def progress_for(stats: Any, *, total_estimate: int = 0) -> tuple[int, int]:
    """`(value, maximum)` for the bar, given a progress tick.

    **The bug this replaces:** the numerator was `unchanged + skipped`, which
    leaves out `indexed` - the files the run is actually doing work on. A first
    index of a fresh corpus has nothing unchanged and little skipped, so the bar
    sat near zero for hours while the log showed thousands of files done. The
    one job of a progress bar is to say how far through it is, and it was
    reporting the opposite of the truth on the run where it matters most.

    Everything the walker has finished with counts, whichever way it finished.

    The denominator is what the walker has found *so far*, which grows as it
    goes. Honest, and it moves - unlike a fixed total nobody can know before the
    walk completes, or an indeterminate bar that spins forever and reads as
    stuck.

    A phase with nothing to count (`phase_words`) is always indeterminate:
    whatever the numbers say, none of them are moving.
    """
    if phase_words(stats):
        return 0, 0
    done = (
        int(getattr(stats, "indexed", 0) or 0)
        + int(getattr(stats, "unchanged", 0) or 0)
        + int(getattr(stats, "skipped", 0) or 0)
    )
    seen = int(getattr(stats, "seen", 0) or 0)

    # **`seen` is not a total until the walk ends, and using it as one put the
    # bar at 100% within seconds of starting.**
    #
    # The work queue is bounded - that is what stops a fast walker building a
    # million-entry list in memory - so the walker can never get more than a
    # queue-length ahead of the workers. `seen` is therefore always roughly
    # `done` plus a queue, and `done / seen` climbs to near 1 almost immediately
    # and stays there: at 10,000 files done and 10,256 seen it reads 97%, with
    # the entire rest of the corpus still to come. The bar was measuring how
    # full the queue was, not how far through the run it was.
    #
    # `(0, 0)` is Qt's indeterminate range: a moving barber pole, which is the
    # honest answer to "how far through are we" while the size of the job is
    # still unknown. It is only tolerable because the text beside it carries
    # live counts - an indeterminate bar on its own does read as stuck.
    #
    # A caller that genuinely knows the total - from a counting pre-pass - can
    # still pass `total_estimate` and get a real percentage from the first tick.
    if not total_estimate and not getattr(stats, "walk_complete", False):
        return 0, 0

    total = max(int(total_estimate or 0), seen, done, 1)
    # Clamped: `seen` can lag `done` by a tick, and a bar drawn past its own
    # maximum is a Qt warning on the console and a full bar on screen while the
    # run is plainly still going.
    return min(done, total), total


# ---------------------------------------------------------------------------
# A run this window did not start
#
# `app.cli index` and the window are two processes now that the run lock is
# separate from the window lock, so an index may be under way with nothing in
# this process knowing about it. `is_running()` was `self._worker is not None`,
# which meant the bar sat at zero and Start stayed enabled while a run was
# plainly in progress - and pressing Start then hit the lock and produced an
# error for something the window should simply have been showing.
#
# The pipeline publishes a snapshot on every checkpoint. These turn that record
# into what goes on screen, and they are here rather than in the view because
# every one of them is a decision with an edge case worth a test.
# ---------------------------------------------------------------------------

#: A published run older than this is treated as finished, whatever it says.
#:
#: The lock is the authority and this is only a safety net for the gap between
#: a process dying and anything noticing: the mutex is released immediately, but
#: a window polling on a timer can still hold the last record it read. Generous,
#: because a checkpoint is every 50 files or two seconds and a single huge file
#: - a 30GB archive - can legitimately sit between two of them for minutes.
STALE_RUN_S = 900.0


def external_snapshot(record: Any) -> Any:
    """The published `stats` dict as something `progress_for` can read.

    `progress_for` and `progress_text` take an object and use `getattr`, which
    is right for the in-process tick. Converting here rather than teaching them
    about dictionaries keeps one set of progress rules for both paths - the
    alternative is two, which drift, and the bar is the thing that has already
    been wrong three times.
    """
    from types import SimpleNamespace

    found = (record or {}).get("stats") if isinstance(record, dict) else None
    values = dict(found or {})
    # The two the progress rules need and a published record may predate.
    values.setdefault("walk_complete", False)
    values.setdefault("skipped_by_code", {})
    values.setdefault("skipped_roots", ())
    values.setdefault("notices", ())
    return SimpleNamespace(**values)


def external_is_live(record: Any, *, now: Optional[float] = None) -> bool:
    """Is this published record describing a run that is still going?

    Answers on the record alone. The caller pairs it with the lock, which is the
    part an operating system maintains and therefore the part that is true.
    """
    import time as _t

    if not isinstance(record, dict):
        return False
    updated = record.get("updated_at") or record.get("started_at")
    try:
        age = (now if now is not None else _t.time()) - float(updated)
    except (TypeError, ValueError):
        return False
    return age <= STALE_RUN_S


def external_run_text(record: Any) -> tuple[str, str]:
    """Headline and detail for a run belonging to another process.

    Deliberately says **whose** run it is. "Indexing…" while the person is
    looking at a window they did not start it from invites them to press Stop
    expecting it to be theirs - and Stop does now reach it, so the sentence has
    to make clear what will be stopped.
    """
    owner = str((record or {}).get("owner") or "another process")
    headline, detail = progress_text(external_snapshot(record))
    return f"{headline} — started by {owner}", detail


def start_blocked_reason(record: Any, *, locked: bool) -> str:
    """Why Start is unavailable, or `""` when it is available.

    **`locked` comes from the mutex and settles it**; the record only supplies
    the words. A record with the lock free is a process that died, which must
    never be a reason to refuse - that is a paper lock, and it is broken by hand
    at the worst possible moment.
    """
    if not locked:
        return ""
    owner = str((record or {}).get("owner") or "another process")
    return (f"An index run started by {owner} is already in progress. "
            f"Stop will end it; searching is unaffected.")


def progress_text(stats: Any, *, total_estimate: int = 0, stopping: bool = False) -> tuple[str, str]:
    """`(headline, detail)` for a progress tick.

    Here rather than in the view for the reason this module exists: it is string
    formatting with three branches, and a branch inside a Qt widget can only be
    checked by a person watching an index run at the right moment.
    """
    if stopping:
        # Stopping can take a while on a large file, and a dead button with no
        # explanation reads as a click that was ignored.
        return (
            "Stopping after the current file…",
            f"{format_count(getattr(stats, 'indexed', 0))} indexed so far. "
            "Everything indexed is kept.",
        )

    if getattr(stats, "paused", False):
        # **A pause froze the bar and said nothing.** The resource governor
        # waits for memory to settle or for the machine to be idle, and one
        # observed pause ran for six minutes. Nothing moved and nothing
        # explained why, which is indistinguishable from a hang - and the
        # correct response to a hang is to kill the run.
        #
        # The reason comes from the governor itself rather than being invented
        # here, so it names the real ceiling and the real number.
        return (
            "Paused - waiting for the machine",
            f"{getattr(stats, 'pause_reason', '') or 'Waiting for resources.'} "
            f"{format_count(getattr(stats, 'indexed', 0))} indexed so far; "
            "it will continue on its own.",
        )

    headline = (
        f"{format_count(getattr(stats, 'indexed', 0))} documents  ·  "
        f"{format_count(getattr(stats, 'seen', 0))} files seen  ·  "
        f"{format_count(getattr(stats, 'skipped', 0))} skipped"
    )
    # Counts stay in the headline - they are still true - and the detail says
    # what is happening instead of a rate that is not being earned.
    doing = phase_words(stats)
    if doing:
        return headline, doing

    done, _total = progress_for(stats, total_estimate=total_estimate)

    # **The rate over the last quarter of an hour, not since the start.**
    #
    # An average over four days barely moves, so a run that has slowed to a
    # crawl still reports the number it managed on day one - and that is
    # exactly the moment somebody needs to know. `None` while there is no
    # measurement yet, which `format_eta` turns into "estimating…" rather than
    # into a confident zero.
    recent = getattr(stats, "recent_files_per_minute", None)
    rate = recent if recent is not None else (
        getattr(stats, "files_per_minute", 0) or 0)

    # **An ETA that is allowed to say it does not know.** Without a `scan` there
    # is no total, so the only honest answer to "how much longer" is that
    # nothing here can tell - and no number is better than a wrong one on a job
    # measured in days.
    if total_estimate:
        eta = format_eta(max(0, total_estimate - done), files_per_minute=rate)
    else:
        eta = "time remaining unknown - run `app.cli scan` for a real estimate"

    current = getattr(stats, "current", "") or ""
    # **Say when it is OCR.** A run reading text moves at hundreds of files a
    # minute; OCR moves at seconds *per page*, so the same progress line reads
    # as a stall - and a run that looks stalled gets killed, which is how a
    # four-hour job becomes four hours wasted.
    verb = "reading" if getattr(stats, "ocr_mode", "") != "images" else "reading with OCR"
    reading = f"  ·  {verb} {current}" if current else ""
    if current and getattr(stats, "current_item", 0):
        reading += f" [{stats.current_item:,}]"

    measured = (
        f"{rate:,.0f} files/min (last 15 min)" if recent is not None
        else f"{rate:,.0f} files/min"
    )
    detail = (
        f"{format_count(getattr(stats, 'chunks', 0))} chunks  ·  "
        f"{measured}  ·  {eta}{reading}"
    )
    return headline, detail


def finished_text(stats: Any) -> tuple[str, str]:
    """`(headline, detail)` for a completed run.

    A run stopped by the resource governor reports *its* message rather than a
    tally: "Finished: 400 indexed" after a run that gave up at 4% because the
    disk filled is technically true and completely misleading.
    """
    stopped = getattr(stats, "stopped_early", None)
    if stopped is not None:
        return str(getattr(stopped, "message", stopped)), str(getattr(stopped, "suggestion", ""))

    headline = (
        f"Finished: {format_count(getattr(stats, 'indexed', 0))} indexed, "
        f"{format_count(getattr(stats, 'skipped', 0))} skipped, "
        f"{format_count(getattr(stats, 'deleted', 0))} removed"
    )
    detail = (
        f"{format_count(getattr(stats, 'chunks', 0))} chunks in "
        f"{getattr(stats, 'elapsed_s', 0) or 0:,.0f}s  ·  "
        f"{getattr(stats, 'files_per_minute', 0) or 0:,.0f} files/min, "
        f"{getattr(stats, 'mb_per_minute', 0) or 0:,.1f} MB/min"
    )
    return headline, detail


def resting_headline(stats: Optional[Mapping[str, Any]]) -> str:
    r"""The Indexing page's headline before any run has been shown, or "".

    **Why this exists** (order 0x section 9, review finding 10, 2026-09-27).
    The page opens with the headline "Nothing indexed yet." and only a run -
    starting, finishing, failing - ever replaced it. So a person who opened
    Indexing on an index of seventeen documents, with no run this session, read
    "Nothing indexed yet." directly above "Documents 17": the page answering
    its own first question two ways.

    **No new words.** When the index holds documents this returns the sentence
    the Search page already says about the same count ("17 documents ready to
    search."), taken from `first_contact.greeting` itself rather than copied,
    so the two pages cannot drift apart. It says what is true without claiming
    anything about freshness - a run may well be owed.

    Returns "" for an empty or unreadable index, which the caller reads as
    "keep the starting headline" - that one is still right then.
    """
    # Imported here, not at the top: `first_contact` is a plain-Python module
    # beside the presenter and imports no Qt, but nothing else in this package
    # depends on it, and this keeps that dependency to the one line that needs it.
    from app.ui.first_contact import greeting

    try:
        documents = int((stats or {}).get("files_total", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return ""
    return greeting(documents) if documents > 0 else ""


#: Entities loaded into the Graph panel's table. Above this the table itself
#: becomes the bottleneck rather than the query, and nobody scrolls 500 rows.
GRAPH_TABLE_LIMIT = 500




# ---------------------------------------------------------------------------
# What is in the index, as a panel
#
# The Indexing tab showed one line - documents and chunks - and showed nothing
# at all when the store read failed, because the handler was `except: return`.
# A blank page is the worst possible answer to "is my index working": it is
# indistinguishable from an empty index, a broken one, and a bug.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StatRow:
    """One line of the index summary."""

    label: str
    value: str
    #: A quiet explanation under the value, or "".
    note: str = ""
    #: True when this row is reporting a problem the person should act on.
    warn: bool = False


def index_summary(
    stats: Optional[Mapping[str, Any]],
    vectors: Optional[Mapping[str, Any]] = None,
    *,
    data_path: str = "",
    disk_bytes: Optional[int] = None,
    last_run: str = "",
    next_run: str = "",
    error: str = "",
    warned: Optional[Mapping[str, Any]] = None,
    pictures_not_read: Optional[Mapping[str, Any]] = None,
) -> list[StatRow]:
    """Everything worth knowing about the index, in one list.

    Takes plain mappings rather than a store, so it is tested without a
    database and cannot itself do I/O on the UI thread.

    **It always returns rows.** When the store could not be read it says so, in
    the same shape as any other answer - because the alternative, which is what
    it did before, is an empty panel that looks identical to having no index.
    """
    if error:
        return [StatRow("Could not read the index", error, warn=True)]

    if not stats:
        return [StatRow(
            "Nothing indexed yet",
            "Add a folder in Settings, then press Start indexing.",
        )]

    documents = int(stats.get("files_total", 0) or 0)
    chunks = int(stats.get("chunks_total", 0) or 0)
    embedded = int(stats.get("chunks_embedded", 0) or 0)
    rows_in_vectors = int((vectors or {}).get("rows", 0) or 0)

    out = [
        StatRow("Documents", f"{documents:,}", _by_status(stats.get("files"))),
        StatRow("Searchable passages", f"{chunks:,}"),
    ]

    # **The coverage line.** Meaning-based search runs only on passages that
    # have a vector, and a partly-embedded corpus is findable by exact words
    # only - silently. This is the number that was invisible for weeks while
    # 154 of 3,355 passages were embedded and search quietly did half its job.
    if chunks:
        covered = rows_in_vectors / chunks
        out.append(StatRow(
            "Meaning-based search covers",
            f"{covered:.0%}",
            note=(f"{rows_in_vectors:,} of {chunks:,} passages have a vector. "
                  "The rest are findable by exact words only."
                  if covered < 0.95 else
                  f"{rows_in_vectors:,} vectors"),
            warn=covered < 0.95,
        ))
        if embedded and rows_in_vectors > embedded * 1.05:
            out.append(StatRow(
                "Orphaned vectors",
                f"{rows_in_vectors - embedded:,}",
                note="More vectors than passages. Rebuild to clear them.",
                warn=True,
            ))

    skipped = stats.get("skipped_by_code") or {}
    if skipped:
        worst = sorted(skipped.items(), key=lambda kv: -int(kv[1]))[:3]
        out.append(StatRow(
            "Skipped",
            f"{sum(int(v) for v in skipped.values()):,}",
            note=", ".join(f"{code} ({count})" for code, count in worst),
        ))

    not_read = pictures_not_read_counts(pictures_not_read or {})
    if not_read:
        # Order 0z lane D: "a count on the Indexing page". From the last run's
        # record, like the partly-read row below; not a problem, so no warn.
        from app.ui.presenter.activity import PICTURE_REASON_WORDS

        out.append(StatRow(
            "Pictures in mail not read",
            f"{sum(not_read.values()):,}",
            note=(", ".join(f"{n:,} {PICTURE_REASON_WORDS.get(reason, reason)}"
                            for reason, n in sorted(not_read.items()) if n)
                  + ". Signature logos, icons and dividers; switch this off in "
                    "Index tuning to read every picture."),
        ))

    partial = int((warned or {}).get("ERR_PST_PARTIAL", 0) or 0)
    if partial:
        # Work order `pst-resilience` 3d. **Said, because "indexed" reads as
        # "complete".** An archive with unreadable messages still indexes the
        # rest, and without this row the tab looked the same as after a clean
        # run - the CLI's `Partial` line was the only place it was ever said.
        out.append(StatRow(
            "Mail archives partly read",
            f"{partial:,}",
            note=("Everything readable is searchable. The log names what was "
                  "missed; scanpst.exe repairs a damaged archive, then index again."),
            warn=True,
        ))

    if data_path:
        # Answers "is the index where I told it to be" at a glance, which is
        # otherwise a question requiring the CLI.
        size = f"  ·  {format_size(disk_bytes)}" if disk_bytes else ""
        out.append(StatRow("Index location", data_path, note=f"on disk{size}" if size else ""))

    if last_run:
        out.append(StatRow("Last run", last_run, note=next_run))
    elif next_run:
        out.append(StatRow("Next run", next_run))

    return out


def pictures_not_read_counts(raw: Any) -> dict[str, int]:
    """`pictures_not_read` from the stored `last_run_stats` (or the dict itself), or `{}`.

    Order 0z lane D. Same reading rules as `warned_counts`.
    """
    if isinstance(raw, Mapping):
        counts: Any = raw
    else:
        counts = _last_run_field(raw, "pictures_not_read")
    if not isinstance(counts, Mapping):
        return {}
    out: dict[str, int] = {}
    for reason, count in counts.items():
        try:
            if int(count) > 0:
                out[str(reason)] = int(count)
        except (TypeError, ValueError):
            continue
    return out


def _last_run_field(raw: Any, name: str) -> Any:
    if not raw:
        return None
    import ast

    try:
        stats = ast.literal_eval(str(raw))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    return stats.get(name) if isinstance(stats, dict) else None


def warned_counts(raw: Any) -> dict[str, int]:
    """`warned_by_code` out of the stored `last_run_stats`, or `{}`.

    The run stores `repr()` of a plain dict, which `literal_eval` reads without
    executing anything. A missing, old or unreadable record is not an error -
    it is one row the panel does not draw.
    """
    if not raw:
        return {}
    import ast

    try:
        stats = ast.literal_eval(str(raw))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return {}
    counts = stats.get("warned_by_code") if isinstance(stats, dict) else None
    if not isinstance(counts, dict):
        return {}
    out: dict[str, int] = {}
    for code, count in counts.items():
        try:
            out[str(code)] = int(count)
        except (TypeError, ValueError):
            continue
    return out


def _by_status(files: Any) -> str:
    """`{'INDEXED': 355}` as words. Failures are named; successes are counted."""
    if not isinstance(files, Mapping) or not files:
        return ""
    parts = []
    for status, count in sorted(files.items()):
        label = str(status).lower()
        parts.append(f"{int(count):,} {label}")
    return ", ".join(parts)


def when_text(iso: str) -> str:
    """An ISO timestamp as "2 hours ago", or "" if it is not one.

    A corrupt or absent timestamp must not stop the panel drawing - it is one
    line out of eight, and losing the other seven to it would be a poor trade.
    """
    if not iso:
        return ""
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return format_when(int(moment.timestamp() * 1e9))


def mail_summary(shown: int, leftover: str = "", *, page_size: int = 500,
                 total: Optional[int] = None) -> str:
    """The line under the Mail table: how many, what was capped, what was dropped.

    Moved out of `mail_view.py`, which had **one line** of headroom under the
    250-line guard - and a rule with one line of headroom is a rule about to be
    broken by the next feature, at the moment when the pressure to raise the
    number is highest and the reasoning worst.

    It belongs here anyway: three branches of string formatting inside a Qt
    widget can only be checked by a person looking at a mail tab at the right
    moment, which is how every UI fault in this project has been found.

    **No count when nothing matched, deliberately.** `COUNT(*)` over `messages`
    is instant on a test corpus and is not on two hundred thousand of them, and
    this runs inside the handler that paints results - so the wording covers
    both "no mail indexed" and "no match" rather than paying a query to tell
    them apart.
    """
    if not shown:
        return ("No message matches those filters. If no mail is indexed "
                "yet, add a .pst in Settings and run an index.")

    # **2026-09-29, the total.** The owner: *"when searching for mails the
    # search displays maximum 500 but does not tell how much total"*. `total`
    # is a bounded count from the worker (`tasks.browse_messages_page`); when
    # it is known and larger than the page, the sentence says both numbers.
    # The two sentences below stay as they were for a total that is unknown.
    capped = capped_total(shown, total, "messages", "/from, /after …", cap=MAIL_TOTAL_CAP)
    parts = [capped or f"{shown:,} message{'s' if shown != 1 else ''}"]
    if shown >= page_size and not capped:
        parts.append(
            f"showing the newest {page_size:,} — narrow the filters to see more")
    if leftover:
        parts.append(
            f"'{leftover}' was ignored — this tab filters on the header fields "
            "only. Use Search to look inside messages."
        )
    return "  ·  ".join(parts)


def archive_summary(rows: Any) -> tuple[str, list[str]]:
    r"""`(title, lines)` for the folders a run deliberately did not walk.

    **The counts and the dates are the point.** An archival root is skipped for
    the best of reasons - it turns an incremental pass over a settled 1.5TB
    corpus from hours of fruitless `stat()`ing into seconds - but a folder
    skipped in silence looks exactly like one that was never indexed, and the
    person who reaches that conclusion deletes their index and starts a
    fortnight over.

    So every line says how many files the folder holds and when it was last
    read in full. `("", [])` when nothing was skipped, which the panel reads as
    "hide yourself".
    """
    entries = list(rows or [])
    if not entries:
        return "", []

    lines: list[str] = []
    for row in entries:
        stamp = int(row.get("archived_at", 0) or 0)
        when = format_when(stamp * 1_000_000_000) if stamp else "an unknown date"
        files = int(row.get("files", 0) or 0)
        lines.append(
            f"{row.get('root', '?')} — not walked: {row.get('reason', 'archived')}. "
            f"{format_count(files)} files, last fully indexed {when}."
        )
    lines.append(
        "These are indexed and searchable. They are re-walked when the folder "
        "itself changes, when the interval in Settings elapses, or when you ask."
    )
    plural = "" if len(entries) == 1 else "s"
    return f"{len(entries)} archived folder{plural} skipped", lines
