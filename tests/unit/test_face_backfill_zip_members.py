r"""A picture inside a zip is looked at for faces from its bytes, not its key.

Layer: L3

2026-10-25, the owner's restart (`logs/runs/run-20261025-113053-index.log`):
the face backfill tried to open `...\x.zip\assets\logo.png` as a file, logged a
FileNotFoundError twice for each of 46 pictures, found nothing in what it could
not read and marked every one as looked at. A zip member's key is not a place on
disk: its bytes are read out of the zip, as the window's thumbnails read them
(`app.ui.attachment_open.read_zip_member`), and a zip that cannot be read leaves
its pictures for a later run with one line, not one per picture.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.core.row_facts import zip_member_of
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_photo_tagger import _pipeline


def _zip_with(tmp_path, members, name="pack.zip"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zipped:
        for inner, data in members.items():
            zipped.writestr(inner, data)
    return path


def _nested(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zipped:
        zipped.writestr("deep/face.jpg", b"deep bytes")
    return _zip_with(tmp_path, {"inner.zip": inner.getvalue(), "assets/logo.png": b"logo"})


def test_the_index_and_the_window_share_one_rule_for_a_zip_member():
    from app.ui import attachment_open

    assert attachment_open.zip_member_of is zip_member_of
    assert zip_member_of(r"D:\a\x.zip/assets/logo.png") == (r"D:\a\x.zip", "assets/logo.png")
    assert zip_member_of(r"D:\a\photo.jpg") == ("", "")
    assert zip_member_of("pst://store/1/attachments/x.zip/a.png") == ("", "")


def test_a_zip_members_bytes_come_out_of_the_zip(tmp_path):
    archive = _nested(tmp_path)
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, people_recognition_enabled=True)
        assert pipeline._mail_picture_bytes(f"{archive}/assets/logo.png") == b"logo"
        assert pipeline._mail_picture_bytes(f"{archive}/inner.zip/deep/face.jpg") == b"deep bytes"
        assert pipeline._mail_picture_bytes(str(tmp_path / "plain.jpg")) is None
        with pytest.raises(LookupError):
            pipeline._mail_picture_bytes(f"{archive}/assets/gone.png")
        with pytest.raises(FileNotFoundError) as missing:
            pipeline._mail_picture_bytes(f"{tmp_path / 'away.zip'}/a.png")
        assert pipeline._picture_left_for_later(missing.value), "a missing zip is tried later"


def test_the_picture_passes_get_a_real_file_for_a_zip_member(tmp_path):
    archive = _zip_with(tmp_path, {"assets/logo.png": b"logo"})
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, people_recognition_enabled=True)
        with pipeline._picture_file(f"{archive}/assets/logo.png") as real:
            assert real is not None and real.is_file() and real.suffix == ".png"
            assert real.read_bytes() == b"logo"
        assert not real.exists(), "the scratch copy is removed"
        with pipeline._picture_file(f"{tmp_path / 'away.zip'}/a.png") as real:
            assert real is None, "a zip that is not there leaves the picture for later"


def _photo_row(store, path):
    return store.upsert_file(str(path), size_bytes=1, mtime_ns=1, ext="png",
                             parent_dir="x", source_kind="archive", status="INDEXED")


def test_the_backfill_reads_zip_members_and_says_once_what_it_left(tmp_path, monkeypatch):
    from app.extract import face_detect

    archive = _zip_with(tmp_path, {"assets/logo.png": b"logo bytes"})
    handed: list = []

    def fake_detect(path, data=None):
        handed.append((str(path), data))
        return []

    monkeypatch.setattr(face_detect, "available", lambda: True)
    monkeypatch.setattr(face_detect, "detect_faces", fake_detect)
    with SqliteStore(tmp_path / "index.db") as store:
        here = _photo_row(store, f"{archive}/assets/logo.png")
        gone = [_photo_row(store, f"{tmp_path / 'away.zip'}/p{n}.png") for n in range(3)]
        for file_id in (here, *gone):
            store.mark_indexed(file_id)
        pipeline = _pipeline(store, people_recognition_enabled=True)
        warned: list = []
        monkeypatch.setattr(pipeline, "_log", type("L", (), {
            "warning": lambda self, msg, *a: warned.append(msg.format(*a)),
            "info": lambda self, *a: None, "debug": lambda self, *a: None})())
        from app.index.pipeline import IndexStats

        pipeline._drain_face_backfill(IndexStats())

        assert len(handed) == 1, "a missing zip's pictures were handed to the model"
        assert handed[0][1] == b"logo bytes", "the model was given a key, not the bytes"
        scanned = {row[0] for row in store.conn.execute("SELECT file_id FROM face_scans")}
        assert scanned == {here}, "a picture whose zip is not there was marked as looked at"
        zip_lines = [w for w in warned if "inside zips" in w]
        assert zip_lines == ["3 picture(s) inside zips were not looked at for faces because "
                             "their zip could not be read; they are tried again on the next run."]


def test_zip_pictures_marked_without_being_opened_are_looked_at_again(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        member = _photo_row(store, r"D:\a\x.zip/assets/logo.png")
        with_face = _photo_row(store, r"D:\a\x.zip/assets/team.png")
        plain = store.upsert_file(r"D:\a\photo.png", size_bytes=1, mtime_ns=1)
        for file_id in (member, with_face, plain):
            store.mark_face_scanned(file_id)
        store.add_face(with_face, (0.0, 0.0, 1.0, 1.0), b"\x00" * 16)

        assert store.forget_face_scans_of_zip_pictures() == 1
        left = {row[0] for row in store.conn.execute("SELECT file_id FROM face_scans")}
        assert left == {with_face, plain}
        assert store.forget_face_scans_of_zip_pictures() == 0
