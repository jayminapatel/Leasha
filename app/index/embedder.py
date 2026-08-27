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

import numpy as np
from typing import Callable, Iterable, Iterator, Optional, Sequence

from app.core.errors import AppErrorException, make_error
from app.index import backends

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
        device: str = backends.AUTO,
        profile: Optional[object] = None,
        problems: Optional[list] = None,
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
        #: What was asked for. What actually ran is `self.choice`, and the two
        #: differ whenever a GPU was requested and would not have it - which is
        #: precisely the case the run log has to be able to show.
        self.device = str(device or backends.AUTO)
        self._profile = profile
        self._problems = problems
        #: Set once the model loads. `None` until then, so nothing reports a
        #: provider that has not yet been proven to work.
        self.choice: Optional[backends.Choice] = None

    @classmethod
    def from_settings(cls, settings: object, **overrides: object) -> "Embedder":
        """The embedder this configuration asks for.

        **Seven places built one of these by hand**, and every one of them
        would have had to grow a `device=` argument for §2b to reach the
        machine - six of which somebody would eventually forget, producing an
        application where the setting worked in the window and not in the CLI.
        One constructor is the fix, and new arguments now reach every caller by
        existing rather than by being copied.
        """
        # **Merged into a dict rather than passed alongside `**overrides`.**
        # Spelling them out as keywords made `from_settings(s, device="cpu")`
        # a `TypeError` - two values for one argument - which is precisely the
        # call the benchmark makes to time both processors. An override that
        # cannot override is not an override.
        fields: dict = dict(
            dim=int(getattr(settings, "embed_dim", 384) or 384),
            cache_dir=str(getattr(settings, "model_cache", "") or "") or None,
            device=str(getattr(settings, "embed_device", backends.AUTO)
                       or backends.AUTO),
        )
        fields.update(overrides)
        return cls(
            str(getattr(settings, "embed_model", "") or "BAAI/bge-small-en-v1.5"),
            **fields,
        )

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

            wanted = backends.choose(self._resolved_profile(), self.device)

            def build(providers: tuple) -> object:
                if providers == (backends.CPU_PROVIDER,):
                    # **The CPU path is byte-for-byte what it was.** Passing a
                    # providers list that means "the default" would still be a
                    # new argument to somebody else's constructor on every
                    # machine, and §2's promise is that CPU behaviour is
                    # untouched by this seam existing.
                    return TextEmbedding(model_name=self.model_name,
                                         cache_dir=self.cache_dir)
                return TextEmbedding(model_name=self.model_name,
                                     cache_dir=self.cache_dir,
                                     providers=list(providers))

            try:
                model, self.choice = backends.with_fallback(
                    build, wanted, problems=self._problems)
            except Exception as exc:               # noqa: BLE001 - download, disk, or ONNX
                raise AppErrorException(make_error(
                    "ERR_MODEL_LOAD", "index.embedder",
                    details=f"{self.model_name}: {type(exc).__name__}: {exc}",
                )) from exc

            if wanted.fell_back_from and self._problems is not None:
                self._problems.append(wanted.why)

            backends.record_provider("meaning model", self.choice)
            self._encoder = lambda texts: model.embed(list(texts))
            return self._encoder

    def _resolved_profile(self) -> object:
        """The profile to decide against - the given one, or this machine's.

        Detected lazily rather than in `__init__` because an `Embedder` is
        constructed in places that never load a model, and probing the
        hardware to then not use it is work nobody asked for.
        """
        if self._profile is not None:
            return self._profile
        try:
            from app.core.compute_profile import detect

            self._profile = detect()
        except Exception:                          # noqa: BLE001 - detection
            # A profile that cannot be read is not a reason to fail to embed:
            # an empty one means "no GPU known", which lands on the CPU.
            self._profile = object()
        return self._profile

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

        # **One numpy block, not 98,304 Python floats.** A batch of 256 vectors
        # at 384 dimensions was converted element by element and then normalised
        # with a Python `sum()` over each one. Measured on a batch of 256:
        # **10.05ms before, 0.88ms after, 11.4x**, and the two agree to 1e-12.
        # Over a million chunks that is 41 seconds against 4.3.
        #
        # `.tolist()` at the end is deliberate: `list[float]` is what the store,
        # the tests and every caller expect, and keeping the array would save a
        # further 0.4ms per batch in exchange for a type change across four
        # modules. The win is in the arithmetic, not in the container.
        # **float64, not float32.** The old path did `float(x)` on each value,
        # which widens the model's float32 to a Python double - so the norm was
        # computed in double and an already-unit vector came back bit-identical.
        # float32 here was 39x rather than 16x and broke both of those: unit
        # length came out at 1.0000000015 against a 1e-9 tolerance, and a vector
        # the code promises to leave alone came back altered in the eighth
        # decimal. Two tests said so, which is the only reason this line is not
        # float32 today.
        block = np.asarray(raw, dtype=np.float64)
        if block.ndim != 2:
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.embedder",
                details=f"expected a rectangular batch, got shape {block.shape}",
                suggestion="The embedding model returned vectors of differing "
                           "widths, so none of them can be trusted. Delete the "
                           "model cache under <DATA_PATH>\\models and let it "
                           "download again.",
            ))
        self._check_dimension(block[0])

        magnitudes = np.linalg.norm(block, axis=1, keepdims=True)
        # Only the ones that need it, and never a divide by zero: a zero vector
        # is returned unchanged, exactly as `l2_normalise` promises.
        adrift = np.abs(magnitudes - 1.0) > _NORM_TOLERANCE
        divide = adrift & (magnitudes != 0.0)
        if divide.any():
            np.divide(block, magnitudes, out=block, where=divide)
        return block.tolist()

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
        """Assert the model's width matches `EMBED_DIM`, once per Embedder.

        **`self.dim` is `EMBED_DIM`, not the width of anything stored.** Nothing
        here opens the vector table; `VectorStore._verify_dimension` is what
        compares against what is on disk, and it has its own message. Keeping
        the two apart matters because they have different fixes: this one is a
        one-line edit to `.env`, and that one may require dropping the vectors.

        Checked on the first batch rather than at load, because a model can load
        happily and still be the wrong shape - which is exactly what changing
        `EMBED_MODEL` in .env looks like.
        """
        if self._checked_dim:
            return
        if len(vector) != self.dim:
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.embedder",
                # **Was "but this index stores {self.dim}", and that was wrong.**
                # It named the index for a value that came from `.env`, and the
                # suggestion then sent somebody to "rebuild the index from
                # scratch" - which cannot affect this check, because this check
                # never reads the index. Reported from the window on
                # 2026-08-26 after a reset and rebuild that could not have
                # helped and did not. An error that misnames its own cause
                # costs more than no error, because it is acted on.
                details=f"{self.model_name} returns {len(vector)} dimensions, "
                        f"but EMBED_DIM is {self.dim}",
                suggestion=(
                    f"Set EMBED_DIM={len(vector)} in .env to match "
                    f"{self.model_name}, or put EMBED_MODEL back to a "
                    f"{self.dim}-dimension model. Nothing needs re-indexing for "
                    f"this. If vectors were already stored at the old width, "
                    f"the vector store will say so separately - they are "
                    f"derived data and can be rebuilt with 'app.cli reembed'."
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
