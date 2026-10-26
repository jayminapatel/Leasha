r"""The Indexing cockpit's words and numbers: state, phases, tiles, lanes, resources.

Layer: L5. Part of the presenter package; imports no Qt.

The owner, 2026-10-14: *"the sizes of boxes change on content etc it is
disturbing for the eye ... That should be the cockpit for indexing"* and *"any
thing the threads are executing should be visible on the indexing status page
nothing silent"*. The Status shelf became a fixed instrument panel - a state
pill, a phase stepper, six tiles, the bar, one row per thread, a resource strip
and a tab panel - and every value it shows is decided here, from the same
snapshot the progress line reads, so the window and a test see the same thing.

**Nothing here invents a measurement.** A value the snapshot does not carry is
`NO_VALUE` ("-"), shown in place: a widget's size never depends on whether a
number is known. Fields are read with `live_progress._get`, so an in-process
`IndexStats`, a `SimpleNamespace` and a dictionary from the indexing process
read the same.

**Old words are reused, never reworded.** Phase sentences stay in
`indexing.PHASE_WORDS`, stage words in `live_progress.STAGE_WORDS`, rates and
ETAs come from `indexing.run_rate` / `run_eta`. The short labels below - the
pill's five words, the stepper's chip names, the tile and resource captions,
the lane names - are new words for new controls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.core.file_state import DEFERRED, DEFERRED_CODES, EXPLANATIONS
from app.ui.presenter.formatting import format_count
from app.ui.presenter.indexing import (
    ETA_UNKNOWN, group_skips, phase_words, run_eta, run_rate, run_skipped_and_held,
)
from app.ui.presenter.live_progress import (
    TRAIL_SEPARATOR, _frame_counts, _get, _int, _segments, position_text,
    since_text, stage_words, telling_folders, worker_lines,
)

__all__ = [
    "DEVICE_CPU", "DEVICE_GPU", "GPU_FREE", "HELD_SENTENCE", "LANE_ORDER", "LANE_STATE_WORDS", "LaneRow",
    "NO_VALUE", "RESOURCE_LABELS", "STATES", "STATE_ICONS", "STATE_TOKENS",
    "STEPS", "TILE_LABELS", "device_words", "held_summary", "lane_rows", "next_state",
    "reader_rows", "resources", "run_state", "step_states", "tab_title", "tiles",
]

#: What a cell shows when there is nothing to show. One character, so an empty
#: cell and a full one are the same size.
NO_VALUE = "-"

# -- the state pill ----------------------------------------------------------

#: The pill's words, by state. New words for a new control (2026-10-14).
STATES: dict[str, str] = {
    "running": "Running",
    "paused": "Paused",
    "stopping": "Stopping",
    "idle": "Idle",
    "failed": "Failed",
}
#: The Lucide glyph beside each word. The word carries the meaning; the icon
#: and its colour only repeat it.
STATE_ICONS: dict[str, str] = {
    "running": "play",
    "paused": "pause",
    "stopping": "square",
    "idle": "circle-minus",
    "failed": "triangle-alert",
}
#: Which theme token colours the pill's icon in each state. Names, never values.
STATE_TOKENS: dict[str, str] = {
    "running": "accent",
    "paused": "warning",
    "stopping": "warning",
    "idle": "text_faint",
    "failed": "danger",
}


def next_state(current: str, event: str) -> str:
    """The pill's state after `event`. A failure stays until the next start.

    A failed run ends like any other - the buttons go back to idle a moment
    after the error - and the pill must not turn "Failed" into a calm "Idle"
    over the top of the error it is explaining. Only a new run clears it.
    """
    event = event if event in STATES else "idle"
    if current == "failed" and event == "idle":
        return "failed"
    return event


def run_state(*, paused: bool, held: bool, stopping: bool) -> str:
    """The pill's state for a progress tick: stopping beats paused beats running."""
    if stopping:
        return "stopping"
    if paused or held:
        return "paused"
    return "running"


# -- the phase stepper -------------------------------------------------------

