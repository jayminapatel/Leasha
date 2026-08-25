r"""Counting a corpus before spending a week indexing it.

Layer: L3

From `docs/WORKORDER-terabyte-scale.md` §2: *"Do this first, and do not skip it.
Every estimate here depends on the mix of file types, and guessing the mix is
how a five-day estimate becomes fifteen."*

**So the tests are about the numbers being right rather than about the walk
working.** A scan that silently under-counts is worse than no scan at all: it
produces a confident figure somebody plans a fortnight around, and there is
nothing on screen to suggest it is wrong. Every case below is one where the
count could be quietly off - a bucket decided once and reused, a denominator
that includes the files nobody could read, a subtree pruned without being
mentioned.
"""

from __future__ import annotations

import json

import pytest

from app.index.scan import (
    NO_EXTENSION,
    OCR_SECONDS_PER_PAGE,
    PageProbe,
    ScanConfig,
    ScanResult,
    Tally,
    format_report,
    human_bytes,
    saved_total,
    scan,
    tier_for,
)


def _write(root, name: str, size: int = 16) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


# --- which reader would take a file ----------------------------------------

def _tier(extension: str, filename: str = "", **over):
    from app.extract import supported_extensions  # noqa: F401 - fills REGISTRY
    from app.extract.base import REGISTRY, supported_names
    from app.extract.ocr import OcrExtractor

    kwargs = {
        "rules": None,
        "registry": REGISTRY,
        "names": supported_names(),
        "ocr_extensions": frozenset(OcrExtractor.extensions),
    }
    kwargs.update(over)
    return tier_for(extension, filename or f"a{extension}", **kwargs)


def test_images_are_their_own_tier_not_just_another_extractor():
    """**The one number this command exists to produce.**

    `OcrExtractor` sits in the same registry as every other reader, so the
    obvious implementation reports a `.png` as `extractor` - and the OCR bill,
    which at 3.6 seconds a page is the difference between a day and a fortnight,
    disappears into a column of milliseconds.
    """
    assert _tier(".png") == "ocr"
    assert _tier(".tiff") == "ocr"
    assert _tier(".pdf") == "extractor"


def test_a_file_with_no_extension_can_still_be_read():
    """`Path("Makefile").suffix` is `""`, and so is a Unix binary's."""
    assert _tier("", "Makefile") == "extractor"
    assert _tier("", "some-random-binary") == "none"


def test_a_type_switched_off_in_settings_is_reported_as_switched_off():
    """Not as `extractor`. The report would otherwise promise work that the
    run will not do, and the reason it will not do it is one click away."""

    class Rules:
        extensions: dict = {}

        def is_enabled(self, extension):
            return extension != ".py"

        def converter_for(self, extension):
            return None

    assert _tier(".py", rules=Rules()) == "disabled"
    assert _tier(".txt", rules=Rules()) == "extractor"


def test_an_image_switched_off_is_switched_off_rather_than_ocr():
    class Rules:
        extensions: dict = {}

        def is_enabled(self, extension):
            return False

        def converter_for(self, extension):
            return None

    assert _tier(".png", rules=Rules()) == "disabled"


def test_a_converter_route_is_its_own_tier():
    """Tier 2 costs a subprocess per file, which is nothing like tier 1 and
    nothing like OCR. Three costs, three rows."""

    class Converter:
        enabled = True

    class Rules:
        extensions: dict = {}

        def is_enabled(self, extension):
            return True

        def converter_for(self, extension):
            return Converter() if extension == ".doc" else None

    assert _tier(".doc", rules=Rules()) == "converter"
    assert _tier(".qqq", rules=Rules()) == "none"


# --- the walk ---------------------------------------------------------------

def test_every_byte_lands_in_exactly_one_place(tmp_path):
    """The total is the sum of its parts, or one of the parts is a lie."""
    _write(tmp_path, "a.py", 100)
    _write(tmp_path, "b.png", 200)
    _write(tmp_path, "c.qqq", 300)
    _write(tmp_path, ".git/objects/pack/x.pack", 400)
    _write(tmp_path, "empty.py", 0)

    found = scan(ScanConfig(roots=[tmp_path], sample_pdfs=0))

    tiered = sum(found.tier(name).bytes for name in ("extractor", "ocr", "none"))
    assert found.total.bytes == tiered + found.git.bytes
    assert found.git.bytes == 400
    assert found.empty_files == 1


