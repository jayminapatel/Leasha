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
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger
from app.search import definitions, filename_match, folding, keyword, recency, relax, vector
from app.search.fusion import RRF_K, fuse_hits
from app.search.plain_notices import for_register
from app.search.policy import SEARCH, SearchPolicy, for_surface
from app.search.query import ParsedQuery, parse_query, with_terms
from app.search.rerank import Reranker

__all__ = [
    "SearchEngine", "SearchResult", "SearchResponse", "Notice",
    "NOTICE_NO_VECTORS", "NOTICE_NO_IMAGES", "NOTICE_UNMATCHED_TERMS",
    "NOTICE_RERANK_UNAVAILABLE", "NOTICE_WILDCARD", "NOTICE_SORTED",
    "NOTICE_SPELLING", "NOTICE_RELAXED", "NOTICE_EXACT",
]

#: Results returned after fusion, before reranking. From the spec's pipeline.
FUSED_LIMIT = 50

#: The interim tier's size. Deliberately small: it is a glance, not an answer.
INTERIM_LIMIT = 20

#: Seconds either retriever may take before the other's answer is served alone.
#:
#: **Forty times the whole budget, on purpose.** This is not a performance
#: setting - a search that takes twelve seconds is already broken and this will
#: not save it. It is a liveness setting: the pool has as many workers as
#: retrievers (see `SearchEngine._pool`), so a future that never returns takes
#: one of them for ever, the next search takes another, and eventually
#: everything waits behind all of them. One wedged LanceDB scan used to end
#: searching for the rest of the session.
#:
#: High enough that a genuinely slow first query on a cold index - the ONNX load
#: alone is seconds - is never cut off and reported as a failure.
RETRIEVER_TIMEOUT_S = 12.0

_log = logger.bind(component="search.engine")


#: Searches remembered per session. Fifty is generous for the thing this
#: actually serves - somebody refining one query, or flicking between scope
#: chips - and small enough that the memory is a rounding error beside one
#: ONNX model.
CACHE_ENTRIES = 50


class _LruCache:
    """The smallest cache that is honestly a cache. Not thread-safe by design.

    `SearchEngine` submits retrieval to a pool but calls `search()` from one
    thread at a time, and a lock here would cost more than the dictionary it
    protects. The worst a race could do is evict an entry twice.
    """

    __slots__ = ("_entries", "_limit")

    def __init__(self, limit: int = CACHE_ENTRIES) -> None:
        self._entries: "OrderedDict[str, Any]" = OrderedDict()
        self._limit = max(1, int(limit))

    def get(self, key: str) -> Any:
        """The entry for `key`, moved to most-recent, or None."""
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        return self._entries[key]

    def set(self, key: str, value: Any) -> None:
        """Store `value`, evicting the least recently used once past `limit`."""
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def _fold(values: Any) -> tuple:
    """Lowercase a tuple of strings for the cache key. See `_cache_key`."""
    return tuple(str(value).lower() for value in (values or ()))


