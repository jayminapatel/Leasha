r"""Incremental face clustering - the pure half. Work order 0j sections 1b/2c.

Layer: L3

`app/index/face_clustering.py` is deliberately dependency-free (no SQLite, no
insightface) so its own correctness is provable in milliseconds, the same
argument `test_query.py` and `test_fusion.py` already make for their own
modules - see `HANDOFF.md` "Built out of order, deliberately".
"""

from __future__ import annotations

import numpy as np
import pytest

from app.index import face_clustering as fc


def _vec(*values: float) -> bytes:
    return fc.to_bytes(list(values) + [0.0] * (8 - len(values)))


def test_to_bytes_and_from_bytes_round_trip():
    original = [0.1, 0.2, -0.3, 0.4]
    restored = fc.from_bytes(fc.to_bytes(original))
    assert restored.dtype == np.float32
    assert np.allclose(restored, original, atol=1e-6)


def test_cosine_similarity_of_identical_vectors_is_one():
    v = fc.from_bytes(_vec(1, 2, 3))
    assert fc.cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_of_orthogonal_vectors_is_zero():
    a = fc.from_bytes(fc.to_bytes([1.0, 0.0]))
    b = fc.from_bytes(fc.to_bytes([0.0, 1.0]))
    assert fc.cosine_similarity(a, b) == pytest.approx(0.0)


def test_cosine_similarity_never_raises_on_a_zero_vector():
    zero = fc.from_bytes(fc.to_bytes([0.0, 0.0]))
    other = fc.from_bytes(fc.to_bytes([1.0, 0.0]))
    assert fc.cosine_similarity(zero, other) == 0.0
    assert fc.cosine_similarity(zero, zero) == 0.0


def test_centroid_of_is_the_mean_direction():
    embeddings = [fc.to_bytes([1.0, 0.0]), fc.to_bytes([0.0, 1.0])]
    centroid = fc.centroid_of(embeddings)
    assert np.allclose(centroid, [0.5, 0.5])


def test_classify_bands():
    assert fc.classify(0.9) == "assign"
    assert fc.classify(fc.AUTO_ASSIGN_THRESHOLD) == "assign"
    assert fc.classify(0.5) == "suggest"
    assert fc.classify(fc.SUGGEST_THRESHOLD) == "suggest"
    assert fc.classify(0.1) == "new"


def test_best_match_picks_the_closest_pile():
    target = fc.from_bytes(fc.to_bytes([1.0, 0.0, 0.0]))
    centroids = {
        1: fc.from_bytes(fc.to_bytes([0.0, 1.0, 0.0])),
        2: fc.from_bytes(fc.to_bytes([0.9, 0.1, 0.0])),
    }
    pile_id, score = fc.best_match(target, centroids)
    assert pile_id == 2
    assert score > 0.9


def test_best_match_is_none_with_no_piles():
    target = fc.from_bytes(fc.to_bytes([1.0, 0.0]))
    assert fc.best_match(target, {}) is None


# ---------------------------------------------------------------------------
# cluster_batch - the whole verdict machinery
# ---------------------------------------------------------------------------

def test_cluster_batch_assigns_a_face_close_to_a_named_pile():
    daddy = fc.to_bytes([1.0, 0.0, 0.0])
    new_face = fc.to_bytes([0.99, 0.05, 0.0])
    plan = fc.cluster_batch([(101, new_face)], {5: fc.from_bytes(daddy)})

    assert plan.assign == [(101, 5, pytest.approx(plan.assign[0][2]))]
    assert plan.assign[0][2] > fc.AUTO_ASSIGN_THRESHOLD
    assert plan.suggest == []
    assert plan.new_piles == []


def test_cluster_batch_suggests_a_borderline_face():
    # Chosen so the cosine similarity lands strictly between the two
    # thresholds - see the module's own threshold values.
    daddy = fc.to_bytes([1.0, 0.0])
    borderline = fc.to_bytes([0.6, 0.8])            # cos ~ 0.6
    plan = fc.cluster_batch([(1, borderline)], {5: fc.from_bytes(daddy)})

    assert plan.assign == []
    assert len(plan.suggest) == 1
    face_id, pile_id, score = plan.suggest[0]
    assert (face_id, pile_id) == (1, 5)
    assert fc.SUGGEST_THRESHOLD <= score < fc.AUTO_ASSIGN_THRESHOLD


