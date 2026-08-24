"""The search engine: parse, dispatch both retrievers, fuse, rerank, answer.

Layer: L4

**The budget is the design.** <300ms warm. Everything here is arranged around
that: the two retrievers run in parallel because they are independent and the
slower one sets the floor; fusion is a pure function costing 0.1ms; the
cross-encoder is last and optional; and no LLM is involved at any point, which
is the project's first non-negotiable.

**Two tiers, because as-you-type and pressing Enter are different questions.**
Typing a character means "show me something now" — BM25 only, no model, no ANN,
20 results. Stopping typing means "I meant that" — the full hybrid pipeline. The
interim tier exists because embedding a query and probing the ANN index on every
keystroke burst is work that is thrown away before the user has finished the word.

**The cache is keyed on the index generation.** Any write bumps it, so a cached
result can never outlive the data it came from. That is the difference between a
cache and a lie: without the generation in the key, re-indexing a file leaves
every stale result for it sitting in the cache, and the user sees search results
for text they just deleted.

**Logging never fails a search.** The usage tables exist for Layer 10, and a
tuning feature seven layers away is not permitted to break the feature people
actually use, so every write to them is guarded.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from app.core.errors import AppErrorException
from app.core.logging import logger
from app.search import keyword, vector
from app.search.fusion import RRF_K, fuse_hits
from app.search.query import ParsedQuery, parse_query
from app.search.rerank import Reranker

__all__ = ["SearchEngine", "SearchResult", "SearchResponse"]

#: Results returned after fusion, before reranking. From the spec's pipeline.
FUSED_LIMIT = 50

#: The interim tier's size. Deliberately small: it is a glance, not an answer.
INTERIM_LIMIT = 20

_log = logger.bind(component="search.engine")


@dataclass
class SearchResult:
    """One hit, with everything the UI needs to show and open it."""

    chunk_id: int
    file_id: int
    path: str
    text: str
    score: float
    rank: int
    page: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    #: Which retrievers found it. Both agreeing is the strongest signal the
    #: pipeline produces, and the UI is expected to say so.
    sources: tuple[int, ...] = ()
    rerank_score: Optional[float] = None

    @property
    def found_by_both(self) -> bool:
        return len(self.sources) > 1

    def explain(self) -> str:
        """Why this result is here, in words. Trust comes from being able to ask."""
        if self.found_by_both:
            reason = "keyword and meaning both matched"
        elif self.sources == (0,):
            reason = "keyword match"
        elif self.sources == (1,):
            reason = "similar meaning"
        else:
            reason = "matched"
        if self.rerank_score is not None:
            reason += ", reranked"
        return reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank, "chunk_id": self.chunk_id, "file_id": self.file_id,
            "path": self.path, "page": self.page, "score": round(self.score, 6),
            "sources": list(self.sources), "explain": self.explain(),
            "char_start": self.char_start, "char_end": self.char_end,
            "text": self.text,
        }


@dataclass
class SearchResponse:
    results: list[SearchResult] = field(default_factory=list)
    parsed: Optional[ParsedQuery] = None
    elapsed_ms: float = 0.0
    from_cache: bool = False
    interim: bool = False
    reranked: bool = False
    search_id: Optional[int] = None
    timings: dict[str, float] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": len(self.results),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "from_cache": self.from_cache,
            "interim": self.interim,
            "reranked": self.reranked,
            "search_id": self.search_id,
            "timings_ms": {k: round(v, 1) for k, v in self.timings.items()},
            "unknown_operators": list(self.parsed.unknown_operators) if self.parsed else [],
            "results": [result.as_dict() for result in self.results],
        }


class SearchEngine:
    """Everything between a typed string and a ranked list."""

    def __init__(
        self,
        store: Any,
        vectors: Any,
        embedder: Any,
        *,
        reranker: Optional[Reranker] = None,
        cache: Any = None,
        rrf_k: int = RRF_K,
        weights: Optional[tuple[float, float]] = None,
        log_usage: bool = True,
    ) -> None:
        self.store = store
        self.vectors = vectors
        self.embedder = embedder
        self.reranker = reranker
        self.cache = cache
        self.rrf_k = rrf_k
        #: (keyword, vector). Equal today; Layer 10 is where these stop being a
        #: guess and start being measured against this corpus.
        self.weights = weights
        self.log_usage = log_usage
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="search")

    def close(self) -> None:
        self._pool.shutdown(wait=False)

    def warm_up(self) -> None:
        """Load the models now, off the first search's critical path."""
        try:
            self.embedder.warm_up()
        except AppErrorException as exc:
            _log.warning("embedding model not ready: {}", exc.error.message)
        if self.reranker is not None:
            self.reranker.warm_up()

    # -- the two tiers ------------------------------------------------------

    def interim(self, raw: str, *, limit: int = INTERIM_LIMIT) -> SearchResponse:
        """Keyword only, no model, no ANN. What runs while someone is typing.

        Never cached and never logged: it is not a search anybody made, it is a
        glance at a half-typed word, and recording it as intent would poison the
        very data Layer 10 depends on.
        """
        started = time.perf_counter()
        parsed = parse_query(raw)
        if not parsed.has_text and not parsed.has_filters:
            return SearchResponse(parsed=parsed, interim=True)

        hits = keyword.search(self.store, parsed, limit=limit)
        results = [
            self._to_result(hit, rank, (0,), float(-hit.get("score", 0.0)))
            for rank, hit in enumerate(hits, start=1)
        ]
        return SearchResponse(
            results=results, parsed=parsed, interim=True,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def search(
        self,
        raw: str,
        *,
        limit: int = FUSED_LIMIT,
        rerank: Optional[bool] = None,
        use_cache: bool = True,
    ) -> SearchResponse:
        """The full pipeline. What runs when someone stops typing or presses Enter."""
        started = time.perf_counter()
        timings: dict[str, float] = {}

        mark = time.perf_counter()
        parsed = parse_query(raw)
        timings["parse"] = (time.perf_counter() - mark) * 1000

        if not parsed.has_text and not parsed.has_filters:
            return SearchResponse(parsed=parsed, elapsed_ms=timings["parse"])

        want_rerank = self._wants_rerank(rerank)
        cache_key = self._cache_key(raw, parsed, want_rerank, limit)

        if use_cache and self.cache is not None:
            cached = self._cache_get(cache_key)
            if cached is not None:
                cached.from_cache = True
                cached.elapsed_ms = (time.perf_counter() - started) * 1000
                return cached

        # Both retrievers at once: independent, so the slower one sets the floor
        # rather than the sum setting it.
        mark = time.perf_counter()
        allowed = keyword.file_ids_matching(self.store, parsed)
        keyword_future = self._pool.submit(keyword.search, self.store, parsed)
        vector_future = self._pool.submit(
            vector.search, self.vectors, self.embedder, parsed, allowed_file_ids=allowed
        )
        keyword_hits = keyword_future.result()
        raw_vector_hits = vector_future.result()
        timings["retrieve"] = (time.perf_counter() - mark) * 1000

        mark = time.perf_counter()
        vector_hits = vector.hydrate(self.store, raw_vector_hits)
        timings["hydrate"] = (time.perf_counter() - mark) * 1000

        mark = time.perf_counter()
        fused = fuse_hits(
            [keyword_hits, vector_hits], id_key="chunk_id", k=self.rrf_k,
            weights=list(self.weights) if self.weights else None, limit=limit,
        )
        timings["fuse"] = (time.perf_counter() - mark) * 1000

        reranked = False
        if want_rerank and fused:
            mark = time.perf_counter()
            before = [hit["chunk_id"] for hit in fused]
            fused = self.reranker.rerank(parsed.embed_text or raw, fused)
            reranked = [hit["chunk_id"] for hit in fused] != before or any(
                "rerank_score" in hit for hit in fused
            )
            timings["rerank"] = (time.perf_counter() - mark) * 1000

        results = [
            self._to_result(hit, rank, tuple(hit.get("sources", ())), hit.get("rrf_score", 0.0))
            for rank, hit in enumerate(fused, start=1)
        ]

        response = SearchResponse(
            results=results, parsed=parsed, reranked=bool(reranked),
            elapsed_ms=(time.perf_counter() - started) * 1000, timings=timings,
        )

        if use_cache and self.cache is not None:
            self._cache_set(cache_key, response)

        response.search_id = self._log_search(raw, parsed, response, want_rerank)
        return response

    # -- opening a result ---------------------------------------------------

    def record_open(self, search_id: Optional[int], chunk_id: int) -> None:
        """The single most valuable signal in the system: this one was useful.

        Everything in Layer 10 is derived from these. Guarded, because a tuning
        feature must never be able to break opening a file.
        """
        if search_id is None or not self.log_usage:
            return
        try:
            self.store.mark_opened(search_id, chunk_id)
        except Exception as exc:        # noqa: BLE001
            _log.debug("could not record an open: {}", exc)

    # -- internals ----------------------------------------------------------

    def _wants_rerank(self, override: Optional[bool]) -> bool:
        if self.reranker is None:
            return False
        if override is not None:
            return override and self.reranker.available
        return self.reranker.available

    def _cache_key(self, raw: str, parsed: ParsedQuery, rerank: bool, limit: int) -> str:
        """Includes the index generation, so a write invalidates everything.

        Without it, re-indexing a file leaves every stale result for that file
        in the cache, and the user gets hits on text they have just deleted.
        """
        try:
            generation = self.store.generation()
        except Exception:               # noqa: BLE001 - a cache key is not worth failing over
            generation = -1
        return "|".join([
            "v1", str(generation), raw.strip().lower(),
            repr(parsed.ext), repr(parsed.after), repr(parsed.before),
            repr(parsed.paths), repr(parsed.senders),
            "r" if rerank else "-", str(limit),
        ])

    def _cache_get(self, key: str) -> Optional[SearchResponse]:
        try:
            cached = self.cache.get(key)
        except Exception as exc:        # noqa: BLE001 - a broken cache degrades to no cache
            _log.debug("cache read failed: {}", exc)
            return None
        if cached is None:
            return None

        # A copy, never the stored object. Handing out the cached instance and
        # then stamping `from_cache` and `elapsed_ms` on it mutates what every
        # previous caller is still holding - so the response somebody got two
        # searches ago silently changes to say it came from a cache it did not.
        # diskcache pickles and so returns a fresh object anyway; an in-memory
        # cache does not, and the engine must not depend on which it was given.
        return replace(cached, results=list(cached.results), timings=dict(cached.timings))

    def _cache_set(self, key: str, response: SearchResponse) -> None:
        try:
            self.cache.set(key, response)
        except Exception as exc:        # noqa: BLE001
            _log.debug("cache write failed: {}", exc)

    def _to_result(
        self, hit: dict[str, Any], rank: int, sources: tuple[int, ...], score: float
    ) -> SearchResult:
        return SearchResult(
            chunk_id=int(hit.get("chunk_id", 0)),
            file_id=int(hit.get("file_id", 0)),
            path=str(hit.get("path", "")),
            text=str(hit.get("text", "")),
            page=hit.get("page"),
            char_start=hit.get("char_start"),
            char_end=hit.get("char_end"),
            score=float(hit.get("rerank_score", score) if "rerank_score" in hit else score),
            rank=rank,
            sources=tuple(sources),
            rerank_score=hit.get("rerank_score"),
        )

    def _log_search(
        self, raw: str, parsed: ParsedQuery, response: SearchResponse, rerank: bool
    ) -> Optional[int]:
        if not self.log_usage:
            return None
        try:
            filters = json.dumps({
                "ext": list(parsed.ext),
                "after": parsed.after.isoformat() if parsed.after else None,
                "before": parsed.before.isoformat() if parsed.before else None,
                "paths": list(parsed.paths),
                "senders": list(parsed.senders),
            })
            search_id = self.store.log_search(
                raw, filters=filters, hits=len(response.results),
                elapsed_ms=int(response.elapsed_ms), rerank_on=rerank,
            )
            self.store.log_hits(search_id, [
                {"chunk_id": result.chunk_id, "sources": ",".join(map(str, result.sources))}
                for result in response.results
            ])
            return search_id
        except Exception as exc:        # noqa: BLE001 - Layer 10's data is never worth a
            _log.debug("usage logging failed, search unaffected: {}", exc)
            return None
