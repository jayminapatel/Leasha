r"""What is inside the corpus's archives, without opening any of it.

Layer: L3 (measurement)

`docs/WORKORDER-zip-archives.md` §6 is the reason this exists:

> *Not because it is unimportant - because the cost is unknown. The `scan`
> command reports how many archives exist and how large. Deciding before that
> number exists is guessing, and guessing at 1.5TB is expensive.*

So this is the measurement that gates section 3, and it has one hard
constraint: **it must not become the thing it is measuring**. A scan that reads
inside archives to find out whether reading inside archives is worthwhile has
already paid the cost it was supposed to be estimating. Every test below is
ultimately about that line - the numbers come from each archive's own central
directory, which is a few bytes per member at the tail of the file, and nothing
is ever decompressed.

The other half is that a declaration is not a measurement. A zip states what its
members will expand to, and a crafted one states whatever it likes. Those tests
are the ones about `BOMB_RATIO` and `ARCHIVE_MEMBER_LIMIT`.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from app.index.scan import (
    BOMB_MIN_BYTES,
    NO_EXTENSION,
    ArchiveProbe,
    ScanConfig,
    _declared_members,
    _probe_archive,
    _routing,
    format_report,
    scan,
)


@pytest.fixture(scope="module")
def routing():
    """Resolved once: it reads `extractors.toml`."""
    return _routing()


def _zip(path: Path, members: dict[str, bytes], *, compress=zipfile.ZIP_DEFLATED) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compress) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return path


# --- the central directory, and nothing else --------------------------------

def test_members_are_counted_from_the_index_not_from_reading(tmp_path, routing, monkeypatch):
    r"""**The constraint the whole feature rests on.**

    `ZipFile.open` is the only door to a member's bytes, so making it raise
    proves the claim rather than asserting it in a comment. If this test ever
    starts failing because the probe "just needed to peek at one member", the
    probe has become an extractor and section 3 arrived early and undesigned.
    """
    archive = _zip(tmp_path / "reports.zip", {
        "q3/summary.docx": b"x" * 5_000,
        "q3/notes.txt": b"Northern pump station commissioning. " * 50,
    })

    def refuse(*args, **kwargs):
        raise AssertionError("the scan decompressed a member")

    monkeypatch.setattr(zipfile.ZipFile, "open", refuse)
    monkeypatch.setattr(zipfile.ZipFile, "read", refuse)

    found = _probe_archive(archive, routing)

    assert found.members == 2
    assert not found.failed


def test_a_folder_entry_is_not_a_file(tmp_path, routing):
    """Zips record directories as zero-length members. Counting them inflates
    the member total by however deep the tree is, which on a backup of a source
    checkout is most of it."""
    archive = _zip(tmp_path / "tree.zip", {
        "src/": b"", "src/deep/": b"", "src/deep/main.py": b"print('x')\n",
    })

    assert _probe_archive(archive, routing).members == 1


def test_member_types_are_routed_by_the_same_rules_as_files_on_disk(tmp_path, routing):
    """**The number section 3 turns on**, so it must not be a second opinion.

    `.dwg` counts because a converter is configured for it, `.bin` does not
    because nothing reads it, and `Makefile` does because the named-file
    registry says so - exactly as each would on disk.
    """
    archive = _zip(tmp_path / "mixed.zip", {
        "a.docx": b"x" * 100, "b.dwg": b"y" * 100, "Makefile": b"all:\n",
        "c.bin": b"z" * 100, "noextension": b"q",
    })

    found = _probe_archive(archive, routing)

    assert found.members == 5
    assert found.readable == 3                     # docx, dwg, Makefile
    assert found.by_extension[".docx"] == 1
    assert found.by_extension["makefile"] == 1     # a name, so no leading dot
    assert found.by_extension[NO_EXTENSION] == 1


def test_a_named_member_is_not_labelled_as_an_extension(tmp_path, routing):
    """`.makefile` is not a file type, and a report that invents one teaches
    the reader to distrust the rest of the table."""
    archive = _zip(tmp_path / "named.zip", {"Makefile": b"all:\n"})

    assert ".makefile" not in _probe_archive(archive, routing).by_extension


def test_backslashes_in_a_member_name_do_not_survive(tmp_path, routing):
    r"""Zip names use forward slashes by specification, but plenty of Windows
    tools have written `q3\report.docx` anyway. On Linux `Path(...).suffix` on
    the whole string still finds `.docx`, but the *name* would carry the
    directory with it - the backslash trap this project has now hit seven
    times."""
    path = tmp_path / "windows.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("q3\\report.docx", b"x" * 50)

    assert _probe_archive(path, routing).by_extension == {".docx": 1}


# --- what a declaration is worth --------------------------------------------

def test_an_encrypted_member_is_seen_without_a_password(tmp_path, routing):
    """Read from the flag bits, so it is known before anything is attempted.
    412 files nobody can read is a finding; a decryption prompt in an indexer
    is not."""
    path = tmp_path / "locked.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("secret.txt", b"x" * 100)
    # Set the encryption bit in both the local and central headers, which is
    # what a real encrypted archive carries.
    raw = bytearray(path.read_bytes())
    for signature in (b"PK\x03\x04", b"PK\x01\x02"):
        at = raw.find(signature)
        offset = 6 if signature == b"PK\x03\x04" else 8
        raw[at + offset] |= 0x1
    path.write_bytes(bytes(raw))

    assert _probe_archive(path, routing).encrypted == 1


def test_a_huge_declared_expansion_is_counted_apart(tmp_path, routing):
    """A member claiming 8MB from a few hundred bytes does not join the corpus
    total. **Nothing is decompressed to decide that** - the sizes are both in
    the header, which is the work order's *"check the header, not the read"*."""
    archive = _zip(tmp_path / "bomb.zip", {
        "big.bin": b"\x00" * (8 * BOMB_MIN_BYTES),
        "ordinary.txt": b"real text that compresses normally. " * 20,
    })

    found = _probe_archive(archive, routing)

    assert found.bombs == 1
    assert found.uncompressed < BOMB_MIN_BYTES     # the 8MB is not in the total