def test_git_is_counted_and_never_offered_to_a_reader(tmp_path):
    r"""**The only directory this walk enters that the indexer prunes.**

    On a corpus of repositories the object store is routinely most of the disk,
    and somebody deciding whether to index 600GB needs to know that 200GB of it
    is git internals. But nothing inside `.git` is ever indexed, so putting
    `.pack` in the file-type table would be a row nobody can act on.
    """
    _write(tmp_path, ".git/config", 50)
    _write(tmp_path, ".git/objects/ab/cdef", 500)
    _write(tmp_path, "src/main.py", 10)

    found = scan(ScanConfig(roots=[tmp_path], sample_pdfs=0))

    assert found.git.files == 2
    assert found.git.bytes == 550
    assert found.indexable.files == 1
    assert ".pack" not in found.by_extension
    # `.git/config` has no suffix; it must not have leaked into the (none)
    # bucket either, or the extension table double-counts it.
    assert NO_EXTENSION not in found.by_extension


def test_a_pruned_subtree_is_named_rather_than_merely_absent(tmp_path):
    """*"Skip cheaply, but never silently."* A subtree missing from the totals
    with nothing said about it is how somebody concludes their archive was
    never scanned."""
    _write(tmp_path, "node_modules/left-pad/index.js", 10)
    _write(tmp_path, "app/node_modules/x/y.js", 10)
    _write(tmp_path, "app/main.py", 10)

    found = scan(ScanConfig(roots=[tmp_path], sample_pdfs=0))

    assert found.pruned == {"node_modules": 2}
    assert found.total.files == 1
    assert "node_modules x2" in "\n".join(format_report(found))


def test_all_descends_into_what_the_indexer_would_prune(tmp_path):
    _write(tmp_path, "node_modules/x/y.js", 10)

    found = scan(ScanConfig(roots=[tmp_path], sample_pdfs=0, include_excluded=True))

    assert found.total.files == 1
    assert not found.pruned


def test_overlapping_roots_are_counted_once(tmp_path):
    """Two roots where one contains the other is an ordinary configuration,
    and double-counting it would double every estimate downstream."""
    _write(tmp_path, "a/b.py", 100)

    found = scan(ScanConfig(roots=[tmp_path, tmp_path / "a"], sample_pdfs=0))

    assert found.total.files == 1
    assert found.total.bytes == 100


def test_a_missing_root_is_reported_not_ignored(tmp_path):
    found = scan(ScanConfig(roots=[tmp_path / "nope"], sample_pdfs=0))

    assert found.missing_roots == (str(tmp_path / "nope"),)
    assert "does not exist" in "\n".join(format_report(found))


def test_extensionless_files_are_not_decided_once_and_reused(tmp_path):
    r"""**The bucket that cannot be cached, and the reason.**

    `Makefile` is read and `some-binary` is not, and `Path.suffix` is `""` for
    both. Deciding the `(none)` bucket from the first file the walk happens to
    meet attributes every extensionless file on the disk to whichever kind that
    was - which on a corpus with one Makefile and 40,000 Unix binaries is a
    40,000-file error in the "would be indexed" total.
    """
    _write(tmp_path, "Makefile", 10)
    _write(tmp_path, "some-binary", 90)

    found = scan(ScanConfig(roots=[tmp_path], sample_pdfs=0))

    assert found.by_extension[NO_EXTENSION].files == 2
    assert found.tier("extractor").files == 1
    assert found.tier("none").files == 1
    assert found.tier_of[NO_EXTENSION] == "mixed"


def test_a_file_over_the_ceiling_is_not_counted_as_indexable(tmp_path):
    _write(tmp_path, "big.py", 5_000)

    found = scan(ScanConfig(roots=[tmp_path], sample_pdfs=0, max_file_bytes=1_000))

    assert found.oversize.files == 1
    assert found.indexable.files == 0
    assert found.total.files == 1                # still part of what is on disk


def test_lock_files_are_excluded_the_way_the_indexer_excludes_them(tmp_path):
    _write(tmp_path, "~$report.docx", 10)
    _write(tmp_path, "report.docx", 10)

    found = scan(ScanConfig(roots=[tmp_path], sample_pdfs=0))

    assert found.excluded_by_name.files == 1
    assert found.indexable.files == 1


