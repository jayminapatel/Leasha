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

from app.core.logging import logger

__all__ = ["Reranker", "RERANK_TOP_N"]

#: Candidates reordered. Beyond this the latency cost outgrows the benefit:
#: anything below rank 30 after fusion is rarely the answer.
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
    ) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.top_n = top_n
        self.enabled = enabled
        self._scorer = scorer
        self._lock = threading.Lock()
        self._unavailable = False       # tried once, failed; do not try again this session
        self._warned = False

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

                model = TextCrossEncoder(model_name=self.model_name, cache_dir=self.cache_dir)
                self._scorer = lambda query, passages: list(
                    model.rerank(query, list(passages))
                )
                return self._scorer
            except Exception as exc:    # noqa: BLE001 - absent, partial, or broken
                self._unavailable = True
                self._warn_once(exc)
                return None

    def _warn_once(self, exc: BaseException) -> None:
        if self._warned:
            return
        self._warned = True
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
    ) -> list[dict[str, Any]]:
        """Reorder the top `top_n` hits. Returns every hit, always.

        The tail below `top_n` keeps its fused order and is appended unchanged -
        reranking is a reordering of the head, not a filter, and dropping the
        tail would silently shrink every result list.
        """
        results = list(hits)
        if not results or not query.strip():
            return results

        scorer = self._ensure_scorer()
        if scorer is None:
            return results

        head, tail = results[: self.top_n], results[self.top_n :]
        passages = [str(hit.get(text_key, "")) for hit in head]

        try:
            scores = list(scorer(query, passages))
        except Exception as exc:        # noqa: BLE001 - a scoring failure is not a search failure
            self._unavailable = True
            self._warn_once(exc)
            return results

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
        return head + tail
