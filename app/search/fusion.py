"""Reciprocal rank fusion (k=60). Pure function, unit-tested.

Layer: L4

BM25 scores and cosine distances are not comparable - different scales, different
signs, different distributions per query. RRF sidesteps the problem entirely by
throwing the scores away and fusing on *rank*: `score = sum 1/(k + rank)`. No
normalisation to tune, no per-query calibration to drift.

k=60 is the value from the original paper and the spec. It is deliberately large
relative to the list lengths: it flattens the curve so that rank 1 does not
dominate, which is what lets a document ranked 8th by both retrievers beat one
ranked 1st by only one of them. That behaviour is the entire point of hybrid search.

Budget: <5ms. Nothing here allocates per-item beyond a dict entry, and the whole
module is stdlib.
"""

from __future__ import annotations

from typing import Any, Hashable, Iterable, Mapping, Optional, Sequence

__all__ = ["RRF_K", "rrf", "fuse_hits"]

RRF_K = 60


def rrf(
    rankings: Sequence[Sequence[Hashable]],
    *,
    k: int = RRF_K,
    weights: Optional[Sequence[float]] = None,
    limit: Optional[int] = None,
) -> list[tuple[Hashable, float]]:
    """Fuse ranked ID lists into one ranked list of `(id, score)`, best first.

    `rankings` is one sequence per retriever, each already ordered best-first.
    Duplicate IDs inside a single ranking are ignored after their first (best)
    appearance, so a retriever cannot inflate an item by returning it twice.

    Ties are broken by earliest appearance across the input lists, then by the
    order the lists were given. This makes the output fully deterministic, which
    is what makes a recall@10 regression test meaningful.
    """
    if k <= 0:
        raise ValueError(f"RRF k must be positive, got {k}")
    if weights is not None and len(weights) != len(rankings):
        raise ValueError(
            f"weights has {len(weights)} entries but there are {len(rankings)} rankings"
        )

    scores: dict[Hashable, float] = {}
    first_seen: dict[Hashable, tuple[int, int]] = {}

    for list_index, ranking in enumerate(rankings):
        weight = 1.0 if weights is None else float(weights[list_index])
        if weight == 0.0:
            continue
        seen_here: set[Hashable] = set()
        for position, key in enumerate(ranking):
            if key in seen_here:
                continue
            seen_here.add(key)
            scores[key] = scores.get(key, 0.0) + weight / (k + position + 1)
            if key not in first_seen:
                first_seen[key] = (position, list_index)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], first_seen[item[0]]))
    return ordered[:limit] if limit is not None else ordered


def fuse_hits(
    hit_lists: Sequence[Sequence[Mapping[str, Any]]],
    *,
    id_key: str = "chunk_id",
    k: int = RRF_K,
    weights: Optional[Sequence[float]] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """`rrf` over result rows rather than bare IDs.

    Returns the merged rows, best first, each carrying:

        rrf_score     the fused score
        sources       which input lists contained it, by index
        source_ranks  its 1-based rank within each of those lists

    `sources` is not decoration: a row found by both retrievers is the strongest
    signal the pipeline produces, and the UI is expected to show it. Rows are
    shallow-copied, so the caller's inputs are never mutated.

    A row missing `id_key` is skipped rather than fused under `None`, which would
    silently merge every malformed row into one result.
    """
    rankings: list[list[Hashable]] = []
    rows: dict[Hashable, dict[str, Any]] = {}
    ranks: dict[Hashable, dict[int, int]] = {}

    for list_index, hits in enumerate(hit_lists):
        ids: list[Hashable] = []
        for position, hit in enumerate(hits):
            key = hit.get(id_key)
            if key is None:
                continue
            ids.append(key)
            ranks.setdefault(key, {}).setdefault(list_index, position + 1)
            if key not in rows:
                rows[key] = dict(hit)
            else:
                # Keep the first row seen, but fill gaps from later retrievers:
                # the ANN row may carry fields the BM25 row lacks, and vice versa.
                for field_name, value in hit.items():
                    rows[key].setdefault(field_name, value)
        rankings.append(ids)

    fused = rrf(rankings, k=k, weights=weights, limit=limit)

    merged: list[dict[str, Any]] = []
    for key, score in fused:
        row = rows[key]
        row["rrf_score"] = score
        row["sources"] = tuple(sorted(ranks[key]))
        row["source_ranks"] = dict(sorted(ranks[key].items()))
        merged.append(row)
    return merged