def test_a_small_compressible_member_is_not_a_bomb(tmp_path, routing):
    r"""**The regression that made the ratio alone useless.**

    A 5KB log of one repeated line compresses a thousand to one, and so does a
    zero-padded header and a mostly-empty XML stub. Judged on ratio alone every
    one of them was excluded from the size estimate - so the measurement built
    to answer *"how much is locked up in archives"* quietly under-reported the
    ordinary contents of every archive it looked at. A bomb is dangerous
    because of what it expands *to*, so the expansion is what qualifies it.
    """
    archive = _zip(tmp_path / "logs.zip", {
        "app.log": b"the same line over and over\n" * 300,
        "pad.bin": b"\x00" * 9_000,
    })

    found = _probe_archive(archive, routing)

    assert found.bombs == 0
    assert found.uncompressed > 9_000              # both are in the total


# --- refusing before parsing ------------------------------------------------

def test_a_declared_member_count_is_read_without_parsing_the_directory(tmp_path):
    archive = _zip(tmp_path / "three.zip", {"a": b"1", "b": b"2", "c": b"3"})

    assert _declared_members(archive) == 3


def test_a_zip64_member_count_is_followed_through_the_locator(tmp_path):
    """ZIP64 is not exotic - it is every archive over 65,535 entries or 4GB,
    which is precisely the population this measurement is about. Refusing them
    all would have been a guard that hid the answer."""
    path = tmp_path / "big.zip"
    with zipfile.ZipFile(path, "w", allowZip64=True, strict_timestamps=False) as archive:
        for index in range(5):
            archive.writestr(f"member{index}.txt", b"x")
    raw = bytearray(path.read_bytes())
    # Force the ZIP64 path: the EOCD says 0xFFFF and the real count lives in
    # the ZIP64 end record. Written by hand because producing 65,536 members to
    # test this would make the suite unusable.
    eocd = raw.rfind(b"PK\x05\x06")
    central = struct.unpack("<I", raw[eocd + 16:eocd + 20])[0]
    zip64 = bytearray(b"PK\x06\x06")
    zip64 += struct.pack("<Q", 44)                 # size of the record that follows
    zip64 += struct.pack("<HH", 45, 45)
    zip64 += struct.pack("<II", 0, 0)
    zip64 += struct.pack("<QQ", 5, 5)              # entries on this disk, and in total
    zip64 += struct.pack("<QQ", eocd - central, central)
    locator = bytearray(b"PK\x06\x07")
    locator += struct.pack("<I", 0)
    locator += struct.pack("<Q", eocd)             # where the ZIP64 record now sits
    locator += struct.pack("<I", 1)
    raw[eocd + 10:eocd + 12] = struct.pack("<H", 0xFFFF)
    path.write_bytes(bytes(raw[:eocd]) + bytes(zip64) + bytes(locator) + bytes(raw[eocd:]))

    assert _declared_members(path) == 5


