r"""Measure a corpus before indexing it, without reading a single document.

Layer: L3

**Why this exists.** Everything about a long index run is estimated from the
mix of file types, and until now nobody had counted the mix. A 600GB corpus was
projected at roughly five days from one 97-second sample - a number that is
wrong by a factor of three in either direction depending on how many of those
bytes turn out to be scanned images, `.git` object stores, or file types this
application does not read at all. Guessing the mix is how a five-day estimate
becomes fifteen.

**It only `stat()`s.** No file is opened, nothing is extracted, nothing is
embedded and nothing is written. The one exception is the PDF probe below,
which opens a *sample* of PDFs and reads the text layer of their first two
pages - because "is this PDF a photograph?" cannot be answered from a `stat`,
and it is the single most expensive fact about the corpus.

**It counts what the indexer would ignore, too.** A scan that reported only
indexable files would answer "how long will indexing take" and leave "why is my
600GB corpus only 40GB of index" unanswered - and those are the same question
asked twice. So every byte lands in exactly one tier, including `none`.

**`.git` is descended into on purpose**, and it is the one directory here that
differs from the indexer's own walk. On a corpus of repositories the object
store is routinely most of the disk, and a person deciding whether to index
600GB needs to know that 200GB of it is git internals that no search will ever
touch. Every other excluded directory is pruned exactly as the indexer prunes
it - and *counted as pruned*, by name, so a subtree missing from the totals is
visible rather than merely absent.
"""

from __future__ import annotations

import fnmatch
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from app.core.logging import logger
from app.index.walker import DEFAULT_EXCLUDE_DIRS, DEFAULT_EXCLUDE_GLOBS

__all__ = [
    "ScanConfig",
    "ScanResult",
    "Tally",
    "PageProbe",
    "scan",
    "tier_for",
    "format_report",
    "human_bytes",
    "saved_total",
    "OCR_SECONDS_PER_PAGE",
    "PROBE_PAGES",
    "SCAN_STATE_KEY",
    "TIERS",
    "NO_EXTENSION",
]

log = logger.bind(component="index.scan")

#: Measured on this project's own hardware during the terabyte review: 3.6
#: seconds to OCR one page, about eight times what embedding a passage costs.
#: Named rather than inlined because every OCR estimate in the application
#: should move when somebody re-measures it, and because a magic 3.6 in a
#: division is unattributable a month later.
OCR_SECONDS_PER_PAGE = 3.6

#: Pages read from a sampled PDF to decide whether it has a text layer. Two,
#: because a scanned document is scanned throughout and a born-digital one has
#: text on its first page - and because this is the only part of a scan that
#: opens a file, so its cost is the one that must stay small.
PROBE_PAGES = 2

#: Characters on the probed pages below which the PDF is treated as image-only.
#: Matches `extract.pdf.MIN_PAGE_CHARS` in spirit: a scan often yields a page
#: number or a stray ligature from a header stamp, which is not a text layer.
PROBE_MIN_CHARS = 16

#: Where a scan's result is stored, so the next index run can show a real
#: percentage from its first tick rather than an indeterminate bar.
SCAN_STATE_KEY = "scan:last"

#: The key used for a file with no extension at all - `Makefile`, `.gitignore`,
#: a Unix binary. `""` sorts oddly and prints as nothing, and a blank row in a
#: table of extensions reads as a rendering bug.
NO_EXTENSION = "(none)"

#: In cost order, which is also the order the report shows them. `disabled`
#: sits between the tiers that work and the one that does nothing because it is
#: the only one that can be changed from Settings.
TIERS = ("extractor", "converter", "ocr", "disabled", "none")

TIER_LABELS = {
    "extractor": "read directly",
    "converter": "external converter",
    "ocr": "OCR (images)",
    "disabled": "switched off in Settings",
    "none": "not indexed - no reader",
}


@dataclass
class Tally:
    """A count and a size. The pair travels together everywhere here.

    Files alone hide a corpus that is 40,000 tiny source files and one 300GB
    archive; bytes alone hide the opposite. Both estimates - time and index
    size - need the two numbers.
    """

    files: int = 0
    bytes: int = 0

    def add(self, size: int) -> None:
        self.files += 1
        self.bytes += size

    def as_dict(self) -> dict[str, int]:
        return {"files": self.files, "bytes": self.bytes}