def test_cluster_batch_groups_mutually_similar_strangers_into_a_new_pile():
    """Section 1b's cold-start case: nobody named yet, but two photos plainly
    show the same new person - they must land in one fresh pile together,
    not sit unresolved forever."""
    a = fc.to_bytes([0.0, 1.0, 0.0])
    b = fc.to_bytes([0.02, 0.999, 0.0])
    plan = fc.cluster_batch([(1, a), (2, b)], {})

    assert plan.assign == []
    assert plan.suggest == []
    assert len(plan.new_piles) == 1
    assert set(plan.new_piles[0]) == {1, 2}
    assert plan.unresolved == []


def test_cluster_batch_leaves_a_lone_stranger_unresolved():
    """One face with no company yet is not evidence of a repeated person."""
    lone = fc.to_bytes([0.3, -0.7, 0.1])
    plan = fc.cluster_batch([(1, lone)], {})

    assert plan.assign == plan.suggest == plan.new_piles == []
    assert plan.unresolved == [1]


def test_cluster_batch_accepts_precomputed_centroids_or_raw_bytes():
    """The drain passes `np.ndarray` centroids (already averaged);
    `cluster_batch` must accept either that or raw embedding bytes without
    the caller having to know which."""
    raw = fc.to_bytes([1.0, 0.0])
    as_array = fc.from_bytes(raw)
    new_face = fc.to_bytes([0.99, 0.1])

    plan_bytes = fc.cluster_batch([(1, new_face)], {9: raw})
    plan_array = fc.cluster_batch([(1, new_face)], {9: as_array})

    assert plan_bytes.assign and plan_array.assign
    assert plan_bytes.assign[0][:2] == plan_array.assign[0][:2] == (1, 9)


def _loop_matches(faces, centroids, declined):
    """The face-by-face answer `cluster_batch` gave before 2026-10-20."""
    out = []
    for face_id, raw in faces:
        refused = declined.get(face_id)
        candidates = ({p: v for p, v in centroids.items() if p not in refused}
                      if refused else centroids)
        out.append(fc.best_match(fc.from_bytes(raw), candidates) if candidates else None)
    return out


def test_the_batch_match_gives_the_face_by_face_answer():
    """2026-10-20: one matrix product in place of a call per face and pile.
    Same pile and score for every face - declined piles, a zero face and a
    zero pile included - so no verdict moves."""
    rng = np.random.default_rng(7)
    centroids = {pid: rng.normal(size=16).astype(np.float32) for pid in range(100, 140)}
    centroids[999] = np.zeros(16, dtype=np.float32)
    faces = [(i, fc.to_bytes(rng.normal(size=16))) for i in range(60)]
    faces.append((60, fc.to_bytes([0.0] * 16)))
    # Each face is near one pile, so assign and suggest both occur.
    faces += [(70 + k, fc.to_bytes(centroids[100 + k] + rng.normal(scale=s, size=16)))
              for k, s in enumerate([0.05, 0.3, 0.6, 1.0, 0.05])]
    declined = {70: {100}, 3: set(centroids) - {120}, 4: set(centroids)}

    expected = _loop_matches(faces, centroids, declined)
    got = fc._best_matches([fc.from_bytes(r) for _i, r in faces],
                           [i for i, _r in faces], centroids, declined)

    assert [m and m[0] for m in got] == [m and m[0] for m in expected]
    for g, e in zip(got, expected):
        if e is not None:
            assert g[1] == pytest.approx(e[1], abs=1e-5)
    assert got[4] is None and got[3][0] == 120


@pytest.mark.slow
def test_a_drain_of_thousands_of_faces_against_hundreds_of_piles_is_quick():
    """The owner's index, 2026-10-20: 7,753 unmatched faces, 614 piles. The
    loop took 90 s and blocked the writer; the batch is well under one."""
    import time
    rng = np.random.default_rng(1)
    centroids = {pid: rng.normal(size=512).astype(np.float32) for pid in range(614)}
    faces = [(i, fc.to_bytes(rng.normal(size=512))) for i in range(32)]
    started = time.perf_counter()
    for _ in range(243):            # 7,753 faces in drain batches of 32
        fc.cluster_batch(faces, centroids)
    assert time.perf_counter() - started < 5.0
