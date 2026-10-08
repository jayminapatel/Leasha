r"""The Index Tuning screen, as words and rules rather than widgets.

Layer: L5 — pure. **No Qt is imported here**, and a test asserts it.

The screen is mostly *sentences*: what this machine is, what a control resolves
to, what happens when a limit is reached, what the last run actually measured.
Every one of those is a claim somebody will act on, so every one of them is
testable - which it would not be if it lived inside a widget constructor.

**Three modes, one rule.** Defaults and Auto-tune both mean "the envelope
decides"; Manual means "the stored number decides, clamped". A value typed in
Manual is kept when the mode moves away from it, so the mode switch is a
reversible experiment rather than a thing that eats what you typed. `resolve`
is where that rule lives, and it is the only place it lives.

`app/core/envelope.py` supplies the bounds and their reasons; this file spends
them on the screen. It never invents a bound of its own.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from app.core import envelope

__all__ = [
    "MODES",
    "MODE_LABELS",
    "DEFAULTS",
    "AUTO",
    "MANUAL",
    "machine_line",
    "resolve",
    "resolved_text",
    "limit_note",
    "footer_text",
    "cost_hint",
    "quantised_note",
]

DEFAULTS = "defaults"
AUTO = "auto"
MANUAL = "manual"

#: The three modes, in the order the switch shows them.
MODES = (DEFAULTS, AUTO, MANUAL)

MODE_LABELS = {
    DEFAULTS: "Defaults",
    AUTO: "Auto-tune",
    MANUAL: "Manual",
}

MODE_HELP = {
    DEFAULTS: "What this machine's specification implies. Nothing is measured.",
    AUTO: "The same, refined by what past runs actually measured on this "
          "machine.",
    MANUAL: "You choose, within what this machine allows. Your values are kept "
            "if you switch away and come back.",
}


# --- 4b: the machine, in plain words ----------------------------------------


def machine_line(profile: Any) -> str:
    r"""The ComputeProfile as one sentence somebody can check against the box.

    "10 cores / 12 threads · 32 GB · SSD · no graphics card". Deliberately the
    vocabulary of a specification sheet rather than of this codebase: the
    person reading it is comparing it against what they think they bought, and
    a line they cannot check is a line they cannot trust.

    **Unknowns say "unknown", never zero.** Detection is allowed to fail - a
    locked-down machine, no PowerShell - and "0 GB" would read as a fault in
    the machine rather than a gap in what was detected.
    """
    parts: list[str] = [_core_words(profile)]

    ram_mb = int(getattr(profile, "ram_mb", 0) or 0)
    parts.append(f"{ram_mb / 1024:.0f} GB" if ram_mb else "memory unknown")

    disk = str(getattr(profile, "index_disk", "") or "")
    parts.append({"ssd": "SSD", "hdd": "hard disk"}.get(disk, "drive unknown"))

    parts.append(_gpu_words(profile))

    if not getattr(profile, "avx2", False):
        # Worth a place on the line rather than a footnote: without AVX2 the
        # ONNX runtime falls back to slower kernels, and somebody wondering
        # why an old machine is slow deserves the actual reason.
        parts.append("no AVX2")

    if getattr(profile, "overridden", False):
        parts.append("OVERRIDDEN by COMPUTE_PROFILE_OVERRIDE")

    return " · ".join(parts)


def _core_words(profile: Any) -> str:
    """Cores, the fast/efficient split and threads, in specification-sheet words."""
    physical = int(getattr(profile, "physical_cores", 0) or 0)
    logical = int(getattr(profile, "logical_processors", 0) or 0)
    performance = int(getattr(profile, "performance_cores", 0) or 0)
    efficiency = int(getattr(profile, "efficiency_cores", 0) or 0)

    if not physical and not logical:
        return "cores unknown"

    core_words = f"{physical or logical} cores"
    if performance and efficiency:
        # **The distinction that made §0 necessary.** Ten cores that are two
        # fast and eight slow schedule differently from ten of one kind, and
        # somebody comparing this screen against a benchmark needs to see it.
        core_words += f" ({performance} fast, {efficiency} efficient)"
    if logical and logical != (physical or logical):
        core_words += f" / {logical} threads"
    return core_words


def _gpu_words(profile: Any) -> str:
    """The first adapter, its memory, and whether DirectML can use it."""
    adapters = tuple(getattr(profile, "gpus", ()) or ())
    if not adapters:
        return "no graphics card"

    first = adapters[0]
    name = str(getattr(first, "name", "") or "graphics card")
    vram_mb = int(getattr(first, "vram_mb", 0) or 0)
    words = f"{name}, {vram_mb / 1024:.0f} GB" if vram_mb else name
    if not getattr(profile, "directml_available", False):
        # Present and unusable is the case that confuses people most, so it is
        # said on the line rather than only in the greyed control's tooltip.
        words += " (no DirectML - not usable)"
    return words


# --- 4a / 4c: what a control resolves to ------------------------------------


def resolve(key: str, mode: str, stored: Any, profile: Any,
            measured: Optional[dict] = None) -> tuple[int, str]:
    """`(value, why)` for one tunable, under one mode.

    **The stored value is not discarded in Defaults or Auto-tune, only
    ignored.** That is what makes the mode switch reversible: somebody tries
    Manual, sets four things, decides against it, and gets their four things
    back by switching to Manual again rather than by remembering them.

    `measured` is §5's territory - the rates a past run recorded. Until it
    exists, Auto-tune resolves exactly as Defaults does and says so, which is
    honest about a feature that is present and not yet informed rather than
    pretending to a precision it has not got.
    """
    # **Only Auto-tune consults the measurements**, which is what makes the
    # mode mean something: Defaults is reproducible from the specification
    # sheet alone, and a person comparing two machines can rely on that.
    rates = measured if str(mode) == AUTO else None
    bounds = envelope.for_setting(key, profile, _rates_object(rates))
    if bounds is None:
        # No per-machine bound: a schedule, a theme. The stored value stands.
        return _as_int(stored), ""

    if str(mode) == MANUAL:
        value, notice = bounds.resolve(stored)
        return value, notice or bounds.why

    refined = (measured or {}).get(key)
    if str(mode) == AUTO and refined is not None:
        value, _notice = bounds.clamp(refined)
        return value, f"measured on this machine: {bounds.why}"

    if str(mode) == AUTO:
        if "measured" in bounds.why:
            return bounds.auto, bounds.why
        return bounds.auto, f"nothing measured yet, so: {bounds.why}"
    return bounds.auto, bounds.why


def _rates_object(measured: Any) -> Any:
    """A `Measured` from whatever the screen was handed.

    The screen passes a plain dict - `{setting key: value}` - because that is
    what a per-setting override is. `envelope`'s measured forms want the rates
    object. Accepting both here keeps every caller from having to know which
    kind it holds, and a dict that carries no rates simply yields nothing.
    """
    if measured is None:
        return None
    if hasattr(measured, "embed_per_second"):
        return measured
    rates = (measured or {}).get("rates") if isinstance(measured, dict) else None
    return rates


def resolved_text(key: str, mode: str, stored: Any, profile: Any,
                  measured: Optional[dict] = None) -> str:
    """What the label beside a control reads.

    `Auto (6)` in Defaults and Auto-tune, so the number in force is visible
    without switching modes to find out - which is the whole complaint about
    settings screens that show `0` and mean "we decided something".
    """
    value, _why = resolve(key, mode, stored, profile, measured)
    if str(mode) == MANUAL:
        return str(value)
    return f"Auto ({value})"


def _as_int(value: Any) -> int:
    """`int(value)`, or 0 for anything that is not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --- 4e: what happens at the limit ------------------------------------------

