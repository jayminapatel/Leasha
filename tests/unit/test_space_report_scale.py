r"""Order 202626270602 (0n) section 3c - the Space Report at scale.

Layer: L4

"Measured on the scale fixture - the H2-lesson applied in advance" was the
item's own wording, and until this file it had never been measured: the
indexes existed, nothing had ever timed the queries. The floors below are the
measurement, pinned with margin, the way `test_perf_floors.py` pins the rest.

**What was measured, and under what** (2026-09-19, the machine this project is
built on: Windows 11 Pro, 12 logical CPUs, CPython 3.12.10, SQLite 3.49.1,
NumPy 2.5, an NVMe temp directory, *while six other engineers were running
test suites on it* - so these are honest loaded-machine numbers, best of
three, and a quiet run was up to 3x faster):

    fixture      200,000 `files` rows, 85,160 distinct content hashes of which
                 38,235 held more than once, 27,037 photographs carrying 12,732
                 distinct pHashes, three catalogued volumes plus "This
                 computer", 57 MB database
    generated_at   45-250 ms   (`MAX(indexed_at)`: a full scan, no index)
    duplicate groups   97-325 ms
    total reclaimable  95-361 ms
    similar photos    565-2,490 ms  (was ~200 s - see below)
    duplication by source   335-1,130 ms
    the only copy     164-650 ms
    the whole snapshot (`_report_snapshot`)   ~2.5 s

**Representative?** Row count, duplicate structure and photo share are
plausible for the stated ~100 GB / low-hundreds-of-thousands-of-files corpus;
they are synthetic, not a copy of the owner's real `D:\Leasha\Data`, which
cannot be reached from here - so treat the row count as representative and the
exact ratios (55% single-copy, 15% photos, 25% on volumes) as assumptions. The
real index also carries chunk/FTS tables the fixture leaves empty. Rebuild it
with `tests/fixtures/space_scale.py`.

**One query WAS too slow, and it was not one of the SQL ones.**
`find_near_duplicate_photo_groups` clustered photos by calling
`phash_distance` once per (photo, cluster) pair in Python. Measured quadratic:
1.2k distinct photo hashes 0.84 s, 3.2k 13 s, so 12.7k hashes ~200 s (a
regression of the H9 kind - correct on every small fixture, minutes at real
size). Fixed in `_cluster_by_phash` with a vectorised XOR + popcount per photo,
same greedy rule, same clusters (`test_the_fast_clustering_...` proves it
against `phash_distance` itself). No index was added: every SQL query reads
`idx_files_content_hash` (`EXPLAIN QUERY PLAN` shows SEARCH ... USING (COVERING)
INDEX) and all finish well inside a second on a quiet machine, so a new index
would be write cost on every index run for a saving nobody would notice.

**The floors are ~10x the loaded measurement**, so they catch an algorithmic
regression (the near-duplicate one was 100x+) and not a slow morning.
"""

from __future__ import annotations

import random
import time

import pytest

from app.reports import space
from app.reports.inheritance import report_generated_at
from app.search.folding import PHASH_NEAR_THRESHOLD, phash_distance
from tests.fixtures.space_scale import build_space_scale_store

FILES = 200_000

#: Per-query floors, milliseconds. Each is ~10x the slowest loaded
#: measurement above, rounded.
QUERY_FLOOR_MS = {
    "generated_at": 3_000,
    "duplicate groups": 4_000,
    "total reclaimable": 4_000,
    "similar photos": 25_000,
    "duplication by source": 12_000,
    "the only copy": 8_000,
}
#: The whole `_report_snapshot`, including rendering the document.
SNAPSHOT_FLOOR_MS = 30_000


def _best_of_three(work) -> float:
    runs = []
    for _ in range(3):
        started = time.perf_counter()
        work()
        runs.append((time.perf_counter() - started) * 1000)
    return min(runs)


@pytest.fixture(scope="module")
def scale_store(tmp_path_factory):
    store, facts = build_space_scale_store(
        tmp_path_factory.mktemp("space_scale") / "index.db", FILES)
    yield store, facts
    store.close()


