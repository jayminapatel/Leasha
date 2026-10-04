r"""Write names into photos: XMP into JPEG and PNG, sidecars for the rest.

Layer: L3

2026-10-05, the owner: "go ahead with option a but an option b button which
can be used" - option b writes the people and the description into each photo,
with a backup first. Not one pixel may change, and a camera's own XMP stays.
"""

from __future__ import annotations

import io
import os

import pytest

from app.index import photo_metadata as pm

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

META = pm.PhotoMetadata(file_id=1, path="", people=("Jason", "Sarita"),
                        description="Two people on a beach at sunset")


def _jpeg(path, *, exif=True):
    image = Image.new("RGB", (64, 48), (10, 120, 200))
    kwargs = {}
    if exif:
        data = Image.Exif()
        data[0x0110] = "Test camera"
        kwargs["exif"] = data.tobytes()
    image.save(path, "JPEG", quality=90, **kwargs)
    return path


def _pixels(path):
    with Image.open(path) as opened:
        return opened.convert("RGB").tobytes()


def _write(path, tmp_path, meta=META, **kwargs):
    item = pm.PhotoMetadata(meta.file_id, str(path), meta.people, meta.description)
    seen = []
    result = pm.write_photo_metadata([item], backup_root=tmp_path / "backup",
                                     on_written=lambda m, size, mtime: seen.append((size, mtime)),
                                     **kwargs)
    return result, seen


def test_a_jpeg_gets_the_names_and_keeps_every_pixel_and_its_exif(tmp_path):
    photo = _jpeg(tmp_path / "beach.jpg")
    before_pixels, before_bytes = _pixels(photo), photo.read_bytes()
    os.utime(photo, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))

    result, seen = _write(photo, tmp_path)

    assert result.written == 1 and result.failed == 0
    assert _pixels(photo) == before_pixels, "not re-encoded"
    packet = pm.read_xmp(photo).decode("utf-8")
    for expected in ("Jason", "Sarita", "PersonInImage", "dc:subject",
                     "Two people on a beach at sunset"):
        assert expected in packet
    with Image.open(photo) as opened:
        assert opened.getexif().get(0x0110) == "Test camera"
    assert photo.stat().st_mtime_ns == 1_600_000_000_000_000_000, "its date is kept"
    backup = next((tmp_path / "backup").rglob("beach.jpg"))
    assert backup.read_bytes() == before_bytes, "the original is kept first"
    assert seen == [(photo.stat().st_size, 1_600_000_000_000_000_000)]


def test_writing_twice_replaces_rather_than_adds_and_keeps_other_xmp(tmp_path):
    photo = _jpeg(tmp_path / "again.jpg", exif=False)
    camera = pm.merge_xmp(pm.build_xmp(pm.PhotoMetadata(0, "", ())), META).replace(
        b"<rdf:Description", b'<rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
                             b' xmp:CreatorTool="Phone 12"', 1)
    data = photo.read_bytes()
    segment = b"\xff\xe1" + (2 + len(pm._XMP_HEADER) + len(camera)).to_bytes(2, "big") \
        + pm._XMP_HEADER + camera
    photo.write_bytes(data[:2] + segment + data[2:])

    _write(photo, tmp_path, pm.PhotoMetadata(1, "", ("Jaymin",), ""))
    _write(photo, tmp_path, pm.PhotoMetadata(1, "", ("Jaymin",), ""))

    packet = pm.read_xmp(photo).decode("utf-8")
    assert packet.count("Jaymin") == 2, "once as a keyword, once as a person"
    assert "Jason" not in packet and "beach" not in packet, "the old names went"
    assert "Phone 12" in packet, "the phone's own XMP stays"
    assert photo.read_bytes().count(pm._XMP_HEADER) == 1


def test_a_png_gets_an_xmp_chunk_and_keeps_its_pixels(tmp_path):
    photo = tmp_path / "shot.png"
    Image.new("RGBA", (20, 10), (1, 2, 3, 200)).save(photo)
    before = _pixels(photo)
    result, _ = _write(photo, tmp_path)
    assert result.written == 1
    assert _pixels(photo) == before
    assert b"Sarita" in pm.read_xmp(photo)
    with Image.open(photo) as opened:
        opened.load()                                 # every chunk's CRC is checked


def test_heic_and_the_sidecar_choice_leave_the_photo_untouched(tmp_path):
    other = tmp_path / "IMG_0001.heic"
    other.write_bytes(b"not really a heic")
    result, seen = _write(other, tmp_path)
    assert result.sidecars == 1 and seen == []
    assert other.read_bytes() == b"not really a heic"
    assert (tmp_path / "IMG_0001.xmp").exists()

    jpeg = _jpeg(tmp_path / "keep.jpg")
    before = jpeg.read_bytes()
    result, _ = _write(jpeg, tmp_path, where=pm.SIDECAR)
    assert result.sidecars == 1 and jpeg.read_bytes() == before
    assert b"Jason" in (tmp_path / "keep.xmp").read_bytes()


def test_a_sidecar_name_two_photos_would_share_takes_the_long_form(tmp_path):
    (tmp_path / "IMG_2.heic").write_bytes(b"x")
    (tmp_path / "IMG_2.jpg").write_bytes(b"y")
    assert pm.sidecar_path(tmp_path / "IMG_2.heic").name == "IMG_2.heic.xmp"


def test_one_broken_photo_is_reported_and_the_rest_are_written(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\x00garbage")
    good = _jpeg(tmp_path / "good.jpg")
    items = [pm.PhotoMetadata(1, str(broken), ("A",)), pm.PhotoMetadata(2, str(good), ("B",))]
    result = pm.write_photo_metadata(items, backup_root=tmp_path / "b")
    assert result.failed == 1 and result.written == 1
    assert "broken.jpg" in result.problems[0]
    assert broken.read_bytes() == b"\xff\xd8\x00garbage"


def test_the_store_lists_what_to_write_and_records_the_new_size(tmp_path):
    from app.index import face_clustering as fc
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(tmp_path / "index.db") as store:
        photo = store.upsert_file(path="/p/a.jpg", size_bytes=10, mtime_ns=5,
                                  source_kind="file")
        store.upsert_file(path="/p/nobody.jpg", size_bytes=10, mtime_ns=5, source_kind="file")
        pile = store.split_pile([store.add_face(photo, (0, 0, 1, 1), fc.to_bytes([1.0, 0.0]))])
        store.rename_pile(pile, "Jason")
        store.add_caption_chunk(photo, "A dog in a park", label="AI description")

        assert store.photo_metadata_rows() == [(photo, "/p/a.jpg", ("Jason",), "A dog in a park")]
        assert store.photo_metadata_rows([photo + 99]) == []
        store.note_photo_rewritten(photo, 12, 5)
        assert store.conn.execute("SELECT size_bytes FROM files WHERE id = ?",
                                  (photo,)).fetchone()[0] == 12
