"""What the run is doing right now, in words: per reader, inside one file, and a heartbeat.

Layer: L5. Part of the presenter package; imports no Qt.

Work order 0x section 3. The index layer records *where* each reader is as
codes and numbers (`IndexStats.workers`, `last_activity`, `stage`,
`embed_batch`/`embed_batches`; see `app/index/live_progress.py` and
`app/extract/progress.py`). This module is the only place those become
sentences, so the Indexing page, a log and anything else say the same thing.

**Everything here takes plain values.** A snapshot from the run in this
process and a dictionary that arrived as a JSON line from the indexer's own
process (0x section 2) read the same: every function looks fields up with
`_get`, which accepts either an object with attributes or a mapping.

**Nothing here changes the existing progress wording.** `progress_text` in
`indexing.py` still says what it always said; these are additional lines for
the page to show beside it.

The functions, for the Indexing page:

* `live_headline(stats)` - one sentence for the busiest reader:
  "Reading Archive2019.pst › Inbox/Projects — message 4,512 of 18,300".
* `worker_lines(stats, now=...)` - one line per reader:
  "Reader 1: backup.zip › mail.mbox › message 812 of 2,000 · 3 min 20 s".
* `heartbeat_line(stats, now=...)` - `(text, quiet)`:
  "Working · last activity 2 s ago", or, after `QUIET_AFTER_S`, a calm
  sentence saying nothing has moved for a while and what that usually means.
* `writer_line(stats)` - what happens behind the readers: "Making text
  searchable by meaning, batch 3 of 12", or "Saving where to resume from".
* `live_view(stats, now=...)` - all four at once, as a `LiveProgress`.
* `now_headline(stats, stopping=...)` - the Indexing page's "now" line
  (0x §4b): `live_headline` when there is something to say that no other
  line on the page already says, else the 0w words, else "".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from app.ui.presenter.activity import READING_WORDS, status_counts_text
from app.ui.presenter.indexing import PHASE_WORDS

__all__ = [
    "LiveProgress",
    "QUIET_AFTER_S",
    "STAGE_WORDS",
    "TRAIL_SEPARATOR",
    "heartbeat_line",
    "inner_trail",
    "live_headline",
    "live_view",
    "now_headline",
    "position_text",
    "since_text",
    "stage_words",
    "telling_folders",
    "worker_lines",
    "writer_line",
]

#: Between the parts of a place inside a file: `backup.zip › mail.mbox`.
#: A display character only - never used to build or split a real path, so it
#: is the same on Windows and macOS.
TRAIL_SEPARATOR = " › "

#: The words for every stage code (`app.index.live_progress.STAGES`). Work
#: order 0x section 3a. Keys are the codes; the codes never change for the
#: sake of wording, and the wording can change here without touching a reader.
STAGE_WORDS: dict[str, str] = {
    "finding": "Finding files",
    "reading": "Reading",
    "opening": "Opening an archive",
    "folder": "Reading a folder",
    "messages": "Reading messages",
    "attachments": "Reading attachments",
    "zip": "Reading inside a zip",
    "ocr": "Reading text from pictures (OCR)",
    "chunking": "Splitting text into passages",
    "handing_over": "Waiting for the index writer",
    "embedding": "Making text searchable by meaning",
    "writing": "Writing to the index",
    "saving_resume": "Saving where to resume from",
}

#: Seconds without any sign of progress before the heartbeat stops saying
#: "working" and says, plainly, that nothing has moved for a while.
#:
#: **A constant, not a setting** (non-negotiable 11). Nobody would tune it, and
#: the warning claims nothing - it never says "stuck" - so a value a little too
#: short costs a calm sentence, not a wrong decision. Sixty seconds because
#: the ordinary slow things - one PDF page through OCR, one large attachment -
#: take seconds to tens of seconds; a minute of nothing is worth mentioning,
#: and still usually harmless. Evidence that would change it: real runs where
#: the sentence appears often during healthy work (raise it), or where a
#: genuine hang went unmentioned for too long (lower it).
QUIET_AFTER_S = 60.0


def _get(source: Any, name: str, default: Any = None) -> Any:
    """A field from an object *or* a mapping. Never raises."""
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(name, default)
    try:
        return getattr(source, name, default)
    except Exception:                            # noqa: BLE001 - a torn read
        return default


def _int(value: Any) -> int:
    """`int(value)`, or 0 for anything that is not a number."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def stage_words(code: str) -> str:
    """The words for one stage code, or "" for a code nobody has worded."""
    return STAGE_WORDS.get(str(code or ""), "")


