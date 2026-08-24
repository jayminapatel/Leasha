"""Layer 2 acceptance tests.

The criteria from BUILD_SPEC_V2.md, verbatim:

  1. A fixtures folder with one healthy and one deliberately corrupt file of
     each type.
  2. Every healthy fixture yields non-empty text and plausible chunk counts.
  3. Every corrupt fixture yields ERR_FILE_CORRUPT with SKIP_CONTINUE - and the
     run continues.
  4. A password-protected PDF and a locked-open XLSX both skip cleanly.
  5. PST extraction over a small test archive preserves conversation grouping.
  6. The live Outlook mailbox is enumerated alongside .pst stores, and closing
     Outlook mid-run yields ERR_OUTLOOK_BUSY rather than failing the run.
  7. A OneDrive folder with Files On-Demand indexes pinned files and skips
     placeholders, and no placeholder is hydrated.
  8. Chunk overlap verified: no text lost at boundaries.

**Criteria 5 and 6 are not covered here.** They need `win32com` and a live
Outlook, so they belong to the PST pass and are marked `xfail(run=False)` rather
than quietly omitted - an untested criterion that looks tested is worse than one
that is visibly outstanding.

Criterion 7 is covered at the attribute level: `winfs.is_cloud_placeholder`
accepts injected attribute bits, so placeholder *detection* is proved off
Windows. That no placeholder is hydrated must still be confirmed by watching the
sync client on a real machine, exactly as the spec says.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from app.core.errors import ActionType, AppErrorException
from app.core.winfs import (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    describe_placeholder,
    is_cloud_placeholder,
)
from app.extract import chunk_document, extract
from app.extract.chunker import chunk_text

HEALTHY_OF_EACH_TYPE = {
    "pdf": "pdf/healthy.pdf",
    "docx": "office/healthy.docx",
    "xlsx": "office/healthy.xlsx",
    "pptx": "office/healthy.pptx",
    "eml": "email/thread_root.eml",
    "txt": "plaintext/utf8.txt",
    "csv": "plaintext/table.csv",
}

BAD_OF_EACH_TYPE = {
    "pdf-corrupt": "pdf/truncated.pdf",
    "pdf-encrypted": "pdf/encrypted.pdf",
    "pdf-scanned": "pdf/scanned.pdf",
    "pdf-lying-extension": "corrupt/lies.pdf",
    "docx-empty": "corrupt/empty.docx",
    "xlsx-not-a-package": "corrupt/notoffice.xlsx",
    "unsupported": "corrupt/unsupported.xyz",
    "txt-binary": "plaintext/binary.log",
    "eml-no-content": "email/empty_body.eml",
}


# --- 1 ----------------------------------------------------------------------

def test_fixture_corpus_covers_every_type(fixture_root: Path) -> None:
    for name in (*HEALTHY_OF_EACH_TYPE.values(), *BAD_OF_EACH_TYPE.values()):
        assert (fixture_root / name).exists(), f"missing fixture: {name}"


# --- 2 ----------------------------------------------------------------------

@pytest.mark.parametrize("name", list(HEALTHY_OF_EACH_TYPE.values()), ids=list(HEALTHY_OF_EACH_TYPE))
def test_healthy_fixtures_yield_text_and_plausible_chunks(fixture_root: Path, name: str) -> None:
    documents = list(extract(fixture_root / name))
    assert len(documents) == 1

    document = documents[0]
    assert len(document.text.strip()) > 20

    chunks = chunk_document(document)
    assert chunks, "text but no chunks"
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk.text.strip()
        assert document.text[chunk.char_start:chunk.char_end] == chunk.text


# --- 3 ----------------------------------------------------------------------

@pytest.mark.parametrize("name", list(BAD_OF_EACH_TYPE.values()), ids=list(BAD_OF_EACH_TYPE))
def test_bad_fixtures_skip_and_continue(fixture_root: Path, name: str) -> None:
    with pytest.raises(AppErrorException) as caught:
        list(extract(fixture_root / name))

    error = caught.value.error
    assert error.action_type is ActionType.SKIP_CONTINUE
    assert error.suggestion.strip()
    assert not error.is_fatal


def test_a_batch_survives_every_bad_file_in_the_corpus(fixture_root: Path) -> None:
    """The layer's whole purpose: one bad file never halts a 100GB run."""
    everything = sorted(
        path
        for path in fixture_root.rglob("*")
        if path.is_file() and path.suffix not in {".py", ".md"} and path.name != ".gitkeep"
    )
    assert len(everything) >= 15

    indexed, skipped = 0, 0
    for path in everything:
        try:
            for document in extract(path):
                chunk_document(document)
                indexed += 1
        except AppErrorException:
            skipped += 1

    assert indexed >= 7
    assert skipped >= 8
    assert indexed + skipped == len(everything)


