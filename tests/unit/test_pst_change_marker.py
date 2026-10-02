r"""An archive whose date moved and whose mail did not is not read again.

2026-10-02. Outlook rewrites the header of every `.pst` it has mounted, so the
file's date moves while nothing in it has. On the owner's laptop all eight
archives in the index had the size they were indexed at and a date 15-17 hours
later, and date and size were the whole change test for an archive - so every
run read every message of every archive again, and an archive interrupted
part-way lost its place the moment Outlook touched it.

`archive_marker` reads 564 bytes of the header and keeps the numbers that move
when mail does. Measured on `2010.pst` and `2011.pst` across an afternoon
mounted in Outlook and its closing: the header's write counter rose by five,
both checksums and the date changed, and every number the marker uses was the
same. The headers below are built to that layout ([MS-PST] 2.2.2.6).
"""

from __future__ import annotations

import json
import os
import pathlib

from app.extract.base import change_marker
from app.extract.email_pst import MARKER_PREFIX, archive_marker


def _header(*, next_page=0x4D8A8B9, next_block=0x30FF418, eof=0xC9080000,
            nbt=(0x4D8A8B7, 0xC7C5E000), bbt=(0x4D8A8B3, 0xC7C5D600),
            unique=1994456, checksums=(0x6E6B2C53, 0x928C3F33),
            version=23, magic=b"!BDN", client=b"SM") -> bytes:
    raw = bytearray(564)
    raw[0:4] = magic
    raw[4:8] = checksums[0].to_bytes(4, "little")
    raw[8:10] = client
    raw[10:12] = version.to_bytes(2, "little")
    raw[32:40] = next_page.to_bytes(8, "little")
    raw[40:44] = unique.to_bytes(4, "little")
    raw[184:192] = eof.to_bytes(8, "little")
    raw[216:224] = nbt[0].to_bytes(8, "little")
    raw[224:232] = nbt[1].to_bytes(8, "little")
    raw[232:240] = bbt[0].to_bytes(8, "little")
    raw[240:248] = bbt[1].to_bytes(8, "little")
    raw[516:524] = next_block.to_bytes(8, "little")
    raw[524:528] = checksums[1].to_bytes(4, "little")
    return bytes(raw) + b"\0" * 2048        # the same size whatever the header says


def _archive(tmp_path, name="2010.pst", **header) -> pathlib.Path:
    path = tmp_path / name
    path.write_bytes(_header(**header))
    return path


# ---------------------------------------------------------------------------
# The marker itself
# ---------------------------------------------------------------------------

class TestWhatTheMarkerIsMadeOf:

    def test_it_is_labelled_so_it_can_never_pass_for_a_hash_of_bytes(self, tmp_path):
        marker = archive_marker(_archive(tmp_path))
        assert marker and marker.startswith(MARKER_PREFIX)

    def test_what_outlook_does_on_mounting_leaves_it_alone(self, tmp_path):
        """The measured case: the write counter and both checksums move."""
        before = archive_marker(_archive(tmp_path))
        after = archive_marker(_archive(
            tmp_path, unique=1994461, checksums=(0x1F8A6B12, 0x3BEFF009)))
        assert before == after

    def test_new_mail_moves_it(self, tmp_path):
        before = archive_marker(_archive(tmp_path))
        for change in (dict(next_block=0x30FF41C), dict(next_page=0x4D8A8BB),
                       dict(nbt=(0x4D8A8B9, 0xC7C5E000)), dict(bbt=(0x4D8A8B3, 0xC7C5D800)),
                       dict(eof=0xC9100000)):
            assert archive_marker(_archive(tmp_path, **change)) != before, change

    def test_a_header_it_does_not_know_gives_no_marker(self, tmp_path):
        """None means "the date and size decide", which is what happened before."""
        assert archive_marker(_archive(tmp_path, magic=b"PK\x03\x04")) is None
        assert archive_marker(_archive(tmp_path, client=b"SO")) is None
        assert archive_marker(_archive(tmp_path, version=14)) is None, "an ANSI archive"
        short = tmp_path / "short.pst"
        short.write_bytes(_header()[:300])
        assert archive_marker(short) is None
        assert archive_marker(tmp_path / "not-there.pst") is None

    def test_only_a_reader_that_offers_one_is_asked(self, tmp_path):
        archive = _archive(tmp_path)
        assert change_marker(archive) == archive_marker(archive)
        note = tmp_path / "note.txt"
        note.write_text("plain text is hashed, not marked", encoding="utf-8")
        assert change_marker(note) is None


# ---------------------------------------------------------------------------
# The change test
# ---------------------------------------------------------------------------

def _classifier(tmp_path):
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[tmp_path]))
    return store, Pipeline(store=store, vectors=None, embedder=None, config=config)


def _candidate(path):
    from app.index.walker import Candidate

    stat = path.stat()
    return Candidate(path=path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)


def _indexed_row(store, path, *, marker, age_ns=3_600_000_000_000):
    """The archive as a finished run left it: indexed, its date an hour older
    than the file's is now."""
    stat = path.stat()
    file_id = store.upsert_file(
        str(path), size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns - age_ns,
        ext="pst", parent_dir=str(path.parent), content_hash=marker)
    store.mark_indexed(file_id)


def _an_hour_ago(path):
    """Out of the window in which a just-written file is not trusted."""
    stamp = path.stat().st_mtime - 3600
    os.utime(path, (stamp, stamp))


