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

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger
from app.search import keyword, vector
from app.search.fusion import RRF_K, fuse_hits
from app.search.query import ParsedQuery, parse_query
from app.search.rerank import Reranker

__all__ = [
    "SearchEngine", "SearchResult", "SearchResponse", "Notice",
    "NOTICE_NO_VECTORS", "NOTICE_UNMATCHED_TERMS", "NOTICE_RERANK_UNAVAILABLE",
    "NOTICE_WILDCARD", "NOTICE_SORTED",
]

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
    #: The file's extension and modification time, for the result row to show.
    #:
    #: **Both were already being fetched and thrown away.** `keyword.py` and
    #: `vector.py` have selected `f.ext` and `f.mtime_ns` since Layer 4; this
    #: dataclass simply had nowhere to put them, so `_to_result` dropped them on
    #: the floor. In a fifteen-year archive with eight versions of everything,
    #: the date is frequently the only thing distinguishing two results - and it
    #: cost one line and no new query to show it.
    ext: str = ""
    mtime_ns: int = 0
    #: Which retrievers found it. Both agreeing is the strongest signal the
    #: pipeline produces, and the UI is expected to say so.
    sources: tuple[int, ...] = ()
    rerank_score: Optional[float] = None
    #: Where this row came from, when it did not come from the index.
    #:
    #: **Empty for everything the engine itself produces**, which is why this is
    #: a label rather than a kind: the engine has exactly one source and does not
    #: need to say so. A row federated in from somewhere else - today, a
    #: repository's history - fills it, and `explain()` shows it instead of
    #: "keyword match", because "keyword match" would be a claim about a
    #: retriever that never saw this row.
    #:
    #: A plain string rather than an enum because it is displayed verbatim and
    #: carries the commit, author and date that make a historical hit
    #: identifiable at all.
    source_label: str = ""

    @property
    def found_by_both(self) -> bool:
        return len(self.sources) > 1

    def explain(self) -> str:
        """Why this result is here, in words. Trust comes from being able to ask."""
        if self.source_label:
            return self.source_label
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


#: Codes for `Notice`. Stable, because the UI branches on them and a renamed
#: code is a silently-dropped notice.
NOTICE_NO_VECTORS = "NOTICE_NO_VECTORS"
NOTICE_UNMATCHED_TERMS = "NOTICE_UNMATCHED_TERMS"
NOTICE_RERANK_UNAVAILABLE = "NOTICE_RERANK_UNAVAILABLE"
#: What a wildcard turned into - how many terms, whether it was capped, and
#: whether stemming widened it. **The deliverable of the wildcard feature**, not
#: decoration: an expansion nobody can see is the silence it was built to fix.
NOTICE_WILDCARD = "NOTICE_WILDCARD"
#: The results were re-ordered by date, so relevance order was abandoned.
NOTICE_SORTED = "NOTICE_SORTED"