def since_text(seconds: float) -> str:
    """A short, plain length of time: "2 s", "3 min 20 s", "1 h 5 min".

    Whole seconds only. "2.4 s ago" is precision the heartbeat does not have -
    it is refreshed about once a second.
    """
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def position_text(frame: Any) -> str:
    """Where a reader is inside one container: "message 4,512 of 18,300".

    "message 812" without "of ..." when the total is not known - readers never
    guess one. "" when nothing inside has been reached yet.
    """
    n = _int(_get(frame, "n"))
    if n <= 0:
        return ""
    unit = str(_get(frame, "unit", "") or "item")
    total = _get(frame, "total")
    if total is not None and _int(total) >= n:
        return f"{unit} {n:,} of {_int(total):,}"
    return f"{unit} {n:,}"


def _segments(frames: list[Any], top: str = "") -> list[str]:
    """The places in a frame stack, outermost first, without repeating a name.

    Each frame adds its own file name, unless the frame above has just named
    it (a zip's member *is* the mbox that opens the next frame), then the
    place inside it (a `.pst` folder, a zip member).
    """
    segments: list[str] = []
    if top:
        segments.append(top)
    for frame in frames:
        name = str(_get(frame, "name", "") or "")
        last = segments[-1] if segments else ""
        if name and name != last and name != last.replace("\\", "/").rsplit("/", 1)[-1]:
            segments.append(name)
        where = str(_get(frame, "where", "") or "")
        if where:
            segments.append(where)
    return segments


def inner_trail(frames: Iterable[Any], *, top: str = "") -> str:
    """A reader's place inside nested files: `backup.zip › mail.mbox › message 812`.

    `frames` is a worker's `inner` list (outermost first). `top` is the file's
    own name, used when the reader has opened no frame (an ordinary document)
    or to lead the trail when it differs from the first frame's name.
    """
    stack = list(frames or [])
    segments = _segments(stack, top)
    if stack:
        position = position_text(stack[-1])
        if position:
            segments.append(position)
        detail = str(_get(stack[-1], "detail", "") or "")
        if detail and _get(stack[-1], "stage") == "attachments":
            segments.append(f"attachment {detail}")
    return TRAIL_SEPARATOR.join(segments)


def _busy(stats: Any) -> list[tuple[str, Mapping[str, Any]]]:
    """Workers with a file open, as `(id, worker)`, in id order."""
    workers = _get(stats, "workers") or {}
    if not isinstance(workers, Mapping):
        return []
    busy = []
    for key, worker in workers.items():
        if isinstance(worker, Mapping) and worker.get("file"):
            busy.append((str(key), worker))
    return sorted(busy, key=lambda pair: (_int(pair[0]) or 0, pair[0]))


def _headline_for(worker: Mapping[str, Any]) -> str:
    """The headline sentence for one busy worker."""
    frames = list(worker.get("inner") or [])
    top = str(worker.get("file") or "")
    if not frames:
        return f"Reading {top}"
    innermost = frames[-1]
    segments = _segments(frames, top)
    place = TRAIL_SEPARATOR.join(segments)
    stage = _get(innermost, "stage")
    if stage == "opening" and _int(_get(innermost, "n")) == 0:
        return f"Opening {place}"
    position = position_text(innermost)
    sentence = f"Reading {place}"
    if position:
        sentence += f" — {position}"
    detail = str(_get(innermost, "detail", "") or "")
    if stage == "attachments" and detail:
        sentence += f", attachment {detail}"
    elif stage == "ocr":
        sentence += ", reading text from a picture"
    return sentence


def live_headline(stats: Any) -> str:
    """One sentence for what the run is reading, or "" when no reader is busy.

    With several readers busy, the one inside a container (it has the most to
    say) that has been on its file longest is chosen; the per-reader lines
    show the rest. Examples::

        Reading Archive2019.pst › Inbox/Projects — message 4,512 of 18,300
        Reading backup.zip › mail.mbox — message 812 of 2,000
        Opening Archive2019.pst
        Reading report.docx
        Finding files…          (no reader busy yet, the walk still going)
    """
    busy = _busy(stats)
    if not busy:
        # Before the first file is handed to a reader, the walker is the only
        # thing moving - worth a sentence rather than a blank. Only for stats
        # that carry a `workers` map at all: one without (an older snapshot, a
        # record from another process) says nothing about the readers, and
        # "no readers busy" would be a guess.
        if (_get(stats, "workers") is not None
                and _get(stats, "phase") == "reading"
                and not _get(stats, "walk_complete", False)):
            return f"{stage_words('finding')}…"
        return ""

    def rank(pair: tuple[str, Mapping[str, Any]]) -> tuple:
        """Busiest first: a reader inside a container, then the one on its file longest."""
        worker = pair[1]
        started = worker.get("started_at") or 0.0
        return (0 if worker.get("inner") else 1, started or float("inf"))

    _key, worker = min(busy, key=rank)
    return _headline_for(worker)


