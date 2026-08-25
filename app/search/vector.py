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
) -> list[dict[str, Any]]:
    """ANN hits for a parsed query, nearest first.

    Returns [] when there is nothing to embed, when the index is empty, or when
    the filters exclude everything. All three are ordinary states rather than
    errors: searching before the first index run is normal, and a filter that
    matches nothing should produce no results, not a failure.
    """
    text = parsed.embed_text
    if not text:
        return []

    if allowed_file_ids is not None and not allowed_file_ids:
        return []                      # filters excluded everything; nothing to search

    try:
        query_vector = embedder.embed([text])[0]
    except AppErrorException:
        raise
    except Exception as exc:           # noqa: BLE001 - boundary
        _log.error("query embedding failed: {}", exc)
        return []

    try:
        if allowed_file_ids is None or len(allowed_file_ids) <= MAX_PREFILTER_IDS:
            # Fast path: no filter, or one small enough to push down whole.
            rows = vectors.search(
                query_vector, k=limit, where=_id_clause(allowed_file_ids)
            )
            return [_normalise(row) for row in rows]
        return _search_large_filter(vectors, query_vector, limit, allowed_file_ids)
    except AppErrorException:
        raise
    except Exception as exc:           # noqa: BLE001 - a vector-store hiccup must not
        _log.error("ANN search failed, continuing with keyword results only: {}", exc)
        return []


def _search_large_filter(
    vectors: Any,
    query_vector: Sequence[float],
    limit: int,
    allowed_file_ids: set[int],
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
        results = [
            row for row in (_normalise(r) for r in rows)
            if row["file_id"] in allowed_file_ids
        ]
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
    _log.debug(
        "over-fetch exhausted; pushing down full {}-id prefilter",
        len(allowed_file_ids),
    )
    rows = vectors.search(query_vector, k=limit, where=_id_clause(allowed_file_ids))
    return [_normalise(row) for row in rows]


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
            SELECT c.id AS chunk_id, c.file_id, c.text, c.page,
                   c.char_start, c.char_end, f.path, f.ext, f.mtime_ns
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