def test_an_archive_declaring_millions_of_members_is_refused_before_zipfile_sees_it(
    tmp_path, routing, monkeypatch
):
    r"""**The guard that stops a stat-only scan taking the machine down.**

    `infolist()` builds a `ZipInfo` per entry, eagerly. Forty million of them
    is tens of gigabytes of memory to *list* an archive - no decompression
    involved, so neither the ratio check nor the byte ceiling sees it coming.
    The count therefore has to be read from the end record and refused before
    `zipfile` is handed the file at all, which is what this asserts by making
    the constructor fail.
    """
    path = _zip(tmp_path / "many.zip", {"a.txt": b"x", "b.txt": b"y"})
    # The limit is lowered rather than the archive inflated: writing 200,001
    # members to prove a ceiling would put a minute into every suite run, and
    # the mechanism under test is the *order* of the two checks, not the number.
    monkeypatch.setattr("app.index.scan.ARCHIVE_MEMBER_LIMIT", 1)

    def refuse(*args, **kwargs):
        raise AssertionError("zipfile was given an archive that should be refused")

    monkeypatch.setattr(zipfile.ZipFile, "__init__", refuse)

    found = _probe_archive(path, routing)

    assert found.failed
    assert "members" in found.reason


def test_a_corrupt_archive_is_a_finding_with_a_reason(tmp_path, routing):
    """One bad archive in 600GB must be a line in a report, never the end of
    the scan - and the reason is kept, because "31 could not be listed" is not
    actionable and "31 are truncated" is."""
    path = tmp_path / "broken.zip"
    path.write_bytes(b"PK\x03\x04" + b"garbage" * 20)

    found = _probe_archive(path, routing)

    assert found.failed and found.reason


# --- the walk, and what it extrapolates -------------------------------------

def _corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    _zip(root / "reports.zip", {"a.docx": b"x" * 200, "b.txt": b"y" * 200})
    _zip(root / "more.zip", {"c.docx": b"x" * 200})
    (root / "old.7z").write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 400)
    (root / "notes.txt").write_text("ordinary", encoding="utf-8")
    return root


def test_the_scan_separates_archives_it_can_see_inside_from_ones_it_cannot(tmp_path):
    """`.7z` and `.rar` need a dependency, and whether to take one is a
    separate decision from whether to read zips. Merging them into one number
    would answer neither question."""
    result = scan(ScanConfig(roots=[_corpus(tmp_path)], sample_pdfs=0))

    assert result.archives.files == 3
    assert result.opaque_archives.files == 1
    assert result.zip_family.files == 2
    assert result.archives_probed == 2             # the .7z was never opened


def test_office_documents_are_not_counted_as_archives(tmp_path):
    r"""**A deliberate exclusion, and the one most likely to be "fixed" later.**

    `.docx`, `.odt`, `.epub` and `.xlsx` are all zip containers with extractors
    already. Counting them here would put the entire Office corpus into the row
    that is supposed to answer *"how much is locked up inside archives"*, and
    the answer would come back as "most of it" on every corpus in the world.
    """
    root = tmp_path / "office"
    _zip(root / "report.docx", {"word/document.xml": b"<w:p/>"})
    _zip(root / "sheet.xlsx", {"xl/workbook.xml": b"<x/>"})
    _zip(root / "book.epub", {"content.opf": b"<p/>"})

    result = scan(ScanConfig(roots=[root], sample_pdfs=0))

    assert result.archives.files == 0