def worker_lines(stats: Any, *, now: Optional[float] = None) -> list[str]:
    """One line per reader, busy or waiting, numbered as the run numbers them.

    ``now`` is wall-clock seconds (`time.time()`), the same clock as each
    worker's `started_at`; it defaults to the current time. Examples::

        Reader 1: Archive2019.pst › Inbox/Projects › message 4,512 of 18,300 · 3 min 20 s
        Reader 1: Archive2019.pst › Inbox › message 12 of 40 · 12,400 Indexed · 3 Failed · 3 min 20 s
        Reader 2: report.docx · 2 s
        Reader 3: waiting for the next file
    """
    workers = _get(stats, "workers") or {}
    if not isinstance(workers, Mapping):
        return []
    clock = time.time() if now is None else float(now)
    folders = telling_folders(workers)
    lines = []
    for key in sorted(workers, key=lambda k: (_int(k) or 0, str(k))):
        worker = workers[key]
        if not isinstance(worker, Mapping) or not worker.get("file"):
            lines.append(f"Reader {key}: waiting for the next file")
            continue
        trail = inner_trail(worker.get("inner") or [], top=str(worker.get("file")))
        folder = folders.get(str(key), "")
        if folder:
            trail += f" ({folder})"
        counts = _frame_counts(worker.get("inner") or [])
        waiting = (f" · {stage_words('handing_over').lower()}"
                   if worker.get("stage") == "handing_over" else "")
        started = float(worker.get("started_at") or 0.0)
        took = f" · {since_text(clock - started)}" if started else ""
        lines.append(f"Reader {key}: {trail}{counts}{waiting}{took}")
    return lines


def _path_parts(path: str) -> tuple[list[str], str]:
    """A path's folders (not its file name), and the separator it was written with."""
    sep = "\\" if "\\" in path else "/"
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    return parts[:-1], sep


def telling_folders(workers: Mapping[str, Any]) -> dict[str, str]:
    r"""For busy readers whose files share a name, the folders that tell them apart.

    2026-10-05: D:\JEFF holds nine copies of many files (each kit and
    template release), and copies made together are read together - so two
    lines said the same name and looked like one file read twice. Each reader
    whose file name another busy reader also has gets the shortest run of
    folders, just below the folders they all share, that no other copy has:
    ``aso.md (Kit\kit-v0.93)`` beside ``aso.md (JT_Template)``. A name only
    one reader has gets nothing, and the line reads as it always did.
    """
    by_name: dict[str, list[tuple[str, list[str], str]]] = {}
    for key, worker in workers.items():
        if not isinstance(worker, Mapping) or not worker.get("file"):
            continue
        path = str(worker.get("path") or "")
        if not path:
            continue
        folders, sep = _path_parts(path)
        by_name.setdefault(str(worker["file"]).lower(), []).append(
            (str(key), folders, sep))
    told: dict[str, str] = {}
    for group in by_name.values():
        if len(group) < 2:
            continue
        lowered = [[f.lower() for f in folders] for _key, folders, _sep in group]
        shared = 0
        while (all(len(f) > shared for f in lowered)
               and len({f[shared] for f in lowered}) == 1):
            shared += 1
        for index, (key, folders, sep) in enumerate(group):
            own = lowered[index]
            length = 1
            while shared + length < len(own) and any(
                    other[shared:shared + length] == own[shared:shared + length]
                    for n, other in enumerate(lowered) if n != index):
                length += 1
            told[key] = sep.join(folders[shared:shared + length])
    return told


def _frame_counts(frames: Iterable[Any]) -> str:
    """" · 12,400 Indexed · 3 Failed" from the innermost frame that counts, or "".

    Order 0z lane C: the per-item status words a mail archive's reader keeps
    (`app.extract.progress.STATUS_WORDS`), shown on its reader's line.
    """
    for frame in reversed(list(frames or [])):
        text = status_counts_text(_get(frame, "counts") or {})
        if text:
            return f" · {text}"
    return ""


