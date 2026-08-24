"""The job that turns an index into a graph.

Layer: L6

Reads `chunks` in id order, extracts entities with `cooccurrence.extract`, and
writes entities, mentions and accumulating edge weights. Then a second pass
scores every edge with normalised PMI and prunes the weak ones.

**Two passes, not one, because PMI cannot be computed early.** An edge's score
depends on how often *each* entity appears across the whole corpus, and that
number is not known until the last chunk has been read. Scoring during the walk
would mean scoring against partial totals - every early edge judged against a
corpus that did not exist yet, and no way to tell afterwards which ones those
were.

**Resumable, because this runs over 100GB.** The cursor is the last chunk id
processed, and it is committed in the same transaction as the batch it describes
(`SqliteStore.commit_graph_batch`). Interrupt it at any point and the next run
picks up at exactly the right chunk, having neither skipped nor double-counted
one. That matters more here than in the indexer: edge weights accumulate, so a
double-counted batch is silently wrong rather than loudly broken.

**It never blocks search.** Nothing in Layer 4 reads these tables, so a build can
run for an hour while someone searches, and killing it costs the unfinished
batch and nothing else.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from app.core.logging import logger
from app.graph import cooccurrence
from app.graph.cooccurrence import Entity

__all__ = ["GraphBuilder", "GraphProgress", "GraphResult", "MAX_ENTITIES_PER_CHUNK"]

log = logger.bind(component="graph.builder")

#: Pairs are quadratic in entities-per-chunk, so this cap is what stops one
#: pathological chunk - a contact list, a bibliography, an email footer with
#: forty names - from generating 20,000 edges on its own and burying the graph.
#: 24 entities is 276 pairs, which is the most a ~512-token passage can plausibly
#: be *about*. Beyond it the most frequent within the chunk are kept, so the cap
#: removes the tail rather than an arbitrary slice.
MAX_ENTITIES_PER_CHUNK = 24

#: How many chunks are processed before a batch is committed. Small enough that
#: an interrupt loses seconds, large enough that the transaction overhead is not
#: the bottleneck.
BATCH_CHUNKS = 500


@dataclass(frozen=True, slots=True)
class GraphProgress:
    """One progress tick. Deliberately plain data - the UI formats it."""

    chunks_done: int
    chunks_total: int
    entities: int
    edges: int
    phase: str  # "extract" | "score" | "prune"
    elapsed_s: float


@dataclass(slots=True)
class GraphResult:
    chunks_processed: int = 0
    entities: int = 0
    edges: int = 0
    edges_pruned: int = 0
    entities_merged: int = 0
    elapsed_s: float = 0.0
    interrupted: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "chunks_processed": self.chunks_processed,
            "entities": self.entities,
            "edges": self.edges,
            "edges_pruned": self.edges_pruned,
            "entities_merged": self.entities_merged,
            "elapsed_s": round(self.elapsed_s, 2),
            "interrupted": self.interrupted,
        }


class _Store(Protocol):
    """The slice of SqliteStore this needs. A Protocol so tests use a fake."""

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]: ...
    def set_state(self, key: str, value: str) -> None: ...
    def iter_chunks_after(self, chunk_id: int, batch_size: int = ...): ...
    def commit_graph_batch(self, *, entities, mentions, pairs, cursor) -> dict[str, int]: ...
    def recount_entities(self) -> None: ...
    def entity_chunk_counts(self) -> dict[int, int]: ...
    def graph_chunk_total(self) -> int: ...
    def iter_edges_for_scoring(self, batch_size: int = ...): ...
    def set_edge_scores(self, rows) -> None: ...
    def merge_contained_entities(self) -> int: ...
    def prune_graph(self, *, min_weight: int, min_pmi: float) -> int: ...
    def clear_graph(self) -> None: ...
    def graph_stats(self) -> dict: ...


class GraphBuilder:
    """Builds or extends the knowledge graph over an existing index."""

    def __init__(
        self,
        store: _Store,
        *,
        min_weight: int = 2,
        min_npmi: float = 0.0,
        max_entities_per_chunk: int = MAX_ENTITIES_PER_CHUNK,
        batch_chunks: int = BATCH_CHUNKS,
        on_progress: Optional[Callable[[GraphProgress], None]] = None,
    ) -> None:
        self.store = store
        self.min_weight = min_weight
        self.min_npmi = min_npmi
        self.max_entities_per_chunk = max_entities_per_chunk
        self.batch_chunks = batch_chunks
        self.on_progress = on_progress
        self._stop = False

    def request_stop(self) -> None:
        """Ask the build to finish the current batch and return.

        Checked between batches, never inside one, so stopping always lands on a
        committed boundary. The alternative - aborting mid-batch - leaves the
        cursor describing work that was rolled back, which is the one state the
        resume logic cannot reason about.
        """
        self._stop = True

    # -- pass 1: extract -----------------------------------------------------

    def build(self, *, rebuild: bool = False, chunks_total: int = 0) -> GraphResult:
        started = time.monotonic()
        result = GraphResult()

        if rebuild:
            log.info("graph: rebuilding from scratch")
            self.store.clear_graph()
            self.store.set_state("graph:cursor", "0")

        cursor = int(self.store.get_state("graph:cursor", "0") or 0)
        if cursor:
            log.info("graph: resuming after chunk {}", cursor)

        pending_entities: dict[str, tuple[str, str, str, str]] = {}
        pending_mentions: list[tuple[str, int, int, int]] = []
        pending_pairs: Counter[tuple[str, str]] = Counter()
        in_batch = 0

        for rows in self.store.iter_chunks_after(cursor, self.batch_chunks):
            for chunk_id, file_id, text in rows:
                cursor = chunk_id
                found = self._entities_for_chunk(text)
                keys = []
                for entity, count in found:
                    pending_entities.setdefault(
                        entity.key, (entity.key, entity.display, entity.kind, "cooccurrence")
                    )
                    pending_mentions.append((entity.key, chunk_id, file_id, count))
                    keys.append(entity.key)
                for key_a, key_b in _pairs(keys):
                    pending_pairs[(key_a, key_b)] += 1
                in_batch += 1
                result.chunks_processed += 1

            if in_batch >= self.batch_chunks:
                self._flush(pending_entities, pending_mentions, pending_pairs, cursor)
                pending_entities, pending_mentions, pending_pairs = {}, [], Counter()
                in_batch = 0
                self._tick("extract", result.chunks_processed, chunks_total, started)

            if self._stop:
                result.interrupted = True
                break

        if pending_entities or pending_mentions or pending_pairs:
            self._flush(pending_entities, pending_mentions, pending_pairs, cursor)

        if result.interrupted:
            # Deliberately no scoring pass. Scores computed against a corpus that
            # was only half read are worse than no scores: they look finished.
            log.info("graph: stopped by request after {} chunks", result.chunks_processed)
            result.elapsed_s = time.monotonic() - started
            stats = self.store.graph_stats()
            result.entities, result.edges = int(stats["entities"]), int(stats["edges"])
            return result

        self.store.recount_entities()
        # Before scoring: fold away short forms that never appear without their
        # long form ("AVEVA Group" inside "AVEVA Group Limited"). Doing it here
        # rather than after means the survivor's counts and every PMI score are
        # computed from the merged evidence, not from half of it.
        result.entities_merged = self.store.merge_contained_entities()
        if result.entities_merged:
            self.store.recount_entities()
        self._tick("score", result.chunks_processed, chunks_total, started)
        self.score_edges(max(self.store.graph_chunk_total(), 1))

        self._tick("prune", result.chunks_processed, chunks_total, started)
        result.edges_pruned = self.store.prune_graph(
            min_weight=self.min_weight, min_pmi=self.min_npmi
        )

        stats = self.store.graph_stats()
        result.entities, result.edges = int(stats["entities"]), int(stats["edges"])
        result.elapsed_s = time.monotonic() - started
        log.info(
            "graph: {} entities, {} edges ({} pruned) in {:.1f}s",
            result.entities, result.edges, result.edges_pruned, result.elapsed_s,
        )
        return result

    def _entities_for_chunk(self, text: str) -> list[tuple[Entity, int]]:
        counts = cooccurrence.extract(text).counts
        if len(counts) <= self.max_entities_per_chunk:
            return list(counts.items())
        # Most frequent first, then by key so the cap is deterministic - two runs
        # over the same corpus must produce the same graph or nothing downstream
        # can be compared between them.
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].key))
        return ranked[: self.max_entities_per_chunk]

    def _flush(self, entities, mentions, pairs, cursor: int) -> None:
        self.store.commit_graph_batch(
            entities=list(entities.values()),
            mentions=mentions,
            pairs=pairs,
            cursor=cursor,
        )

    def _tick(self, phase: str, done: int, total: int, started: float) -> None:
        if self.on_progress is None:
            return
        stats = self.store.graph_stats()
        self.on_progress(GraphProgress(
            chunks_done=done,
            chunks_total=total,
            entities=int(stats["entities"]),
            edges=int(stats["edges"]),
            phase=phase,
            elapsed_s=time.monotonic() - started,
        ))

    # -- pass 2: score -------------------------------------------------------

    def score_edges(self, total_chunks: int) -> None:
        """Give every edge a normalised PMI, in batches.

        Batched for the same reason the build is: an index with a million edges
        cannot be loaded, scored and written back as one list. `entity_chunk_counts`
        *is* loaded whole, which is fine - there are orders of magnitude fewer
        entities than edges.
        """
        counts = self.store.entity_chunk_counts()
        log.debug("graph: scoring against {} chunks", total_chunks)
        for batch in self.store.iter_edges_for_scoring():
            scored = [
                (
                    cooccurrence.npmi(
                        weight, counts.get(a_id, 0), counts.get(b_id, 0), total_chunks
                    ),
                    a_id,
                    b_id,
                )
                for a_id, b_id, weight in batch
            ]
            self.store.set_edge_scores(scored)


def _pairs(keys: list[str]):
    unique = sorted(set(keys))
    for index, key_a in enumerate(unique):
        for key_b in unique[index + 1:]:
            yield (key_a, key_b)