@dataclass(frozen=True)
class PageProbe:
    """What one sampled PDF turned out to be.

    `chars` is from the first `PROBE_PAGES` pages only; `pages` is the whole
    document, because an OCR estimate needs the page count and the page count
    is free once the file is open.
    """

    pages: int
    chars: int
    #: Set when the file could not be opened at all. Counted separately: an
    #: unreadable PDF is a finding, not a scanned one.
    failed: bool = False

    @property
    def image_only(self) -> bool:
        return not self.failed and self.chars < PROBE_MIN_CHARS


@dataclass
class ScanConfig:
    """What to walk, and how hard to look at PDFs."""

    roots: Sequence[Path]
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS
    exclude_globs: Sequence[str] = DEFAULT_EXCLUDE_GLOBS
    #: Absolute directory paths never descended into - the application's own
    #: index, logs and model cache. Same set the indexer uses.
    exclude_paths: frozenset[str] = field(default_factory=frozenset)
    #: Descend into every excluded directory too. Slow and occasionally what
    #: somebody wants: it answers "what is actually on this disk", rather than
    #: "what would indexing see".
    include_excluded: bool = False
    #: PDFs opened to decide how much of the corpus is scanned. 0 disables the
    #: probe entirely, and the report then says the OCR figure is unknown
    #: rather than printing a zero that reads as "none".
    sample_pdfs: int = 400
    #: Files above this are skipped by the walker, so they are counted apart
    #: rather than folded into a tier that will never see them.
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    #: Fixed so two scans of the same corpus sample the same PDFs and can be
    #: compared. A scan whose answer moves by 4% each run is a scan nobody
    #: trusts.
    seed: int = 20260825


