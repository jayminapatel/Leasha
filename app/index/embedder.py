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
from pathlib import Path

import numpy as np
from typing import Callable, Iterable, Iterator, Optional, Sequence

from app.core.errors import AppErrorException, make_error
from app.core.gpu_serialize import (
    gpu_exclusive,
    is_transient_gpu_error,
    mark_gpu_unreliable,
)
from app.core.logging import logger
from app.index import backends

_log = logger.bind(component="index.embedder")

__all__ = ["Embedder", "EMBED_BATCH", "l2_normalise"]

#: Rough download size in bytes, keyed by the model names this application
#: actually ships with - for progress display only, never for correctness.
#: fastembed's own `TextEmbedding(...)` call does not forward a progress
#: hook this far (its __init__ calls `download_model(...)` with a fixed
#: argument list that drops any extra kwargs before they would reach
#: huggingface_hub's `tqdm_class` parameter), so progress here is measured
#: from the outside: how big the cache directory has grown, against this
#: estimate. A model larger than expected simply stops advancing before
#: 100% rather than reporting something false; the "download finished"
#: signal is the constructor call returning, not the bar reaching the end.
_APPROX_MODEL_BYTES: dict[str, int] = {
    "BAAI/bge-small-en-v1.5": 130_000_000,
}
_DEFAULT_APPROX_BYTES = 500_000_000

#: How often the progress watcher re-measures the cache directory.
_PROGRESS_POLL_SECONDS = 0.3