#: The stepper's chips, in run order, each with the phase codes it stands for
#: (`indexing.PHASE_WORDS` and "reading"). **"Faces and pictures" has no phase
#: of its own**: faces and pictures are read on the pictures lane while the
#: run reads, so that chip lights from the lane (`step_states`). `media`
#: (videos and recordings) is reading, so it sits with Read.
STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Prepare", ("model", "word_index_check", "catch_up", "planning", "scanning")),
    ("Read", ("reading", "media")),
    ("Meaning", ("meaning",)),
    ("Faces and pictures", ()),
    ("Describe photos", ("photo_tags",)),
    ("Text in pictures", ("picture_text",)),
    ("Cards", ("doc_cards",)),
    ("Tidy", ("tidying", "vector_index", "word_index")),
)
_FACES = 3

#: 2026-10-26, the owner: "no icon ... and they dont have tool tips". A step
#: not yet reached drew an empty slot. Each step's own glyph, shown dim until
#: the step is reached (done ticks, the current one turns), and what it does.
STEP_ICONS: dict[str, str] = {
    "Prepare": "list-checks",
    "Read": "file-text",
    "Meaning": "brain",
    "Faces and pictures": "users",
    "Describe photos": "sparkles",
    "Text in pictures": "scan-search",
    "Cards": "id-card",
    "Tidy": "database",
}
STEP_TIPS: dict[str, str] = {
    "Prepare": "Gets the models ready, finishes what the last run left undone and finds the files to read.",
    "Read": "Reads the text out of each file and makes it searchable by its words.",
    "Meaning": "Makes the text searchable by what it means, not only by its words.",
    "Faces and pictures": "Finds photos by what they show, and the faces in them.",
    "Describe photos": "Describes the photos that have no text in them.",
    "Text in pictures": "Reads any text in the pictures, last of all.",
    "Cards": "Writes a card for each document: what it is, who and when.",
    "Tidy": "Tidies the index at the end of a run so searches stay quick.",
}
#: What a chip's state adds to its tooltip.
STEP_STATE_WORDS = {"done": "Done.", "current": "Happening now.", "todo": "Not reached yet."}


def step_tip(words: str, state: str) -> str:
    """A chip's tooltip: what the step does, then where the run is with it."""
    tip = STEP_TIPS.get(words, "")
    said = STEP_STATE_WORDS.get(state, "")
    return f"{tip} {said}".strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _lane(stats: Any, key: str) -> Mapping[str, Any]:
    """One lane's mapping from `stats.lanes`, or `{}`."""
    return _mapping(_mapping(_get(stats, "lanes")).get(key))


def step_states(stats: Any, *, finished: bool = False) -> list[str]:
    """One of "done", "current" or "todo" per chip in `STEPS`.

    Before a run every chip is "todo"; a run that reached its end has every
    chip "done". In between, the chip holding the run's phase is current and
    the chips before it are done (passed, whether or not the run had work in
    them). The pictures chip is current while the pictures lane works.
    """
    if stats is None:
        return ["todo"] * len(STEPS)
    if finished:
        return ["done"] * len(STEPS)
    phase = str(_get(stats, "phase", "") or "")
    at = next((i for i, (_w, codes) in enumerate(STEPS) if phase in codes), -1)
    states = ["done" if i < at else "current" if i == at else "todo"
              for i in range(len(STEPS))]
    pictures = str(_lane(stats, "pictures").get("state") or "")
    if pictures == "working":
        states[_FACES] = "current"
    elif pictures == "done" and states[_FACES] == "todo":
        states[_FACES] = "done"
    return states


# -- the six tiles -----------------------------------------------------------

#: The tiles' captions, left to right.
#: 2026-10-26, the owner ("do all recommended"): "Indexed 0" read as an empty
#: index beside 305,725 in the line above. The tiles count this run.
TILE_LABELS: tuple[str, ...] = (
    "Indexed this run", "Files seen", "Skipped", "Passages", "Speed", "Time left")

