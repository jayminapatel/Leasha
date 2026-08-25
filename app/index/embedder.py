"""Turning chunks into vectors, in batches, without surprises.

Layer: L3

FastEmbed runs bge-small as ONNX **in this process**. That is the decision the
whole architecture rests on: an embedding server that crashes takes search with
it, and V1's did. In-process means the only way embedding stops working is the
model file going missing, which is checkable.

Three things this module is careful about, each of which fails silently otherwise:

**Dimension.** LanceDB's table has a fixed 384-wide vector column. Point
`EMBED_MODEL` at a 768-dim model and every write is rejected - or worse, on a
fresh index, accepted, producing a store that can never be searched by the
running build. The dimension is asserted on the first batch, once, and a mismatch
is `ERR_MODEL_LOAD` naming both numbers.

**Normalisation.** The vector store uses cosine distance. bge models are trained
for cosine and FastEmbed normalises already, but "already" is an assumption about
somebody else's library across version bumps, so it is checked and enforced here.
An un-normalised vector does not error; it just ranks slightly wrong, forever.

**Batching.** One `embed()` call per chunk wastes most of the ONNX runtime's
throughput. 64 is the spec's number and is a reasonable balance: large enough to
saturate the batch dimension, small enough that a batch's worth of text is not a
memory event on an 8GB machine.

The model is loaded **lazily and once**. Layer 4 calls `warm_up()` from a
background thread at startup so the first search is not the one paying the
one-to-two second ONNX load.
"""

from __future__ import annotations

import math
import threading
from typing import Callable, Iterable, Iterator, Optional, Sequence

from app.core.errors import AppErrorException, make_error

__all__ = ["Embedder", "EMBED_BATCH", "l2_normalise"]

#: Chunks per `embed()` call, and **the only definition of that number**.
#:
#: It was 64 here and 256 in `pipeline.py`, and the pipeline's was the one
#: nobody could act on: `_embed_pending` gathered 256 chunks, handed them to
#: `embed_all`, and `embed_all` re-split them into four calls of 64. So the
#: constant documented as "the single biggest throughput lever in the whole
#: pipeline" reached the model as a quarter of itself, and raising it did
#: nothing at all - the exact silent re-split the terabyte review asked to be
#: verified rather than assumed.
#:
#: 256 chunks is roughly 400KB of text, which is nothing against the memory
#: ceiling, and large enough that ONNX spends its time on matrix work rather
#: than on per-call overhead. `Pipeline` imports this rather than declaring a
#: second one, and aligns the embedder it is given - see `Pipeline.__init__`.
EMBED_BATCH = 256

#: How far a vector's magnitude may drift from 1.0 before it is renormalised.
#: Floating point noise lands around 1e-7; anything past this is a real signal
#: that the model is not returning unit vectors.
_NORM_TOLERANCE = 1e-3

#: Encoder signature: texts in, one vector per text out.
Encoder = Callable[[Sequence[str]], Iterable[Sequence[float]]]


def l2_normalise(vector: Sequence[float]) -> list[float]:
    """Scale to unit length. A zero vector is returned unchanged, not divided by."""
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0.0:
        return list(vector)
    return [value / magnitude for value in vector]