def _directory_size(path: Path) -> int:
    """Total bytes under `path`, recursively. 0 if it does not exist."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


class _DownloadProgressWatcher:
    """Polls a cache directory's growth on a background thread.

    Reports an approximate 0-100 progress to `on_progress` until `stop()` is
    called. A daemon thread, and every call to `on_progress` is guarded -
    this exists to make a splash screen more informative, and must never be
    the reason a model fails to load.
    """

    def __init__(
        self,
        cache_dir: str,
        on_progress: Callable[[float], None],
        approx_total_bytes: int,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._on_progress = on_progress
        self._approx_total = max(1, approx_total_bytes)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            size = _directory_size(self._cache_dir)
            percent = min(99.0, 100.0 * size / self._approx_total)
            try:
                self._on_progress(percent)
            except Exception:                      # noqa: BLE001 - a UI callback must never break loading
                pass
            self._stop.wait(_PROGRESS_POLL_SECONDS)

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
        threads: int = 0,
        quantised: bool = False,
        on_progress: Optional[Callable[[float], None]] = None,
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
        #: Intra-op threads for the ONNX session. `0` leaves onnxruntime's own
        #: default, which is every core - fine for a benchmark and wrong during
        #: an index run, where the extraction workers already hold several.
        #: The number comes from `index/resolve.py`, which is the same
        #: arithmetic the tuning screen shows.
        self.threads = max(0, int(threads or 0))
        #: Prefer the quantised model file: several times smaller and faster on
        #: a processor, no gain on a graphics card, a small cost in ranking.
        self.quantised = bool(quantised)
        self._profile = profile
        self._problems = problems
        #: Set once the model loads. `None` until then, so nothing reports a
        #: provider that has not yet been proven to work.
        self.choice: Optional[backends.Choice] = None
        #: Called with 0-100 while a first-run model download is in
        #: progress, so a caller (the splash screen at startup) can show
        #: real numbers rather than a static "downloading" message. `None`
        #: when nobody asked, and cheap to check when nobody did.
        self._on_progress = on_progress

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
            quantised=bool(getattr(settings, "embed_quantised", False)),
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
            if self.quantised and wanted.is_gpu:
                # **Refused where it buys nothing, and said out loud.**
                # Quantisation is a processor optimisation; on the graphics
                # card it costs ranking quality for no speed at all. The
                # control is greyed for this reason, so reaching here means a
                # stored setting met a machine that changed under it.
                _log.info("the smaller model file was asked for and is not "
                          "used: it gains nothing on the graphics card")

            def build(providers: tuple) -> object:
                extra: dict = {}
                if self.threads:
                    # **Only when it was decided**, never a default of our own.
                    # Left alone, onnxruntime takes every core - which is right
                    # for a benchmark and wrong during an index run, where the
                    # extraction workers already hold several and the two
                    # multiply into a machine slower than it started.
                    extra["threads"] = self.threads
                if providers == (backends.CPU_PROVIDER,) and not extra:
                    # **The CPU path is byte-for-byte what it was.** Passing a
                    # providers list that means "the default" would still be a
                    # new argument to somebody else's constructor on every
                    # machine, and §2's promise is that CPU behaviour is
                    # untouched by this seam existing.
                    return TextEmbedding(model_name=self.model_name,
                                         cache_dir=self.cache_dir)
                if providers != (backends.CPU_PROVIDER,):
                    extra["providers"] = list(providers)
                return TextEmbedding(model_name=self.model_name,
                                     cache_dir=self.cache_dir, **extra)

            watcher = self._start_progress_watcher_if_downloading()
            try:
                # **Only the construction call itself, gated on what was
                # asked for.** A second subsystem building its own ONNX/
                # DirectML session at the same moment is the access-violation
                # in `logs/crash/crash.log` (2026-09-07); see `gpu_serialize`.
                with gpu_exclusive(wanted.is_gpu):
                    model, self.choice = backends.with_fallback(
                        build, wanted, problems=self._problems)
            except Exception as exc:               # noqa: BLE001 - download, disk, or ONNX
                raise AppErrorException(make_error(
                    "ERR_MODEL_LOAD", "index.embedder",
                    details=f"{self.model_name}: {type(exc).__name__}: {exc}",
                )) from exc
            finally:
                if watcher is not None:
                    watcher.stop()
                    if self._on_progress is not None:
                        # Whatever the estimate said, the real signal that
                        # the download is over is this call returning.
                        try:
                            self._on_progress(100.0)
                        except Exception:          # noqa: BLE001
                            pass

            if wanted.fell_back_from and self._problems is not None:
                self._problems.append(wanted.why)

            backends.record_provider("meaning model", self.choice)
            self._encoder = lambda texts: model.embed(list(texts))
            return self._encoder

    def _start_progress_watcher_if_downloading(self) -> Optional["_DownloadProgressWatcher"]:
        r"""Start watching the cache directory grow, if there is anyone to tell.

        **Only when there is real work to report.** A first-run download and
        an ordinary load of an already-cached model both call the same
        `TextEmbedding(...)` constructor from the caller's point of view -
        the only visible difference is whether the cache directory is
        already close to the model's expected size. Starting the watcher
        unconditionally would report a spurious "downloading" progress bar
        on every ordinary, already-cached warm-up.
        """
        if self._on_progress is None or not self.cache_dir:
            return None
        approx_total = _APPROX_MODEL_BYTES.get(self.model_name, _DEFAULT_APPROX_BYTES)
        already_cached = _directory_size(Path(self.cache_dir)) >= approx_total * 0.9
        if already_cached:
            return None
        watcher = _DownloadProgressWatcher(self.cache_dir, self._on_progress, approx_total)
        watcher.start()
        return watcher

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

    def embed(self, texts: Sequence[str]) -> "np.ndarray":
        """Embed one batch. Returns a float32 `(len(texts), dim)` array, one
        unit vector per input row, in order - see order 0b section 6c. An
        empty `texts` still returns a plain `[]`, not an empty array: a file
        with no chunks must not touch the model to say so."""
        if not texts:
            return []

        encoder = self._ensure_encoder()
        try:
            raw = self._run(encoder, texts)
        except AppErrorException:
            raise
        except Exception as exc:                   # noqa: BLE001 - boundary
            # **A transient DXGI device-removed event is not the same failure
            # as a corrupt model.** `logs/runs/run-20260908-050751-window.log`
            # (line 121-123): the driver reported the GPU as suspended
            # mid-batch - a driver reset or the device briefly dropping out of
            # the system, not the downloaded model being wrong - and yet every
            # exception here used to get the same "delete the model cache"
            # suggestion regardless. Worse, `self._encoder` was never cleared,
            # so every later `embed()` on this same instance kept hitting the
            # identical now-broken session for the rest of the run even after
            # a driver reset that often resolves within seconds. Clearing it
            # here makes the *next* `_ensure_encoder()` call rebuild from
            # scratch through `backends.with_fallback` - which may land back
            # on the graphics card if it recovered, or fall back to the
            # processor if it has not, either of which beats repeating a call
            # that is doomed to fail identically every time.
            #
            # **2026-09-08, later the same day: clearing was not enough.**
            # `logs/runs/run-20260908-055844-window.log` at 06:29:49: the
            # driver failed with `887A0020` thirty minutes into a run, the
            # exception left this method, and `pipeline._feed_worker` ended
            # the whole run on it - by design, since a batch that cannot be
            # embedded must not be silently dropped. So the "next batch"
            # the old suggestion promised never came, and the user sat for
            # three and a half hours in front of a run that was already
            # dead. The batch is now retried **once**, on the processor, in
            # `_retry_on_processor` below; only a second failure reaches the
            # pipeline. A non-transient exception takes exactly the path it
            # always did.
            if not is_transient_gpu_error(exc):
                raise AppErrorException(make_error(
                    "ERR_MODEL_LOAD", "index.embedder",
                    details=f"embedding {len(texts)} text(s) failed: "
                            f"{type(exc).__name__}: {exc}",
                    suggestion=(
                        "The model loaded but would not run. Delete the model "
                        "cache under <DATA_PATH>\\models and let it download "
                        "again."
                    ),
                )) from exc
            raw = self._retry_on_processor(texts, exc)

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
        # **10.05ms before, 0.88ms after, 11.4x**, and the two agree to 1e-12
        # under the float64 path this comment used to describe.
        #
        # **float32 out, end to end - order 0b, index tuning, section 6c.**
        # This used to widen to float64 (a Python `float` *is* a C double, so
        # `.tolist()` on a float64 block produced one) because an earlier
        # float32 attempt changed two answers - unit length came back
        # 1.0000000015 against a 1e-9 tolerance, and a vector the code
        # promises to leave alone came back altered in the eighth decimal
        # (both pinned in tests/unit/test_embedder.py, tolerances now sized
        # for float32). That was the right call while every caller still
        # wanted list[float].
        #
        # It stopped being the right call once VectorStore.add started
        # building its Arrow table straight from this array
        # (app/storage/vector_store.py, _arrow_table): LanceDB's own schema
        # already declares vector: list_(float32(), dim), so returning
        # float64 here meant every batch was widened from the model's native
        # float32, normalised, and narrowed straight back to float32 by Arrow
        # on the way into the table - a round trip that bought nothing and
        # cost a second full copy of every batch. float32 in, float32
        # arithmetic, float32 out matches the model's own precision and the
        # column's, so nothing is widened only to be thrown away a batch
        # later.
        #
        # No .tolist(): the array itself is the return value now. A caller
        # wanting a plain list still gets one row at a time from embed_all,
        # each of which is list()-able exactly as before; VectorStore.add
        # takes the block directly.
        block = np.asarray(raw, dtype=np.float32)
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
        return block

    def _run(self, encoder: Encoder, texts: Sequence[str]) -> list:
        """One inference call, behind the cross-subsystem gate. Raises
        whatever the encoder raises - classification is the caller's job."""
        # **Gated on what actually ran, not on what was asked for.** A
        # fallen-back-to-CPU choice must not keep paying the cross-
        # subsystem lock it no longer needs; see `gpu_serialize`.
        with gpu_exclusive(bool(self.choice and self.choice.is_gpu)):
            return list(encoder(texts))

    def _retry_on_processor(self, texts: Sequence[str],
                            cause: BaseException) -> list:
        """The same batch, once more, on the processor. 2026-09-08.

        Called only for an exception `is_transient_gpu_error` recognised.
        The flow, in order, and bounded to exactly one retry:

        1. `mark_gpu_unreliable` - process-wide, so `backends.choose()` sends
           this rebuild **and every other subsystem's next rebuild** to the
           processor. The driver's own words were "the application should
           not continue"; it is not asked again this session.
        2. Warn once, in plain words, and put the same words in `problems`
           so the run's notice carries it.
        3. Drop the dead session and rebuild through `_ensure_encoder()`,
           which lands on the processor because of step 1 - proved by
           `test_embedder.py`, not assumed.
        4. Call the encoder once more on the same `texts`. A straight second
           call, deliberately **not** through `embed()`, so there is no way
           back into the except path that led here: a second failure raises
           out of this method and ends the run, as any genuine breakage
           should (H4: degrade loudly, never loop, never hang).

        A rebuild that itself fails raises `_ensure_encoder`'s own
        `AppErrorException`, whose details already name the construction
        failure; it is left as it is rather than re-wrapped.
        """
        reason = f"{type(cause).__name__}: {cause}"
        if len(reason) > 200:
            # The driver text runs to several lines with a source path in
            # it; the HRESULT and the first sentence are what a reader needs.
            reason = reason[:200] + "..."
        mark_gpu_unreliable(reason)

        message = ("the graphics driver failed while embedding, so this run "
                   "continues on the processor - slower, and searches still "
                   "work")
        _log.warning("{} ({})", message, reason)
        if self._problems is not None:
            try:
                self._problems.append(message)
            except Exception:                      # noqa: BLE001 - a notice list must never break a batch
                pass

        self._encoder = None
        self.choice = None
        encoder = self._ensure_encoder()           # lands on the processor - see step 3

        try:
            return self._run(encoder, texts)
        except Exception as exc:                   # noqa: BLE001 - boundary
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.embedder",
                details=f"embedding {len(texts)} text(s) failed on the graphics "
                        f"card ({reason}) and again on the processor: "
                        f"{type(exc).__name__}: {exc}",
                suggestion=(
                    "Your graphics driver reported the GPU as unavailable (a "
                    "driver reset, heavy system load, or the machine waking from "
                    "sleep can cause this) - this is not a problem with the "
                    "downloaded model. Leasha retried once on the processor and "
                    "that failed too, so this run has stopped. Restart Leasha "
                    "and run indexing again; what was already indexed is kept."
                ),
            )) from exc

    def embed_all(self, texts: Sequence[str]) -> "Iterator[np.ndarray]":
        """Embed any number of texts, `batch_size` at a time, lazily.

        Lazy so the caller can write each batch to the store as it arrives; on a
        million chunks, materialising every vector first would be several GB of
        list before a single row was persisted. Yields one float32 1D array per
        text - each row of the block `embed()` returns - not a `list[float]`;
        `.tolist()` it if a plain list is what's wanted.
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
