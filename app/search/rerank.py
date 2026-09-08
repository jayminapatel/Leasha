"""Cross-encoder reranking — precision, bought for 200ms, and always optional.

Layer: L4

Retrieval is fast and approximate: BM25 scores a query against a document
without either one ever seeing the other in context, and a bi-encoder compares
two vectors that were computed independently. A cross-encoder reads the query
and the candidate *together*, which is why it is better and why it cannot be
used for retrieval — it would have to run against every chunk in the index.

So it runs last, over the top 30 only, and reorders what fusion already found.

**It must never be able to fail a search.** The model is optional, ~1.1GB, and
may be absent, half-downloaded, or deleted by someone reclaiming disk space
mid-session. Every one of those is handled the same way: log it once, return the
fused order untouched, and let the search succeed slightly less well. A search
engine that returns nothing because an *optional* precision step could not load
is worse than one that never had the step.

"Once" matters. On a corpus this size the failure would otherwise be logged on
every keystroke of every search, burying everything else in the log.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional, Sequence

from app.core.gpu_serialize import (
    gpu_exclusive,
    is_transient_gpu_error,
    mark_gpu_unreliable,
)
from app.core.logging import logger
from app.search.window import RERANK_WINDOW_CHARS, windows_for

#: Consecutive scoring failures before reranking is given up on for the
#: session. **Consecutive**, and reset by any success: the fault this replaces
#: was a single transient disabling reranking permanently, with nothing but a
#: restart to undo it.
RERANK_FAILURE_BUDGET = 3

__all__ = ["Reranker", "RERANK_TOP_N", "RERANK_WINDOW_CHARS"]

#: Candidates reordered. Beyond this the latency cost outgrows the benefit:
#: anything below rank 30 after fusion is rarely the answer.
#:
#: **Measured, and it was costing 8.3 of a 9-second search.** Thirty passages
#: through a 278M-parameter cross-encoder is 278ms *each* on a CPU. The count is
#: not the main lever - the model and the passage length are - but it multiplies
#: both, so it is settable rather than fixed.
RERANK_TOP_N = 30

_log = logger.bind(component="search.rerank")

#: Scorer signature: (query, passages) -> one score per passage, higher is better.
Scorer = Callable[[str, Sequence[str]], Sequence[float]]


class Reranker:
    """Lazily loaded cross-encoder that degrades to a no-op rather than failing.

    `scorer` is injectable, so ordering behaviour and every degradation path are
    testable without a 1.1GB download.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-base",
        *,
        cache_dir: Optional[str] = None,
        top_n: int = RERANK_TOP_N,
        enabled: bool = True,
        scorer: Optional[Scorer] = None,
        window_chars: int = RERANK_WINDOW_CHARS,
        device: str = "auto",
        profile: Optional[object] = None,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.top_n = top_n
        self.enabled = enabled
        self.window_chars = int(window_chars)
        #: The same `EMBED_DEVICE` the embedder uses - **one decision, three
        #: consumers**. A machine where the meaning model ran on the graphics
        #: card and the reranker did not would be one whose search latency
        #: nobody could account for.
        self.device = str(device or "auto")
        self._profile = profile
        #: Which processor actually ran it. `None` until the model loads.
        self.choice: Optional[object] = None
        self._scorer = scorer
        self._lock = threading.Lock()
        self._unavailable = False       # set once the failure budget is spent
        #: Consecutive scoring failures. See `RERANK_FAILURE_BUDGET`.
        self._failures = 0
        self._warned = False

    @classmethod
    def from_settings(cls, settings: object, **overrides: object) -> "Reranker":
        """The reranker this configuration asks for, `EMBED_DEVICE` included.

        Same reasoning as `Embedder.from_settings`: five call sites each
        spelling out four arguments is five places for a new one to be
        forgotten, and the one being added here is the one that decides which
        processor runs the model.
        """
        fields: dict = dict(
            cache_dir=str(getattr(settings, "model_cache", "") or "") or None,
            enabled=bool(getattr(settings, "rerank_enabled", True)),
            top_n=int(getattr(settings, "rerank_top_n", RERANK_TOP_N)),
            window_chars=int(getattr(settings, "rerank_window_chars",
                                     RERANK_WINDOW_CHARS)),
            device=str(getattr(settings, "embed_device", "auto") or "auto"),
        )
        fields.update(overrides)
        return cls(str(getattr(settings, "rerank_model", "")
                       or "BAAI/bge-reranker-base"), **fields)

    @property
    def available(self) -> bool:
        return self.enabled and not self._unavailable

    def warm_up(self) -> bool:
        """Load the model now, from a background thread at startup.

        Returns whether it is usable, so Settings can show the toggle's real
        state rather than what the user asked for.
        """
        return self._ensure_scorer() is not None

    def _ensure_scorer(self) -> Optional[Scorer]:
        if self._scorer is not None:
            return self._scorer
        if self._unavailable or not self.enabled:
            return None

        with self._lock:
            if self._scorer is not None:
                return self._scorer
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                from app.index import backends

                def build(providers: tuple) -> object:
                    if providers == (backends.CPU_PROVIDER,):
                        return TextCrossEncoder(model_name=self.model_name,
                                                cache_dir=self.cache_dir)
                    return TextCrossEncoder(model_name=self.model_name,
                                            cache_dir=self.cache_dir,
                                            providers=list(providers))

                wanted = backends.choose(self._backend_profile(), self.device)
                # **Only the construction call itself, gated on what was
                # asked for.** A second subsystem building its own ONNX/
                # DirectML session at the same moment is the access-violation
                # in `logs/crash/crash.log` (2026-09-07); see `gpu_serialize`.
                with gpu_exclusive(wanted.is_gpu):
                    model, self.choice = backends.with_fallback(build, wanted)
                backends.record_provider("reranker", self.choice)
                self._scorer = lambda query, passages: list(
                    model.rerank(query, list(passages))
                )
                return self._scorer
            except Exception as exc:    # noqa: BLE001 - absent, partial, or broken
                self._unavailable = True
                self._warn_once(exc)
                return None

    def _backend_profile(self) -> object:
        """This machine, detected once and only when a model is being loaded."""
        if self._profile is None:
            try:
                from app.core.compute_profile import detect

                self._profile = detect()
            except Exception:               # noqa: BLE001 - detection never fatal
                self._profile = object()
        return self._profile

    def _warn_once(self, exc: BaseException, *, transient: bool = False) -> None:
        if self._warned:
            return
        self._warned = True
        if transient:
            # **The accurate reason, not the generic one.** A DXGI device-
            # removed event is a driver reset or the GPU briefly dropping out
            # of the system, not a missing or broken reranker model - and
            # unlike the message below, a fresh scorer will be attempted
            # again on the next search, within the same failure budget.
            _log.warning(
                "Reranking's graphics-card session was reported unavailable (a "
                "driver reset, heavy system load, or the machine waking from "
                "sleep can cause this) - not a problem with the reranker model. "
                "This search keeps its fused order; Leasha will reload the "
                "model and try again on the next search. Cause: {}: {}.",
                type(exc).__name__, exc,
            )
            return
        _log.warning(
            "Reranking is unavailable, so results keep their fused order. Search is unaffected "
            "apart from slightly weaker ordering. Cause: {}: {}. "
            "Re-run the installer to fetch the model, or turn reranking off in Settings to "
            "stop it being attempted.",
            type(exc).__name__, exc,
        )

    def rerank(
        self,
        query: str,
        hits: Sequence[dict[str, Any]],
        *,
        text_key: str = "text",
        terms: Optional[Sequence[str]] = None,
    ) -> list[dict[str, Any]]:
        """Reorder the top `top_n` hits. Returns every hit, always.

        The tail below `top_n` keeps its fused order and is appended unchanged -
        reranking is a reordering of the head, not a filter, and dropping the
        tail would silently shrink every result list.

        **A row with no `text_key` never reaches the model.** `hydrate_images`
        (`vector.py`) hands back photo hits with a path and an ext, and
        deliberately no text - there is no passage, no page, no `chunks` row to
        take one from. Scoring an empty string against it is not "no opinion";
        a cross-encoder reads an empty passage as having nothing in common with
        the query and says so with the same confidence as a real verdict, which
        buries every image hit at the bottom of the head. So these rows are
        pulled out before scoring - by index, never by substituting the
        filename or anything else as a stand-in passage - and reinserted
        afterwards at the exact index fusion put them at, untouched.
        """
        results = list(hits)
        if not results or not query.strip():
            return results

        # Partition by fused index, before the scorer is even loaded: an
        # index in `textless` never becomes a passage, and its original
        # position is what lets it go straight back in below.
        textless: dict[int, dict[str, Any]] = {}
        scoreable: list[dict[str, Any]] = []
        for index, hit in enumerate(results):
            if str(hit.get(text_key, "")).strip():
                scoreable.append(hit)
            else:
                textless[index] = hit

        if not scoreable:
            # An all-image result set. There is nothing to rerank, so there
            # is nothing to load the model for either.
            return results

        scorer = self._ensure_scorer()
        if scorer is None:
            return results

        # The head/tail split is over the *scoreable* rows only - a photo
        # sitting near the top of the fused list must not spend one of the
        # `top_n` slots that real passages compete for.
        head, tail = scoreable[: self.top_n], scoreable[self.top_n :]

        # **Only the part worth scoring.** A cross-encoder's cost is at best
        # linear in passage length and usually worse, and it was being handed
        # whole chunks - a mean of 1,070 characters, a maximum of 2,734 - to
        # reach a verdict the sentence around the match already supports. See
        # `window.py`; the cut is centred on the densest run of query terms.
        passages = windows_for(
            (str(hit.get(text_key, "")) for hit in head),
            list(terms or ()) or query.split(),
            width=self.window_chars,
        )

        try:
            # **Gated on what actually ran, not on what was asked for.** A
            # fallen-back-to-CPU choice must not keep paying the cross-
            # subsystem lock it no longer needs; see `gpu_serialize`.
            with gpu_exclusive(bool(self.choice and self.choice.is_gpu)):
                scores = list(scorer(query, passages))
        except Exception as exc:        # noqa: BLE001 - a scoring failure is not a search failure
            # **A budget, not a latch.** One failure used to disable reranking
            # for the rest of the session: a single transient - a model file
            # being written, a moment of memory pressure, one malformed passage
            # - and every later search silently returned weaker ordering, with
            # the notice explaining it only on the first one. Nothing ever
            # tried again, so the only cure was restarting the application.
            transient = is_transient_gpu_error(exc)
            if transient:
                # **Each retry within the budget gets a fresh scorer, not the
                # same dead one.** Without this, `self._scorer` kept pointing
                # at the session DirectML had already reported unavailable, so
                # every one of the budget's remaining attempts was doomed
                # identically - the budget counted down to zero without ever
                # giving recovery a real chance. Clearing it here makes the
                # *next* `_ensure_scorer()` call rebuild through
                # `backends.with_fallback`, same idea as `embedder.py`.
                #
                # 2026-09-08, later: that rebuild now lands on the processor
                # rather than the same suspect driver - `mark_gpu_unreliable`
                # is the process-wide latch `backends.choose()` reads. The
                # failure budget below is untouched: this attempt still
                # counts, and a CPU scorer that then works clears the debt.
                mark_gpu_unreliable(f"{type(exc).__name__}: {exc}"[:200])
                self._scorer = None
                self.choice = None
            self._failures += 1
            self._unavailable = self._failures >= RERANK_FAILURE_BUDGET
            self._warn_once(exc, transient=transient)
            return results

        # A run that works clears the debt: the budget is for *consecutive*
        # trouble, not for a machine that had one bad minute an hour ago.
        self._failures = 0

        if len(scores) != len(head):
            _log.error(
                "reranker returned {} scores for {} passages; keeping the fused order",
                len(scores), len(head),
            )
            return results

        for hit, score in zip(head, scores, strict=True):
            hit["rerank_score"] = float(score)

        # Stable sort on score alone: ties keep their fused order, which is the
        # better fallback and keeps the whole pipeline deterministic.
        head.sort(key=lambda hit: -hit["rerank_score"])
        ranked = iter(head + tail)

        # Reassemble to the original length: every textless row goes back to
        # its own fused index, and the scored rows fill every other slot in
        # their new order.
        return [
            textless[index] if index in textless else next(ranked)
            for index in range(len(results))
        ]
