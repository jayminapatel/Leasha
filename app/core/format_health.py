r"""Whether each file type actually works on *this* machine.

Layer: L0

`formats.py` answers "what is this file type routed to". This answers the
question the person actually has: **"is it going to read my files?"** They are
different questions, and until now only the first had an answer anywhere.

The gap this closes: `.doc` can be switched on, correctly routed to a
converter, and fail on every single file because LibreOffice is not installed -
silently, once per file, three hours into a run. `.dwg` can be routed to an
extractor whose library was never pip-installed. Nothing on screen distinguished
either case from a format that works.

**One source of truth, two consumers.** `doctor.py` prints this at setup and the
Settings editor colours its rows from it. Two independent probes would be two
answers to one question, which is how a format ends up reported healthy in one
place and broken in the other.

**Optional dependencies are declared, not guessed.** An extractor states what it
needs with a `requires` attribute; absence means "no dependency", which is true
of every built-in that reads bytes with the standard library. Nothing here
imports an extractor's library - `find_spec` only - because probing must stay
cheap enough to run on every Settings open and must never load an ONNX runtime
to answer a yes/no question.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "Requirement",
    "FormatStatus",
    "READY",
    "DEGRADED",
    "BLOCKED",
    "OFF",
    "module_present",
    "format_health",
    "summarise",
]

#: Reads its files with no third-party help, or everything it needs is present.
READY = "ready"
#: Works, but something optional is missing and it is doing less as a result -
#: `.vsd` indexed by name because `olefile` is absent, say. Never a failure.
DEGRADED = "degraded"
#: Routed and switched on, but it cannot read a single file as things stand.
#: This is the state that used to be invisible.
BLOCKED = "blocked"
#: Switched off in configuration. Not a problem - a choice - and reported
#: separately so it never inflates a count of things that are broken.
OFF = "off"


@dataclass(frozen=True, slots=True)
class Requirement:
    """One optional dependency of an extractor.

    `hard=True` means the extractor cannot read anything without it; `False`
    means it degrades - the distinction between a red row and an amber one.
    """

    module: str                 # what to import, e.g. "rapidocr_onnxruntime"
    package: str                # what to install, e.g. "rapidocr-onnxruntime"
    provides: str = ""          # what is lost without it, in plain English
    hard: bool = True
    #: Which of the extractor's extensions this applies to. Empty means all.
    #:
    #: One extractor often covers formats with different needs: `visio` handles
    #: `.vsdx` (helped by the `vsdx` package) and `.vsd` (helped by `olefile`),
    #: and neither package does anything for the other extension. Without this,
    #: a `.vsd` row told people to install `vsdx` - a fix that changes nothing,
    #: which is how somebody learns to ignore the whole column.
    extensions: tuple[str, ...] = ()

    def applies_to(self, extension: str) -> bool:
        return not self.extensions or extension.lower() in self.extensions

    @property
    def fix(self) -> str:
        """The exact command, ready to paste. Windows venv layout, as shipped."""
        return f"venv\\Scripts\\pip install {self.package}"


@dataclass(frozen=True, slots=True)
class FormatStatus:
    """One extension's real state, with the fix when there is one."""

    extension: str
    reader: str                 # extractor name, or "converter -> plaintext"
    state: str                  # READY | DEGRADED | BLOCKED | OFF
    detail: str = ""            # why it is in that state
    fix: str = ""               # the command or action that would resolve it
    enabled: bool = True

    @property
    def ok(self) -> bool:
        """True when this format reads files today. `DEGRADED` counts: reading
        a Visio file's title is less than its shapes, but it is not a failure."""
        return self.state in (READY, DEGRADED)


@lru_cache(maxsize=256)
def module_present(name: str) -> bool:
    """Is this importable, without importing it?

    `find_spec` and nothing else. Importing to find out would load an ONNX
    runtime, start a JVM, or spend a second on a Qt binding - on every Settings
    open, to answer a question with two possible values.

    Cached because Settings asks the same dozen questions every time it opens,
    and the answer cannot change while the process runs.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        # ValueError: a parent package exists but is broken - which for our
        # purposes is indistinguishable from missing, and equally not usable.
        return False


def _requirements_of(extractor: Any) -> tuple[Requirement, ...]:
    """An extractor's declared optional dependencies. Absent means none."""
    declared = getattr(extractor, "requires", ())
    return tuple(r for r in declared if isinstance(r, Requirement))