#: The sentence each ceiling owes the person setting it.
#:
#: **The rule `indexing_settings.py` established, written down.** Somebody
#: choosing a memory ceiling needs to know that exceeding it *pauses* rather
#: than fails, or they set it far too high out of fear of losing a run - and
#: then it protects nothing at all. A ceiling whose consequence is unstated is
#: a ceiling that gets routed around.
_AT_THE_LIMIT = {
    "INDEX_MEMORY_MB": "Above this, indexing PAUSES and resumes. It does not "
                       "fail, and nothing already indexed is lost.",
    "INDEX_CPU_PERCENT": "While the machine is busier than this, indexing "
                         "PAUSES. It does not fail.",
    "MIN_FREE_GB": "Below this, indexing STOPS and keeps everything already "
                   "indexed. Free some space and run again.",
    "REQUIRED_FREE_GB": "Checked before a run starts. Below it you get a "
                        "warning, not a refusal - the run still goes ahead.",
    "INDEX_WORKERS": "More readers finish sooner and leave less of the machine "
                     "for you. Past your core count they queue rather than "
                     "help.",
    "ONNX_INTRA_OP_THREADS": "Threads beyond the cores the readers have left "
                             "contend for the processor and make the run "
                             "slower, not faster.",
    "EMBED_BATCH": "A batch is text held in memory all at once. Too large and "
                   "the memory ceiling starts pausing the run you were trying "
                   "to speed up.",
    "ARCHIVE_MAX_MB": "A larger archive is recorded by name and never opened, "
                      "with a message saying so.",
    "PDF_OCR_PAGES": "Pages past this are left unread. At about 3.6 seconds a "
                     "page, this is a time budget rather than a quality "
                     "setting.",
    "ARCHIVE_RECHECK_DAYS": "0 means an archived folder is only re-walked when "
                            "it changes or you ask.",
}