#: Each tile's tooltip, before a run and when it has nothing more to add.
TILE_TIPS: tuple[str, ...] = (
    "Files this run has read and saved. The whole index is counted on the line above.",
    "Files this run has found so far, read or not.",
    "Files this run could not read. Files held for a later pass are not counted here.",
    "Pieces of text this run has saved, ready to search.",
    "Files read a minute, over the last few minutes.",
    "How long the files still to read should take at this speed.",
)


def tiles(stats: Any, *, total_estimate: int = 0,
          finished: bool = False) -> list[tuple[str, str, str]]:
    """`(caption, value, tooltip)` per tile. Every value is `NO_VALUE` before a run.

    The counts are the ones the progress line prints (`progress_text`); the
    speed and the time left are `run_rate` / `run_eta`, so a tile and the line
    under the bar can never disagree. A stretch with no count (loading the
    model, tidying) shows no speed and no time left: none is being earned. A
    finished run keeps its counts and its whole-run speed.
    """
    if stats is None:
        return [(label, NO_VALUE, tip) for label, tip in zip(TILE_LABELS, TILE_TIPS)]
    skipped, held = run_skipped_and_held(stats)
    counts = [format_count(_int(_get(stats, name))) for name in ("indexed", "seen")]
    counts += [format_count(skipped), format_count(_int(_get(stats, "chunks")))]
    counting = not finished and not phase_words(stats)
    speed, left, left_tip = NO_VALUE, NO_VALUE, ""
    if counting or finished:
        _recent, rate = run_rate(stats)
        if finished:
            rate = _get(stats, "files_per_minute", 0) or 0
        try:
            speed = f"{float(rate):,.0f} files/min"
        except (TypeError, ValueError):
            speed = NO_VALUE
    if counting:
        eta = run_eta(stats, total_estimate=total_estimate)
        if eta == ETA_UNKNOWN:
            left_tip = eta
        else:
            left = eta
    values = counts + [speed, left]
    tips = list(TILE_TIPS)
    if held:
        tips[2] = f"{tips[2]} {format_count(held)} held for later this run."
    if left_tip:
        tips[5] = left_tip
    return list(zip(TILE_LABELS, values, tips))


# -- the lanes table ---------------------------------------------------------

#: The lanes after the readers, in this fixed order: `(lane id, name)`.
#: "beside" is shown only when the run reports it.
LANE_ORDER: tuple[tuple[str, str], ...] = (
    ("walker", "Finding files"),
    ("pictures", "Pictures"),
    ("meaning", "Meaning"),
    ("writer", "Writing"),
    ("beside", "Pictures beside meaning"),
    ("pass", "Run's end"),
)
_OPTIONAL_LANES = frozenset({"beside"})

#: A lane's state word, for when it has no task to name.
LANE_STATE_WORDS: dict[str, str] = {
    "working": "Working",
    "idle": "Idle",
    "waiting": "Waiting",
    "paused": "Paused",
    "done": "Done",
}


#: Words for where a lane runs, instead of the provider's own name (2026-10-14,
#: the owner's review: "DirectML" means nothing to the eight-year-old).
DEVICE_GPU = "graphics card"
DEVICE_CPU = "processor"
_GPU_NAMES = ("directml", "dml", "cuda", "gpu", "rocm", "tensorrt", "openvino")


def device_words(device: Any) -> str:
    """"graphics card" for any graphics provider, "processor" for the CPU, "" for none.

    The raw provider name ("DirectML", "CUDAExecutionProvider") is kept for the
    chip's tooltip by the caller; a name this does not recognise is shown as given.
    """
    raw = str(device or "").strip()
    lowered = raw.lower()
    if not raw:
        return ""
    if "cpu" in lowered:
        return DEVICE_CPU
    if any(name in lowered for name in _GPU_NAMES):
        return DEVICE_GPU
    return raw


@dataclass
class LaneRow:
    """One row of the lanes table, as plain strings and one clock value."""

    key: str
    name: str
    state: str = "idle"
    doing: str = NO_VALUE
    on: str = NO_VALUE
    on_tip: str = ""
    started: float = 0.0
    done: str = NO_VALUE
    device: str = ""
    #: The provider's own name ("DirectML"), for the device chip's tooltip.
    device_tip: str = ""
    #: The reader's whole line (`worker_lines`), for the row's tooltip.
    line: str = ""
    #: The file a Force skip press would skip, or "" (readers only).
    file: str = ""

    def took(self, now: float) -> str:
        """How long it has been on this, against `now` - or `NO_VALUE`."""
        if self.started <= 0 or self.state != "working":
            return NO_VALUE
        return since_text(now - self.started)