def test_an_unreadable_folder_is_counted_rather_than_swallowed(tmp_path, monkeypatch):
    """A permission error on one folder is not a reason to abandon a scan -
    but an unreported one is a hole in a total everything else is planned
    from."""
    import app.index.scan as module

    real = module.os.scandir
    target = tmp_path / "locked"
    target.mkdir()
    _write(tmp_path, "a.py", 10)

    def refuse(path):
        if str(path) == str(target):
            raise PermissionError(str(path))
        return real(path)

    monkeypatch.setattr(module.os, "scandir", refuse)
    found = scan(ScanConfig(roots=[tmp_path], sample_pdfs=0))

    assert found.unreadable_dirs == 1
    assert found.total.files == 1
    assert "could not be listed" in "\n".join(format_report(found))


def test_the_scan_can_be_stopped_and_says_so(tmp_path):
    for index in range(5):
        _write(tmp_path, f"d{index}/f.py", 10)

    found = scan(ScanConfig(roots=[tmp_path], sample_pdfs=0),
                 should_stop=lambda: True)

    assert found.stopped_early
    assert "partial" in "\n".join(format_report(found))


# --- the PDF sample, which is the OCR estimate ------------------------------

def test_the_scanned_share_ignores_the_pdfs_nobody_could_open(tmp_path):
    """**The denominator bug, kept.**

    A sample of five where three are corrupt and one of the remaining two is
    scanned is 50% scanned, not 20%. Dividing by the whole sample folds "could
    not tell" in with "has text", which understates the OCR bill by however
    many files were unreadable - and unreadable PDFs cluster in exactly the old
    archives most likely to be scans.
    """
    for index in range(4):
        _write(tmp_path, f"p{index}.pdf", 10)

    answers = {
        "p0.pdf": PageProbe(pages=0, chars=0, failed=True),
        "p1.pdf": PageProbe(pages=0, chars=0, failed=True),
        "p2.pdf": PageProbe(pages=10, chars=0),        # scanned
        "p3.pdf": PageProbe(pages=10, chars=5_000),    # has text
    }
    found = scan(ScanConfig(roots=[tmp_path]), probe=lambda p: answers[p.name])

    assert found.pdfs_probed == 4
    assert found.pdfs_unreadable == 2
    assert found.pdfs_readable == 2
    assert found.estimated_scanned_pdfs == 2         # 50% of 4, not 25%


def test_nothing_probed_reports_unknown_rather_than_none(tmp_path):
    """A confident "0 scanned PDFs" about a corpus nobody looked at is exactly
    the sort of number that gets planned around."""
    _write(tmp_path, "a.pdf", 10)

    found = scan(ScanConfig(roots=[tmp_path], sample_pdfs=0))

    assert found.estimated_scanned_pdfs is None
    assert found.estimated_ocr_hours is None
    report = "\n".join(format_report(found))
    assert "unknown" in report
    assert "0 scanned" not in report


def test_the_ocr_estimate_respects_the_per_file_page_cap(tmp_path):
    """`ocr.MAX_PAGES` is real, and ignoring it triples the estimate for a
    corpus of 400-page scanned manuals."""
    from app.extract.ocr import MAX_PAGES

    _write(tmp_path, "a.pdf", 10)

    found = scan(ScanConfig(roots=[tmp_path]),
                 probe=lambda p: PageProbe(pages=4_000, chars=0))

    assert found.estimated_ocr_pages == MAX_PAGES


def test_images_count_one_page_each(tmp_path):
    _write(tmp_path, "a.png", 10)
    _write(tmp_path, "b.jpg", 10)
    _write(tmp_path, "c.pdf", 10)

    found = scan(ScanConfig(roots=[tmp_path]),
                 probe=lambda p: PageProbe(pages=1, chars=9_000))

    assert found.estimated_ocr_pages == 2
    assert found.estimated_ocr_hours == pytest.approx(2 * OCR_SECONDS_PER_PAGE / 3600)


def test_a_probe_that_raises_costs_one_file_not_the_scan(tmp_path):
    _write(tmp_path, "a.pdf", 10)
    _write(tmp_path, "b.pdf", 10)

    def explode(path):
        raise RuntimeError("mupdf fell over")

    found = scan(ScanConfig(roots=[tmp_path]), probe=explode)

    assert found.pdfs_probed == 2
    assert found.pdfs_unreadable == 2
    assert found.estimated_scanned_pdfs is None


