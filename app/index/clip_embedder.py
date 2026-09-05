"""Turning images into CLIP vectors — the vision half of the picture lane.

Layer: L3

Work order 0h §1a. A **parallel** class to `Embedder`, not a modification of
it: `Embedder` wraps FastEmbed's `TextEmbedding` and calls `.embed(documents=...)`
on a list of strings. FastEmbed's `ImageEmbedding` is a different class with a
different call shape — `.embed(images=...)` on a list of paths — so the two
share no model-loading code, only the same three guards, because the same three
things fail silently around any embedding model this application writes to
LanceDB:

**Dimension.** The image table is a fixed 512-wide vector column (`Qdrant/
clip-ViT-B-32-vision`). A model swap that changes the width would either be
rejected by the store or, on a fresh table, accepted and produce an index the
running build can never search — so the dimension is asserted on the first
batch, exactly as `Embedder._check_dimension` does for the text tower.

**Normalisation.** The image table uses cosine distance, same as the text
table. CLIP-family models are typically trained for cosine and FastEmbed
usually normalises already, but "usually" is an assumption about somebody
else's library across version bumps — checked and enforced here rather than
trusted.

**Batching.** One `embed()` call per image wastes the ONNX runtime's
throughput the same way one call per chunk did for text. Left smaller than the
text batch (`EMBED_BATCH` = 256) because an image forward pass costs far more
memory per item than a short text passage; see `CLIP_IMAGE_BATCH` below.

Free/unencumbered only: `Qdrant/clip-ViT-B-32-vision` is MIT-licensed, paired
in the same model family and the same 512 dimensions with `Qdrant/
clip-ViT-B-32-text` (also MIT) for the query side — see `app/search/vector.py`.
No Ultralytics, no AGPL model, anywhere in this module.

The model is loaded **lazily and once**, exactly like `Embedder` — nothing
here touches the network or the model cache until the first image is embedded.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from typing import Callable, Iterable, Iterator, Optional, Sequence, Union

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

_log = logger.bind(component="index.clip_embedder")

__all__ = ["ClipImageEmbedder", "CLIP_IMAGE_BATCH", "CLIP_IMAGE_MODEL", "CLIP_IMAGE_DIM"]

#: The MIT-licensed CLIP vision tower this application ships with. Verified
#: available via `ImageEmbedding.list_supported_models()` before this module
#: was written — see the work order for the exact check.
CLIP_IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"

#: Its output width. Paired with `CLIP_TEXT_MODEL` in `app/search/vector.py`,
#: same family, same dimension, same license — the two must agree, or a query
#: vector and an image vector are not comparable at all.
CLIP_IMAGE_DIM = 512

#: Images per `embed()` call. Smaller than `EMBED_BATCH` (256, text) because
#: an image forward pass holds a decoded bitmap in memory per item, not a few
#: hundred characters of text — 16 is FastEmbed's own default for
#: `ImageEmbedding.embed` and is not second-guessed here without a measurement
#: showing a machine that wants otherwise.
CLIP_IMAGE_BATCH = 16

#: Same tolerance as `embedder._NORM_TOLERANCE`, for the same reason: floating
#: point noise lands around 1e-7, and anything past this is a real signal the
#: model is not returning unit vectors.
_NORM_TOLERANCE = 1e-3

#: One vector per image path, in order.
ImagePath = Union[str, Path]
Encoder = Callable[[Sequence[ImagePath]], Iterable[Sequence[float]]]


class ClipImageEmbedder:
    """Batched, dimension-checked CLIP image embedding, lazily loaded.

    `encoder` is injectable so every behaviour here — batching, normalisation,
    the dimension guard, empty handling — is testable with no model present and
    no download, exactly as `Embedder` is tested. Left `None`, the real
    FastEmbed `ImageEmbedding` model is loaded on first use.
    """

    def __init__(
        self,
        model_name: str = CLIP_IMAGE_MODEL,
        *,
        dim: int = CLIP_IMAGE_DIM,
        cache_dir: Optional[str] = None,
        batch_size: int = CLIP_IMAGE_BATCH,
        encoder: Optional[Encoder] = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        self.model_name = model_name
        self.dim = dim
        self.cache_dir = cache_dir
        self.batch_size = batch_size
        self._encoder = encoder
        self._lock = threading.Lock()
        self._checked_dim = False

    @classmethod
    def from_settings(cls, settings: object, **overrides: object) -> "ClipImageEmbedder":
        """The image embedder this configuration asks for.

        Mirrors `Embedder.from_settings` — one constructor so a new argument
        reaches every caller by existing, rather than by being copied into
        each place that builds one of these by hand.
        """
        fields: dict = dict(
            cache_dir=str(getattr(settings, "model_cache", "") or "") or None,
        )
        fields.update(overrides)
        return cls(**fields)

    @property
    def loaded(self) -> bool:
        return self._encoder is not None

    def warm_up(self) -> None:
        """Load the model now, off the thread a search or an index run is on."""
        self._ensure_encoder()

    def _ensure_encoder(self) -> Encoder:
        if self._encoder is not None:
            return self._encoder
        with self._lock:
            if self._encoder is not None:      # another thread won the race
                return self._encoder
            try:
                from fastembed import ImageEmbedding
            except ImportError as exc:
                raise AppErrorException(make_error(
                    "ERR_MODEL_LOAD", "index.clip_embedder",
                    details=f"fastembed is not importable: {exc}",
                    suggestion="Re-run the installer, or: "
                               "venv\\Scripts\\python.exe -m pip install fastembed",
                )) from exc
            try:
                model = ImageEmbedding(model_name=self.model_name, cache_dir=self.cache_dir)
            except Exception as exc:           # noqa: BLE001 - download, disk, or ONNX
                raise AppErrorException(make_error(
                    "ERR_MODEL_LOAD", "index.clip_embedder",
                    details=f"{self.model_name}: {type(exc).__name__}: {exc}",
                )) from exc
            self._encoder = lambda paths: model.embed([str(p) for p in paths])
            return self._encoder

    # -- embedding ------------------------------------------------------------

    def embed(self, paths: Sequence[ImagePath]) -> list[list[float]]:
        """Embed one batch of image paths. Returns one unit vector per path, in order."""
        if not paths:
            return []

        encoder = self._ensure_encoder()
        try:
            raw = list(encoder(paths))
        except AppErrorException:
            raise
        except Exception as exc:               # noqa: BLE001 - boundary
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.clip_embedder",
                details=f"embedding {len(paths)} image(s) failed: {type(exc).__name__}: {exc}",
                suggestion="The model loaded but would not run on one of these images. "
                           "A single corrupt image should be skipped by the caller rather "
                           "than lose the rest of the batch.",
            )) from exc

        if len(raw) != len(paths):
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.clip_embedder",
                details=f"asked for {len(paths)} vectors, got {len(raw)}",
                suggestion="The image embedding model returned the wrong number of "
                           "vectors, so no image can be matched to its vector. This is "
                           "a bug in the model wrapper, not in your photos.",
            ))

        # Same numpy-block approach as `Embedder.embed` — see that module's
        # comment for the measured 11.4x this replaced a Python loop with.
        block = np.asarray(raw, dtype=np.float64)
        if block.ndim != 2:
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.clip_embedder",
                details=f"expected a rectangular batch, got shape {block.shape}",
                suggestion="The image embedding model returned vectors of differing "
                           "widths, so none of them can be trusted.",
            ))
        self._check_dimension(block[0])

        magnitudes = np.linalg.norm(block, axis=1, keepdims=True)
        adrift = np.abs(magnitudes - 1.0) > _NORM_TOLERANCE
        divide = adrift & (magnitudes != 0.0)
        if divide.any():
            np.divide(block, magnitudes, out=block, where=divide)
        return block.tolist()

    def embed_all(self, paths: Sequence[ImagePath]) -> Iterator[list[float]]:
        """Embed any number of image paths, `batch_size` at a time, lazily.

        Lazy for the same reason `Embedder.embed_all` is: on a corpus of a
        million photos, materialising every vector before writing one row
        would be several GB of list before a single image was searchable.
        """
        for start in range(0, len(paths), self.batch_size):
            yield from self.embed(list(paths[start:start + self.batch_size]))

    # -- guards ----------------------------------------------------------------

    def _check_dimension(self, vector: Sequence[float]) -> None:
        """Assert the model's width matches `self.dim`, once per instance.

        Same reasoning as `Embedder._check_dimension`: a model can load
        happily and still be the wrong shape, which is exactly what pointing
        `CLIP_IMAGE_MODEL` at a different vision tower looks like.
        """
        if self._checked_dim:
            return
        if len(vector) != self.dim:
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.clip_embedder",
                details=f"{self.model_name} returns {len(vector)} dimensions, "
                        f"but the image vector table is {self.dim}-wide",
                suggestion=(
                    f"Set the image embedder's dim to {len(vector)} to match "
                    f"{self.model_name}, or use a {self.dim}-dimension model. "
                    f"Image vectors are derived data and can be rebuilt."
                ),
            ))
        self._checked_dim = True