@dataclass
class ScanResult:
    """What is out there, in the units the decisions are made in."""

    roots: tuple[str, ...] = ()
    missing_roots: tuple[str, ...] = ()
    total: Tally = field(default_factory=Tally)
    by_extension: dict[str, Tally] = field(default_factory=dict)
    #: extension -> tier. One entry per extension seen, so the table can show
    #: *why* a type is or is not counted without a second lookup per row.
    tier_of: dict[str, str] = field(default_factory=dict)
    by_tier: dict[str, Tally] = field(default_factory=dict)
    #: Everything inside a `.git` directory. Part of `total`, and broken out
    #: because on a corpus of repositories it is frequently most of the disk.
    git: Tally = field(default_factory=Tally)
    #: Directory *name* -> how many times a directory of that name was pruned.
    #: The names are what make the omission reviewable: 83 `node_modules` is a
    #: normal corpus, 83 `Projects` is a scan that answered the wrong question.
    pruned: dict[str, int] = field(default_factory=dict)
    #: Directories that could not be listed at all. A permission error is not a
    #: reason to abandon a scan, but an unreported one is a hole in the total.
    unreadable_dirs: int = 0
    #: Files the walker would skip on size: zero bytes, or over the ceiling.
    oversize: Tally = field(default_factory=Tally)
    empty_files: int = 0
    #: Files excluded by name pattern - Office lock files, `*.tmp`.
    excluded_by_name: Tally = field(default_factory=Tally)
    #: PDFs found, PDFs opened, and what the opened ones were.
    pdfs: Tally = field(default_factory=Tally)
    pdfs_probed: int = 0
    pdfs_image_only: int = 0
    pdfs_unreadable: int = 0
    #: Total pages across the probed PDFs that came back image-only. The basis
    #: of the OCR estimate, and reported so the estimate can be checked.
    image_only_pages: int = 0
    elapsed_s: float = 0.0
    #: Set when the scan was stopped before it finished, so no total here is
    #: mistaken for a complete one.
    stopped_early: str = ""

    # -- the questions the report and the estimates ask ---------------------

    def tier(self, name: str) -> Tally:
        return self.by_tier.get(name, Tally())

    @property
    def indexable(self) -> Tally:
        """What a run would actually open. The denominator for every estimate."""
        found = Tally()
        for name in ("extractor", "converter", "ocr"):
            tally = self.by_tier.get(name)
            if tally:
                found.files += tally.files
                found.bytes += tally.bytes
        return found

    @property
    def pdfs_readable(self) -> int:
        """Sampled PDFs that actually opened.

        **The denominator, and not `pdfs_probed`.** A sample of five where three
        were corrupt and one of the remaining two was scanned is 50% scanned,
        not 20% - dividing by the whole sample folds "could not tell" in with
        "has text", which understates the OCR bill by however many files were
        unreadable. On this project's own fixture folder that was the
        difference between 20% and 50%.
        """
        return max(0, self.pdfs_probed - self.pdfs_unreadable)

    @property
    def estimated_scanned_pdfs(self) -> Optional[int]:
        """How many of the corpus's PDFs are photographs, extrapolated.

        `None` when nothing could be read, which is a different answer from
        zero and must print differently. A confident "0 scanned PDFs" on a
        corpus nobody looked at is exactly the sort of number that gets planned
        around.
        """
        if not self.pdfs_readable:
            return None
        share = self.pdfs_image_only / self.pdfs_readable
        return int(round(share * self.pdfs.files))

    @property
    def estimated_ocr_pages(self) -> Optional[int]:
        """Pages OCR would have to read, images and scanned PDFs together.

        Each image file counts as one page. Scanned PDFs count at the mean page
        count of the probed ones, capped per file at `ocr.MAX_PAGES` - because
        that cap is real and ignoring it would triple the estimate for a corpus
        of 400-page scanned manuals.
        """
        if not self.pdfs_readable:
            return None
        from app.extract.ocr import MAX_PAGES

        scanned = self.estimated_scanned_pdfs or 0
        mean_pages = (
            self.image_only_pages / self.pdfs_image_only
            if self.pdfs_image_only else 0.0
        )
        per_file = min(mean_pages, float(MAX_PAGES))
        return int(round(self.tier("ocr").files + scanned * per_file))

    @property
    def estimated_ocr_hours(self) -> Optional[float]:
        pages = self.estimated_ocr_pages
        if pages is None:
            return None
        return pages * OCR_SECONDS_PER_PAGE / 3600

    def hours_at(self, mb_per_minute: float) -> Optional[float]:
        """How long the non-OCR work takes at a measured throughput.

        `None` rather than infinity when the rate is zero or absent: this is
        the number somebody plans a week around, and the honest answer to "at
        what speed?" with no measurement is that it is not known. See
        `docs/WORKORDER-terabyte-scale.md` §7 - measure a subtree first.
        """
        if mb_per_minute <= 0:
            return None
        readable = self.indexable.bytes - self.tier("ocr").bytes
        return max(0.0, readable) / 1_048_576 / mb_per_minute / 60

    def top_extensions(self, limit: int = 25) -> list[tuple[str, Tally]]:
        """Biggest first, by bytes. Files-first would put a folder of a million
        empty `.pyc` stubs above the archive that is the actual corpus."""
        return sorted(
            self.by_extension.items(), key=lambda row: row[1].bytes, reverse=True
        )[:limit]

    def as_dict(self) -> dict[str, Any]:
        return {
            "roots": list(self.roots),
            "not_found": list(self.missing_roots),
            "total": self.total.as_dict(),
            "indexable": self.indexable.as_dict(),
            "by_tier": {
                name: self.tier(name).as_dict()
                for name in TIERS if self.tier(name).files
            },
            "by_extension": {
                extension: {**tally.as_dict(), "tier": self.tier_of.get(extension, "none")}
                for extension, tally in self.top_extensions(limit=10_000)
            },
            "git": self.git.as_dict(),
            "pruned_directories": dict(sorted(self.pruned.items())),
            "unreadable_directories": self.unreadable_dirs,
            "oversize": self.oversize.as_dict(),
            "empty_files": self.empty_files,
            "excluded_by_name": self.excluded_by_name.as_dict(),
            "pdfs": {
                **self.pdfs.as_dict(),
                "probed": self.pdfs_probed,
                "image_only_in_sample": self.pdfs_image_only,
                "unreadable_in_sample": self.pdfs_unreadable,
                "estimated_image_only": self.estimated_scanned_pdfs,
            },
            "ocr": {
                "image_files": self.tier("ocr").files,
                "estimated_pages": self.estimated_ocr_pages,
                "estimated_hours": (
                    round(self.estimated_ocr_hours, 1)
                    if self.estimated_ocr_hours is not None else None
                ),
                "seconds_per_page": OCR_SECONDS_PER_PAGE,
            },
            "elapsed_s": round(self.elapsed_s, 2),
            "stopped_early": self.stopped_early or None,
        }


