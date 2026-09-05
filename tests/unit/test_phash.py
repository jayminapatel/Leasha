r"""Work order 0h §2a: `PhashComputer`, the cheap half of same-image intelligence.

`imagehash.phash` itself is a well-tested third-party function; these tests
pin down what this module adds around it - the injectable seam (mirroring
`ClipImageEmbedder.encoder`), the H4 failure boundary, and that two visually
close images really do land a handful of bits apart while two unrelated ones
land far apart, on the real library, no fake involved.
"""

from __future__ import annotations

import math

import pytest

from app.core.errors import AppErrorException
from app.index.phash import PHASH_HASH_SIZE, PhashComputer

pytest.importorskip("imagehash")
pytest.importorskip("PIL")


def _solid(color) -> "object":
    from PIL import Image

    return Image.new("RGB", (64, 64), color=color)


def _natural(variant: int = 1, w: int = 320, h: int = 240) -> "object":
    r"""A synthetic photo-like image with broadband low-frequency content.

    **Not a flat gradient or a fine checkerboard - both were tried first and
    both are pathological fixtures for a DCT-based hash.** A flat gradient
    concentrates almost all of its energy in one or two DCT coefficients, so
    ordinary JPEG quantisation noise swings `phash`'s output far more than it
    would on a real photograph - measured at 20-30 bits of Hamming distance
    for a mere quality-95-to-85 re-encode of a gradient, which is nothing
    like a real duplicate photo's behaviour. A fine checkerboard is the
    opposite failure: its period is destroyed entirely by the resize `phash`
    does internally, landing it almost on top of a plain solid colour.

    A sum of several sine waves at different frequencies and phases per
    channel gives broadband low-frequency energy across many DCT
    coefficients, which is what a real photograph's coarse structure looks
    like to this kind of hash - and is what `PHASH_NEAR_THRESHOLD`
    (`app/search/folding.py`) was actually measured against.
    """
    from PIL import Image

    formulas = {
        1: lambda x, y: (
            128 + 60 * math.sin(x / 37 + 1) + 30 * math.sin(y / 23 + 2)
            + 20 * math.sin((x + y) / 51 + 0.3),
            128 + 50 * math.sin(x / 29 + 0.7) + 40 * math.sin(y / 41 + 1.4)
            + 15 * math.sin((x - y) / 61 + 2.1),
            128 + 45 * math.sin(x / 19 + 2.2) + 35 * math.sin(y / 33 + 0.5)
            + 25 * math.sin((x * y) % 97 / 17.0),
        ),
        2: lambda x, y: (
            128 + 70 * math.sin(x / 41 + 0.5) + 25 * math.sin(y / 19 + 1.1)
            + 15 * math.sin((x * 2 + y) / 67 + 2.0),
            128 + 55 * math.sin(x / 31 + 2.3) + 35 * math.sin(y / 13 + 0.4),
            128 + 40 * math.sin(x / 23 + 1.0) + 50 * math.sin(y / 29 + 2.9),
        ),
    }
    formula = formulas[variant]
    img = Image.new("RGB", (w, h))
    px = img.load()
    for x in range(w):
        for y in range(h):
            r, g, b = formula(x, y)
            px[x, y] = (int(max(0, min(255, r))), int(max(0, min(255, g))),
                        int(max(0, min(255, b))))
    return img


# --------------------------------------------------------------------------
# The injectable seam
# --------------------------------------------------------------------------


def test_an_injected_hasher_is_used_instead_of_the_real_one():
    calls = []

    def fake(path):
        calls.append(str(path))
        return "abc123"

    computer = PhashComputer(hasher=fake)
    result = computer.compute("D:/Photos/one.jpg")

    assert result == "abc123"
    assert calls == ["D:/Photos/one.jpg"]


def test_default_hash_size_matches_the_documented_constant():
    assert PHASH_HASH_SIZE == 8
    assert PhashComputer().hash_size == 8


def test_hash_size_is_configurable():
    assert PhashComputer(hash_size=16).hash_size == 16


# --------------------------------------------------------------------------
# H4: a broken hash costs only itself
# --------------------------------------------------------------------------


def test_a_hasher_that_raises_becomes_an_app_error():
    def broken(path):
        raise ValueError("not a real image")

    computer = PhashComputer(hasher=broken)
    with pytest.raises(AppErrorException) as excinfo:
        computer.compute("D:/Photos/corrupt.jpg")
    assert excinfo.value.error.code == "ERR_PHASH"


def test_an_app_error_from_the_hasher_passes_through_unwrapped():
    from app.core.errors import make_error

    def broken(path):
        raise AppErrorException(make_error("ERR_UNEXPECTED", "test"))

    computer = PhashComputer(hasher=broken)
    with pytest.raises(AppErrorException) as excinfo:
        computer.compute("D:/Photos/x.jpg")
    assert excinfo.value.error.code == "ERR_UNEXPECTED"