class TestTheDateAloneNoLongerMeansChanged:

    def test_an_archive_outlook_only_touched_is_unchanged(self, tmp_path):
        from app.index.pipeline import UNCHANGED

        archive = _archive(tmp_path)
        _an_hour_ago(archive)
        store, pipeline = _classifier(tmp_path)
        try:
            _indexed_row(store, archive, marker=archive_marker(archive))
            archive.write_bytes(_header(unique=1994461, checksums=(7, 9)))
            _an_hour_ago(archive)
            assert store.get_file(str(archive)).mtime_ns != archive.stat().st_mtime_ns

            assert pipeline._classify(_candidate(archive)) is UNCHANGED
            assert pipeline._classify(_candidate(archive), hash_now=False) is UNCHANGED
        finally:
            store.close()

    def test_an_archive_with_new_mail_is_read_and_its_new_marker_kept(self, tmp_path):
        from app.index.pipeline import UNCHANGED

        archive = _archive(tmp_path)
        store, pipeline = _classifier(tmp_path)
        try:
            _indexed_row(store, archive, marker=archive_marker(archive))
            archive.write_bytes(_header(next_block=0x30FF41C, bbt=(0x4D8A8C1, 0xC7C5D600)))
            _an_hour_ago(archive)

            decision = pipeline._classify(_candidate(archive))
            assert decision is not UNCHANGED
            assert decision == archive_marker(archive)
        finally:
            store.close()

    def test_new_mail_is_seen_even_when_the_date_did_not_move(self, tmp_path):
        """The marker overrules the date in both directions."""
        from app.index.pipeline import UNCHANGED

        archive = _archive(tmp_path)
        _an_hour_ago(archive)
        store, pipeline = _classifier(tmp_path)
        try:
            _indexed_row(store, archive, marker=archive_marker(
                _archive(tmp_path, "earlier.pst", next_block=0x30FF000)), age_ns=0)
            assert pipeline._classify(_candidate(archive)) is not UNCHANGED
        finally:
            store.close()

    def test_a_row_from_before_markers_is_read_once_more_and_gets_one(self, tmp_path):
        """No stored marker is no evidence: the date decides, as it always did."""
        from app.index.pipeline import UNCHANGED

        archive = _archive(tmp_path)
        _an_hour_ago(archive)
        store, pipeline = _classifier(tmp_path)
        try:
            _indexed_row(store, archive, marker=None)
            decision = pipeline._classify(_candidate(archive))
            assert decision is not UNCHANGED
            assert decision == archive_marker(archive)
        finally:
            store.close()

    def test_an_archive_never_seen_starts_with_its_marker(self, tmp_path):
        archive = _archive(tmp_path)
        store, pipeline = _classifier(tmp_path)
        try:
            assert pipeline._classify(_candidate(archive)) == archive_marker(archive)
        finally:
            store.close()

    def test_an_archive_whose_header_cannot_be_read_is_decided_by_its_date(self, tmp_path):
        """What an archive mounted in Outlook is: the read fails, so no marker."""
        from app.index.pipeline import UNCHANGED

        archive = _archive(tmp_path, version=14)
        _an_hour_ago(archive)
        store, pipeline = _classifier(tmp_path)
        try:
            _indexed_row(store, archive, marker=MARKER_PREFIX + "anything")
            assert pipeline._classify(_candidate(archive)) is not UNCHANGED
        finally:
            store.close()

    def test_a_different_size_is_changed_whatever_the_marker_says(self, tmp_path):
        from app.index.pipeline import UNCHANGED

        archive = _archive(tmp_path)
        _an_hour_ago(archive)
        store, pipeline = _classifier(tmp_path)
        try:
            marker = archive_marker(archive)
            file_id = store.upsert_file(
                str(archive), size_bytes=archive.stat().st_size - 512, mtime_ns=1,
                ext="pst", parent_dir=str(tmp_path), content_hash=marker)
            store.mark_indexed(file_id)
            assert pipeline._classify(_candidate(archive)) is not UNCHANGED
        finally:
            store.close()


# ---------------------------------------------------------------------------
# An interrupted archive keeps its place
# ---------------------------------------------------------------------------

class TestAnInterruptedArchiveKeepsItsPlace:
    """`_load_archive_cursor` used the cursor only while size *and date* were
    what they had been - so Outlook opening the archive between two runs sent
    the second one back to the first folder."""

    def _cursor(self, store, archive, *, marker, date_moved=True):
        stat = archive.stat()
        store.set_state("resume:test", json.dumps({
            "path": str(archive), "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns - (5_000_000_000 if date_moved else 0),
            "marker": marker, "folder": 7, "read": 1200, "seen": ["abc"]}))

    def test_the_date_alone_moving_keeps_the_cursor(self, tmp_path):
        archive = _archive(tmp_path)
        store, pipeline = _classifier(tmp_path)
        try:
            self._cursor(store, archive, marker=archive_marker(archive))
            folder, extra = pipeline._load_archive_cursor(_candidate(archive), "resume:test")
            assert folder == 7 and extra == {"seen": ["abc"], "read": 1200}
        finally:
            store.close()

    def test_new_mail_since_sends_it_back_to_the_start(self, tmp_path):
        archive = _archive(tmp_path)
        store, pipeline = _classifier(tmp_path)
        try:
            self._cursor(store, archive, marker=archive_marker(
                _archive(tmp_path, "earlier.pst", next_block=0x30FF000)))
            assert pipeline._load_archive_cursor(
                _candidate(archive), "resume:test") == (0, None)
        finally:
            store.close()

    def test_a_cursor_with_no_marker_is_judged_by_date_as_before(self, tmp_path):
        archive = _archive(tmp_path)
        store, pipeline = _classifier(tmp_path)
        try:
            self._cursor(store, archive, marker=None)
            assert pipeline._load_archive_cursor(
                _candidate(archive), "resume:test") == (0, None)
            self._cursor(store, archive, marker=None, date_moved=False)
            folder, _extra = pipeline._load_archive_cursor(_candidate(archive), "resume:test")
            assert folder == 7
        finally:
            store.close()