def heartbeat_line(stats: Any, *, now: Optional[float] = None) -> tuple[str, bool]:
    """`(text, quiet)`: "working, last activity 2 s ago", or a calm warning.

    `quiet` is True once nothing has moved for `QUIET_AFTER_S`, so the page
    can draw the sentence differently. `("", False)` before the first sign of
    life, and while paused - a pause has its own explanation and is not
    "nothing happening". ``now`` is wall-clock seconds, like `last_activity`.

    **It never says the run has hung**, because nothing here can know that:
    one scanned page through OCR, or one very large attachment, can take
    minutes with no count to move. It says what it can see and what that
    usually means. Examples::

        ("Working · last activity 2 s ago", False)
        ("No progress for 2 min 5 s. That can be normal: reading a large scan
          with OCR, or a very large attachment, can take several minutes.
          Indexing carries on by itself.", True)
    """
    last = float(_get(stats, "last_activity", 0.0) or 0.0)
    if last <= 0 or _get(stats, "paused", False):
        return "", False
    clock = time.time() if now is None else float(now)
    age = max(0.0, clock - last)
    if age < QUIET_AFTER_S:
        when = "just now" if age < 1 else f"{since_text(age)} ago"
        return f"Working · last activity {when}", False
    quiet = f"No progress for {since_text(age)}."
    if PHASE_WORDS.get(str(_get(stats, "phase", "") or "")):
        # Model loading, tidying, building the vector index: steps with no
        # count, which on a large index take minutes by design.
        return (f"{quiet} This step can take several minutes on a large index, "
                "and has no count to show while it works."), True
    return (f"{quiet} That can be normal: reading a large scan with OCR, or a "
            "very large attachment, can take several minutes. Indexing carries "
            "on by itself."), True


def writer_line(stats: Any) -> str:
    """What is happening behind the readers, or "" when nothing worth saying.

    Examples::

        Making text searchable by meaning, batch 3 of 12
        Making text searchable by meaning, batch 3
        Saving where to resume from
    """
    stage = str(_get(stats, "stage", "") or "")
    if stage == "saving_resume":
        return stage_words(stage)
    batch = _int(_get(stats, "embed_batch"))
    if batch > 0:
        batches = _int(_get(stats, "embed_batches"))
        of = f" of {batches:,}" if batches >= batch else ""
        return f"{stage_words('embedding')}, batch {batch:,}{of}"
    return ""


@dataclass
class LiveProgress:
    """Everything above, for one tick. Plain strings; the page draws them."""

    headline: str = ""
    workers: list[str] = field(default_factory=list)
    heartbeat: str = ""
    quiet: bool = False
    writer: str = ""


def live_view(stats: Any, *, now: Optional[float] = None) -> LiveProgress:
    """All the live lines for one snapshot, worked out against one clock."""
    clock = time.time() if now is None else float(now)
    heartbeat, quiet = heartbeat_line(stats, now=clock)
    return LiveProgress(
        headline=live_headline(stats),
        workers=worker_lines(stats, now=clock),
        heartbeat=heartbeat,
        quiet=quiet,
        writer=writer_line(stats),
    )


def now_headline(stats: Any, *, stopping: bool = False) -> str:
    """The Indexing page's "what is happening now" sentence (0x §4b), or "".

    The slot on the page is `widgets/indexing_headline.now_sentence`, which
    calls this and nothing else. **It never says what another line on the
    page already says**, the rule §4b's first version set:

    * stopping or paused - the counts line itself changes to say so, so "";
    * a stretch with its own words (loading the model, tidying the index) -
      the detail line under the bar carries those words, so "". This is also
      why the detail line needs no change to stop repeating them: the two
      are never both shown;
    * reading - the busiest reader's place, "Reading Archive2019.pst ›
      Inbox/Projects — message 4,512 of 18,300", or "Finding files…" before
      the first file is handed out;
    * reading, with nothing more precise known (a snapshot from before this
      existed, or a run in another process that sends no `workers`) - the
      0w words the page has always used, "Reading your files…".
    """
    if stats is None or stopping or _get(stats, "paused", False) \
            or _get(stats, "paused_by_person", False):
        return ""
    if PHASE_WORDS.get(str(_get(stats, "phase", "") or "")):
        return ""
    return live_headline(stats) or READING_WORDS