@pytest.mark.slow
def test_the_fixture_has_the_structure_the_measurement_claims(scale_store) -> None:
    """A floor over a fixture that quietly lost its duplicates measures
    nothing - so the shape the docstring states is asserted, not assumed."""
    store, facts = scale_store
    assert facts["files"] == FILES
    assert store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == FILES
    assert facts["duplicate_hashes"] > 30_000
    assert facts["photo_hashes"] > 10_000
    assert facts["volumes"] == 3
    groups = space.find_duplicate_groups(store)
    assert len(groups) == space.DUPLICATE_GROUPS_SHOWN
    assert space.total_reclaimable_bytes(store) > 0
    assert space.find_near_duplicate_photo_groups(store), "planted variants were not found"


@pytest.mark.slow
@pytest.mark.parametrize("label, query", [
    ("generated_at", report_generated_at),
    ("duplicate groups", space.find_duplicate_groups),
    ("total reclaimable", space.total_reclaimable_bytes),
    ("similar photos", space.find_near_duplicate_photo_groups),
    ("duplication by source", space.find_source_duplicate_share),
    ("the only copy", space.find_source_uniqueness),
])
def test_each_space_report_query_stays_under_its_floor(scale_store, label, query) -> None:
    store, _facts = scale_store
    took = _best_of_three(lambda: query(store))
    floor = QUERY_FLOOR_MS[label]
    assert took < floor, (
        f"{label} took {took:.0f}ms over {FILES:,} files against a {floor}ms floor. "
        f"Similar photos was quadratic once (~200 s at this size); the SQL ones "
        f"read idx_files_content_hash - check EXPLAIN QUERY PLAN first.")


@pytest.mark.slow
def test_the_whole_snapshot_stays_under_its_floor(scale_store) -> None:
    from app.ui.reports_view import _report_snapshot

    store, _facts = scale_store
    took = _best_of_three(lambda: _report_snapshot(store))
    assert took < SNAPSHOT_FLOOR_MS, (
        f"the Space Report snapshot took {took:.0f}ms over {FILES:,} files "
        f"against a {SNAPSHOT_FLOOR_MS}ms floor.")


@pytest.mark.slow
def test_the_indexes_the_queries_rely_on_are_used_not_scanned(scale_store) -> None:
    """The H2 lesson, asserted: the GROUP BY reads the index, it does not scan
    the table. (`generated_at` and the per-source totals do scan - by design,
    and measured above.)"""
    store, _facts = scale_store
    plan = " ".join(str(row[3]) for row in store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT content_hash, size_bytes, COUNT(*) FROM files "
        "WHERE content_hash IS NOT NULL GROUP BY content_hash HAVING COUNT(*) > 1"))
    assert "idx_files_content_hash" in plan, plan


# ---------------------------------------------------------------------------
# The fast clustering is the same rule, not a new one - cheap, not slow.
# ---------------------------------------------------------------------------

def _reference_clusters(rows, threshold):
    """The pair-at-a-time algorithm `_cluster_by_phash` replaced, verbatim."""
    clusters: list = []
    for row in rows:
        match = next((c for c in clusters
                      if phash_distance(str(row["phash"]), str(c[0]["phash"])) <= threshold), None)
        if match is not None:
            match.append(row)
        else:
            clusters.append([row])
    return clusters


def _rows(seed: int) -> list:
    rng = random.Random(seed)
    bases = [f"{rng.getrandbits(64):016x}" for _ in range(120)]
    rows = []
    for _ in range(1500):
        value = int(rng.choice(bases), 16)
        for bit in rng.sample(range(64), rng.randint(0, 24)):
            value ^= 1 << bit
        rows.append({"phash": f"{value:016x}"})
    rows += [{"phash": "not hex"}, {"phash": "f" * 20}, {"phash": "0"}]
    return rows


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_fast_clustering_gives_the_same_clusters_as_phash_distance(seed) -> None:
    rows = _rows(seed)
    fast = space._cluster_by_phash(rows, PHASH_NEAR_THRESHOLD)
    slow = _reference_clusters(rows, PHASH_NEAR_THRESHOLD)
    assert [[id(r) for r in c] for c in fast] == [[id(r) for r in c] for c in slow]


def test_the_clustering_fallback_without_numpy_gives_the_same_clusters(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def refuse_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("numpy hidden for this test")
        return real_import(name, *args, **kwargs)

    rows = _rows(4)
    monkeypatch.setattr(builtins, "__import__", refuse_numpy)
    fallback = space._cluster_by_phash(rows, PHASH_NEAR_THRESHOLD)
    monkeypatch.undo()
    slow = _reference_clusters(rows, PHASH_NEAR_THRESHOLD)
    assert [[id(r) for r in c] for c in fallback] == [[id(r) for r in c] for c in slow]
