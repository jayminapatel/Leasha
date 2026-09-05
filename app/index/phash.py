r"""Perceptual hashing for photos — the cheap half of same-image intelligence.

Layer: L3

Work order 0h §2a. A structural fingerprint of what an image *looks like*,
independent of its bytes. `files.content_hash` (blake2b) already says two
files are byte-identical; this says two files are *the same picture* even
when a single byte does not match — the same photo, resized, recompressed by
WhatsApp, or re-saved by a different program. A recompression changes every
byte and therefore `content_hash` completely, which is exactly the gap this
module exists to close.

**`imagehash.phash` — a DCT over the pixels, not a model.** No download, no
ONNX, no GPU: greyscale, resize to a small fixed square, run a discrete
cosine transform, and keep whether each low-frequency coefficient sits above
or below the median as one bit. Two visually similar images land a handful
of bits apart in the resulting fingerprint; two unrelated ones land near
half the bits apart, by chance. See `app/search/folding.py`'s
`PHASH_NEAR_THRESHOLD` for what "a handful" means here, and for the honest
note that the number is not yet measured against a real duplicate-photo
corpus.

**`imagehash` was not, in fact, already installed.** The work order that
asked for this module states it was; `pip show imagehash` said otherwise
before a line of this file was written, and `requirements.txt` now carries
it, pinned, with that discrepancy recorded rather than smoothed over — see
the comment there.

A **parallel** class to `ClipImageEmbedder` (`app/index/clip_embedder.py`),
for the same reason that one is parallel to `Embedder`: a different library,
a different call shape, and the two are computed independently of one
another — a CLIP model failing to load says nothing about whether Pillow can
open the same file, and vice versa. §2a's H4 promise is that a failure here
costs only this photo's duplicate-detection coverage, never its CLIP vector,
never its OCR text, never the file itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Union

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

_log = logger.bind(component="index.phash")

__all__ = ["PhashComputer", "PHASH_HASH_SIZE"]

#: `imagehash.phash`'s own default: an 8x8 DCT, i.e. a 64-bit fingerprint
#: (16 hex characters once stringified). Not second-guessed here without a
#: measurement showing this corpus wants a different one — the same
#: reasoning `ClipImageEmbedder.CLIP_IMAGE_BATCH`'s docstring gives for
#: keeping FastEmbed's own default rather than inventing one.
PHASH_HASH_SIZE = 8

ImagePath = Union[str, Path]
#: One image path in, one hex-string hash out.
Hasher = Callable[[ImagePath], str]


class PhashComputer:
    """One perceptual hash per image path. Lazy nothing, injectable, and never
    raises past its own boundary — the caller decides what a failed hash costs.

    `hasher` is injectable for the same reason `ClipImageEmbedder.encoder` is:
    the H4 failure path needs to be testable without depending on Pillow
    actually failing on cue, and without paying a real decode for every test
    that only cares about the pipeline wiring around this class. Left `None`,
    a real `imagehash.phash` over a real `PIL.Image.open` is used — no model
    to load and no network involved, so there is no "warm up" step the way
    `ClipImageEmbedder` needs one.
    """

    def __init__(
        self,
        *,
        hash_size: int = PHASH_HASH_SIZE,
        hasher: Optional[Hasher] = None,
    ) -> None:
        self.hash_size = int(hash_size)
        self._hasher = hasher

    def compute(self, path: ImagePath) -> str:
        r"""The pHash of one image, as a lowercase hex string.

        Raises `AppErrorException(ERR_PHASH)` on any failure — a missing
        Pillow, a corrupt image, an unreadable path. Deliberately does not
        swallow the failure itself: `Pipeline._maybe_compute_phash`
        (`app/index/pipeline.py`) is the H4 boundary that decides a broken
        photo costs only its own duplicate-detection coverage, never the
        run — the same split of responsibility `ClipImageEmbedder.embed`
        and `Pipeline._maybe_embed_image` already have. A method that both
        computed *and* decided what a failure means would be the two
        genuinely different jobs `_maybe_embed_image`'s own docstring warns
        against conflating.
        """
        hasher = self._hasher or self._default_hasher
        try:
            value = hasher(path)
        except AppErrorException:
            raise
        except Exception as exc:                # noqa: BLE001 - boundary
            raise AppErrorException(make_error(
                "ERR_PHASH", "index.phash",
                details=f"{path}: {type(exc).__name__}: {exc}",
                suggestion="A single unreadable or corrupt image should be "
                           "skipped by the caller rather than lose the rest "
                           "of the run.",
            )) from exc
        text = str(value)
        if not text:
            raise AppErrorException(make_error(
                "ERR_PHASH", "index.phash",
                details=f"{path}: the hasher returned an empty hash",
            ))
        return text

    def _default_hasher(self, path: ImagePath) -> str:
        r"""`imagehash.phash` over a real `PIL.Image.open`.

        Imported here rather than at module level, matching `ClipImageEmbedder.
        _ensure_encoder`'s own lazy-import reasoning: this class is
        constructed in places — the pipeline's own tests among them — that
        inject a fake `hasher` and never need Pillow or imagehash at all, and
        importing either eagerly would make every one of those pay for a
        dependency it does not use.

        `Image.open` is a context manager: closing the file handle promptly
        matters on a run touching hundreds of thousands of photos, where an
        unclosed handle per file would exhaust the process before the disk
        floor ever did.
        """
        from PIL import Image
        import imagehash

        with Image.open(path) as img:
            return str(imagehash.phash(img, hash_size=self.hash_size))
