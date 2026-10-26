r"""A photo whose EXIF Pillow cannot write back is still turned upright and read.

Layer: L2

2026-10-26, the owner's run: "face detection failed on Maya_MedicalCard_2015.jpg:
TypeError: can't concat str to bytes" - photos from a BlackBerry Z30 store their XMP
packet (tag 0x02BC) as text where TIFF says bytes. `ImageOps.exif_transpose` writes the
EXIF back after turning the picture and fails on it, so the photo lost its faces, its
picture search and its thumbnail. `picture.upright` turns it by the orientation tag alone.
The photo here is built, not the owner's: the same two tags, nothing else.
"""

from __future__ import annotations

import io
import struct

import numpy as np
import pytest
from PIL import Image, ImageOps

from app.extract import picture

XMP = b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta/>'


def _odd_exif(orientation: int) -> bytes:
    """Little-endian TIFF: Orientation, and XMP typed ASCII (2) instead of BYTE (1)."""
    entries = [(0x0112, 3, 1, struct.pack("<HH", orientation, 0)), (0x02BC, 2, len(XMP), None)]
    data_at = 8 + 2 + 12 * len(entries) + 4
    out = b"II*\x00" + struct.pack("<IH", 8, len(entries))
    for tag, kind, count, value in entries:
        out += struct.pack("<HHI", tag, kind, count) + (value or struct.pack("<I", data_at))
    return b"Exif\x00\x00" + out + struct.pack("<I", 0) + XMP


def _photo(orientation: int = 3) -> bytes:
    """A 4x2 picture, red on the left half and blue on the right, saved as PNG-exact JPEG."""
    image = Image.new("RGB", (4, 2), (255, 0, 0))
    image.paste((0, 0, 255), (2, 0, 4, 2))
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=100, subsampling=0, exif=_odd_exif(orientation))
    return buf.getvalue()


def test_pillow_still_fails_on_it():
    """The case is real: if Pillow ever writes this EXIF, `upright` is a plain call."""
    with Image.open(io.BytesIO(_photo())) as opened:
        with pytest.raises(TypeError):
            ImageOps.exif_transpose(opened)


def test_upright_turns_it_by_its_orientation():
    with Image.open(io.BytesIO(_photo(3))) as opened:
        turned = picture.upright(opened).convert("RGB")
    # Orientation 3 is upside down: turned 180, the blue half is now on the left.
    left, right = turned.getpixel((0, 0)), turned.getpixel((3, 0))
    assert left[2] > 200 and right[0] > 200


def test_a_file_with_it_decodes(tmp_path):
    path = tmp_path / "card.jpg"
    path.write_bytes(_photo(6))
    found = picture.decoded(path)
    assert found is not None and found.size == (2, 4), "orientation 6 turns it on its side"


def test_the_face_reader_reads_its_bytes():
    from app.extract.face_detect import _bytes_bgr

    image = _bytes_bgr(_photo(3), np)
    assert image is not None and image.shape == (2, 4, 3)