@dataclass(frozen=True)
class Notice:
    """Something the person should be told, which is not an error.

    **Not an `AppError`.** Nothing failed and nothing needs retrying - the
    search returned results. What happened is that it returned *worse* results
    than it should have, and saying so is the difference between a person
    trusting the answer and a person being quietly misled.

    `code` is what callers branch on; `message` is already worded for a human
    and is free to change without breaking anything.
    """

    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


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

    #: How many hits each retriever contributed, before fusion.
    #:
    #: Recorded because `vector.search` returns `[]` for an empty vector store,
    #: a failed embedding or a LanceDB hiccup - deliberately, since keyword
    #: results are better than an error. But that makes the failure invisible:
    #: results look thin and nothing distinguishes "the corpus is thin" from
    #: "the semantic half is dead". `vector_count == 0` on a query that clearly
    #: has meaning is the signal, and without these two numbers nobody can see
    #: it. This is the same shape as the sentinel bug that hid every PST.
    keyword_count: int = 0
    vector_count: int = 0
    #: Query words that appear nowhere in the index.
    #:
    #: **Usually the entire explanation for a baffling result list.** Terms are
    #: ORed, so a query whose one distinctive word matches nothing silently
    #: becomes a search for its most common words - and returns twenty
    #: confident, irrelevant results with nothing to say why.
    unmatched: tuple[str, ...] = ()

    #: Degradations the person should be told about, already worded.
    #:
    #: **A search that quietly returns worse results is the worst failure this
    #: application has**, because it looks exactly like a search that worked.
    #: `keyword_count` and `vector_count` were added to make that visible, and
    #: they did - to anyone who knew the rule and was reading a log file. In
    #: the window there was nothing at all.
    #:
    #: So the *judgement* lives here rather than in each caller. Every notice
    #: carries a `code`, because the contract is that the UI never parses a
    #: message string to decide anything.
    notices: tuple[Notice, ...] = ()

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
            "unmatched_terms": list(self.unmatched),
            # Before `results`, deliberately. A degradation buried under twenty
            # result objects has been reported, and read by nobody.
            "notices": [notice.as_dict() for notice in self.notices],
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
        self._closed = False
        #: Wildcard pattern -> `Expansion`, for this session.
        #:
        #: Somebody refining a query re-runs the same wildcard repeatedly, and
        #: the vocabulary cannot change underneath it without an index write -
        #: which bumps the generation and clears the result cache in any case.
        self._wildcard_cache: dict = {}

    @property
    def closed(self) -> bool:
        """True once `close()` has run. Ask before starting work.

        A search already in flight when the window closes will otherwise reach
        `self._pool.submit` on a shut-down executor and raise `RuntimeError:
        cannot schedule new futures after shutdown` - which the worker's
        boundary dutifully reported as ERR_UNEXPECTED, three times, telling the
        owner it was "a bug" and to send the log. Shutting down is not a bug.
        """
        return self._closed

    def close(self) -> None:
        """Stop taking work, and drop whatever is queued.

        **`cancel_futures=True` is why the window can actually exit.**

        `ThreadPoolExecutor` worker threads are non-daemon, and
        `concurrent.futures` installs an `atexit` hook that *joins every one of
        them* at interpreter shutdown. `shutdown(wait=False)` returns
        immediately - and then Python blocks on that join anyway, after Qt has
        closed the window and there is nothing left on screen to explain it.
        Reported as "when you close the gui it does not exit, it is stuck, I
        need to press ctrl c".

        A queued search is exactly the work worth abandoning: nobody is waiting
        for a result in a window that has closed. Running ones still finish -
        that cannot be helped without killing a thread mid-write - which is why
        the shutdown grace in `shell._drain_workers` exists on top of this.
        """
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)

    def warm_up(self) -> None:
        """Load the models now, off the first search's critical path."""
        try:
            self.embedder.warm_up()
        except AppErrorException as exc:
            _log.warning("embedding model not ready: {}", exc.error.message)
        if self.reranker is not None:
            self.reranker.warm_up()

    # -- the two tiers ------------------------------------------------------

    def interim(self, raw: str, *, limit: int = INTERIM_LIMIT, scope: str = "all") -> SearchResponse:
        """Keyword only, no model, no ANN. What runs while someone is typing.

        Never cached and never logged: it is not a search anybody made, it is a
        glance at a half-typed word, and recording it as intent would poison the
        very data Layer 10 depends on.
        """
        started = time.perf_counter()
        parsed = parse_query(raw).scoped(scope)
        if not parsed.has_text and not parsed.has_filters:
            return SearchResponse(parsed=parsed, interim=True)

        hits = keyword.search(self.store, parsed, limit=limit, prefix_last=True)
        results = [
            self._to_result(hit, rank, (0,), float(-hit.get("score", 0.0)))
            for rank, hit in enumerate(hits, start=1)
        ]
        return SearchResponse(
            results=results, parsed=parsed, interim=True,
            keyword_count=len(hits),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def search(
        self,
        raw: str,
        *,
        limit: int = FUSED_LIMIT,
        rerank: Optional[bool] = None,
        use_cache: bool = True,
        scope: str = "all",
    ) -> SearchResponse:
        """The full pipeline. What runs when someone stops typing or presses Enter."""
        # **First line, and it was buried three hundred lines down inside
        # `if use_cache and self.cache is not None:`.** No cache is ever
        # configured - `cache=` is passed at none of the four constructions - so
        # the guard that documents fixing a thrice-reported crash has never once
        # run. A search in flight when the window closes still reached
        # `self._pool.submit` on a shut-down executor.
        #
        # Nothing below this point is safe after `close()`: the executor is
        # gone, the stores are being shut, and the work is for a window that has
        # already disappeared. So the question is asked once, at the top, where
        # its answer cannot depend on an unrelated feature being switched on.
        if self._closed:
            raise AppErrorException(make_error("ERR_SHUTTING_DOWN", "search.engine"))

        started = time.perf_counter()
        timings: dict[str, float] = {}

        mark = time.perf_counter()
        parsed = parse_query(raw).scoped(scope)
        # **Wildcards are resolved here, not in the parser.** Parsing is pure and
        # cannot know what words a corpus holds; `*voice` therefore survives the
        # parse as itself and is turned into the terms the index actually
        # contains by a lookup over `chunks_vocab`. Paid only when a wildcard is
        # present - `expansions` is empty for every ordinary search, and the
        # expression built from it is byte-identical to what it was before this
        # existed.
        parsed, wildcards = self._expand_wildcards(parsed)
        timings["parse"] = (time.perf_counter() - mark) * 1000

        if not parsed.has_text and not parsed.has_filters:
            return SearchResponse(parsed=parsed, elapsed_ms=timings["parse"])

        want_rerank = self._wants_rerank(rerank)
        cache_key = self._cache_key(raw, parsed, want_rerank, limit)

        if use_cache and self.cache is not None:
            # Checked again, not only at entry: the window can close while the
            # wildcard expansion above is mid-query, and the next thing this
            # method does is submit to an executor that may no longer exist.
            if self._closed:
                raise AppErrorException(make_error("ERR_SHUTTING_DOWN", "search.engine"))
            cached = self._cache_get(cache_key)
            if cached is not None:
                cached.from_cache = True
                cached.elapsed_ms = (time.perf_counter() - started) * 1000
                return cached

        # Both retrievers at once: independent, so the slower one sets the floor
        # rather than the sum setting it.
        mark = time.perf_counter()
        allowed = keyword.file_ids_matching(self.store, parsed)
        # **Why the vector half came back empty, if it did.** It no longer
        # raises when the embedding model is broken - see `vector.search` - so
        # without this the failure that most deserves saying out loud would be
        # the one that looks exactly like a filter-only query.
        vector_problems: list[str] = []
        keyword_future = self._pool.submit(keyword.search, self.store, parsed)
        vector_future = self._pool.submit(
            vector.search, self.vectors, self.embedder, parsed,
            allowed_file_ids=allowed, problems=vector_problems,
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
            fused = self.reranker.rerank(parsed.embed_text or raw, fused, terms=parsed.terms)
            reranked = [hit["chunk_id"] for hit in fused] != before or any(
                "rerank_score" in hit for hit in fused
            )
            timings["rerank"] = (time.perf_counter() - mark) * 1000

        # **Sorted after reranking, deliberately, and it abandons the order.**
        #
        # *"Which is the latest"* was unanswerable by any mechanism this
        # application had: eleven filters that all narrow, and nothing that
        # orders. Re-sorting the final set is cheap, gives Interpret something
        # to emit for a phrase people use constantly, and is honest about the
        # trade - relevance order is discarded, because the person asked for
        # recency and cannot have both.
        #
        # After the reranker rather than instead of it: reranking still decides
        # *which* fifty results these are, and a date sort over the best fifty
        # is a far better answer than a date sort over an arbitrary fifty.
        if parsed.sort:
            fused.sort(key=lambda hit: int(hit.get("mtime_ns") or 0),
                       reverse=parsed.sort != "oldest")

        results = [
            self._to_result(hit, rank, tuple(hit.get("sources", ())), hit.get("rrf_score", 0.0))
            for rank, hit in enumerate(fused, start=1)
        ]

        # **Which words found nothing.** Terms are ORed, so a query whose one
        # distinctive word is absent quietly becomes a search for its most
        # common words - twenty confident, irrelevant results and nothing to
        # explain them. One indexed lookup per word, on the full tier only.
        # **A wildcard is not a word, so it is never "not in the index".**
        # `unmatched_terms` asks whether each term appears in the corpus, and
        # `*voice` never does - the words it matched do. Reporting the pattern
        # as missing beside a notice saying it matched two terms is the two
        # halves of the feature contradicting each other on screen.
        expanded_patterns = {pattern for pattern, _terms in parsed.expansions}
        unmatched = keyword.unmatched_terms(self.store, tuple(
            term for term in parsed.terms if term not in expanded_patterns))

        response = SearchResponse(
            results=results, parsed=parsed, reranked=bool(reranked),
            keyword_count=len(keyword_hits), vector_count=len(vector_hits),
            unmatched=unmatched,
            elapsed_ms=(time.perf_counter() - started) * 1000, timings=timings,
        )
        if unmatched:
            _log.info("no document contains: {}", ", ".join(unmatched))
        notices: list[Notice] = []
        # **First, because it explains the rest.** A capped or approximate
        # expansion changes what every number below it means, and the defect
        # this feature exists to remove is silence about exactly that.
        notices.extend(
            Notice(NOTICE_WILDCARD, found.message()) for found in wildcards)
        if parsed.sort:
            # **Said on screen, every time.** A list silently ordered by date
            # while somebody believes it is ordered by relevance is worse than
            # not having the feature: they read the top three, conclude the
            # search is poor, and never learn that they asked for this.
            notices.append(Notice(
                NOTICE_SORTED,
                f"Sorted by date, {parsed.sort} first — not by best match. "
                f"Remove /{parsed.sort} to rank by relevance again.",
            ))
        if unmatched:
            notices.append(Notice(
                NOTICE_UNMATCHED_TERMS,
                "Not in the index: " + ", ".join(unmatched)
                + ". Results match the remaining words only.",
            ))
        # **Only when the vector half was actually asked a question.**
        #
        # `vector.search()` returns `[]` for three reasons and two of them are
        # entirely normal: there was nothing to embed - a filter-only query like
        # `type:pdf` has no free text - or the filters excluded everything. The
        # warning fired on all three, and the owner's log holds **sixty
        # occurrences, every one a false alarm**: the store held 38,986 vectors
        # of 39,306 chunks, and the two lines that mark a real failure,
        # `query embedding failed` and `ANN search failed`, have never appeared
        # in any log.
        #
        # A warning that is wrong sixty times out of sixty trains everybody to
        # ignore it, and this one guards a genuine failure mode. So it now
        # requires the two conditions that make silence surprising: something
        # was embedded, and the filters left something to find.
        asked_the_vector_half = bool(parsed.embed_text.strip())
        if vector_problems:
            # **A broken half is said whether or not the other half found
            # anything.** The conditions below exist to stop a *guess* being
            # shouted sixty times in a row; this is not a guess, it is the
            # vector half reporting its own failure with the reason attached.
            # Gating it behind "but only if keyword found something" would hide
            # it in exactly the case that looks worst - no results at all.
            _log.warning("meaning-based search is degraded: {}",
                         "; ".join(vector_problems))
            notices.append(Notice(
                NOTICE_NO_VECTORS,
                "These are keyword matches only - meaning-based search is not "
                "working. " + " ".join(vector_problems),
            ))
        elif keyword_hits and not vector_hits and asked_the_vector_half:
            # Worth a line in the log every time. Meaning-based search returning
            # nothing while keyword search returns plenty is not a normal state -
            # it means the vector store is empty, the embedder failed, or a
            # filter excluded everything - and without this the only symptom is
            # results that feel worse than they should.
            _log.warning(
                "no vector hits for a query with {} keyword hits - "
                "meaning-based search may not be working. Check: app.cli stats",
                len(keyword_hits),
            )
            # **And say so where somebody will actually see it.** The log line
            # above has existed all along; it is visible to a person running
            # from a console and to nobody else. A degraded search that looks
            # identical to a working one is the worst failure this application
            # has, because it is the one nobody reports.
            notices.append(Notice(
                NOTICE_NO_VECTORS,
                "Meaning-based search returned nothing, so these are "
                "keyword matches only. Run `app.cli stats` to check the "
                "vector store, and `app.cli reembed` to rebuild it.",
            ))
        elif keyword_hits and not vector_hits:
            # The ordinary cases, at DEBUG. Recorded rather than dropped so the
            # log can still answer "why were there no vector hits" - it simply
            # no longer shouts about it.
            _log.debug(
                "no vector hits and nothing to embed - a filter-only query")
        if want_rerank and not reranked and self.reranker is not None:
            notices.append(Notice(
                NOTICE_RERANK_UNAVAILABLE,
                "Results were not reranked, so their order is weaker than "
                "usual. Search is otherwise unaffected.",
            ))
        response.notices = tuple(notices)

        if use_cache and self.cache is not None:
            self._cache_set(cache_key, response)

        response.search_id = self._log_search(raw, parsed, response, want_rerank)
        return response

    def _expand_wildcards(self, parsed: ParsedQuery) -> tuple[ParsedQuery, list]:
        r"""Turn `*voice` into the terms the index actually holds.

        Returns the query with `expansions` filled and the findings to report.
        **Never raises**, and returns the query untouched when no term carries a
        wildcard - which is almost every search, and the reason this costs
        nothing to have.

        The cache is per engine and therefore per session, keyed on the pattern:
        somebody refining a query re-runs the same wildcard repeatedly, and the
        vocabulary cannot change under it without an index write, which bumps
        the generation and clears the result cache anyway.
        """
        from app.search.wildcards import expand, needs_expansion

        patterns = [term for term in parsed.terms if needs_expansion(term)]
        if not patterns:
            return parsed, []

        found = []
        expansions: list[tuple[str, tuple[str, ...]]] = []
        for pattern in patterns:
            expansion = expand(self.store, pattern, cache=self._wildcard_cache)
            found.append(expansion)
            expansions.append((pattern, expansion.terms))
        return replace(parsed, expansions=tuple(expansions)), found

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
            if not getattr(self.store, "is_open", True):
                # The window is closing and a search is still in flight. Not an
                # error, and not worth a log line per keystroke.
                return "closed"
            generation = self.store.generation      # a property, not a method
        except Exception as exc:        # noqa: BLE001 - a cache key is not worth failing over
            # Loud, because the silent version of this shipped: `generation()`
            # raised TypeError, the except swallowed it, and every search keyed
            # on -1 - so the cache never invalidated and stale results would be
            # served forever. A cache that cannot tell it is stale is a lie, and
            # this fallback must announce itself rather than hide.
            _log.error(
                "index generation unreadable, so the search cache cannot be invalidated: {}", exc
            )
            generation = -1
        # The scope is part of the key. Without it "All" and "Mail" share an
        # entry for the same typed text, and whichever ran first answers for
        # both - the same class of bug as the generation being wrong, and just
        # as invisible.
        # **Which reranker produced it is part of what the answer is.**
        #
        # `rerank` was a bare on/off flag, so every result reranked by one
        # model was served afterwards as though a different model had produced
        # it. Changing `RERANK_MODEL` invalidated nothing: every query already
        # asked kept its old ordering until the index generation happened to
        # change. Swapping to a model measured 9.2x faster and finding search
        # unchanged is exactly what that looks like from the outside, and it
        # would have been blamed on the model rather than on the cache.
        #
        # Only when reranking is on: with it off no model touched the result,
        # and keying on one would split the cache for no reason.
        model = ""
        if rerank:
            model = str(getattr(self.reranker, "model_name", "") or "")

        return "|".join([
            "v3", str(generation), raw.strip().lower(),
            repr(parsed.ext), repr(parsed.after), repr(parsed.before),
            repr(parsed.paths), repr(parsed.senders), parsed.scope,
            "r" if rerank else "-", model, str(limit),
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
            # Normalised the way `upsert_file` stores it - bare, lowercase, no
            # leading dot. A dotted extension here would make `type:pdf` and the
            # result row's own label disagree about the same file.
            ext=str(hit.get("ext", "") or "").lower().lstrip("."),
            mtime_ns=int(hit.get("mtime_ns") or 0),
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