def test_an_empty_hash_is_treated_as_a_failure():
    computer = PhashComputer(hasher=lambda path: "")
    with pytest.raises(AppErrorException) as excinfo:
        computer.compute("D:/Photos/x.jpg")
    assert excinfo.value.error.code == "ERR_PHASH"


def test_a_missing_file_raises_erp_phash_through_the_default_hasher(tmp_path):
    computer = PhashComputer()
    with pytest.raises(AppErrorException) as excinfo:
        computer.compute(tmp_path / "does-not-exist.jpg")
    assert excinfo.value.error.code == "ERR_PHASH"


def test_a_corrupt_file_raises_through_the_default_hasher(tmp_path):
    bad = tmp_path / "corrupt.jpg"
    bad.write_bytes(b"not an image at all, just bytes")
    computer = PhashComputer()
    with pytest.raises(AppErrorException) as excinfo:
        computer.compute(bad)
    assert excinfo.value.error.code == "ERR_PHASH"


# --------------------------------------------------------------------------
# The real library, on real (tiny, synthetic) images
# --------------------------------------------------------------------------


def test_the_same_image_hashed_twice_gives_the_same_hash(tmp_path):
    path = tmp_path / "solid.png"
    _solid((10, 120, 200)).save(path)

    computer = PhashComputer()
    assert computer.compute(path) == computer.compute(path)


def test_the_hash_is_a_16_character_hex_string(tmp_path):
    path = tmp_path / "solid.png"
    _solid((200, 40, 40)).save(path)

    value = PhashComputer().compute(path)
    assert len(value) == 16
    int(value, 16)                     # must parse as hex - raises otherwise


@pytest.mark.parametrize("quality", [85, 70, 55, 40])
def test_a_recompressed_copy_lands_within_the_measured_threshold(tmp_path, quality):
    """The exact case §2c's acceptance demo is about: a resave (here, a JPEG
    re-encode at a lower quality, the way WhatsApp and similar re-encode a
    shared photo) must not scramble the hash past `PHASH_NEAR_THRESHOLD` -
    the same fixture shape and the same measurement that set that constant
    in `app/search/folding.py` (see its docstring for the full numbers)."""
    from app.search.folding import PHASH_NEAR_THRESHOLD

    original = tmp_path / "original.jpg"
    recompressed = tmp_path / f"recompressed_{quality}.jpg"
    photo = _natural(1)
    photo.save(original, quality=95)
    photo.save(recompressed, quality=quality)

    computer = PhashComputer()
    a = computer.compute(original)
    b = computer.compute(recompressed)

    assert a != b, "different bytes - a real re-encode, not a no-op fixture"
    distance = bin(int(a, 16) ^ int(b, 16)).count("1")
    assert distance <= PHASH_NEAR_THRESHOLD, (
        f"a recompressed copy of the same photo should stay within the "
        f"measured near-duplicate threshold, got Hamming distance {distance} "
        f"at quality={quality}"
    )


def test_a_resized_copy_lands_within_the_measured_threshold(tmp_path):
    """WhatsApp resizes as well as recompressing - the resize half of the
    same acceptance case, on its own."""
    from app.search.folding import PHASH_NEAR_THRESHOLD

    original = tmp_path / "original.jpg"
    resized = tmp_path / "resized.jpg"
    photo = _natural(1)
    photo.save(original, quality=95)
    photo.resize((160, 120)).save(resized, quality=60)

    computer = PhashComputer()
    a = computer.compute(original)
    b = computer.compute(resized)
    distance = bin(int(a, 16) ^ int(b, 16)).count("1")
    assert distance <= PHASH_NEAR_THRESHOLD


def test_two_unrelated_photos_land_well_outside_the_threshold(tmp_path):
    """The other half of the same measurement: two genuinely different
    photographs must land comfortably clear of `PHASH_NEAR_THRESHOLD`, not
    merely on the far side of it."""
    from app.search.folding import PHASH_NEAR_THRESHOLD

    a_path = tmp_path / "a.jpg"
    b_path = tmp_path / "b.jpg"
    _natural(1).save(a_path, quality=95)
    _natural(2).save(b_path, quality=95)

    computer = PhashComputer()
    a = computer.compute(a_path)
    b = computer.compute(b_path)
    distance = bin(int(a, 16) ^ int(b, 16)).count("1")
    assert distance > PHASH_NEAR_THRESHOLD + 5, (
        "two unrelated photographs should land well clear of the "
        "near-duplicate threshold, not merely past it"
    )