def test_the_sample_is_extrapolated_to_the_corpus(tmp_path):
    """Two archives, both listed, five members between them - so the estimate
    is the sample. The scaling matters when the sample is 200 of 80,000."""
    result = scan(ScanConfig(roots=[_corpus(tmp_path)], sample_pdfs=0))

    assert result.archive_members == 3
    assert result.estimated_archive_members == 3
    assert result.estimated_readable_members == 3


def test_nothing_listed_is_not_the_same_answer_as_nothing_inside(tmp_path):
    r"""A confident *"0 files inside your archives"* on a corpus nobody could
    open is exactly the kind of number that gets planned around. `None` prints
    differently, and that is the whole point of it."""
    root = tmp_path / "bad"
    root.mkdir()
    (root / "one.zip").write_bytes(b"not a zip at all")

    result = scan(ScanConfig(roots=[root], sample_pdfs=0))

    assert result.archives_probed == 1
    assert result.archives_listed == 0
    assert result.estimated_archive_members is None
    assert "unknown" in "\n".join(format_report(result))


def test_the_sample_can_be_switched_off_entirely(tmp_path):
    """`--no-sample` means open nothing. The archives are still counted - that
    costs a `stat` the walk already did."""
    result = scan(ScanConfig(roots=[_corpus(tmp_path)], sample_pdfs=0, sample_archives=0))

    assert result.archives.files == 3
    assert result.archives_probed == 0
    assert "unknown" in "\n".join(format_report(result))


def test_the_report_says_what_it_did_not_do(tmp_path):
    """The claim is unusual enough to be worth stating in the output: these
    numbers cost a seek per archive, not a read of 240GB."""
    lines = "\n".join(format_report(scan(ScanConfig(roots=[_corpus(tmp_path)], sample_pdfs=0))))

    assert "Nothing was decompressed" in lines
    assert "3 archive file(s)" in lines


def test_no_archives_means_no_archive_section(tmp_path):
    """A block of zeroes on every scan of every corpus without archives is
    noise, and noise is what teaches people to stop reading a report."""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")

    assert "Archives" not in "\n".join(format_report(scan(ScanConfig(roots=[root], sample_pdfs=0))))


def test_the_probe_is_a_seam(tmp_path):
    """Same shape as the PDF probe: the tests drive the estimate without any
    archives, and a failure to open one never reaches the caller."""
    root = tmp_path / "seam"
    _zip(root / "a.zip", {"x.txt": b"x"})

    result = scan(
        ScanConfig(roots=[root], sample_pdfs=0),
        archive_probe=lambda path: ArchiveProbe(members=900, readable=400,
                                                uncompressed=1_000_000),
    )

    assert result.estimated_archive_members == 900
    assert result.estimated_readable_members == 400


def test_a_probe_that_raises_is_recorded_rather_than_escaping(tmp_path):
    root = tmp_path / "raises"
    _zip(root / "a.zip", {"x.txt": b"x"})

    def explode(path):
        raise RuntimeError("no")

    result = scan(ScanConfig(roots=[root], sample_pdfs=0), archive_probe=explode)

    assert result.archives_unreadable == 1
    assert result.archive_failures


def test_the_numbers_reach_the_json_summary(tmp_path):
    """`--json` is how a scheduled measurement is read, and it must not be a
    less honest account than the printed one."""
    payload = scan(ScanConfig(roots=[_corpus(tmp_path)], sample_pdfs=0)).as_dict()

    assert payload["archives"]["files"] == 3
    assert payload["archives"]["opaque"]["files"] == 1
    assert payload["archives"]["estimated_readable_members"] == 3
    assert payload["archives"]["member_types_in_sample"][".docx"] == 2