def saved_total(raw: str, roots: Sequence[Any]) -> int:
    """The indexable file count from the last scan - if it was of these folders.

    **The root check is the whole of it.** A total from a scan of `D:\\Archive`
    used as the denominator for a run over `D:\\Current` produces a bar that
    reaches 4% and stops, or one that hits 100% in a minute and sits there for
    a day. Either is worse than the growing denominator it replaced, because a
    wrong percentage is believed and an obviously-unknown one is not.

    Returns 0 for anything it cannot vouch for: no scan, a scan of different
    folders, or a record it cannot parse. 0 is the value `progress_for` already
    treats as "no estimate", so the caller needs no branch.
    """
    import json

    if not raw:
        return 0
    try:
        record = json.loads(raw)
        scanned = {str(Path(r)).rstrip("\\/").lower() for r in record.get("roots", [])}
        files = int(record.get("files", 0) or 0)
    except Exception:                              # noqa: BLE001 - a bad record is no record
        return 0

    wanted = {str(Path(r)).rstrip("\\/").lower() for r in roots}
    # Equality, not "covers": a run over a *subset* of the scanned folders has
    # a denominator that is too big, and one over a superset has one that is
    # too small. Only the same set is honest.
    return files if wanted and wanted == scanned else 0


# ---------------------------------------------------------------------------
# Which tier reads a file
# ---------------------------------------------------------------------------

def tier_for(
    extension: str,
    filename: str,
    *,
    rules: Any,
    registry: Any,
    names: Iterable[str],
    ocr_extensions: Iterable[str],
) -> str:
    """Which of `TIERS` would read this file.

    Order matters and is not arbitrary:

    * **OCR first**, even though `OcrExtractor` is in the registry like any
      other. It is the only tier whose cost is measured in seconds per file
      rather than milliseconds, so folding it into `extractor` would hide the
      single number this whole exercise exists to find.
    * **`disabled` before everything else that would work**, because a type
      switched off in Settings is not going to be read however many extractors
      claim it - and reporting it as `extractor` would have the report promise
      work that never happens.
    * **A whole-filename match counts**, so `Makefile` and `.gitignore` are not
      reported as unreadable. Both have an empty `Path.suffix`.
    """
    extension = (extension or "").lower()
    lowered = (filename or "").lower()

    if extension in ocr_extensions:
        # Asked before `is_enabled` deliberately: an image type switched off is
        # still an image, and the OCR estimate is about what is *there*. The
        # disabled check below then reports it honestly as switched off.
        if rules is not None and not rules.is_enabled(extension):
            return "disabled"
        return "ocr"

    if extension and rules is not None and not rules.is_enabled(extension):
        return "disabled"

    if extension and extension in registry:
        return "extractor"
    if lowered in names:
        return "extractor"
    if rules is not None and extension:
        if extension in getattr(rules, "extensions", {}):
            return "extractor"
        converter = rules.converter_for(extension)
        if converter is not None:
            return "converter" if converter.enabled else "disabled"
    return "none"


def _routing() -> tuple[Any, Any, frozenset[str], frozenset[str]]:
    """`(rules, registry, names, ocr_extensions)` - resolved once per scan.

    Resolved here rather than per file, and defensively: a broken
    `extractors.toml` must not stop somebody counting their disk. The registry
    alone still answers most of the question.
    """
    from app.extract import supported_extensions  # noqa: F401 - populates REGISTRY
    from app.extract.base import REGISTRY, supported_names
    from app.extract.ocr import OcrExtractor

    try:
        from app.core.formats import load_rules

        rules = load_rules()
    except Exception as exc:                       # noqa: BLE001 - see the docstring
        log.warning("file-type configuration could not be read, using the "
                    "built-in readers only: {}", exc)
        rules = None

    return rules, REGISTRY, supported_names(), frozenset(OcrExtractor.extensions)


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------