def _reader_order(key: Any) -> tuple:
    return (_int(key) or 0, str(key))


def reader_rows(stats: Any, *, now: Optional[float] = None) -> list[LaneRow]:
    """One row per reader in `stats.workers`, in reader order.

    `doing` is what the reader is at (its innermost place's stage words),
    `on` the place inside the file (`backup.zip › mail.mbox`, with the folders
    that tell same-named copies apart), `done` the position inside it and the
    per-item counts an archive keeps. A reader with no file is "waiting".
    """
    workers = _get(stats, "workers") if stats is not None else None
    if not isinstance(workers, Mapping):
        return []
    clock = time.time() if now is None else float(now)
    order = sorted(workers, key=_reader_order)
    lines = dict(zip(order, worker_lines(stats, now=clock)))
    folders = telling_folders(workers)
    rows = []
    for key in order:
        worker = workers[key]
        name, line = f"Reader {key}", lines.get(key, "")
        if not isinstance(worker, Mapping) or not worker.get("file"):
            rows.append(LaneRow(str(key), name, "waiting",
                                doing="Waiting for the next file", line=line))
            continue
        frames = list(worker.get("inner") or [])
        top = str(worker.get("file"))
        place = TRAIL_SEPARATOR.join(_segments(frames, top)) or top
        folder = folders.get(str(key), "")
        if folder:
            place += f" ({folder})"
        stage = _get(frames[-1], "stage") if frames else worker.get("stage")
        doing = stage_words(str(stage or "")) or stage_words("reading")
        if worker.get("stage") == "handing_over":
            doing = stage_words("handing_over")
        done = position_text(frames[-1]) if frames else ""
        counts = _frame_counts(frames).removeprefix(" · ")
        done = " · ".join(part for part in (done, counts) if part) or NO_VALUE
        rows.append(LaneRow(
            str(key), name, "working", doing=doing, on=place,
            on_tip=str(worker.get("path") or top),
            started=float(worker.get("started_at") or 0.0), done=done,
            line=line, file=top))
    return rows


def _done_text(done: Any, total: Any) -> str:
    n, of = _int(done), _int(total)
    if of:
        return f"{n:,} of {of:,}"
    return f"{n:,}" if n else NO_VALUE


def lane_rows(stats: Any) -> list[LaneRow]:
    """The lanes after the readers, in `LANE_ORDER`, from `stats.lanes`.

    A lane the snapshot does not carry is an idle row of `NO_VALUE` - the table
    never changes shape because a run is older, or in another process. "Pictures
    beside meaning" appears only when the run reports it.
    """
    lanes = _mapping(_get(stats, "lanes")) if stats is not None else {}
    rows = []
    for key, default in LANE_ORDER:
        lane = lanes.get(key)
        if key in _OPTIONAL_LANES and not isinstance(lane, Mapping):
            continue
        lane = _mapping(lane)
        state = str(lane.get("state") or "idle")
        state = state if state in LANE_STATE_WORDS else "idle"
        task = str(lane.get("task") or "")
        doing = (stage_words(task) or task or LANE_STATE_WORDS[state]) if lane else NO_VALUE
        item = str(lane.get("item") or "")
        rows.append(LaneRow(
            key, str(lane.get("label") or default), state, doing=doing,
            on=item or NO_VALUE, on_tip=str(lane.get("path") or item),
            started=float(lane.get("started_at") or 0.0),
            done=_done_text(lane.get("done"), lane.get("total")),
            device=device_words(lane.get("device")),
            device_tip=str(lane.get("device") or "")))
    return rows


# -- the resource strip ------------------------------------------------------