def test_the_same_corpus_samples_the_same_pdfs_twice(tmp_path):
    """A measurement whose answer moves by 4% between runs is a measurement
    nobody trusts, and the sample is the only random thing here."""
    for index in range(50):
        _write(tmp_path, f"p{index:02d}.pdf", 10)

    seen: list[set[str]] = []
    for _ in range(2):
        names: set[str] = set()

        def note(path, names=names):
            names.add(path.name)
            return PageProbe(pages=1, chars=1_000)

        scan(ScanConfig(roots=[tmp_path], sample_pdfs=10), probe=note)
        seen.append(names)

    assert len(seen[0]) == 10
    assert seen[0] == seen[1]


# --- turning bytes into hours -----------------------------------------------

def test_no_measured_rate_means_no_estimate(tmp_path):
    """*"a guessed rate is how a five-day estimate becomes fifteen."*"""
    found = ScanResult(by_tier={"extractor": Tally(files=10, bytes=10 * 1_048_576)})

    assert found.hours_at(0) is None
    assert found.hours_at(-5) is None
    assert found.hours_at(10) == pytest.approx(10 / 10 / 60)


def test_the_time_estimate_leaves_out_the_images():
    """MB/min is measured with OCR off - see §7 of the work order - so applying
    it to image bytes counts the most expensive files at the cheapest rate."""
    found = ScanResult(by_tier={
        "extractor": Tally(files=1, bytes=600 * 1_048_576),
        "ocr": Tally(files=1, bytes=400 * 1_048_576),
    })

    assert found.hours_at(60) == pytest.approx(600 / 60 / 60)


def test_the_report_says_it_does_not_know_rather_than_guessing():
    lines = "\n".join(format_report(ScanResult()))

    assert "Not estimated" in lines


# --- what the next run reads ------------------------------------------------

def test_a_scan_of_these_folders_becomes_the_progress_denominator():
    raw = json.dumps({"roots": [r"D:\Archive", r"D:\Current"], "files": 4_000})

    assert saved_total(raw, [r"D:\Archive", r"D:\Current"]) == 4_000


def test_a_scan_of_different_folders_is_refused():
    r"""**A wrong percentage is believed; an obviously-unknown one is not.**

    A total from a scan of `D:\Archive` used for a run over `D:\Current`
    produces a bar that stops at 4%, or one that reaches 100% in a minute and
    then sits there for a day. Both are worse than the growing denominator
    `progress_for` falls back to.
    """
    raw = json.dumps({"roots": [r"D:\Archive"], "files": 4_000})

    assert saved_total(raw, [r"D:\Current"]) == 0
    assert saved_total(raw, [r"D:\Archive", r"D:\Current"]) == 0
    assert saved_total(raw, []) == 0


def test_a_subset_is_refused_too():
    """Scanning two folders and indexing one of them gives a denominator that
    is too big - the bar would stop short of the end for ever."""
    raw = json.dumps({"roots": [r"D:\A", r"D:\B"], "files": 4_000})

    assert saved_total(raw, [r"D:\A"]) == 0


@pytest.mark.parametrize("raw", ["", "not json", "{}", '{"roots": 3}', "[]"])
def test_an_unreadable_record_is_no_record(raw):
    """0 is what `progress_for` already treats as "no estimate", so the caller
    needs no branch and a corrupt state key cannot crash a run."""
    assert saved_total(raw, [r"D:\A"]) == 0


def test_trailing_separators_and_case_do_not_break_the_match():
    """These are Windows paths written by one component and read by another,
    and the same folder routinely appears with different casing in the two."""
    raw = json.dumps({"roots": [r"D:\Archive"], "files": 7})

    assert saved_total(raw, [r"d:\archive"]) == 7


# --- the report is read by people -------------------------------------------

def test_sizes_are_readable_rather_than_exact():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(1_600_000_000_000).endswith("TB")


def test_the_json_shape_carries_every_number_the_report_shows(tmp_path):
    _write(tmp_path, "a.py", 10)
    _write(tmp_path, "b.png", 10)

    payload = scan(ScanConfig(roots=[tmp_path], sample_pdfs=0)).as_dict()

    assert payload["total"]["files"] == 2
    assert payload["by_tier"]["ocr"]["files"] == 1
    assert payload["by_extension"][".py"]["tier"] == "extractor"
    assert payload["ocr"]["seconds_per_page"] == OCR_SECONDS_PER_PAGE
    assert payload["ocr"]["estimated_pages"] is None