def _extractor_status(
    extension: str, extractor: Any, *, enabled: bool
) -> FormatStatus:
    name = str(getattr(extractor, "name", "?"))
    missing_hard: list[Requirement] = []
    missing_soft: list[Requirement] = []

    for requirement in _requirements_of(extractor):
        if not requirement.applies_to(extension):
            continue
        if module_present(requirement.module):
            continue
        (missing_hard if requirement.hard else missing_soft).append(requirement)

    if not enabled:
        return FormatStatus(extension, name, OFF, "Switched off in settings.",
                            enabled=False)

    if missing_hard:
        first = missing_hard[0]
        return FormatStatus(
            extension, name, BLOCKED,
            detail=(
                f"'{first.module}' is not installed, so no {extension} file can "
                f"be read. They are indexed by name only."
            ),
            fix=first.fix,
        )

    if missing_soft:
        first = missing_soft[0]
        return FormatStatus(
            extension, name, DEGRADED,
            detail=(
                f"Working, but without '{first.module}' "
                f"{first.provides or 'some content is not read'}."
            ),
            fix=first.fix,
        )

    return FormatStatus(extension, name, READY)


def _converter_status(
    extension: str, rule: Any, *,
    binaries: dict[str, Optional[str]],
    counts: Optional[Mapping[str, int]] = None,
) -> FormatStatus:
    reader = f"converter -> {getattr(rule, 'then', '?')}"
    binary = getattr(rule, "binary", "")

    if not getattr(rule, "enabled", False):
        return FormatStatus(
            extension, reader, OFF,
            detail=(
                f"Switched off. '{binary}' was found, so this can be turned on."
                if binaries.get(binary) else
                f"Switched off, and '{binary}' is not installed anyway."
            ),
            enabled=False,
        )

    if not binaries.get(binary):
        waiting = (counts or {}).get(extension)
        if extension == ".dwg":
            # §5b: plain words naming *both* allowed converters, because
            # either satisfies the reader - a message naming only whichever
            # one happens to be configured in extractors.toml reads as if the
            # other were not an option, when it is equally allow-listed.
            return FormatStatus(
                extension, reader, BLOCKED,
                detail=_dwg_blocked_detail(extension, waiting),
                fix=_DWG_FIX,
            )
        return FormatStatus(
            extension, reader, BLOCKED,
            # **Say where it looked.** "Not found on this machine" is wrong from
            # the reader's point of view the moment they have just installed the
            # thing - and the usual cause is not absence but a stale PATH: a
            # folder added to the system PATH is invisible to processes that
            # were already running, including the terminal the app was started
            # from. Naming the search turns "you are wrong, it is installed"
            # into "ah, I need to reopen the terminal".
            detail=(
                f"Switched on, but '{binary}' was not found on PATH or in the "
                f"usual install folders, so every {extension} file will be "
                f"indexed by name only. If you have just installed it, close "
                f"this application and any terminal and start again - a new "
                f"PATH does not reach programs that are already running."
            ),
            fix=_binary_fix(binary),
        )

    return FormatStatus(extension, reader, READY,
                        detail=f"Converted by {binary}.")


#: §5b's literal wording ("DWG drawings: install LibreDWG or the ODA File
#: Converter to read these") plus the count of files that would benefit,
#: when the caller knows it. Kept apart from `_converter_status` only for
#: readability - it is not reused anywhere else.
def _dwg_blocked_detail(extension: str, waiting: Optional[int]) -> str:
    detail = (
        "DWG drawings: install LibreDWG or the ODA File Converter to read "
        f"these. Neither was found on this machine, so every {extension} "
        "file is indexed by name only. If you have just installed one, "
        "close this application and any terminal and start again - a new "
        "PATH does not reach programs that are already running."
    )
    if waiting:
        plural = "" if waiting == 1 else "s"
        detail += f" {waiting:,} {extension} file{plural} waiting right now."
    return detail


