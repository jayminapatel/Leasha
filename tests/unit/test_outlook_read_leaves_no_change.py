r"""Reading an archive through Outlook does not make it look changed to the next run.

Layer: L3

2026-10-25. Reading a `.pst` through Outlook mounts it, and Outlook writes to it while it is
mounted - its header counters move (`bidNextP`, `BREFNBT`...). The marker the index kept was
the one the scan took *before* the read, so the next run logged "will be read again: its
header moved since the last read" for 2007, 2010 and 2011.pst, the same size to the byte,
and read every archive again, in full, on every run. The closing row now records the archive
as the read left it (`Pipeline._after_outlook_read`).
"""

from __future__ import annotations

from app.extract.email_pst import archive_marker
from tests.unit.test_pst_change_marker import (_an_hour_ago, _archive, _candidate,
                                               _classifier)


def _read_through_outlook(tmp_path, pipeline, *, record_after_read: bool):
    """The scan's marker, then Outlook's writes, then the closing row - as a run does it."""
    from app.index.pipeline import _Extracted

    archive = _archive(tmp_path)
    _an_hour_ago(archive)
    before = _candidate(archive)
    scanned = archive_marker(archive)
    # Outlook's mount: the counters it advances move, the size does not.
    archive.write_bytes(_archive(tmp_path, next_page=0x4D8A8BB, next_block=0x30FF41C,
                                 nbt=(0x4D8A8B9, 0xC7C5E000)).read_bytes())
    _an_hour_ago(archive)
    after = pipeline._after_outlook_read(before, scanned) if record_after_read else None
    pipeline._write_marker(_Extracted(
        candidate=before, content_hash=scanned, key=str(archive), source_kind="archive",
        first_of_file=False, file_marker=True, after_read=after))
    return archive


def test_the_next_run_finds_an_archive_read_through_outlook_unchanged(tmp_path):
    from app.index.pipeline import UNCHANGED

    store, pipeline = _classifier(tmp_path)
    try:
        archive = _read_through_outlook(tmp_path, pipeline, record_after_read=True)
        assert pipeline._classify(_candidate(archive)) is UNCHANGED, (
            "Outlook's own writes during the read made the archive look changed")
    finally:
        store.close()


def test_recording_the_scans_marker_reads_it_again_the_fault_this_fixes(tmp_path):
    """The control: the marker taken before the read is what made every run re-read."""
    from app.index.pipeline import UNCHANGED

    store, pipeline = _classifier(tmp_path)
    try:
        archive = _read_through_outlook(tmp_path, pipeline, record_after_read=False)
        assert pipeline._classify(_candidate(archive)) is not UNCHANGED
    finally:
        store.close()


def test_only_a_pst_with_a_readable_header_is_re_marked(tmp_path):
    store, pipeline = _classifier(tmp_path)
    try:
        archive = _archive(tmp_path)
        candidate = _candidate(archive)
        marker = archive_marker(archive)
        found = pipeline._after_outlook_read(candidate, marker)
        assert found is not None and found[0] == marker and found[1] == archive.stat().st_size
        assert pipeline._after_outlook_read(candidate, None) is None, "no marker before: none after"
        ansi = _archive(tmp_path, name="old.pst", version=14)      # no marker for ANSI
        assert pipeline._after_outlook_read(_candidate(ansi), "pst-marker:x") is None
        note = tmp_path / "note.txt"
        note.write_text("x", encoding="utf-8")
        assert pipeline._after_outlook_read(_candidate(note), "h") is None
    finally:
        store.close()