class Embedder:
    """Batched, dimension-checked embedding with a lazily loaded model.

    `encoder` is injectable so every behaviour here - batching, normalisation,
    the dimension guard, empty handling - is testable with no model present and
    no download. Left None, the real FastEmbed model is loaded on first use.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        *,
        dim: int = 384,
        cache_dir: Optional[str] = None,
        batch_size: int = EMBED_BATCH,
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

    # -- model lifecycle ----------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._encoder is not None

    def warm_up(self) -> None:
        """Load the model now. Safe to call from a background thread at startup.

        Layer 4 uses this so the first user search is not the one paying the
        one-to-two second ONNX load. Idempotent, and holds a lock so two threads
        racing at startup load it once rather than twice.
        """
        self._ensure_encoder()

    def _ensure_encoder(self) -> Encoder:
        if self._encoder is not None:
            return self._encoder

        with self._lock:
            if self._encoder is not None:          # another thread won the race
                return self._encoder

            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise AppErrorException(make_error(
                    "ERR_MODEL_LOAD", "index.embedder",
                    details=f"fastembed is not importable: {exc}",
                    suggestion="Re-run the installer, or: "
                               "venv\\Scripts\\python.exe -m pip install fastembed",
                )) from exc

            try:
                model = TextEmbedding(model_name=self.model_name, cache_dir=self.cache_dir)
            except Exception as exc:               # noqa: BLE001 - download, disk, or ONNX
                raise AppErrorException(make_error(
                    "ERR_MODEL_LOAD", "index.embedder",
                    details=f"{self.model_name}: {type(exc).__name__}: {exc}",
                )) from exc

            self._encoder = lambda texts: model.embed(list(texts))
            return self._encoder

    # -- embedding ----------------------------------------------------------

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed one batch. Returns one unit vector per input, in order."""
        if not texts:
            return []

        encoder = self._ensure_encoder()
        try:
            raw = list(encoder(texts))
        except AppErrorException:
            raise
        except Exception as exc:                   # noqa: BLE001 - boundary
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.embedder",
                details=f"embedding {len(texts)} text(s) failed: {type(exc).__name__}: {exc}",
                suggestion="The model loaded but would not run. Delete the model cache under "
                           "<DATA_PATH>\\models and let it download again.",
            )) from exc

        if len(raw) != len(texts):
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.embedder",
                details=f"asked for {len(texts)} vectors, got {len(raw)}",
                suggestion="The embedding model returned the wrong number of vectors, so no "
                           "chunk can be matched to its text. This is a bug in the model "
                           "wrapper, not in your documents.",
            ))

        vectors = [list(map(float, vector)) for vector in raw]
        self._check_dimension(vectors[0])
        return [self._ensure_unit(vector) for vector in vectors]

    def embed_all(self, texts: Sequence[str]) -> Iterator[list[float]]:
        """Embed any number of texts, `batch_size` at a time, lazily.

        Lazy so the caller can write each batch to the store as it arrives; on a
        million chunks, materialising every vector first would be several GB of
        list before a single row was persisted.
        """
        for start in range(0, len(texts), self.batch_size):
            yield from self.embed(texts[start:start + self.batch_size])

    # -- guards -------------------------------------------------------------

    def _check_dimension(self, vector: Sequence[float]) -> None:
        """Assert the model's width matches the store's, once per Embedder.

        Checked on the first batch rather than at load, because a model can load
        happily and still be the wrong shape - which is exactly what changing
        `EMBED_MODEL` in .env looks like.
        """
        if self._checked_dim:
            return
        if len(vector) != self.dim:
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.embedder",
                details=f"{self.model_name} returns {len(vector)} dimensions, "
                        f"but this index stores {self.dim}",
                suggestion=(
                    f"EMBED_MODEL and EMBED_DIM in .env disagree, or EMBED_MODEL was changed "
                    f"after the index was built. Set EMBED_DIM={len(vector)} and rebuild the "
                    f"index from scratch, or put EMBED_MODEL back to a {self.dim}-dimension "
                    f"model. Vectors of different widths cannot share an index."
                ),
            ))
        self._checked_dim = True

    def _ensure_unit(self, vector: list[float]) -> list[float]:
        """Enforce unit length, because cosine ranking quietly degrades without it.

        FastEmbed normalises bge output already. This is not redundancy for its
        own sake: an un-normalised vector raises nothing and fails nothing, it
        just ranks slightly wrong for the life of the index, and no test that
        checks for errors would ever catch it.
        """
        magnitude = math.sqrt(sum(value * value for value in vector))
        if abs(magnitude - 1.0) <= _NORM_TOLERANCE:
            return vector
        return l2_normalise(vector)