# --- 4 ----------------------------------------------------------------------

def test_password_protected_pdf_skips_cleanly(fixture_root: Path) -> None:
    with pytest.raises(AppErrorException) as caught:
        list(extract(fixture_root / "pdf/encrypted.pdf"))
    error = caught.value.error
    assert error.code == "ERR_FILE_CORRUPT"
    assert error.action_type is ActionType.SKIP_CONTINUE
    assert "password" in error.details.lower()


def test_locked_file_maps_to_the_locked_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file held open by Excel raises PermissionError on Windows.

    POSIX will not reproduce that lock, so the *mapping* is what is proved here:
    a PermissionError must become ERR_FILE_LOCKED ('close the program holding
    it'), never ERR_FILE_CORRUPT ('this file is damaged'). Sending someone to
    repair a perfectly good file is the failure this guards against.
    """
    target = tmp_path / "budget.xlsx"
    target.write_bytes(b"irrelevant - load_workbook is patched")

    import openpyxl

    def refuse(*_args, **_kwargs):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(openpyxl, "load_workbook", refuse)

    with pytest.raises(AppErrorException) as caught:
        list(extract(target))

    error = caught.value.error
    assert error.code == "ERR_FILE_LOCKED"
    assert error.action_type is ActionType.SKIP_CONTINUE
    assert "close" in error.suggestion.lower()


# --- 5 and 6: PST. Outstanding, and visibly so. -----------------------------

@pytest.mark.xfail(run=False, reason="Needs win32com and a live Outlook; PST is its own pass.")
def test_pst_preserves_conversation_grouping() -> None:
    raise AssertionError("not implemented")


@pytest.mark.xfail(run=False, reason="Needs win32com and a live Outlook; PST is its own pass.")
def test_closing_outlook_mid_run_yields_err_outlook_busy() -> None:
    raise AssertionError("not implemented")


# --- 7 ----------------------------------------------------------------------

def test_cloud_placeholders_are_detected_before_anything_reads_them(tmp_path: Path) -> None:
    """Detection is by attribute bits, so it never opens the file - which is the
    point: opening a placeholder is what triggers the download."""
    pinned = tmp_path / "pinned.txt"
    pinned.write_text("locally available", encoding="utf-8")

    assert not is_cloud_placeholder(pinned, attributes=0)
    assert is_cloud_placeholder(pinned, attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    assert describe_placeholder(FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS) == "RECALL_ON_DATA_ACCESS"


def test_cloud_only_error_explains_the_tradeoff() -> None:
    from app.core.errors import make_error

    error = make_error("ERR_CLOUD_ONLY", "index.walker", path="D:\\OneDrive\\report.docx")
    assert error.action_type is ActionType.SKIP_CONTINUE
    assert "Always keep on this device" in error.suggestion


# --- 8 ----------------------------------------------------------------------

def test_no_text_is_lost_at_chunk_boundaries(fixture_root: Path) -> None:
    """Across every healthy fixture, at a chunk size small enough to force many
    boundaries, every word must survive into at least one chunk."""
    import re

    word = re.compile(r"\S+")

    for name in HEALTHY_OF_EACH_TYPE.values():
        document = next(iter(extract(fixture_root / name)))
        chunks = chunk_text(document.text, target_tokens=24, overlap_tokens=6)
        spans = [(chunk.char_start, chunk.char_end) for chunk in chunks]

        missing = [
            match.group()
            for match in word.finditer(document.text)
            if not any(start <= match.start() and match.end() <= end for start, end in spans)
        ]
        assert not missing, f"{name}: {len(missing)} word(s) lost at a boundary: {missing[:5]}"


def test_overlap_actually_overlaps(fixture_root: Path) -> None:
    document = next(iter(extract(fixture_root / "pdf/healthy.pdf")))
    chunks = chunk_text(document.text, target_tokens=20, overlap_tokens=8, min_chunk_chars=0)
    assert len(chunks) > 1
    for previous, following in pairwise(chunks):
        assert following.char_start < previous.char_end
