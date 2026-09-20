r"""What happens to search once the index has ten million vectors in it.

Layer: L1

From `docs/WORKORDER-terabyte-scale.md` §6. None of these is a bug today: at the
sizes this application has actually run at, every one of them is correct. They
are the three places where a rule that is right at 100,000 vectors stops being
right somewhere between 10 and 20 million - and each of them degrades **quietly**,
which is why they are worth pinning now rather than discovering as "search got
slow" a fortnight into a run.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

from app.storage.vector_store import INDEX_MIN_ROWS, MAX_PARTITIONS, VectorStore


def _store(**over) -> VectorStore:
    store = VectorStore.__new__(VectorStore)       # no LanceDB, no filesystem
    store.dim = 384
    store._indexed_at_rows = 0
    store._warned_partitions = False
    for key, value in over.items():
        setattr(store, key, value)
    return store


# --- the partition cap ------------------------------------------------------

def test_below_the_cap_the_heuristic_is_followed():
    store = _store()

    assert store._partitions_for(1_000_000) == 1_000
    assert store._partitions_for(INDEX_MIN_ROWS) == int(math.sqrt(INDEX_MIN_ROWS))


def test_the_cap_binds_at_about_seventeen_million_vectors():
    r"""`4096**2` is 16.8M, and a 1.5TB corpus reaches it.

    Past that, partitions stop multiplying and start growing: at 50M each holds
    about 12,000 vectors instead of the 7,000 the heuristic asks for, and
    search slows or loses recall in proportion.
    """
    store = _store()

    assert store._partitions_for(MAX_PARTITIONS ** 2) == MAX_PARTITIONS
    assert store._partitions_for(50_000_000) == MAX_PARTITIONS


def test_the_moment_the_cap_starts_binding_is_said_out_loud():
    """**A limit nobody can see is a slow search nobody can explain.**

    The cap is not moved here - §6 asks for latency and recall to be measured
    at 5M, 10M and 20M rows first, and this project has learned what happens to
    numbers chosen without measuring. What it does is stop being silent.
    """
    from app.core.logging import logger

    said: list[str] = []
    sink = logger.add(lambda message: said.append(str(message)), level="WARNING")
    try:
        store = _store()
        store._partitions_for(1_000_000)           # below the cap: nothing said
        assert not said
        store._partitions_for(30_000_000)
    finally:
        logger.remove(sink)

    assert said, "the cap bound and nothing was logged"
    assert "partitions" in said[0]


def test_it_is_said_once_rather_than_on_every_rebuild():
    """A warning repeated on every index build is a warning people filter."""
    from app.core.logging import logger

    said: list[str] = []
    sink = logger.add(lambda message: said.append(str(message)), level="WARNING")
    try:
        store = _store()
        for _ in range(5):
            store._partitions_for(30_000_000)
    finally:
        logger.remove(sink)

    assert len(said) == 1


# --- how often the index is retrained ---------------------------------------

def test_below_the_cap_the_index_is_retrained_on_doubling():
    """While `sqrt` still moves, a rebuild genuinely changes the index's
    shape - doubling the rows asks for 41% more partitions."""
    assert _store()._growth_needed(1_000_000) == 2


def test_above_the_cap_it_slows_down():
    r"""**Because the rebuild has stopped buying what it used to buy.**

    Past the cap the partition count is pinned however many rows arrive, so a
    rebuild only reassigns vectors to the same number of centroids. Worth doing
    as the data drifts; not worth doing at every doubling, and least of all at
    the sizes where a rebuild costs the most and lands mid-run.
    """
    assert _store()._growth_needed(MAX_PARTITIONS ** 2 + 1) == 4


def test_the_rebuild_schedule_is_the_one_that_is_documented():
    """100k, 200k, 400k... to 16.8M, then quarter as often. Spelled out as a
    sequence because "retrain when rows double" is easy to read and easy to
    misjudge the consequences of."""
    rows, builds = INDEX_MIN_ROWS, []
    store = _store()
    while rows < 100_000_000:
        builds.append(rows)
        rows *= store._growth_needed(rows)

    assert builds[:4] == [100_000, 200_000, 400_000, 800_000]
    # Without the slowdown this list would be four entries longer, and the last
    # four are the expensive ones.
    assert len([r for r in builds if r > MAX_PARTITIONS ** 2]) <= 2


# --- the keyword index ------------------------------------------------------

def test_the_fts_index_can_be_merged(tmp_path):
    r"""**Never run in this application until now.**

    `PRAGMA optimize` is called on close and is the query planner's statistics -
    a different thing. FTS5 keeps its own segmented index, one segment per batch
    of inserts, and every query touches all of them.
    """
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(str(tmp_path / "a.txt"), size_bytes=10, mtime_ns=1)
        store.replace_chunks(file_id, [
            {"ordinal": index, "text": f"Barnsley Dairy note {index}",
             "page": None, "char_start": 0, "char_end": 10}
            for index in range(50)
        ])

        assert store.optimize_fts() is True
        # And the index still answers afterwards, which is the only thing a
        # merge could plausibly break.
        assert store.search_bm25("Barnsley", limit=5)


def test_a_merge_that_fails_costs_a_slow_index_not_the_run(tmp_path):
    """At the end of a week-long run, "the keyword index could not be merged"
    must not be the thing that loses it."""
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(tmp_path / "index.db")
    store.connect()
    store.close()                                  # every connection gone

    assert store.optimize_fts() is False


def test_a_small_run_does_not_pay_for_a_merge(tmp_path):
    """The merge rewrites the entire index. After an incremental pass that
    added four chunks that is minutes of pure waste."""
    from app.index.pipeline import FTS_OPTIMIZE_AFTER_CHUNKS, IndexStats, Pipeline

    calls: list[int] = []

    class Store:
        def optimize_fts(self):
            calls.append(1)
            return True

    pipeline = Pipeline.__new__(Pipeline)
    pipeline.store = Store()
    pipeline._log = __import__("app.core.logging", fromlist=["logger"]).logger
    # The merge now also reads `bulk_fts` - `off` never merges, `on` always
    # does, `auto` keeps the threshold this test is about. See index-tuning §6f.
    pipeline.config = SimpleNamespace(bulk_fts="auto")
    # Set by `Pipeline.__init__`, which this stub skips; the merge step now looks at
    # it to decide whether dropped triggers need restoring (index-tuning section 6f).
    pipeline._suspended_fts_triggers = []

    pipeline._optimise_keyword_index(IndexStats(chunks=4))
    assert not calls

    pipeline._optimise_keyword_index(IndexStats(chunks=FTS_OPTIMIZE_AFTER_CHUNKS))
    assert calls == [1]