#: Both allowed routes to a working `.dwg` route, named together - `_BINARY_FIXES`
#: only ever knows the one binary a converter rule actually names, and
#: `extractors.toml` currently wires `dwg2dxf` alone (see its own comment on why
#: the ODA File Converter needs a wrapper it does not have yet). The person
#: reading this has not read that file, and should still hear both options.
_DWG_FIX = (
    "Install LibreDWG and put dwg2dxf on PATH: "
    "https://www.gnu.org/software/libredwg/  -  or the ODA File Converter: "
    "https://www.opendesign.com/guestfiles/oda_file_converter"
)


#: How to obtain each allowed converter. In code beside the allow-list, because
#: a wrong install command is a support call and a right one is a paste.
_BINARY_FIXES = {
    "soffice": "winget install --id TheDocumentFoundation.LibreOffice -e",
    "libreoffice": "winget install --id TheDocumentFoundation.LibreOffice -e",
    "pandoc": "winget install --id JohnMacFarlane.Pandoc -e",
    "tesseract": "winget install --id UB-Mannheim.TesseractOCR -e",
    "xstexporter": "Download XstReader from https://github.com/Dijji/XstReader",
    "dwg2dxf": "Install LibreDWG and put dwg2dxf on PATH: "
               "https://www.gnu.org/software/libredwg/",
    "ODAFileConverter": "Download the ODA File Converter (free): "
                        "https://www.opendesign.com/guestfiles/oda_file_converter",
}


def _binary_fix(binary: str) -> str:
    return _BINARY_FIXES.get(binary, f"Install '{binary}' and put it on PATH.")


def format_health(
    rules: Any,
    registry: Optional[dict[str, Any]] = None,
    *,
    binaries: Optional[dict[str, Optional[str]]] = None,
    counts: Optional[Mapping[str, int]] = None,
) -> list[FormatStatus]:
    """Every known extension's real state, sorted by extension.

    Never raises and never imports an extractor's library: this is called while
    Settings is opening and while `doctor` is diagnosing a broken install, and
    both are places where an exception is the least useful possible answer.

    `counts` is optional and unrelated to the probing above: `{".dwg": 40}`
    says how many files of that extension are already known to be waiting
    (indexed by name only), for a caller that can afford to ask the index -
    `doctor` does, Settings does not yet. Absent, the row still reads true,
    just without a number in it - which is the point of it being optional
    rather than a second required argument every caller must now supply.
    """
    if registry is None:
        from app.extract.base import REGISTRY
        registry = REGISTRY

    if binaries is None:
        try:
            from app.extract.converter import available_binaries
            binaries = available_binaries()
        except Exception:                          # noqa: BLE001 - absence is an answer
            binaries = {}

    converters = dict(getattr(rules, "converters", {}) or {})
    statuses: list[FormatStatus] = []
    seen: set[str] = set()

    for extension, extractor in sorted(registry.items()):
        seen.add(extension)
        try:
            enabled = bool(rules.is_enabled(extension))
        except Exception:                          # noqa: BLE001
            enabled = True
        statuses.append(_extractor_status(extension, extractor, enabled=enabled))

    for extension, rule in sorted(converters.items()):
        if extension in seen:
            # An extractor claims it in code, so the converter never runs -
            # `extract()` only reaches Tier 2 when the registry has no answer.
            continue
        statuses.append(_converter_status(
            extension, rule, binaries=binaries, counts=counts))

    return sorted(statuses, key=lambda s: s.extension)


def summarise(statuses: Iterable[FormatStatus]) -> dict[str, int]:
    """Counts per state, for a one-line summary in `doctor` and in Settings."""
    counts = {READY: 0, DEGRADED: 0, BLOCKED: 0, OFF: 0}
    for status in statuses:
        counts[status.state] = counts.get(status.state, 0) + 1
    return counts
