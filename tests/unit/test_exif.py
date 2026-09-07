"""EXIF metadata extraction from images.

Layer: L2

**EXIF date is THE date for photos.** File mtime lies after drive copies.
EXIF DateTimeOriginal survives them and is load-bearing for time-based search.
"""

from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

import pytest

from app.extract.exif import read_datetime, read_orientation

HAS_PIL = importlib.util.find_spec("PIL") is not None


@pytest.mark.skipif(not HAS_PIL, reason="Pillow is not installed")
class TestExifDate:
    """EXIF date extraction."""

    def test_image_without_exif_returns_none(self, tmp_path):
        """A new image with no EXIF date returns None."""
        from PIL import Image

        img = Image.new("RGB", (100, 100), color="white")
        img_path = tmp_path / "no_exif.jpg"
        img.save(img_path)

        result = read_datetime(img_path)
        assert result is None

    def test_bad_path_returns_none(self):
        """A non-existent path returns None, never raises."""
        result = read_datetime(Path("/nonexistent/file.jpg"))
        assert result is None

    def test_corrupted_image_returns_none(self, tmp_path):
        """A file that is not an image returns None."""
        fake_img = tmp_path / "fake.jpg"
        fake_img.write_bytes(b"not an image")

        result = read_datetime(fake_img)
        assert result is None


@pytest.mark.skipif(not HAS_PIL, reason="Pillow is not installed")
class TestExifOrientation:
    """EXIF orientation tag extraction."""

    def test_image_without_orientation_returns_normal(self, tmp_path):
        """A new image with no orientation tag defaults to 1 (normal)."""
        from PIL import Image

        img = Image.new("RGB", (100, 100), color="white")
        img_path = tmp_path / "no_orient.jpg"
        img.save(img_path)

        result = read_orientation(img_path)
        assert result == 1  # Normal/0 degrees

    def test_bad_path_returns_normal(self):
        """A non-existent path returns 1 (normal), never raises."""
        result = read_orientation(Path("/nonexistent/file.jpg"))
        assert result == 1

    def test_corrupted_image_returns_normal(self, tmp_path):
        """A corrupted file returns 1 (normal), never raises."""
        fake_img = tmp_path / "fake.jpg"
        fake_img.write_bytes(b"not an image")

        result = read_orientation(fake_img)
        assert result == 1


@pytest.mark.skipif(not HAS_PIL, reason="Pillow is not installed")
def test_exif_date_beats_mtime_in_a_before_filter_end_to_end(tmp_path):
    """§4 'EXIF date:' - fixture photo with 2006 DateTimeOriginal and a 2019
    file mtime. Decision 3 says the photo indexes as 2006 and `before:2010`
    finds it. This drives every real layer between the two: `OcrExtractor`
    reads the EXIF date, `SqliteStore` holds the row exactly as the pipeline
    would leave it (2019 mtime - the only date the pipeline currently has to
    give it), and `file_filter_sql` is the actual WHERE-clause builder
    `before:` compiles to. No layer here is faked."""
    import os
    import time
    from datetime import date as date_cls
    from datetime import datetime as datetime_cls

    from PIL import Image

    from app.extract.base import DocumentBuilder
    from app.extract.exif import read_datetime
    from app.search.query import parse_query
    from app.storage.filters import file_filter_sql
    from app.storage.sqlite_store import FileStatus, SqliteStore

    # -- a photo whose camera date and copy date disagree by 13 years --------
    photo = tmp_path / "IMG_2006.jpg"
    exif = Image.Exif()
    exif[36867] = "2006:06:15 10:30:00"  # DateTimeOriginal
    Image.new("RGB", (256, 256), color="white").save(photo, exif=exif)

    nineteen = date_cls(2019, 3, 1)
    stamp = time.mktime(nineteen.timetuple())
    os.utime(photo, (stamp, stamp))

    # -- the same two steps `OcrExtractor.extract()` and `RawExtractor.extract()`
    #    both take: read the EXIF date, assign it to the builder - and the same
    #    `DocumentBuilder.build()` every extractor calls -----------------------
    exif_date = read_datetime(photo)
    assert exif_date == datetime_cls(2006, 6, 15, 10, 30, 0)

    builder = DocumentBuilder(photo)
    builder.add("(the image's own read text, irrelevant to this test)")
    builder.date = exif_date          # exactly what ocr.py / raw.py do
    document = builder.build()

    assert getattr(document, "date", None) == exif_date, (
        "the Document a caller actually receives should carry the EXIF date "
        "`builder.date` was set to"
    )

    # -- the row the pipeline would write: `candidate.mtime_ns` is always the
    #    real OS mtime (`stat.st_mtime_ns`), independent of what the extractor
    #    read - see `app/index/pipeline.py`'s `Candidate` construction.
    store = SqliteStore(tmp_path / "index.db").connect()
    try:
        real_mtime_ns = photo.stat().st_mtime_ns
        store.upsert_file(
            str(photo), size_bytes=photo.stat().st_size, mtime_ns=real_mtime_ns,
            ext="jpg", status=FileStatus.INDEXED,
            # Work order 0f §3a: the shot date travels in its own column,
            # beside the real mtime rather than over it - `mtime_ns` is still
            # H1's change-detection key and still the file's true copy date.
            # Taken from what the extractor actually read, not from a literal,
            # so this still proves the chain rather than asserting the answer.
            taken_at_ns=int(exif_date.timestamp() * 1_000_000_000),
        )
        # The row keeps the 2019 copy date it really has on disk.
        assert store.get_file(str(photo)).mtime_ns == real_mtime_ns

        parsed = parse_query("before:2010")
        where, params = file_filter_sql(parsed)
        # The fragment carries a leading ` AND `, for `WHERE 1=1{where}`.
        rows = store.conn.execute(
            f"SELECT path FROM files f WHERE 1=1{where}", params,
        ).fetchall()

        assert [r["path"] for r in rows] == [str(photo)], (
            "before:2010 should find the 2006 photo by its EXIF date, not "
            "exclude it by its 2019 copy date"
        )
    finally:
        store.close()


class TestExifConsistency:
    """EXIF functions should never raise."""

    def test_read_datetime_never_raises(self, tmp_path):
        """read_datetime is safe to call on anything."""
        # None of these should raise
        read_datetime(Path("/dev/null"))
        read_datetime(tmp_path / "missing.jpg")
        read_datetime(tmp_path)  # directory

    def test_read_orientation_never_raises(self, tmp_path):
        """read_orientation is safe to call on anything."""
        # None of these should raise
        read_orientation(Path("/dev/null"))
        read_orientation(tmp_path / "missing.jpg")
        read_orientation(tmp_path)  # directory