def _matches_any(name: str, globs: Sequence[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in globs)


def scan(
    config: ScanConfig,
    *,
    on_progress: Optional[Callable[[ScanResult], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    probe: Optional[Callable[[Path], PageProbe]] = None,
    progress_every: int = 20_000,
) -> ScanResult:
    """Walk `config.roots` and return what is there. Opens nothing but PDFs.

    `probe` is the seam. The default opens a sampled PDF with PyMuPDF; the
    tests pass a function, so every branch of the scanned-PDF estimate is
    exercised on a machine with no PDFs and no PyMuPDF.
    """
    result = ScanResult(roots=tuple(str(root) for root in config.roots))
    started = time.perf_counter()
    rules, registry, names, ocr_extensions = _routing()

    blocked = frozenset(
        str(Path(p)).rstrip("\\/").lower() for p in config.exclude_paths
    )
    # Reservoir of PDF paths. Bounded on purpose: a corpus with 400,000 PDFs
    # must not be measured by holding 400,000 paths in memory, and the sample
    # is what the estimate needs anyway.
    reservoir: list[Path] = []
    pdf_seen = 0
    chooser = random.Random(config.seed)
    seen_files: set[str] = set()

    for root in config.roots:
        root = Path(root)
        if not root.exists():
            result.missing_roots = (*result.missing_roots, str(root))
            continue
        if str(root).rstrip("\\/").lower() in blocked:
            continue

        stack: list[tuple[Path, bool]] = [(root, False)]
        while stack:
            if should_stop is not None and should_stop():
                result.stopped_early = "stopped before the walk finished"
                stack.clear()
                break

            directory, inside_git = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                # Unreadable, and counted. A silent one is a hole in a total
                # that everything downstream is planned from.
                result.unreadable_dirs += 1
                continue

            for entry in entries:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue

                if is_dir:
                    name = entry.name
                    child = Path(entry.path)
                    if str(child).lower() in blocked:
                        result.pruned[name] = result.pruned.get(name, 0) + 1
                        continue
                    if inside_git or name == ".git":
                        # **The one directory this walk enters and the indexer
                        # does not.** Its bytes are the answer to "why is my
                        # corpus 600GB", and they are never indexable.
                        stack.append((child, True))
                        continue
                    if not config.include_excluded and (
                        name in config.exclude_dirs
                        or _matches_any(name, config.exclude_globs)
                    ):
                        result.pruned[name] = result.pruned.get(name, 0) + 1
                        continue
                    stack.append((child, False))
                    continue

                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                size = int(stat.st_size)

                key = str(entry.path).lower()
                if key in seen_files:
                    continue                      # overlapping roots, counted once
                seen_files.add(key)

                result.total.add(size)
                if inside_git:
                    # Tallied and finished with: nothing inside `.git` is a
                    # candidate, and putting `.pack` in the extension table
                    # would be a row nobody can act on.
                    result.git.add(size)
                    continue

                filename = entry.name
                if _matches_any(filename, config.exclude_globs):
                    result.excluded_by_name.add(size)
                    continue
                if size == 0:
                    result.empty_files += 1
                    continue
                if size > config.max_file_bytes:
                    result.oversize.add(size)
                    continue

                extension = Path(filename).suffix.lower()
                bucket = extension or NO_EXTENSION
                tally = result.by_extension.get(bucket)
                if tally is None:
                    tally = result.by_extension[bucket] = Tally()
                tally.add(size)

                if bucket == NO_EXTENSION:
                    # **The one bucket that cannot be decided once.** `Makefile`
                    # is read and `some-unix-binary` is not, and both have an
                    # empty suffix - so caching the first answer would attribute
                    # every extensionless file on the disk to whichever kind the
                    # walk happened to meet first. Asked per file instead, which
                    # is two set lookups.
                    tier = tier_for(
                        extension, filename, rules=rules, registry=registry,
                        names=names, ocr_extensions=ocr_extensions,
                    )
                    known = result.tier_of.get(bucket)
                    result.tier_of[bucket] = (
                        tier if known in (None, tier) else "mixed"
                    )
                else:
                    tier = result.tier_of.get(bucket)
                    if tier is None:
                        tier = tier_for(
                            extension, filename, rules=rules, registry=registry,
                            names=names, ocr_extensions=ocr_extensions,
                        )
                        result.tier_of[bucket] = tier

                bucket_tally = result.by_tier.get(tier)
                if bucket_tally is None:
                    bucket_tally = result.by_tier[tier] = Tally()
                bucket_tally.add(size)

                if extension == ".pdf":
                    result.pdfs.add(size)
                    pdf_seen += 1
                    # Reservoir sampling: every PDF in the corpus has the same
                    # chance of being probed, whatever order the walk finds
                    # them in and without knowing the total first. Taking the
                    # first N instead would sample one folder.
                    if config.sample_pdfs > 0:
                        if len(reservoir) < config.sample_pdfs:
                            reservoir.append(Path(entry.path))
                        else:
                            slot = chooser.randrange(pdf_seen)
                            if slot < config.sample_pdfs:
                                reservoir[slot] = Path(entry.path)

                if on_progress is not None and result.total.files % progress_every == 0:
                    on_progress(result)

    if not result.stopped_early and reservoir:
        _probe_pdfs(reservoir, result, probe=probe, should_stop=should_stop)

    result.elapsed_s = time.perf_counter() - started
    if on_progress is not None:
        on_progress(result)
    return result


def _probe_pdfs(
    paths: list[Path],
    result: ScanResult,
    *,
    probe: Optional[Callable[[Path], PageProbe]],
    should_stop: Optional[Callable[[], bool]],
) -> None:
    """Open the sample and record what it was. Never raises."""
    reader = probe or _probe_pdf
    for path in paths:
        if should_stop is not None and should_stop():
            return
        try:
            found = reader(path)
        except Exception as exc:                   # noqa: BLE001 - one file, not the scan
            log.debug("could not probe {}: {}", path.name, exc)
            found = PageProbe(pages=0, chars=0, failed=True)
        result.pdfs_probed += 1
        if found.failed:
            result.pdfs_unreadable += 1
            continue
        if found.image_only:
            result.pdfs_image_only += 1
            result.image_only_pages += max(1, found.pages)


def _probe_pdf(path: Path) -> PageProbe:
    """Does this PDF have a text layer? Two pages, no OCR, nothing written.

    Deliberately not `PdfExtractor.extract`: that reads every page of the
    document, and reading every page of every sampled PDF is an extraction run
    wearing a scan's name.
    """
    try:
        import pymupdf
    except ImportError:
        return PageProbe(pages=0, chars=0, failed=True)

    import contextlib

    with contextlib.suppress(Exception):
        pymupdf.TOOLS.mupdf_display_errors(False)

    try:
        document = pymupdf.open(path)
    except Exception:                              # noqa: BLE001 - corrupt, locked, encrypted
        return PageProbe(pages=0, chars=0, failed=True)

    try:
        if document.needs_pass:
            return PageProbe(pages=0, chars=0, failed=True)
        chars = 0
        for number in range(min(PROBE_PAGES, document.page_count)):
            try:
                chars += len(document.load_page(number).get_text("text").strip())
            except Exception:                      # noqa: BLE001 - one bad page
                continue
        return PageProbe(pages=int(document.page_count), chars=chars)
    finally:
        document.close()


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def human_bytes(count: int) -> str:
    """`1.4 TB`. Two significant places, because the decision is made at that
    resolution and `1,538,291,200,000 bytes` is not a number anyone reads."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TB"                       # unreachable; kept for the type


def _hours(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value * 3600 < 90:
        # "0 min" for a corpus with eight pages to OCR reads as "nothing to
        # do", which is a different claim from "not long".
        return f"{value * 3600:,.0f}s"
    if value < 1:
        return f"{value * 60:,.0f} min"
    if value < 48:
        return f"{value:,.1f} hours"
    return f"{value / 24:,.1f} days"


def format_report(result: ScanResult, *, mb_per_minute: float = 0.0,
                  limit: int = 25) -> list[str]:
    """The scan as lines of text. Here rather than in `cli.py` so the wording
    is testable - every number below is one somebody will plan a week around,
    and a mislabelled one is worse than an absent one."""
    lines: list[str] = []
    out = lines.append

    for missing in result.missing_roots:
        out(f"  ! {missing} does not exist - nothing under it was counted.")
    if result.missing_roots:
        out("")

    out(f"Scanned {len(result.roots)} folder(s) in {result.elapsed_s:,.1f}s")
    out(f"  {result.total.files:,} files, {human_bytes(result.total.bytes)} in total")
    indexable = result.indexable
    share = (indexable.bytes / result.total.bytes * 100) if result.total.bytes else 0.0
    out(f"  {indexable.files:,} files, {human_bytes(indexable.bytes)} would be "
        f"read by an index run ({share:,.0f}% of the bytes)")
    if result.git.files:
        out(f"  {result.git.files:,} files, {human_bytes(result.git.bytes)} inside "
            f".git - never indexed, and counted here because it is often most "
            f"of a repository corpus")
    if result.stopped_early:
        out(f"  ! {result.stopped_early} - every total below is a partial one.")
    out("")

    out("By tier")
    for name in TIERS:
        tally = result.tier(name)
        if not tally.files:
            continue
        out(f"  {name:<10} {tally.files:>12,} files  {human_bytes(tally.bytes):>12}"
            f"   {TIER_LABELS[name]}")
    out("")

    rows = result.top_extensions(limit=limit)
    if rows:
        out(f"Biggest {len(rows)} file types, by size")
        for extension, tally in rows:
            out(f"  {extension:<14} {tally.files:>12,} files  "
                f"{human_bytes(tally.bytes):>12}   {result.tier_of.get(extension, 'none')}")
        remaining = len(result.by_extension) - len(rows)
        if remaining > 0:
            out(f"  ... and {remaining:,} more types")
        out("")

    out("OCR - the part that is measured in days, not minutes")
    images = result.tier("ocr")
    out(f"  {images.files:,} image file(s), {human_bytes(images.bytes)}")
    if result.pdfs_readable:
        share = result.pdfs_image_only / result.pdfs_readable * 100
        out(f"  {result.pdfs.files:,} PDF(s); {result.pdfs_readable:,} of a sample "
            f"of {result.pdfs_probed:,} could be read, and {result.pdfs_image_only:,} "
            f"of those ({share:,.0f}%) had no text layer")
        estimated = result.estimated_scanned_pdfs
        out(f"  so roughly {estimated:,} scanned PDF(s) in the corpus")
        if result.pdfs_unreadable:
            out(f"  {result.pdfs_unreadable:,} of the sample could not be opened at "
                f"all - corrupt, encrypted or locked. They are left out of the "
                f"percentage above rather than counted as having text.")
        out(f"  ~{result.estimated_ocr_pages:,} page(s) to OCR at "
            f"{OCR_SECONDS_PER_PAGE}s each = {_hours(result.estimated_ocr_hours)}")
    elif result.pdfs_probed:
        out(f"  {result.pdfs.files:,} PDF(s); none of the {result.pdfs_probed:,} "
            f"sampled could be opened, so how many are scanned is unknown.")
    elif result.pdfs.files:
        out(f"  {result.pdfs.files:,} PDF(s) found, none opened - so how many are "
            f"scanned is unknown. Re-run without --no-sample for that number.")
    else:
        out("  No PDFs found.")
    out("")

    if mb_per_minute > 0:
        out("Time, at the throughput you gave")
        out(f"  {_hours(result.hours_at(mb_per_minute))} to read everything except "
            f"images, at {mb_per_minute:,.0f} MB/min")
        both = result.hours_at(mb_per_minute)
        ocr = result.estimated_ocr_hours
        if both is not None and ocr is not None:
            out(f"  {_hours(both + ocr)} including OCR, in one pass")
            out("  Two passes make search useful after the first - see "
                "`app.cli index --skip-ocr`.")
        out("")
    else:
        out("Time")
        out("  Not estimated. Nothing here knows how fast this machine reads this")
        out("  data, and a guessed rate is how a five-day estimate becomes fifteen.")
        out("  Index one 20-30GB subtree with --skip-ocr, take the MB/min it")
        out("  reports, and re-run this with --mb-per-minute.")
        out("")

    if result.pruned:
        top = sorted(result.pruned.items(), key=lambda row: row[1], reverse=True)[:8]
        summary = ", ".join(f"{name} x{count:,}" for name, count in top)
        out(f"Not descended into: {summary}")
        out("  These are excluded from indexing, so their contents are not in any")
        out("  total above. --all counts them too.")
    if result.unreadable_dirs:
        out(f"  {result.unreadable_dirs:,} folder(s) could not be listed - permissions.")
    if result.oversize.files:
        out(f"  {result.oversize.files:,} file(s) over the {human_bytes(2 * 1024**3)} "
            f"ceiling were not counted as indexable.")
    if result.empty_files:
        out(f"  {result.empty_files:,} empty file(s) were not counted as indexable.")
    return lines
