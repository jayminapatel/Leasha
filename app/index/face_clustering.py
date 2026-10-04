r"""Incremental face clustering: the automatic half of the Photo Tagger.

Layer: L3

Work order 0j (`202626270512`) sections 1b and 2c. Pure functions, no I/O and
no dependency on `app.storage` or `app.extract` - the same reasoning that let
`app/search/fusion.py` and `app/search/query.py` be built and tested ahead of
the layers around them (see `HANDOFF.md` "Built out of order, deliberately").
The caller (`app.index.pipeline`'s face-backfill drain, and the images-pass
face step) owns reading faces out of SQLite and writing the verdicts back;
this module only ever answers "how similar are these two embeddings" and
"given what already exists, what should happen to this new one".

**The principled line, restated for this module specifically**: nothing here
ever assigns a *name*. `classify()` and `cluster_batch()` produce three
verdicts - assign to an existing pile, suggest an existing pile, or start a
fresh (unnamed) pile - and every one of those piles starts and stays
identity-free until a person names it. Confidence is a distance, not an
opinion about who somebody is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

__all__ = [
    "AUTO_ASSIGN_THRESHOLD", "SUGGEST_THRESHOLD",
    "to_bytes", "from_bytes", "cosine_similarity", "centroid_of",
    "classify", "best_match", "ClusterPlan", "cluster_batch",
]

# Confidence thresholds - section 2c: "envelope tunables, invisible outside
# Manual [mode]". Full Auto-tune/Manual-mode exposure (the machine-derived
# envelope every other tunable in `BUILD_SPEC_V2.md` gets) is not built this
# session - it is a real follow-on, not a shortcut taken silently, the same
# shape 0i left `image_tag` and the idle-only trigger in. Non-negotiable
# #11's own fallback applies instead: "if nobody will ever change it, it is
# a constant" - these are fixed, with the evidence that would change them
# named here rather than buried in a commit message.
#
# Chosen from insightface's own documented ArcFace cosine-similarity
# behaviour (same-identity pairs cluster above ~0.6 on the buffalo_l model
# card's own evaluation numbers) rather than measured against a real corpus
# on this machine - no labelled face set was available to measure against
# in this session, which is the evidence that would justify moving these.
AUTO_ASSIGN_THRESHOLD = 0.62
SUGGEST_THRESHOLD = 0.45


def to_bytes(vector: Sequence[float]) -> bytes:
    """An embedding as stored in `faces.embedding`. Always float32 - a face
    model's own precision, and halving storage against float64 for no loss
    an 8-bit-quantised JPEG's noise floor would not already dominate."""
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_bytes(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """1.0 for identical direction, 0.0 for orthogonal, never NaN.

    A zero vector (a detector that returned nothing embeddable, which
    should not happen but must not crash clustering if it does) is treated
    as similar to nothing - `0.0`, not a `ZeroDivisionError` a batch job
    would have to guard around one face at a time.
    """
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def centroid_of(embeddings: Sequence[bytes]) -> np.ndarray:
    """The mean direction of a pile's own faces. Empty input is the empty
    vector - callers only ever call this with at least one embedding
    (`SqliteStore.pile_centroids` never returns an empty list for a pile
    that exists), so this is a defined-behaviour guard, not a real path."""
    if not embeddings:
        return np.zeros(0, dtype=np.float32)
    vectors = np.stack([from_bytes(e) for e in embeddings])
    return vectors.mean(axis=0)


def classify(score: float) -> str:
    """`"assign"`, `"suggest"`, or `"new"` - section 2c's three verdicts."""
    if score >= AUTO_ASSIGN_THRESHOLD:
        return "assign"
    if score >= SUGGEST_THRESHOLD:
        return "suggest"
    return "new"


def best_match(embedding: np.ndarray, centroids: dict[int, np.ndarray]
               ) -> "tuple[int, float] | None":
    """The closest existing pile, and how close. `None` for no piles at all."""
    best_id: "int | None" = None
    best_score = -1.0
    for pile_id, centroid in centroids.items():
        score = cosine_similarity(embedding, centroid)
        if score > best_score:
            best_id, best_score = pile_id, score
    if best_id is None:
        return None
    return best_id, best_score


@dataclass(slots=True)
class ClusterPlan:
    """What `cluster_batch` decided. The caller applies it - this module
    writes nothing anywhere, per its own docstring."""

    #: `(face_id, pile_id, confidence)` - section 1b/2c's auto-assign.
    assign: list[tuple[int, int, float]] = field(default_factory=list)
    #: `(face_id, pile_id, confidence)` - section 2c's "Is this Daddy?" queue.
    suggest: list[tuple[int, int, float]] = field(default_factory=list)
    #: Groups of face ids that matched no existing pile closely enough, but
    #: matched *each other* - each group becomes one fresh unnamed pile.
    #: Section 1b's "unsupervised clustering into unnamed piles" from a cold
    #: start, not only comparison against piles that already exist.
    new_piles: list[list[int]] = field(default_factory=list)
    #: Left over: matched nothing, including no other leftover face closely
    #: enough. Stay unclustered until a later batch gives them company - a
    #: face of one, alone, is not yet evidence of a repeated person.
    unresolved: list[int] = field(default_factory=list)


def cluster_batch(
    faces: Sequence[tuple[int, bytes]],
    existing_centroids: "dict[int, bytes] | dict[int, np.ndarray]",
    declined: "Optional[dict[int, set[int]]]" = None,
) -> ClusterPlan:
    r"""Decide what to do with a batch of freshly-detected, unclustered faces.

    `faces` is `(face_id, embedding_bytes)` pairs - typically one drain
    batch's worth (`SqliteStore.iter_unclustered_faces`).
    `existing_centroids` is every current pile's own embeddings-so-far,
    already averaged by the caller (`SqliteStore.pile_centroids` gives the
    raw list; a centroid is one `centroid_of` call away) - named and unnamed
    piles both participate, because an unnamed pile is a real cluster
    already and a new face resembling it belongs there whether or not
    anybody has typed a name yet.

    **Two passes.** First, every face against every existing pile - cheap
    (`O(faces x piles)`, both counts small compared to a corpus) and it is
    where nearly everything lands once even a handful of piles exist.
    Second, whatever matched nothing is clustered against *itself*: a
    single-linkage pass (each leftover face compared to the running
    centroid of new piles formed so far) is what turns "three photos of a
    baby nobody has seen before" into one fresh pile of three rather than
    three faces sitting unresolved forever.
    """
    plan = ClusterPlan()
    centroids = {pid: from_bytes(data) if not isinstance(data, np.ndarray) else data
                 for pid, data in existing_centroids.items()}

    leftover: list[tuple[int, np.ndarray]] = []
    for face_id, raw in faces:
        vector = from_bytes(raw)
        # 2026-10-05: never a person this face was told it is not (`declined`,
        # the chip's No and the manage dialog's "Not this person").
        refused = (declined or {}).get(face_id)
        candidates = ({p: v for p, v in centroids.items() if p not in refused}
                      if refused else centroids)
        match = best_match(vector, candidates) if candidates else None
        if match is None:
            leftover.append((face_id, vector))
            continue
        pile_id, score = match
        verdict = classify(score)
        if verdict == "assign":
            plan.assign.append((face_id, pile_id, score))
        elif verdict == "suggest":
            plan.suggest.append((face_id, pile_id, score))
        else:
            leftover.append((face_id, vector))

    # Second pass: bootstrap fresh piles from mutual similarity among the
    # leftovers. Greedy and order-dependent, like every online single-
    # linkage clustering - acceptable here because a wrong early grouping is
    # never permanent: section 2b's Combine/split/remove-from-pile exist
    # precisely so a person can correct exactly this kind of mistake, cheaply,
    # by dragging one pile onto another rather than by re-running a global
    # clustering pass over the whole corpus.
    new_groups: list[list[int]] = []
    new_group_vectors: list[np.ndarray] = []
    still_unresolved: list[int] = []
    for face_id, vector in leftover:
        placed = False
        for index, group_vector in enumerate(new_group_vectors):
            if cosine_similarity(vector, group_vector) >= AUTO_ASSIGN_THRESHOLD:
                new_groups[index].append(face_id)
                count = len(new_groups[index])
                # Running mean, updated in place - avoids re-averaging every
                # member on every new arrival for what is, in practice, a
                # handful of faces per batch.
                new_group_vectors[index] = (
                    (group_vector * (count - 1) + vector) / count)
                placed = True
                break
        if not placed:
            new_groups.append([face_id])
            new_group_vectors.append(vector)

    for group in new_groups:
        if len(group) >= 2:
            plan.new_piles.append(group)
        else:
            still_unresolved.extend(group)
    plan.unresolved = still_unresolved

    return plan