def limit_note(key: str) -> str:
    """What happens when this control's limit is reached. `""` when it has no
    limit worth stating - a switch is its own explanation."""
    return _AT_THE_LIMIT.get(str(key).upper(), "")


def quantised_note(device: str) -> str:
    """Why the smaller model file is unavailable, or `""` when it is not.

    Greyed with the reason rather than silently ignored: quantisation buys
    nothing on a graphics card, and a checkbox that does nothing is worse than
    a checkbox that says why.
    """
    if str(device).lower() == "gpu":
        return ("no gain on a graphics card - the smaller file is a "
                "processor optimisation")
    return ""


# --- 4c-3: what coverage costs ----------------------------------------------


def cost_hint(key: str, rates: Optional[dict] = None) -> str:
    """What turning this on adds to a run.

    `rates` comes from §6a's measurement. Until it does, the hint is the one
    number this project has actually measured - 3.6 seconds a page for OCR -
    and everything else stays silent rather than inventing a figure. An
    invented cost hint is worse than none: it gets quoted back.
    """
    key = str(key).upper()
    seconds = float((rates or {}).get("ocr_seconds_per_page", 3.6))

    if key in ("INDEX_OCR_MODE", "PDF_OCR_PAGES", "INDEX_OCR_PASS"):
        minutes = seconds * 10_000 / 60.0
        return f"about {minutes:,.0f} minutes per 10,000 scanned pages"
    if key == "INDEX_NAME_ONLY":
        return "costs almost nothing - one row per file, nothing is opened"
    if key == "ARCHIVE_READ_INSIDE":
        return ("an archive's members cost what those files would cost on "
                "disk, one at a time")
    return ""


# --- 4f: the footer that makes Manual tunable by evidence -------------------


def footer_text(run: Optional[dict]) -> str:
    r"""The last run's resolved configuration and where its time went.

    "extract 41% · embed 52% · write 7% - 2,140 chunks/min". **Percentages
    rather than seconds**, because the question this answers is "what should I
    change", and that is a question about proportions: an embed-dominated run
    wants a bigger batch or the graphics card, an extract-dominated one wants
    more readers, and the absolute numbers say neither.

    Read off the record a run left behind rather than recomputed here, so the
    footer cannot disagree with the run log about what happened.
    """
    if not run:
        return "No run has finished yet, so there is nothing measured to show."

    stages = _stage_shares(run.get("stages") or {})
    rate = run.get("chunks_per_minute")
    pieces = [stages] if stages else []
    if rate:
        pieces.append(f"{float(rate):,.0f} chunks/min")

    settled = _settled_words(run.get("resolved") or {})
    measured = " - ".join(piece for piece in pieces if piece)

    if settled and measured:
        return f"Last run: {settled} - {measured}"
    if settled:
        return f"Last run: {settled}. Stage times were not recorded."
    return f"Last run: {measured}" if measured else (
        "The last run recorded no timings.")


def _stage_shares(stages: Any) -> str:
    """Stage times as percentages, largest first; "" without any."""
    try:
        items = [(str(name), float(seconds)) for name, seconds in
                 dict(stages).items() if float(seconds) > 0]
    except (TypeError, ValueError):
        return ""
    total = sum(seconds for _name, seconds in items)
    if not total:
        return ""
    # Largest first: the one worth acting on should not need looking for.
    items.sort(key=lambda pair: pair[1], reverse=True)
    return " · ".join(f"{name} {seconds / total * 100:.0f}%"
                      for name, seconds in items)


def _settled_words(resolved: Any) -> str:
    """The resolved workers, threads, batch and device: "workers 4, batch 32"."""
    try:
        pairs: Iterable = dict(resolved).items()
    except (TypeError, ValueError):
        return ""
    wanted = ("workers", "threads", "batch", "device")
    shown = [f"{name} {value}" for name, value in pairs
             if str(name) in wanted and value not in (None, "")]
    return ", ".join(shown)
