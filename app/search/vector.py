"""ANN search — the half of hybrid search that knows what words mean.

Layer: L4

Where BM25 finds the document containing "pump station", this finds the one
about "compressor house" when that is what the person meant. On its own it is
worse than keyword search at names, part numbers and codes, and better at
everything vague. Neither is the primary; that is the whole argument for fusion.

**Filters are pushed down, not applied afterwards.** LanceDB cannot join to
`files`, so eligible file ids are resolved in SQL first and handed over as a
prefilter. Fetching the top 100 and *then* discarding the ones that fail
`type:pdf` would leave whatever survived — which is not the top 100 PDFs, it is
the PDFs that happened to be in the global top 100, and on a large index that is
frequently none of them.

**One embedding per search, never per candidate.** The query is embedded once.
That single call is the layer's largest fixed cost at roughly 15ms, and it is why
the model is warmed at startup rather than on first search.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from app.core.errors import AppErrorException
from app.core.logging import logger
from app.search.query import ParsedQuery

__all__ = ["search", "VECTOR_LIMIT", "MAX_PREFILTER_IDS"]

#: Candidates handed to fusion. From the spec's pipeline diagram.
VECTOR_LIMIT = 100

#: Above this many eligible file ids, the pushed-down prefilter is skipped on
#: the first attempt. An `IN (...)` list of 50,000 ids is slower to build and
#: parse than the search it was meant to narrow - but skipping it must never
#: change *which* results come back, so the large-filter path over-fetches and
#: escalates until the eligible top-k is guaranteed (see `search`).
MAX_PREFILTER_IDS = 2_000

#: Escalation ladder for the large-filter path: fetch `limit * factor` global
#: candidates and post-filter. A filter big enough to skip the prefilter
#: usually matches most of the corpus, so the first rung nearly always fills
#: the page in one query.
OVERFETCH_FACTORS = (4, 16)

_log = logger.bind(component="search.vector")


def search(
    vectors: Any,
    embedder: Any,
    parsed: ParsedQuery,
    *,
    limit: int = VECTOR_LIMIT,
    allowed_file_ids: Optional[set[int]] = None,
    problems: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    r"""ANN hits for a parsed query, nearest first.

    Returns [] when there is nothing to embed, when the index is empty, or when
    the filters exclude everything. All three are ordinary states rather than
    errors: searching before the first index run is normal, and a filter that
    matches nothing should produce no results, not a failure.

    **And [] when this half breaks, which is the fourth reason and was the bug.**
    Both boundaries below used to re-raise `AppErrorException` while catching
    everything else - and `Embedder.embed` raises exactly that for a model that
    is missing, corrupt or half-downloaded. So a broken embedding model did not
    degrade hybrid search to keyword search; it failed the **whole** search and
    discarded the keyword hits that had already been computed alongside it.

    That contradicted this module's own comment two lines further down - *"a
    vector-store hiccup must not"* stop the search - and it contradicted
    `engine.NOTICE_NO_VECTORS`, machinery built precisely to tell somebody that
    they are looking at keyword-only results. The notice could never fire for
    the one failure it describes best.

    `problems` is how the cause survives the degradation. Returning [] silently
    would trade a loud wrong behaviour for a quiet one, and this codebase has a
    standing rule against exactly that: *"for all things it should not fail
    silently it should notify in some way."* Append-only, optional, and
    unexamined here - the caller decides what to do with the words.
    """
    text = parsed.embed_text
    if not text:
        return []

    eligible = _as_eligibility(allowed_file_ids)
    if eligible is not None and eligible.excludes_everything:
        return []                      # filters excluded everything; nothing to search

    try:
        query_vector = embedder.embed([text])[0]
    except AppErrorException as exc:
        # The model itself. Named separately from a generic failure because it
        # is the one with an action attached - the AppError already carries a
        # suggestion, and dropping it here would waste the best sentence
        # available to whoever is looking at half a search.
        _log.error("query embedding failed, continuing with keyword results "
                   "only: {}", exc.error.message)
        _note(problems, exc.error.suggestion or exc.error.message)
        return []
    except Exception as exc:           # noqa: BLE001 - boundary
        _log.error("query embedding failed: {}", exc)
        _note(problems, f"The query could not be embedded ({exc}).")
        return []

    try:
        pushdown = eligible.ids if eligible is not None else None
        if pushdown is not None and len(pushdown) <= MAX_PREFILTER_IDS:
            # Fast path: a filter small enough to push down whole.
            rows = vectors.search(query_vector, k=limit, where=_id_clause(pushdown))
            return [_normalise(row) for row in rows]
        if eligible is None:
            rows = vectors.search(query_vector, k=limit, where=None)
            return [_normalise(row) for row in rows]
        return _search_large_filter(vectors, query_vector, limit, eligible)
    except AppErrorException as exc:
        _log.error("ANN search failed, continuing with keyword results only: {}",
                   exc.error.message)
        _note(problems, exc.error.suggestion or exc.error.message)
        return []
    except Exception as exc:           # noqa: BLE001 - a vector-store hiccup must not
        _log.error("ANN search failed, continuing with keyword results only: {}", exc)
        _note(problems, f"The vector store could not be searched ({exc}).")
        return []


def _note(problems: Optional[list[str]], text: str) -> None:
    """Record why this half produced nothing. **Never raises.**

    A sink that throws would take down the search this function exists to keep
    alive, which would be an absurd way to lose it.
    """
    if problems is None:
        return
    try:
        problems.append(str(text))
    except Exception:                  # noqa: BLE001
        return


def _search_large_filter(
    vectors: Any,
    query_vector: Sequence[float],
    limit: int,
    eligible: Any,
) -> list[dict[str, Any]]:
    """The eligible top-`limit` when the id list is too big to push down cheaply.

    Fetching the global top-`limit` and discarding ineligible rows would lose
    every eligible hit ranked past `limit` globally - `type:pdf` on a large
    mixed corpus could return nothing while thousands of PDFs match. So:
    over-fetch, post-filter, escalate, and if the ladder runs out, push the
    full id list down anyway. Slower on the last rung, never wrong.
    """
    for factor in OVERFETCH_FACTORS:
        k = limit * factor
        rows = vectors.search(query_vector, k=k)
        # **One question about the candidates, not a list of the eligible.**
        # `keeps` intersects a held set when the filter is narrow and asks
        # SQLite about these few hundred ids when it is broad - which is what
        # stops a `type:pdf` over a large corpus paying 383ms to build a
        # quarter of a million ids nothing then wanted.
        candidates = [_normalise(r) for r in rows]
        allowed = eligible.keeps([row["file_id"] for row in candidates])
        results = [row for row in candidates if row["file_id"] in allowed]
        if len(results) >= limit or len(rows) < k:
            # A full page of eligible hits, or the table itself is exhausted:
            # either way the eligible top-k is complete.
            return results[:limit]
        _log.debug(
            "over-fetch k={} found only {} of {} eligible hits; escalating",
            k, len(results), limit,
        )

    # Last rung: the exact prefilter, however large. This is the query the cap
    # exists to avoid, paid only when the cheap attempts could not fill a page.
    ids = eligible.ids
    if ids is None:
        # **A filter too broad to list, and the ladder still did not fill a
        # page.** There is no id list to push down - building one is the cost
        # this path exists to avoid - so take the widest over-fetch and answer
        # with what is eligible in it. Short of a full page rather than wrong,
        # and only reachable when the filter is broad *and* the query matches
        # almost nothing that passes it.
        rows = vectors.search(query_vector, k=limit * OVERFETCH_FACTORS[-1])
        candidates = [_normalise(r) for r in rows]
        allowed = eligible.keeps([row["file_id"] for row in candidates])
        return [row for row in candidates if row["file_id"] in allowed][:limit]

    _log.debug("over-fetch exhausted; pushing down full {}-id prefilter", len(ids))
    rows = vectors.search(query_vector, k=limit, where=_id_clause(ids))
    return [_normalise(row) for row in rows]


class _SetEligibility:
    """An `Eligibility` face over a plain set of ids.

    `vector.search` is called with a bare set by six regression tests and by
    anybody experimenting at a REPL, and those calls are worth keeping working.
    One adapter here is cheaper than a second code path inside the search.
    """

    __slots__ = ("ids",)

    def __init__(self, ids: set[int]) -> None:
        self.ids = {int(f) for f in ids}

    @property
    def excludes_everything(self) -> bool:
        return not self.ids

    def keeps(self, file_ids: Sequence[int]) -> set[int]:
        return {int(f) for f in file_ids} & self.ids


def _as_eligibility(allowed: Any) -> Any:
    """None, an `Eligibility`, or a set - normalised to the first two."""
    if allowed is None:
        return None
    if hasattr(allowed, "keeps"):
        return allowed
    return _SetEligibility(allowed)


def _id_clause(allowed_file_ids: Optional[set[int]]) -> Optional[str]:
    if allowed_file_ids is None:
        return None
    ids = ", ".join(str(int(fid)) for fid in sorted(allowed_file_ids))
    return f"file_id IN ({ids})"


def _normalise(row: dict[str, Any]) -> dict[str, Any]:
    """LanceDB returns `_distance`; the rest of the pipeline speaks `distance`.

    Kept as a distance rather than converted to a similarity, because RRF fuses
    on rank and never looks at the number - and inventing a similarity score
    would invite somebody downstream to compare it against BM25, which is
    exactly the mistake fusion exists to prevent.
    """
    out = dict(row)
    if "_distance" in out:
        out["distance"] = out.pop("_distance")
    out["chunk_id"] = int(out.get("chunk_id", 0))
    out["file_id"] = int(out.get("file_id", 0))
    return out


def hydrate(store: Any, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill in text, path and page for ANN hits.

    The vector store holds ids and vectors only - deliberately, so SQLite stays
    the authority on content and LanceDB can be rebuilt from it at any time. The
    cost is this lookup, which is one indexed query for the whole batch.
    """
    if not rows:
        return []

    ids = [int(row["chunk_id"]) for row in rows]
    placeholders = ", ".join("?" for _ in ids)
    found = {
        int(record["chunk_id"]): dict(record)
        for record in store.conn.execute(
            f"""
            SELECT c.id AS chunk_id, c.file_id, c.text, c.page, c.label,
                   c.char_start, c.char_end, f.path, f.ext, f.mtime_ns, f.content_hash
            FROM chunks c JOIN files f ON f.id = c.file_id
            WHERE c.id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    }

    hydrated = []
    for row in rows:
        detail = found.get(int(row["chunk_id"]))
        if detail is None:
            # A vector whose chunk is gone: LanceDB is derived, so it can lag
            # SQLite for a moment after a delete. Dropping it is correct - the
            # alternative is a result that opens nothing.
            continue
        merged = dict(detail)
        merged["distance"] = row.get("distance")
        hydrated.append(merged)
    return hydrated