def _result_chunk_id(hit: dict[str, Any]) -> int:
    """`SearchResult.chunk_id` for one fused row - `file_id` for an image hit.

    **Found while wiring the image lane in, not assumed.** `search_images`
    (`app/search/vector.py`) deliberately rewrites an image hit's fused key
    to the namespaced string `"img:<file_id>"`, precisely so it cannot
    collide with a real `chunks.id` inside `fuse_hits` - two independent
    SQLite sequences that both start at 1. `int("img:5")` raises, and every
    fused row used to be cast with a bare `int(hit.get("chunk_id", 0))`
    here, which would have crashed the very first search that fused in a
    photo. The namespacing has done its job by the time a row reaches this
    point - fusion is over - so `SearchResult.chunk_id` stays a plain int
    for every caller downstream by falling back to `file_id`, which is
    already the whole key for a table with no chunk concept (see
    `ImageVectorStore`'s docstring).
    """
    raw = hit.get("chunk_id", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(hit.get("file_id", 0) or 0)


def _wait(future: Any, half: str, problems: list[str]) -> list:
    """One retriever's results, or `[]` if it does not answer in time.

    Abandoned rather than cancelled: a future that has started cannot be
    cancelled, and killing a thread mid-read would risk the very stores this is
    protecting. It finishes into nothing and its worker comes back.

    The failure is recorded rather than swallowed - half a search that does not
    say it is half a search is the thing this codebase keeps being corrected
    for.
    """
    try:
        return future.result(timeout=RETRIEVER_TIMEOUT_S)
    except FutureTimeout:
        _log.warning("{} search did not answer within {}s; serving the other "
                     "half", half, RETRIEVER_TIMEOUT_S)
        problems.append(
            f"The {half} half of this search did not answer within "
            f"{RETRIEVER_TIMEOUT_S:.0f} seconds, so these results are from the "
            f"other half only. Try again - if it keeps happening, run "
            f"`app.cli doctor`.")
        return []


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
    #: Where inside the document this passage starts, when the extractor could
    #: say: `Q3!A14` for a spreadsheet row. Adoptions §6a.
    #:
    #: **`page` was never enough for a spreadsheet.** It holds the sheet index,
    #: so a hit in a forty-thousand-row workbook says "sheet 3" and stops. The
    #: row is what makes it an answer. Empty for every other kind of document,
    #: which is nearly all of them.
    label: str = ""
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
    #: A photograph's EXIF shot date, in the same nanoseconds-since-epoch
    #: units as `mtime_ns` - `0` for every file that is not a photograph, or
    #: a photograph the images pass has not yet read. Work order 0f §3a's
    #: third clause, "any date display use it": `mtime_ns` alone is the
    #: file's *copy* date, and a photograph taken in 2006 and copied to a new
    #: drive in 2019 is a 2006 document however recently the bytes moved.
    #:
    #: **A second field, not a rename or a silent substitution of `mtime_ns`
    #: at the SELECT sites**, for the same reason `files.taken_at_ns` is a
    #: column of its own rather than a rewrite of `mtime_ns`: whatever reads
    #: `mtime_ns` today - and it is read in several places, `folding.py`'s
    #: version-marker fold and `app/index/pipeline.py`'s change detection
    #: among them - still means "the file's real modification time" when it
    #: reads it, and must keep meaning that. Anything that wants "the date to
    #: show or sort by" reads `taken_at_ns or mtime_ns` explicitly, the same
    #: fallback `_date_clause` already applies in SQL.
    taken_at_ns: int = 0
    #: blake2b of the file's bytes, when the index has read them.
    #:
    #: **Carried so §2d can fold identical copies without a second query.**
    #: Both retrievers already joined `files`; this is one more column on a
    #: row that was being fetched anyway. Empty for anything not yet hashed,
    #: and an empty hash never folds - a shared blank is not a match.
    content_hash: str = ""
    #: Work order 0h §2a/§2b. A perceptual hash, for a photo whose file has
    #: been touched by the images pass - `""` for everything else, and for a
    #: photo not yet hashed. **A shared blank is not a match** - the same
    #: rule `content_hash` already carries, and `app/search/folding.py`'s
    #: `_burst_groups` relies on it exactly the same way.
    phash: str = ""
    #: The raw ANN distance to the query, when this hit came from a vector
    #: lane - `None` for a keyword-only hit. **Not the fused `score`**, which
    #: RRF deliberately discards in favour of rank; this is the number
    #: `folding._burst_groups` uses as an approximate CLIP-space tiebreak
    #: when a photo's pHash alone cannot say which near-duplicate cluster it
    #: belongs to (see that function's docstring for the honest limits of
    #: that approximation).
    distance: Optional[float] = None
    #: Work order 0h §2c. Set only on a `search_by_image` result: `"exact"`
    #: when the hit's `phash` is within `folding.PHASH_NEAR_THRESHOLD` of the
    #: query photo's own hash, `"similar"` when it is an image hit found by
    #: CLIP alone. `""` for every other kind of result - this is the
    #: distinction the work order asks reverse-image search to make ("this
    #: exact photo" vs "similar"); showing it is a UI decision and not this
    #: order's job, but the fact has to exist on the row for that UI to read.
    photo_match: str = ""
    #: How fresh this is, 1.0 for today decaying towards 0. Written by
    #: `recency.blend`, and **carried so the row can say why it is here**:
    #: an order somebody cannot interrogate is an order they have to take on
    #: trust, which is the thing this application keeps refusing to ask for.
    recency: float = 0.0
    #: True when this passage declares the symbol that was searched for.
    #: Written by `definitions.boost`, and carried for the same reason.
    declares: bool = False
    #: True when the file's own name carries one of the search terms.
    #: Written by `filename_match.blend`, and carried for the same reason -
    #: work order 0 §6's "Relevance" item. Not yet read by
    #: `app.ui.presenter.why_result`; wiring a fact into a sentence there is
    #: a UI-layer follow-up, the same shape `recency` and `declares` already
    #: went through, not a reason to withhold the fact itself.
    filename_match: bool = False
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
    #: Offline Media, order 202626270513 §3a/3b. `None` for an ordinary file
    #: - the overwhelming majority of rows - and the catalogued volume's id
    #: for one on a drive in a drawer. `path` for such a row is never a real
    #: filesystem path; it is the letter-free key `volume_synthetic_path`
    #: builds, and it must be resolved - `app.index.offline_media.
    #: resolve_file_path` - through the volume's *current* mount point
    #: before it is opened, exactly as 1b requires.
    volume_id: Optional[int] = None
    #: The file's path relative to its volume's root - what resolution
    #: needs alongside `volume_id`. `""` whenever `volume_id` is `None`.
    relative_path: str = ""

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
        """The JSON shape `app.cli search --json` and the MCP server print.

        Only what a reader outside the window needs: the ranking signals
        (`recency`, `declares`, `distance`) stay on the object itself.
        """
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
#: Work order 0h SS1c: the CLIP image lane broke and degraded. **A separate
#: code from `NOTICE_NO_VECTORS`, not a shared one** - the two lanes fail
#: independently (a broken CLIP text-tower model says nothing about the
#: FastEmbed text model, and vice versa), and the fix this file already
#: shipped once for `NOTICE_NO_VECTORS` - see the "sixty occurrences, every
#: one a false alarm" comment below - is precisely the bug reusing one
#: code for two lanes would reintroduce: a person told "meaning-based
#: search is not working" while it is in fact working fine and only the
#: picture lane is degraded.
NOTICE_NO_IMAGES = "NOTICE_NO_IMAGES"
NOTICE_UNMATCHED_TERMS = "NOTICE_UNMATCHED_TERMS"
NOTICE_RERANK_UNAVAILABLE = "NOTICE_RERANK_UNAVAILABLE"
#: What a wildcard turned into - how many terms, whether it was capped, and
#: whether stemming widened it. **The deliverable of the wildcard feature**, not
#: decoration: an expansion nobody can see is the silence it was built to fix.
NOTICE_WILDCARD = "NOTICE_WILDCARD"
#: The results were re-ordered by date, so relevance order was abandoned.
NOTICE_SORTED = "NOTICE_SORTED"
#: A word matched nothing and a real one was used, or offered.
#: **Never silent**: the whole licence for changing somebody's
#: query is that the change is stated where they will see it.
NOTICE_SPELLING = "NOTICE_SPELLING"
#: A narrowing instruction was dropped because keeping it found nothing.
#: **The label is the entire licence for the re-run**, exactly as with
#: spelling: results answering a question slightly different from the one
#: asked are only honest while the difference is on the page.
NOTICE_RELAXED = "NOTICE_RELAXED"
#: The query looked pasted, so its words were searched for in order.
#: **Named on the page, with the way back**, because a phrase search is a
#: narrowing and every narrowing this application does has to be visible.
NOTICE_EXACT = "NOTICE_EXACT"
#: 2026-10-04. A word in a tenth or more of the index was left out of the
#: search, because scoring every match of it cost seconds at scale
#: (`keyword._bounded`). **Said, because it changes the answer**: the person
#: typed that word and the results do not weigh it.
NOTICE_LEFT_OUT = "NOTICE_LEFT_OUT"


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


@dataclass(frozen=True)
class _Retrieved:
    """One pass of the retrieval pipeline. Internal, and never returned.

    Exists so §2b can run that pipeline a second time without a second copy
    of it - the five things the response needs, handed back together.
    """

    results: list
    keyword_hits: list
    vector_hits: list
    reranked: bool
    #: CLIP image hits, hydrated - `[]` when the lane is off
    #: (`image_vectors`/`clip_text_embedder` either `None`) or found nothing.
    #: Not distinguishable from here which of those it was; `image_problems`
    #: (see `_retrieve`) is what tells a real failure from an empty result.
    image_hits: list


@dataclass
class SearchResponse:
    """What one search returned, and how far to trust it.

    `results` is the ranked list. Everything else qualifies it: `notices`
    and the two retriever counts tell a degraded search from a thin corpus,
    `folds` says how to draw the list, and `parsed` is the query that
    actually ran - the corrected or relaxed one when `spelling` or `relaxed`
    is set, with `parsed.raw` still holding what was typed.
    """
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
    #: Work order 0h §1c's third lane. `0` both when it is off
    #: (`image_vectors`/`clip_text_embedder` not given to the engine) and
    #: when it ran and found nothing - same shape as `vector_count`, same
    #: reason: a lane that is silently absent looks identical to one that
    #: found no photos, and only `notices` (`NOTICE_NO_IMAGES`) tells a
    #: real failure apart from either.
    image_count: int = 0
    #: Query words that appear nowhere in the index.
    #:
    #: **Usually the entire explanation for a baffling result list.** Terms are
    #: ORed, so a query whose one distinctive word matches nothing silently
    #: becomes a search for its most common words - and returns twenty
    #: confident, irrelevant results with nothing to say why.
    unmatched: tuple[str, ...] = ()
    #: The `spelling.Suggestion` this search made or offered, or None.
    #: Carried on the response so the chip can be drawn without the
    #: view re-deriving anything.
    spelling: Any = None
    #: How to draw `results` as rows: one `folding.Fold` each, some carrying
    #: older versions or identical copies behind them.
    #:
    #: **`results` is never shortened.** A view that ignores this draws exactly
    #: what it drew before, and nothing is hidden from anything that reads the
    #: list - which is the only safe way to ship a feature whose failure mode
    #: is putting somebody's document behind a disclosure triangle.
    folds: tuple = ()
    #: The `relax.Relaxation` that produced these results, or None when the
    #: query ran as typed. **`parsed` is the relaxed query when this is set**,
    #: because everything downstream - highlighting, the unmatched-terms
    #: check, the usage log - must describe the search that actually ran.
    #: `parsed.raw` still holds what the person typed.
    relaxed: Any = None

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

    #: Filters the window read out of the typed sentence and applied before
    #: calling `search` ("mail from 2017" runs as `type:mail` and a 2017
    #: range). **Never filled here** - the engine does not read sentences, and
    #: must not (see `test_the_search_engine_cannot_reach_the_translator`). The
    #: search worker sets it so the chips travel with the results they
    #: describe. Opaque to this layer.
    applied: tuple = ()

    def __len__(self) -> int:
        return len(self.results)

    def as_dict(self) -> dict[str, Any]:
        """The headless report `app.cli search --json` prints; `SearchRun.as_dict` extends it."""
        return {
            "hits": len(self.results),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "from_cache": self.from_cache,
            "interim": self.interim,
            "reranked": self.reranked,
            "search_id": self.search_id,
            "timings_ms": {k: round(v, 1) for k, v in self.timings.items()},
            "unknown_operators": list(self.parsed.unknown_operators) if self.parsed else [],
            "date_problems": list(self.parsed.date_problems) if self.parsed else [],
            # The date range the query applied, whichever operator set it -
            # `date:2017` and `after:2017 before:2017` read the same here, so
            # a headless check sees the filter the window would have used.
            "dates": {
                "after": _iso(self.parsed.after) if self.parsed else None,
                "before": _iso(self.parsed.before) if self.parsed else None,
            },
            "unmatched_terms": list(self.unmatched),
            "spelling": (self.spelling.suggestion
                         if self.spelling is not None else None),
            # Before `results`, deliberately. A degradation buried under twenty
            # result objects has been reported, and read by nobody.
            "notices": [notice.as_dict() for notice in self.notices],
            "results": [result.as_dict() for result in self.results],
        }


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


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
        image_vectors: Any = None,
        clip_text_embedder: Any = None,
        image_embedder: Any = None,
        phash_computer: Any = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.store = store
        self.vectors = vectors
        self.embedder = embedder
        self.reranker = reranker
        #: Work order 0h SS1c: the third retrieval lane, CLIP image search.
        #: `image_vectors` is an `app.storage.vector_store.ImageVectorStore`;
        #: `clip_text_embedder` is a plain `app.index.embedder.Embedder`
        #: configured for the CLIP **text** tower (`Qdrant/clip-ViT-B-32-
        #: text`, 512-dim) - deliberately not `ClipImageEmbedder`, which is
        #: the vision tower `Pipeline` uses at index time, and deliberately
        #: not this engine's own `self.embedder`, which is the FastEmbed text
        #: model living in a different embedding space entirely. Named
        #: `clip_text_embedder` rather than a shorter `image_embedder` so the
        #: two cannot be confused at a call site - see `app/search/vector.py`
        #: `search_images` for the full reasoning.
        #:
        #: **Both optional, both `None` by default, H4 throughout.** When
        #: either is `None` the engine behaves exactly as it did before this
        #: lane existed - no third future submitted, no third hit list, no
        #: third notice code ever raised. Neither loads anything at
        #: construction: `Embedder`/`ImageVectorStore` are both already lazy
        #: on first use, and `warm_up()` below deliberately does not touch
        #: `clip_text_embedder` either - the window startup budget (work
        #: order 0r) has no room for a second eager ONNX load, so this one
        #: loads on the first search that actually reaches it instead.
        self.image_vectors = image_vectors
        self.clip_text_embedder = clip_text_embedder
        # Work order 0h §2c. `image_embedder` is the CLIP **vision** tower
        # (`app.index.clip_embedder.ClipImageEmbedder`, the same class
        # `Pipeline` uses at index time) - deliberately not
        # `clip_text_embedder` above, which embeds a typed *description* and
        # is useless for embedding a *photo* someone drops into the search
        # box. `phash_computer` (`app.index.phash.PhashComputer`) is what
        # lets `search_by_image` tell "this exact photo" from "similar" in
        # its results. Both `None` by default, H4 throughout: absent means
        # `search_by_image` returns an empty response rather than raising -
        # see that method. Neither is constructed by any real call site this
        # order touches (`app/main.py`, `app/cli.py` are out of this
        # session's scope, the same way `app/ui/shell.py` was out of 0h
        # §1c's) - flagged here and in the work order's own dated note
        # rather than silently left to be discovered.
        self.image_embedder = image_embedder
        self.phash_computer = phash_computer
        #: Work order 0r item 1c, second clause. `None` unless a caller with
        #: somewhere to show a message hands one in (`app/ui/shell.py`'s
        #: `MainWindow` wires its own status bar in after construction, since
        #: at the time this engine is built - `app/main.py`, before the
        #: window exists - there is nothing yet to wire). Takes one plain
        #: string; the percent-to-words translation happens in
        #: `_clip_download_progress` below, not here, so this stays a plain
        #: "somewhere to put a message" seam that any caller (a future CLI
        #: progress line, a test double) can satisfy without knowing
        #: anything about CLIP or downloads.
        #:
        #: **H4 throughout**: `None` means exactly what it meant before this
        #: existed - no callback, no attempt, image search unaffected either
        #: way.
        self.status_callback = status_callback
        # **A cache by default, at last.** `cache=` is passed at none of the
        # four constructions, so for a year every review has recorded "no warm
        # search" against machinery that was complete, correct and unreachable:
        # the key already carries the index generation, `_cache_get` already
        # returns a copy so callers cannot mutate a stored response, and both
        # halves already degrade to no-cache on any error.
        #
        # The choice was to build it or delete it, because a third year of
        # reviews saying the same thing is worse than either. Built - and small,
        # bounded and in-memory rather than `diskcache`: a repeated search is
        # repeated within a session, the whole value is skipping an ONNX
        # embedding and two retrievers, and a cache on disk would add a
        # dependency and a file to invalidate for a benefit nobody measured.
        #
        # `cache=False` switches it off; anything else is used as given, so a
        # test can still hand in its own.
        self.cache = _LruCache() if cache is None else (cache or None)
        self.rrf_k = rrf_k
        #: (keyword, vector). Equal today; Layer 10 is where these stop being a
        #: guess and start being measured against this corpus. **Still a pair,
        #: deliberately**, even with the image lane wired in below: work order
        #: 0h SS1c's literal answer is that the image lane's weight is 1.0, which
        #: is what passing no third element here already means to `fuse_hits`
        #: (`None` weights, or a weights list one entry too short for the
        #: image lane, both treat every list as equal - see `_retrieve`). A
        #: three-lane tuning surface is future work, not assumed here.
        self.weights = weights
        self.log_usage = log_usage
        #: One worker per retriever, so a third, optional lane never
        #: serialises behind the other two when it is active - see
        #: `RETRIEVER_TIMEOUT_S`'s docstring. An idle third thread when the
        #: image lane is off (`image_vectors`/`clip_text_embedder` both
        #: `None`) costs nothing: nothing is ever submitted to it.
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="search")
        self._closed = False
        #: Wildcard pattern -> `Expansion`, for this session.
        #:
        #: Somebody refining a query re-runs the same wildcard repeatedly, and
        #: the vocabulary cannot change underneath it without an index write -
        #: which bumps the generation and clears the result cache in any case.
        self._wildcard_cache: dict = {}
        #: The index generation the wildcard cache was filled against. See
        #: `_expand_wildcards`.
        self._wildcard_generation: int = -1

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
        """Load the models now, off the first search's critical path.

        **Deliberately does not warm `clip_text_embedder`.** Work order 0r's
        startup budget has no room for a second eager ONNX load beside
        `embedder`'s; the CLIP text tower stays lazy and pays its load cost
        on whichever search first reaches `vector.search_images` - the same
        contract `Embedder`/`ImageVectorStore` already promise on their own
        (see their docstrings: nothing touches the network or the model
        cache until first use).
        """
        try:
            self.embedder.warm_up()
        except AppErrorException as exc:
            _log.warning("embedding model not ready: {}", exc.error.message)
        if self.reranker is not None:
            self.reranker.warm_up()

    def _clip_download_progress(self, percent: float) -> None:
        """Turn a raw 0-100 into the notices-bar line for `status_callback`.

        Work order 0r item 1c, second clause. Handed to `vector.search_images`
        as `on_progress` only when `self.status_callback` is set (see
        `_retrieve`) - the same "only when someone is listening" gate
        `Embedder._start_progress_watcher_if_downloading` already applies to
        its own watcher thread, so a search with nobody wired up starts no
        extra thread for this at all.

        **Called from whatever thread the download-progress watcher happens
        to be running on** - a plain `threading.Thread`, per
        `app/index/embedder.py::_DownloadProgressWatcher`, never the pool
        worker and never the GUI thread. `self.status_callback` is what
        actually reaches the window (a `Signal.emit`, in
        `app/ui/shell.py`, chosen specifically because it is safe to call
        cross-thread); this method's own job is only the number-to-words
        step, kept out of `app/ui/shell.py` so that module does not need to
        know this is a percentage from a model download at all.

        **Guarded, H4.** A raised exception here would unwind into
        `_DownloadProgressWatcher._run` or `Embedder._ensure_encoder`'s
        `finally`, both of which already guard their own call to this - but
        guarding here too means a broken `status_callback` (a window
        mid-teardown, a test double that raises) can never be the reason an
        image search fails, not just the reason it fails silently.
        """
        if self.status_callback is None:
            return
        try:
            self.status_callback(
                f"Downloading the picture-search model - {percent:.0f}%")
        except Exception as exc:               # noqa: BLE001 - H4: never break image search
            _log.debug("status callback failed for CLIP download progress: {}", exc)

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
        policy: Optional[SearchPolicy] = None,
    ) -> SearchResponse:
        r"""The full pipeline. What runs when someone stops typing or presses Enter.

        `policy` is **what this surface is allowed to do on the person's
        behalf** - fix a spelling, drop a word, fold older versions. The engine
        reads it; no view branches on which tab it is, which is the whole
        reason it is a parameter rather than four `if` statements spread across
        four view files. `None` means the universal surface's contract, because
        that is what a caller who has not thought about it should get.
        """
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

        # **After the guard, deliberately.** `test_searching_a_closed_engine_
        # is_refused_without_a_cache` asserts the shutdown check is the first
        # thing this method executes, and it is right to: everything below is
        # unsafe once the executor is gone, and a line above it is a line that
        # runs on a window that has already disappeared.
        policy = policy or for_surface(SEARCH)

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
        # §4a. **A pasted error is not a bag of words.** Splitting it on
        # punctuation and ORing the pieces asks for every document containing
        # "object", or "has", or "no": measured on a four-document corpus
        # where two files hold the line, that returned all four. Searched as a
        # phrase it returns exactly the two.
        exact = self._as_pasted(raw, parsed)
        if exact is not None:
            parsed = exact
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

        # §2a. **Before retrieval, not after** - so the corrected word is
        # searched *for*, rather than searched for a second time. Asking the
        # index which words it has never seen is one indexed lookup each, and
        # it is a question this method already asks lower down; asked here it
        # buys the correction as well as the notice.
        parsed, spelling = self._correct_spelling(parsed, policy)

        vector_problems: list[str] = []
        #: Work order 0h §1c. Kept separate from `vector_problems` - see
        #: `NOTICE_NO_IMAGES`'s docstring for why sharing one list between
        #: two independently-failing lanes would be the bug this file
        #: already fixed once for `NOTICE_NO_VECTORS`, reintroduced.
        image_problems: list[str] = []
        retrieved = self._retrieve(parsed, raw, limit, want_rerank,
                                   timings, vector_problems, policy,
                                   image_problems)

        # §2b. **A second pass, and only where the alternative is an empty
        # page.** `relax.candidates` is empty for the ordinary
        # nothing-matched-anything query, so this costs one attribute read on
        # every search that found something and every search there is nothing
        # to relax towards.
        relaxed = None
        if policy.relax_on_empty and not retrieved.results:
            for candidate in relax.candidates(parsed):
                again = self._retrieve(candidate.query, raw, limit,
                                       want_rerank, timings, vector_problems,
                                       policy, image_problems)
                if again.results:
                    parsed, retrieved, relaxed = candidate.query, again, candidate
                    break

        keyword_hits, vector_hits = retrieved.keyword_hits, retrieved.vector_hits
        image_hits = retrieved.image_hits
        reranked = retrieved.reranked
        results = retrieved.results

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
            image_count=len(image_hits),
            unmatched=unmatched, spelling=spelling, relaxed=relaxed,
            # §2d. **Alongside `results`, never instead of it.** A view that
            # ignores this draws the same page it drew before.
            folds=tuple(folding.fold(results, enabled=policy.version_folding)),
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
        if exact is not None:
            # **First, and with the way out.** A phrase search is a
            # narrowing, and somebody who did not mean to paste needs to see
            # both that it happened and how to undo it.
            notices.append(Notice(
                NOTICE_EXACT,
                "That looked pasted, so these match your words in order. "
                "Put quotes round part of it to search for that instead, or "
                "delete the punctuation to search loosely."))
        if relaxed is not None:
            # **Above the spelling notice, because it is the larger claim.**
            # These results answer a question the person did not quite ask,
            # and that has to be the first thing they read - otherwise they
            # scan the list wondering why their filter appears not to work.
            notices.append(Notice(NOTICE_RELAXED, relaxed.sentence()))
        if spelling is not None:
            # **Said whichever way it went.** Changing what somebody typed is
            # only legal because the change is on screen; offering a chip is
            # only useful because they can see it.
            notices.append(Notice(
                NOTICE_SPELLING,
                spelling.sentence() if policy.typo_correction == "auto"
                else spelling.question(),
            ))
        if unmatched:
            notices.append(Notice(
                NOTICE_UNMATCHED_TERMS,
                "Not in the index: " + ", ".join(unmatched)
                + ". Results match the remaining words only.",
            ))
        left_out = keyword.left_out(self.store, parsed)
        if left_out:
            notices.append(Notice(
                NOTICE_LEFT_OUT,
                "Left out as too common to narrow the search: "
                + ", ".join(left_out) + ". Put it in quotes to require it.",
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
            #
            # Work order 0b §6d: "the existing NOTICE_NO_VECTORS wording
            # extends to '...still embedding, N% done'." A corpus that is
            # genuinely still catching up (the M6 repair drains it in the
            # background, see `Pipeline._drain_unembedded`) is a different
            # story from one whose vector store is actually broken, and the
            # two want different next actions - "wait" versus "run reembed".
            # `vector_coverage` is the same number `app.cli stats` already
            # reports; this just says it here too, so nobody has to leave
            # the search box to find out which story they are in.
            message = (
                "Meaning-based search returned nothing, so these are "
                "keyword matches only. Run `app.cli stats` to check the "
                "vector store, and `app.cli reembed` to rebuild it.")
            try:
                coverage = self.store.vector_coverage(self.vectors.count())
            except Exception:                     # noqa: BLE001 - a notice, not the search
                coverage = None
            # **Genuine progress only, not a blank store.** `coverage == 0`
            # says nothing was ever embedded, which is the "check the store,
            # then reembed" story the message above already tells; "still
            # embedding, N% done" is a different, more reassuring story that
            # is only true once there is a real number to report.
            if (coverage is not None and not coverage["vectors_ready"]
                    and coverage["coverage"] > 0):
                percent = round(coverage["coverage"] * 100)
                message = (
                    f"Meaning-based search returned nothing - still "
                    f"embedding, {percent}% done. It will cover more of "
                    f"the index as that finishes; `app.cli stats` shows "
                    f"the exact numbers.")
            notices.append(Notice(NOTICE_NO_VECTORS, message))
        elif keyword_hits and not vector_hits:
            # The ordinary cases, at DEBUG. Recorded rather than dropped so the
            # log can still answer "why were there no vector hits" - it simply
            # no longer shouts about it.
            _log.debug(
                "no vector hits and nothing to embed - a filter-only query")
        # Work order 0h §1c. **Simpler than the block above, deliberately.**
        # `image_problems` is populated by the exact same `vector.search`
        # this engine already trusts for the text-vector half (`search_images`
        # calls it unmodified - see its docstring) - so it is *already* only
        # ever non-empty on a genuine embed or ANN failure, never on "nothing
        # to embed" or "filters excluded everything", both of which return
        # `[]` upstream before `problems` is ever touched. The extra
        # "but only if X and Y and Z" gating above exists because
        # `NOTICE_NO_VECTORS` used to fire on those ordinary states too - a
        # bug this file already paid to find and fix once (the "sixty
        # occurrences, every one a false alarm" comment above). There is no
        # equivalent bug to guard against here, so there is no equivalent
        # gate: an empty `image_hits` with nothing in `image_problems` is the
        # ordinary "no photo matched" case and says nothing at all.
        if image_problems:
            _log.warning("the picture lane is degraded: {}",
                         "; ".join(image_problems))
            notices.append(Notice(
                NOTICE_NO_IMAGES,
                "Photo search is not working right now, so these results "
                "are text and keyword matches only. " + " ".join(image_problems),
            ))
        if want_rerank and not reranked and self.reranker is not None:
            notices.append(Notice(
                NOTICE_RERANK_UNAVAILABLE,
                "Results were not reranked, so their order is weaker than "
                "usual. Search is otherwise unaffected.",
            ))
        # §2c. **The same facts, in the register this surface asked for.**
        # The codes are untouched - they are what the UI branches on, and a
        # notice that changed its identity to change its wording would be one
        # the window stopped recognising.
        response.notices = for_register(tuple(notices), policy.notice_register)

        if use_cache and self.cache is not None:
            self._cache_set(cache_key, response)

        response.search_id = self._log_search(raw, parsed, response, want_rerank)
        return response

    def similar_to(self, chunk_id: int, *, limit: int = 20,
                   same_file: bool = False) -> SearchResponse:
        r"""Passages that read like this one. **The vector half, made visible.**

        §5a. Everything this needs already existed: the passage was embedded
        at index time, `VectorStore.search` takes a raw vector, and
        `vector.hydrate` turns rows into results. What was missing was a way
        to *ask* - so semantic search has been in the product since Layer 4
        and has never once been something a person could point at.

        **No query, so no keyword half and no fusion.** There is no text to
        match; the question is "what else is like this", and the answer is the
        neighbours of one point. Ranked by distance, which is the only signal
        there is.

        The source chunk is always dropped - it is trivially its own nearest
        neighbour - and by default so is the rest of its file, because "more
        like this" answering with the next paragraph of the same document is
        a correct answer to a question nobody asked.
        """
        if self._closed:
            raise AppErrorException(make_error("ERR_SHUTTING_DOWN", "search.engine"))

        started = time.perf_counter()
        empty = SearchResponse(parsed=parse_query(""))
        reader = getattr(self.vectors, "vector_for", None)
        if reader is None:
            return empty
        try:
            point = reader(int(chunk_id))
        except Exception as exc:                   # noqa: BLE001 - a lookup
            _log.debug("no vector to look near: {}", exc)
            return empty
        if not point:
            # **Not an error.** A chunk with no vector is the ordinary state
            # of a run that indexed text and has not embedded it yet, and the
            # honest answer is an empty list rather than a failure.
            return empty

        # **`get_chunk`, and it has to exist.** The first version called a
        # `chunk_by_id` that this store has never had, inside a `try` - so the
        # lookup failed silently on every call and the same-file exclusion
        # below quietly did nothing. A guard that turns a typo into a disabled
        # feature is the defect this codebase keeps finding.
        source_file = 0
        record = self.store.get_chunk(int(chunk_id))
        if record is not None:
            source_file = int(getattr(record, "file_id", 0) or 0)

        # Over-fetch, because the source's own siblings are about to be
        # dropped and a limit applied before that would return short.
        raw = self.vectors.search(point, k=max(limit * 4, limit + 8))
        kept = [hit for hit in raw
                if int(hit.get("chunk_id") or 0) != int(chunk_id)
                and (same_file or int(hit.get("file_id") or 0) != source_file)]
        hydrated = vector.hydrate(self.store, kept[:limit])
        results = [
            self._to_result(hit, rank, ("vector",), 1.0 / (1.0 + rank))
            for rank, hit in enumerate(hydrated, start=1)
        ]
        return SearchResponse(
            results=results, parsed=parse_query(""), vector_count=len(results),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def find_similar_images(self, file_id: int, *, limit: int = 20) -> SearchResponse:
        r"""Photos that look like this one. Work order 0h §2d.

        **`similar_to`'s image-table twin, not a modification of it.**
        `similar_to` is built around `chunks.id` and `self.store.get_chunk` -
        neither exists for a photo, which has no chunk-splitting concept at
        all (`ImageVectorStore`'s own docstring). Rather than growing
        `similar_to` a branch for a shape it was not written for, this is a
        parallel entry point, the same choice work order 0h has made at
        every layer of the image lane so far: `ClipImageEmbedder` beside
        `Embedder`, `ImageVectorStore` beside `VectorStore`, `search_images`
        beside `search`. `chunk_id == file_id` in the image table (see that
        class's docstring), so there is no separate "find the source chunk's
        file" step `similar_to` needs either - the id handed in already is
        the file.

        This is the **backend** half of "the existing right-click action
        extended to photo results via the image table" - the right-click
        menu item itself is UI and is explicitly not this order's job. A UI
        action on an image result (`SearchResult.chunk_id` shaped
        `"img:<file_id>"` per work order 0h §1c's own namespacing - see
        `_result_chunk_id`) calls this with the bare integer `file_id`, the
        same way it would call `similar_to` with a bare integer `chunk_id`
        for a text result.

        H4 throughout, same shape as `similar_to`: no `image_vectors`
        configured, no stored vector for this file, or a lookup that raises
        all return an empty `SearchResponse` rather than propagating a
        failure - "more like this" finding nothing is not an error state for
        either lane.
        """
        if self._closed:
            raise AppErrorException(make_error("ERR_SHUTTING_DOWN", "search.engine"))

        started = time.perf_counter()
        empty = SearchResponse(parsed=parse_query(""))
        if self.image_vectors is None:
            return empty

        try:
            point = self.image_vectors.vector_for(int(file_id))
        except Exception as exc:                   # noqa: BLE001 - a lookup
            _log.debug("no image vector to look near: {}", exc)
            return empty
        if not point:
            # Not an error - a photo not yet CLIP-embedded, or one indexed
            # before the image lane existed. The honest answer is an empty
            # list, exactly as `similar_to` treats the equivalent state.
            return empty

        raw = self.image_vectors.search(point, k=max(limit * 4, limit + 8))
        kept = [hit for hit in raw
                if int(hit.get("file_id") or 0) != int(file_id)]
        for hit in kept:
            hit["chunk_id"] = f"img:{hit['file_id']}"
        hydrated = vector.hydrate_images(self.store, kept[:limit])
        results = [
            self._to_result(hit, rank, ("image",), 1.0 / (1.0 + rank))
            for rank, hit in enumerate(hydrated, start=1)
        ]
        return SearchResponse(
            results=results, parsed=parse_query(""), image_count=len(results),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def search_by_image(self, image_path: Any, *, limit: int = 20) -> SearchResponse:
        r"""Reverse image search. Work order 0h §2c.

        Drop or paste a photo into the search box and find it - and its
        relatives - in the index. The **backend** half only: the drag-and-
        drop or paste trigger on the search box is UI and is explicitly not
        this order's job. `image_path` is a path to the query photo,
        wherever the UI trigger saved or received it from.

        **Distinguishes "this exact photo" from "similar", per the work
        order's own acceptance sentence** ("a degraded-WhatsApp-copy ->
        'full-res original is on <source>' case is the acceptance demo").
        `SearchResult.photo_match` is `"exact"` when a hit's stored pHash is
        within `folding.PHASH_NEAR_THRESHOLD` of the query photo's own hash
        - the recompressed-copy case - and `"similar"` for every other
        image hit, found by CLIP alone. Left `""` (falls back to
        `"similar"`) when `phash_computer` is not configured, since there is
        then nothing to compare against; H4 degrades the *distinction*, not
        the search itself.

        **H4 for the search itself mirrors `NOTICE_NO_IMAGES`, deliberately
        reused rather than a new code.** This is the same CLIP vision-tower
        lane failing, not a genuinely different failure mode - the work
        order's own instruction is to reuse the existing notice unless the
        failure is genuinely different, and a broken model or a broken
        vector store means the same thing whether the query was typed or
        dropped in as a photo.
        """
        if self._closed:
            raise AppErrorException(make_error("ERR_SHUTTING_DOWN", "search.engine"))

        started = time.perf_counter()
        empty_parsed = parse_query("")
        if self.image_vectors is None or self.image_embedder is None:
            return SearchResponse(parsed=empty_parsed)

        problems: list[str] = []
        raw = vector.search_by_image(
            self.image_vectors, self.image_embedder, image_path,
            limit=max(limit * 2, limit + 8), problems=problems,
        )
        if problems:
            _log.warning("reverse-image search is degraded: {}",
                         "; ".join(problems))
            notices = (Notice(
                NOTICE_NO_IMAGES,
                "Photo search is not working right now, so this photo could "
                "not be searched for. " + " ".join(problems),
            ),)
            return SearchResponse(
                parsed=empty_parsed, notices=notices,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )

        hydrated = vector.hydrate_images(self.store, raw)

        query_phash: Optional[str] = None
        if self.phash_computer is not None:
            try:
                query_phash = self.phash_computer.compute(image_path)
            except Exception as exc:                # noqa: BLE001 - the label is a bonus
                _log.debug("no pHash for the reverse-image query itself: {}", exc)

        for hit in hydrated:
            hit_phash = str(hit.get("phash") or "")
            if query_phash and hit_phash and folding.phash_distance(
                query_phash, hit_phash) <= folding.PHASH_NEAR_THRESHOLD:
                hit["photo_match"] = "exact"
            else:
                hit["photo_match"] = "similar"

        results = [
            self._to_result(hit, rank, ("image",), 1.0 / (1.0 + rank))
            for rank, hit in enumerate(hydrated[:limit], start=1)
        ]
        return SearchResponse(
            results=results, parsed=empty_parsed, image_count=len(results),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def _generation_now(self) -> int:
        """The store's write generation, or -1 if it cannot be read.

        Never raises: this only decides whether to drop a cache, and a search
        that fails because a cache-invalidation probe threw would be an absurd
        trade.
        """
        try:
            return int(self.store.generation)
        except Exception:                        # noqa: BLE001
            return -1

    def _retrieve(self, parsed: ParsedQuery, raw: str, limit: int,
                  want_rerank: bool, timings: dict, vector_problems: list,
                  policy: Optional[SearchPolicy] = None,
                  image_problems: Optional[list] = None) -> "_Retrieved":
        r"""Both retrievers, fused, reranked, sorted — for one query.

        **A method rather than a block because §2b runs it twice.** Relaxation
        is "try the same pipeline with one instruction removed", and a second
        copy of forty lines would be two pipelines that drift apart: the day
        somebody changes the fusion weights, only the first result set would
        get them, and the difference would show up as results that reorder
        themselves when a phrase is dropped.

        `timings` and `vector_problems` are handed in and written through, so
        a relaxed second pass reports the total spent rather than only its own
        share - the person waited for both. `image_problems` is the same
        pattern for the third lane, kept separate from `vector_problems` -
        see `NOTICE_NO_IMAGES`'s docstring for why the two must not share one
        list.
        """
        mark = time.perf_counter()
        # **The eligible files, without listing them when there are too many.**
        # This used to be `file_ids_matching`, which materialised every
        # matching id before either retriever started: measured at 383ms and
        # 46MB for `type:pdf` over 500,000 files, against a 300ms budget for
        # the whole search. `Eligibility` stops at ELIGIBLE_CAP and answers the
        # only question anything downstream actually asks.
        allowed = keyword.Eligibility(self.store, parsed)
        keyword_future = self._pool.submit(keyword.search, self.store, parsed)
        vector_future = self._pool.submit(
            vector.search, self.vectors, self.embedder, parsed,
            allowed_file_ids=allowed, problems=vector_problems,
        )
        # Work order 0h §1c: the CLIP image lane, submitted alongside the
        # other two rather than after them - `self._pool` carries a worker
        # per retriever precisely so this never queues behind them. `None`
        # unless both `clip_text_embedder` and `image_vectors` were handed to
        # the constructor (H4: absent means off, exactly as it always did
        # before this lane existed).
        image_future = None
        if self.clip_text_embedder is not None and self.image_vectors is not None:
            image_future = self._pool.submit(
                vector.search_images, self.image_vectors, self.clip_text_embedder,
                parsed, allowed_file_ids=allowed, problems=image_problems,
                # Work order 0r item 1c, second clause: only when there is
                # somewhere to report to - see `_clip_download_progress` and
                # `Embedder._start_progress_watcher_if_downloading`, both of
                # which gate the same way for the same reason.
                on_progress=(self._clip_download_progress
                             if self.status_callback is not None else None),
            )
        # **Bounded, because the pool has one worker per retriever and no
        # queue deep enough to hide a wedge.** A hung LanceDB scan or a
        # wedged SQLite read used to block `result()` for ever: that worker
        # never returns, the next search takes another, and eventually
        # everything waits behind all of them. One stuck query froze
        # searching for the rest of the session.
        #
        # A timed-out future is abandoned rather than cancelled - a running
        # future cannot be cancelled, and killing a thread mid-read is worse
        # than leaking one - so it finishes into nothing and the worker comes
        # back. What matters is that the person gets the halves that answered.
        keyword_hits = _wait(keyword_future, "keyword", vector_problems)
        raw_vector_hits = _wait(vector_future, "meaning-based", vector_problems)
        raw_image_hits = (
            _wait(image_future, "picture", image_problems)
            if image_future is not None else []
        )
        timings["retrieve"] = timings.get("retrieve", 0.0) + (
            time.perf_counter() - mark) * 1000

        mark = time.perf_counter()
        vector_hits = vector.hydrate(self.store, raw_vector_hits)
        image_hits = (
            vector.hydrate_images(self.store, raw_image_hits)
            if raw_image_hits else []
        )
        timings["hydrate"] = timings.get("hydrate", 0.0) + (
            time.perf_counter() - mark) * 1000

        mark = time.perf_counter()
        # **`hit_lists` and `weights` built as parallel lists, so a future
        # tuned `weights=` cannot throw.** `self.weights` is `None` at every
        # construction site today (work order 0h §1c's literal answer: the
        # image lane's weight is 1.0, which is exactly what an absent third
        # entry already means to `fuse_hits` - see `self.weights`'s
        # docstring). But `rrf()` raises `ValueError` on a length mismatch
        # between `weights` and the rankings it is given, so appending the
        # image hit list to `hit_lists` while leaving an *explicit* two-entry
        # `weights` untouched would be exactly that mismatch, waiting for the
        # day somebody sets `weights=(2.0, 1.0)` to tune keyword against
        # vector and the image lane happens to be on. Appending 1.0 here only
        # when `weights` is not `None` keeps the two lists in step in both
        # states: `None` stays `None` (fuse_hits treats every list as equal
        # already, so nothing needs adding), and an explicit pair grows to a
        # triple rather than becoming one entry too short.
        hit_lists = [keyword_hits, vector_hits]
        weights = list(self.weights) if self.weights else None
        if image_future is not None:
            hit_lists.append(image_hits)
            if weights is not None:
                weights.append(1.0)
        fused = fuse_hits(
            hit_lists, id_key="chunk_id", k=self.rrf_k,
            weights=weights, limit=limit,
        )
        timings["fuse"] = timings.get("fuse", 0.0) + (
            time.perf_counter() - mark) * 1000

        reranked = False
        if want_rerank and fused:
            mark = time.perf_counter()
            before = [hit["chunk_id"] for hit in fused]
            fused = self.reranker.rerank(parsed.embed_text or raw, fused, terms=parsed.terms)
            reranked = [hit["chunk_id"] for hit in fused] != before or any(
                "rerank_score" in hit for hit in fused
            )
            timings["rerank"] = timings.get("rerank", 0.0) + (
                time.perf_counter() - mark) * 1000

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
        #
        # **`taken_at_ns` first, work order 0f §3a's third clause.** This is
        # a plain Python sort over the already-fused, already-limited hit
        # list - at most `FUSED_LIMIT` items - so there is no index to lose
        # and no reason to keep the two-branch SQL discipline `keyword.py`'s
        # `_filter_only` needs; a photo's shot date simply wins over its copy
        # date here the same way `SearchResult.taken_at_ns` documents.
        #
        # **Filename match runs first, and persists its boosted score - work
        # order 0 §6's "Relevance" item.** `recency.blend` and
        # `definitions.boost` both discard whatever ran before them: they
        # read `rrf_score` fresh and never write it back, so two independent
        # reorders run in sequence do not blend, the later one simply wins
        # (see `definitions.boost`'s own note below, which names the exact
        # bug this caused once already). A document whose name already
        # carries the query is not competing with recency, it is the same
        # kind of "which of these equally good matches wins" nudge - so it
        # writes its result back into `rrf_score` and lets whatever runs next
        # compose on top of it rather than overwrite it. Skipped under an
        # explicit `/newest`/`/oldest` for the same "relevance has already
        # been abandoned" reason `recency_blend` is skipped below.
        if not parsed.sort:
            fused = filename_match.blend(fused, parsed.terms)

        if parsed.sort:
            fused.sort(
                key=lambda hit: int(hit.get("taken_at_ns") or hit.get("mtime_ns") or 0),
                reverse=parsed.sort != "oldest")
        elif policy is not None and policy.recency_blend:
            # §2d. **Only when relevance is still in charge.** `/newest` is a
            # date sort that abandons relevance and says so; nudging within an
            # order that is already by date would be arithmetic with no effect
            # and a second rule to reason about.
            fused = recency.blend(fused)

        # §4c. **Last, because it is the strongest claim and the ones above it
        # re-sort from the untouched score.**
        #
        # Where is this defined beats where is it used, and BM25 cannot see
        # the difference: a declaration appears once and every caller repeats
        # the name, so the file that answers the question ranked last, behind
        # four that merely call it and a Markdown file that talked about it.
        #
        # Placed above `recency.blend` first, and it did nothing at all -
        # `blend` re-sorts from `rrf_score`, which this deliberately does not
        # write to, so the reorder was computed and then thrown away. Two
        # rankers in sequence and the second silently discards the first: the
        # order they run in is the whole behaviour.
        #
        # Fires only on a single symbol-shaped word, so a sentence never
        # reaches it - and in prose no declaration pattern can match, which
        # makes it a no-op rather than a wrong answer.
        #
        # **Not under an explicit `/newest` or `/oldest`.** `boost` re-sorts
        # the whole list by `rrf_score`, so run after the date sort above it
        # put a single symbol-shaped word back into relevance order with no
        # notice (`SearchEngine /oldest`). An explicit date sort abandons
        # relevance on purpose and must win.
        symbol = None if parsed.sort else definitions.looks_like_symbol(parsed.terms)
        if symbol:
            fused = definitions.boost(fused, symbol)

        results = [
            self._to_result(hit, rank, tuple(hit.get("sources", ())), hit.get("rrf_score", 0.0))
            for rank, hit in enumerate(fused, start=1)
        ]
        return _Retrieved(results=results, keyword_hits=keyword_hits,
                          vector_hits=vector_hits, reranked=bool(reranked),
                          image_hits=image_hits)

    def _as_pasted(self, raw: str, parsed: ParsedQuery) -> Optional[ParsedQuery]:
        r"""The query as one phrase, when it looks pasted. **Never raises.**

        Returns None for the ordinary case, which is almost every search.

        **Order, not punctuation.** §4a asks for verbatim substring matching
        over "the existing trigram index" - which does not exist for content:
        `files_fts` is trigram over file *names*, and `chunks_fts` is
        `porter unicode61`, token-based, and cannot answer a substring query.
        Real verbatim matching would need a second FTS table with a trigram
        tokeniser over every chunk: a schema change, a full re-index, and
        roughly the same storage again. Preserving order needs none of that
        and is the half that finds the line.

        Left alone when the person already used quotes or a filter - they said
        what they wanted, and guessing over the top of that is the behaviour
        this order exists to remove.
        """
        try:
            from app.search.pasted import as_phrase, looks_pasted

            if parsed.phrases or parsed.has_filters or parsed.explicit_and:
                return None
            if not looks_pasted(raw):
                return None
            phrase = as_phrase(raw)
            if len(phrase.split()) < 2:
                return None
            return replace(parsed, phrases=(phrase,), terms=(), or_groups=())
        except Exception as exc:                   # noqa: BLE001 - a helper
            _log.debug("not treating this query as pasted: {}", exc)
            return None

    def _correct_spelling(self, parsed: ParsedQuery,
                          policy: SearchPolicy) -> tuple[ParsedQuery, Any]:
        r"""A word the index has never seen, and the word it probably was.

        Returns the query to run and the `Suggestion`, or the query unchanged
        and `None`. **Never raises**: this happens on a keystroke, and a
        vocabulary that cannot be read is a search without a suggestion rather
        than a search that fails.

        `auto` puts the correction into the query and the header says which
        word was used. `suggest` leaves the query exactly as typed and offers
        a chip. `off` does neither, which is what the Code tab asks for -
        `recieve_handler` may be precisely what is in the codebase, and
        "helpfully" searching for something else hides it.

        **Only ever fires on a word that matched nothing at all.** A word the
        corpus contains is never second-guessed however unusual it looks, so
        the worst case here is a query that was going to return nothing
        returning something instead.
        """
        wanted = str(getattr(policy, "typo_correction", "off") or "off")
        if wanted == "off" or not parsed.terms:
            return parsed, None

        try:
            from app.search.spelling import from_store

            expanded = {pattern for pattern, _terms in parsed.expansions}
            words = tuple(term for term in parsed.terms if term not in expanded)
            if not words:
                return parsed, None

            missing = keyword.unmatched_terms(self.store, words)
            # **Exactly one unknown word, or nothing happens.** One among
            # several is somebody who mistyped; two or more is somebody
            # searching a corpus that has nothing to do with what they asked,
            # and correcting each in turn would manufacture a query nobody
            # typed - and still return nothing, because the words that stayed
            # missing are ORed in with the corrected one.
            if len(missing) != 1:
                return parsed, None

            found = from_store(self.store, missing[0])
            if found is None:
                return parsed, None
            if wanted != "auto":
                return parsed, found                  # a chip, nothing changed

            corrected = tuple(found.suggestion if term == found.typed else term
                              for term in parsed.terms)
            # **`with_terms`, not `replace`.** The expression is built from
            # `or_groups` when there is one, so replacing `terms` alone left
            # the query that actually ran as the misspelled original - the
            # notice said the word had been corrected and the search had not
            # been. See `query.with_terms`.
            return with_terms(parsed, corrected), found
        except Exception as exc:                      # noqa: BLE001 - a helper
            _log.debug("no spelling suggestion for this query: {}", exc)
            return parsed, None

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
        from app.search.numbers import expansions_for
        from app.search.wildcards import expand, needs_expansion

        # §5c. **The same mechanism, for a different reason.** A quantity is
        # written four ways and indexed four ways - `40000`, `40 000`, `40k` -
        # and measured against a corpus naming one amount five times, no query
        # found more than two of them. Rendered as a parenthesised OR in the
        # position the number had, which is exactly what an expanded wildcard
        # already does, so the FTS builder needs no new rule.
        #
        # Costs nothing when no number was typed, and needs no re-index: the
        # documents are already tokenised, and what changes is the question.
        numeric = expansions_for(parsed.terms)

        patterns = [term for term in parsed.terms if needs_expansion(term)]
        if not patterns and not numeric:
            return parsed, []

        # **Dropped when the index changes, which the comment above claimed
        # already happened and did not.** The expansion is a snapshot of the
        # vocabulary: `*voice` resolves to the words the corpus held at the
        # moment it was first asked. An index run adds words, so the invariant
        # "the vocabulary cannot change underneath it" is simply false for any
        # session that spans a run - and the window is exactly such a session,
        # because it indexes without restarting.
        #
        # Clearing on a generation change rather than keying every entry by it
        # keeps the cache small: the old entries can never be wanted again.
        generation = self._generation_now()
        if generation != self._wildcard_generation:
            self._wildcard_cache.clear()
            self._wildcard_generation = generation

        found = []
        expansions: list[tuple[str, tuple[str, ...]]] = []
        for pattern in patterns:
            expansion = expand(self.store, pattern, cache=self._wildcard_cache)
            found.append(expansion)
            expansions.append((pattern, expansion.terms))
        # After the wildcards, so a term that is somehow both keeps the
        # vocabulary answer - which is the one that came from the index.
        seen = {term for term, _alternatives in expansions}
        expansions.extend((term, alternatives) for term, alternatives in numeric
                          if term not in seen)
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

        # **Not `raw.lower()`, and the case is not cosmetic.** `AND`, `OR` and
        # `NOT` are operators only in capitals - that is precisely how they are
        # told apart from the English words - so `pump AND valve` and
        # `pump and valve` are two different searches that folded to one key.
        # Whichever ran first answered for both.
        #
        # **The parse identifies the search; the raw text is not in the key at
        # all.** Two strings that parse identically have identical answers by
        # construction - `Quarterly Report` and `  quarterly report ` are the
        # same query, and splitting the cache on typing is pure waste. Two that
        # parse differently, `pump AND valve` among them, now differ here
        # because `explicit_and` and the term tuples differ, which is exactly
        # the distinction lowercasing the raw string destroyed.
        # Text is folded, structure is not. FTS5 and every `LIKE` here are
        # case-insensitive, so `Quarterly` and `quarterly` retrieve the same
        # rows and must share an entry - while `explicit_and`, the shape of
        # `or_groups` and the exclusion lists carry the operators, which are
        # case-sensitive by design and now genuinely distinguish two searches.
        #
        # v5 (2026-10-08, found in review): `shows`, `place`, `who`, `volumes`,
        # `only`, `statuses` and their six negated twins were never in the key,
        # so `beach shows:dog` and `beach shows:cat` shared one entry and the
        # second search in a session was served the first one's results. Every
        # filter field of `ParsedQuery` is now listed; `test_cache_key` derives
        # the field list from the dataclass so a new filter cannot be left out
        # again.
        return "|".join([
            "v5", str(generation),
            repr(_fold(parsed.terms)), repr(_fold(parsed.phrases)),
            repr(_fold(parsed.excluded)),
            repr(tuple(_fold(group) for group in parsed.or_groups)),
            repr(parsed.explicit_and),
            repr(_fold(parsed.ext)), repr(parsed.after), repr(parsed.before),
            repr(_fold(parsed.paths)), repr(_fold(parsed.senders)),
            repr(_fold(parsed.recipients)), repr(_fold(parsed.subjects)),
            repr(_fold(parsed.names)), repr(_fold(parsed.repos)),
            repr(_fold(parsed.shows)), repr(_fold(parsed.place)),
            repr(_fold(parsed.who)), repr(_fold(parsed.volumes)),
            repr(_fold(parsed.only)), repr(_fold(parsed.statuses)),
            repr(parsed.sizes), repr(parsed.has_attachment), repr(parsed.sort),
            repr(_fold(parsed.not_ext)), repr(_fold(parsed.not_paths)),
            repr(_fold(parsed.not_names)), repr(_fold(parsed.not_senders)),
            repr(_fold(parsed.not_recipients)), repr(_fold(parsed.not_subjects)),
            repr(_fold(parsed.not_repos)), repr(_fold(parsed.not_phrases)),
            repr(_fold(parsed.not_shows)), repr(_fold(parsed.not_place)),
            repr(_fold(parsed.not_who)), repr(_fold(parsed.not_volumes)),
            repr(_fold(parsed.not_only)),
            parsed.scope, "r" if rerank else "-", model, str(limit),
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
            chunk_id=_result_chunk_id(hit),
            file_id=int(hit.get("file_id", 0)),
            path=str(hit.get("path", "")),
            text=str(hit.get("text", "")),
            page=hit.get("page"),
            label=str(hit.get("label", "") or ""),
            char_start=hit.get("char_start"),
            char_end=hit.get("char_end"),
            # Normalised the way `upsert_file` stores it - bare, lowercase, no
            # leading dot. A dotted extension here would make `type:pdf` and the
            # result row's own label disagree about the same file.
            ext=str(hit.get("ext", "") or "").lower().lstrip("."),
            mtime_ns=int(hit.get("mtime_ns") or 0),
            taken_at_ns=int(hit.get("taken_at_ns") or 0),
            content_hash=str(hit.get("content_hash", "") or ""),
            phash=str(hit.get("phash", "") or ""),
            volume_id=(int(hit["volume_id"]) if hit.get("volume_id") is not None else None),
            relative_path=str(hit.get("relative_path", "") or ""),
            distance=hit.get("distance"),
            photo_match=str(hit.get("photo_match", "") or ""),
            recency=float(hit.get("recency") or 0.0),
            declares=bool(hit.get("declares") or False),
            filename_match=bool(hit.get("filename_match") or False),
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