#: The strip's captions, left to right.
RESOURCE_LABELS: tuple[str, ...] = (
    "Processor", "Memory", "Graphics card", "Disk free", "Governor")

#: The graphics card's value when nothing is known to hold it.
GPU_FREE = "free"


def _number(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _gb(mb: float) -> str:
    return f"{mb / 1024:,.1f} GB"


def _gpu(stats: Any, measured: Mapping[str, Any]) -> str:
    """Which lane holds the graphics card, "free", or `NO_VALUE`.

    `stats.resources["gpu_lane"]` when the run reports it (a lane id, or empty
    for none); otherwise the working lanes whose `device` is not the processor.
    """
    names = dict(LANE_ORDER)
    lanes = _mapping(_get(stats, "lanes")) if stats is not None else {}
    if "gpu_lane" in measured:
        held = str(measured.get("gpu_lane") or "")
        if not held:
            return GPU_FREE
        return str(_mapping(lanes.get(held)).get("label") or names.get(held, held))
    if not lanes:
        return NO_VALUE
    holders = [str(lane.get("label") or names.get(key, key))
               for key, lane in lanes.items() if isinstance(lane, Mapping)
               and str(lane.get("device") or "").lower() not in ("", "cpu")
               and lane.get("state") == "working"]
    return ", ".join(holders) if holders else GPU_FREE


def resources(stats: Any) -> list[tuple[str, str, str]]:
    """`(caption, value, tooltip)` per chip. **Only what the snapshot carries.**

    Processor, memory and disk free come from `stats.resources`
    (`cpu_percent`, `memory_mb`, `memory_total_mb`, `disk_free_gb`) when the
    run reports them, and are `NO_VALUE` until then - the UI thread does no
    I/O, so nothing here measures the machine itself. The governor is
    `stats.paused`, `paused_by_person` and `pause_reason`.
    """
    measured = _mapping(_get(stats, "resources")) if stats is not None else {}
    cpu = _number(measured.get("cpu_percent"))
    used = _number(measured.get("memory_mb"))
    total = _number(measured.get("memory_total_mb"))
    disk = _number(measured.get("disk_free_gb"))
    memory = NO_VALUE
    if used is not None:
        memory = f"{_gb(used)} of {_gb(total)}" if total else _gb(used)
    governor, why = NO_VALUE, ""
    if stats is not None:
        if _get(stats, "paused_by_person", False):
            governor = "Held by you"
        elif _get(stats, "paused", False):
            governor = "Waiting"
            why = str(_get(stats, "pause_reason", "") or "")
        else:
            governor = "Not waiting"
    values = [
        NO_VALUE if cpu is None else f"{cpu:,.0f}%",
        memory,
        _gpu(stats, measured),
        NO_VALUE if disk is None else f"{disk:,.0f} GB",
        governor,
    ]
    tips = ["", "", "", "", why]
    return list(zip(RESOURCE_LABELS, values, tips))


# -- the tabs ----------------------------------------------------------------

#: What "held for later" means: the funnel's own sentence for Deferred.
HELD_SENTENCE = EXPLANATIONS[DEFERRED]


def tab_title(name: str, count: int) -> str:
    """A tab's words, with its count when there is one: "Skipped (12)"."""
    return f"{name} ({count:,})" if count else name


def held_summary(skipped_by_code: Any, funnel: Any) -> tuple[int, list[str]]:
    """`(count, lines)` for the Held for later tab.

    The count is the index's own Deferred count when the funnel has read it
    (every file waiting for a later pass, whichever run left it), otherwise
    this run's held files. The lines are this run's held files by reason, in
    the skip registry's own words - the sentence the skipped panel gives.
    """
    held = {code: _int(n) for code, n in dict(skipped_by_code or {}).items()
            if code in DEFERRED_CODES and _int(n)}
    lines = []
    for group in group_skips(held):
        lines.append(f"{format_count(group.count)} · {group.message}")
        if group.suggestion:
            lines.append(group.suggestion)
    count = sum(held.values())
    if isinstance(funnel, Mapping) and DEFERRED in funnel:
        count = _int(funnel.get(DEFERRED))
    return count, lines
