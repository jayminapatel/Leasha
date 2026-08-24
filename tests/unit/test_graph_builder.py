"""Layer 6 — the build job, against a real SQLite index.

These use `SqliteStore` rather than a fake, because most of what could go wrong
here *is* the SQL: the atomic cursor, the accumulating edge weights, the counts
recomputed rather than incremented. A fake store would pass every one of these
tests while the real one double-counted.
"""

from __future__ import annotations

import pytest

from app.graph.builder import GraphBuilder
from app.storage.sqlite_store import SqliteStore

TEXTS = [
    "Acme Water Ltd completed the HACCP review at Barnsley Dairy.",
    "The HACCP review at Barnsley Dairy was signed by Jenny Okonkwo of Acme Water Ltd.",
    "SCADA and MES at Barnsley Dairy. Acme Water Ltd installed the pasteuriser.",
    "Jenny Okonkwo raised HACCP findings for Barnsley Dairy with Acme Water Ltd.",
    "Northgate Systems delivered the MES at Leeds Bakery with SCADA integration.",
    "Leeds Bakery OEE reporting came from the MES built by Northgate Systems.",
]


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        for ordinal, text in enumerate(TEXTS):
            file_id = opened.upsert_file(
                f"D:/docs/doc{ordinal}.txt",
                size_bytes=len(text), mtime_ns=1, ext="txt",
                parent_dir="D:/docs", source_kind="file",
            )
            opened.replace_chunks(file_id, [{
                "ordinal": 0, "text": text,
                "char_start": 0, "char_end": len(text), "page": None,
            }])
        yield opened


def labels(store) -> dict[str, int]:
    return {row["display"]: row["id"] for row in store.top_entities(500)}


# -- the schema --------------------------------------------------------------

def test_a_fresh_index_is_at_the_current_schema_version(store):
    from app.storage.migrations import CURRENT_VERSION

    # Against the constant, not a literal: a new migration should not require
    # editing an unrelated test, and a mismatch here means schema.sql and the
    # migration list have drifted apart.
    assert store.schema_version == CURRENT_VERSION


def test_the_graph_migration_leaves_existing_data_alone(tmp_path):
    """v2 -> v3 must never cost a re-index. On 100GB that would not get run."""
    db = tmp_path / "old.db"
    with SqliteStore(db) as opened:
        file_id = opened.upsert_file(
            "D:/a.txt", size_bytes=1, mtime_ns=1, ext="txt",
            parent_dir="D:/", source_kind="file",
        )
        opened.replace_chunks(file_id, [{"ordinal": 0, "text": "hello", "char_start": 0,
                                         "char_end": 5, "page": None}])
        opened.conn.execute("UPDATE schema_version SET version = 2 WHERE id = 1")
        opened.conn.execute("DROP TABLE entity_edges")
        opened.conn.execute("DROP TABLE entity_mentions")
        opened.conn.execute("DROP TABLE entities")
        opened.conn.commit()

    with SqliteStore(db) as reopened:
        from app.storage.migrations import CURRENT_VERSION

        assert reopened.schema_version == CURRENT_VERSION
        assert reopened.stats()["chunks_total"] == 1, "migration must not drop chunks"
        assert reopened.graph_stats()["entities"] == 0


# -- building ----------------------------------------------------------------

def test_build_finds_the_entities_and_connects_them(store):
    result = GraphBuilder(store).build()

    assert result.chunks_processed == len(TEXTS)
    assert not result.interrupted
    found = labels(store)
    assert "Barnsley Dairy" in found
    assert "Acme Water Ltd" in found
    assert store.graph_stats()["edges"] > 0


def test_edges_are_stored_one_way_round_only(store):
    GraphBuilder(store).build()
    for row in store.conn.execute("SELECT a_id, b_id FROM entity_edges"):
        assert row["a_id"] < row["b_id"]


def test_two_builds_of_the_same_corpus_agree(tmp_path):
    """Determinism is what makes the LLM pass's effect visible - and reversible."""
    def build(name: str) -> set[tuple[str, str, int]]:
        with SqliteStore(tmp_path / name) as opened:
            for ordinal, text in enumerate(TEXTS):
                file_id = opened.upsert_file(
                    f"D:/docs/doc{ordinal}.txt", size_bytes=len(text), mtime_ns=1,
                    ext="txt", parent_dir="D:/docs", source_kind="file",
                )
                opened.replace_chunks(file_id, [{"ordinal": 0, "text": text, "char_start": 0,
                                                 "char_end": len(text), "page": None}])
            GraphBuilder(opened).build()
            names = {row["id"]: row["display"] for row in opened.top_entities(500)}
            return {
                (names[row["a_id"]], names[row["b_id"]], row["weight"])
                for row in opened.conn.execute("SELECT a_id, b_id, weight FROM entity_edges")
            }

    assert build("one.db") == build("two.db")


def test_an_unrelated_document_does_not_join_the_graph(store):
    text = "Cardiff weather notes, nothing to do with anything else."
    file_id = store.upsert_file("D:/docs/odd.txt", size_bytes=len(text), mtime_ns=1,
                                ext="txt", parent_dir="D:/docs", source_kind="file")
    store.replace_chunks(file_id, [{"ordinal": 0, "text": text, "char_start": 0,
                                    "char_end": len(text), "page": None}])
    GraphBuilder(store).build()
    assert "Cardiff" not in labels(store), "a single mention is not a relationship"


# -- resumability, which is the part that can silently corrupt ---------------

def test_stopping_and_resuming_does_not_double_count_a_single_edge(store):
    """The failure this guards raises nothing and shows up only as wrong numbers.

    Edge weights accumulate. If a batch were replayed after an interrupt, every
    pair in it would be counted twice - a graph that is quietly more wrong the
    more often it was interrupted, with no error and no way to detect it after
    the fact.
    """
    interrupted = GraphBuilder(store, batch_chunks=2)
    interrupted.request_stop()
    first = interrupted.build()
    assert first.interrupted
    assert first.chunks_processed < len(TEXTS)

    weights_after_stop = dict(
        ((row["a_id"], row["b_id"]), row["weight"])
        for row in store.conn.execute("SELECT a_id, b_id, weight FROM entity_edges")
    )

    finished = GraphBuilder(store, batch_chunks=2).build()
    assert not finished.interrupted
    assert finished.chunks_processed == len(TEXTS) - first.chunks_processed

    for key, weight in weights_after_stop.items():
        row = store.conn.execute(
            "SELECT weight FROM entity_edges WHERE a_id = ? AND b_id = ?", key
        ).fetchone()
        if row is not None:
            assert row["weight"] >= weight
            assert row["weight"] < weight * 2 or weight == 1


def test_a_resumed_build_matches_an_uninterrupted_one(tmp_path):
    """The strongest statement available: interruption changes nothing at all."""
    def seed(name):
        opened = SqliteStore(tmp_path / name).connect()
        for ordinal, text in enumerate(TEXTS):
            file_id = opened.upsert_file(
                f"D:/docs/doc{ordinal}.txt", size_bytes=len(text), mtime_ns=1,
                ext="txt", parent_dir="D:/docs", source_kind="file",
            )
            opened.replace_chunks(file_id, [{"ordinal": 0, "text": text, "char_start": 0,
                                             "char_end": len(text), "page": None}])
        return opened

    straight = seed("straight.db")
    GraphBuilder(straight, batch_chunks=2).build()

    broken = seed("broken.db")
    for _ in range(6):  # stop after every batch until it finishes
        builder = GraphBuilder(broken, batch_chunks=2)
        builder.request_stop()
        if not builder.build().interrupted:
            break
    GraphBuilder(broken, batch_chunks=2).build()

    def signature(opened):
        names = {row["id"]: row["display"] for row in opened.top_entities(500)}
        return sorted(
            (names[row["a_id"]], names[row["b_id"]], row["weight"])
            for row in opened.conn.execute("SELECT a_id, b_id, weight FROM entity_edges")
        )

    assert signature(broken) == signature(straight)
    straight.close()
    broken.close()


def test_an_interrupted_build_does_not_pretend_to_be_scored(store):
    """Scores from half a corpus look finished, which is worse than no scores."""
    builder = GraphBuilder(store, batch_chunks=2)
    builder.request_stop()
    builder.build()
    unscored = store.conn.execute(
        "SELECT COUNT(*) AS n FROM entity_edges WHERE pmi IS NULL"
    ).fetchone()["n"]
    assert unscored > 0


def test_rebuild_starts_from_nothing(store):
    GraphBuilder(store).build()
    before = store.graph_stats()["entities"]
    GraphBuilder(store).build(rebuild=True)
    assert store.graph_stats()["entities"] == before
    assert int(store.get_state("graph:cursor")) > 0


# -- counts and scoring ------------------------------------------------------

def test_counts_are_derived_from_the_mentions_they_describe(store):
    GraphBuilder(store).build()
    for row in store.conn.execute(
        "SELECT id, mentions, chunk_count, doc_count FROM entities"
    ):
        actual = store.conn.execute(
            "SELECT COUNT(*) AS chunks, COUNT(DISTINCT file_id) AS docs, SUM(count) AS total "
            "FROM entity_mentions WHERE entity_id = ?", (row["id"],)
        ).fetchone()
        assert row["chunk_count"] == actual["chunks"]
        assert row["doc_count"] == actual["docs"]
        assert row["mentions"] == actual["total"]


def test_pmi_is_computed_against_the_whole_graph_not_one_run(store):
    """On a resumed build the two differ, and using the wrong one skews everything."""
    builder = GraphBuilder(store, batch_chunks=2)
    builder.request_stop()
    builder.build()
    partial_total = store.graph_chunk_total()

    GraphBuilder(store, batch_chunks=2).build()
    assert store.graph_chunk_total() > partial_total


def test_every_surviving_edge_is_scored(store):
    GraphBuilder(store).build()
    unscored = store.conn.execute(
        "SELECT COUNT(*) AS n FROM entity_edges WHERE pmi IS NULL"
    ).fetchone()["n"]
    assert unscored == 0


def test_pruning_leaves_no_orphan_entities(store):
    GraphBuilder(store).build()
    orphans = store.conn.execute("""
        SELECT COUNT(*) AS n FROM entities
        WHERE id NOT IN (SELECT a_id FROM entity_edges)
          AND id NOT IN (SELECT b_id FROM entity_edges)
    """).fetchone()["n"]
    assert orphans == 0


def test_the_evidence_behind_a_node_can_always_be_shown(store):
    """A node nobody can trace to a passage is an assertion, not a finding."""
    GraphBuilder(store).build()
    for entity in store.top_entities(10):
        passages = store.chunks_mentioning(entity["id"], 5)
        assert passages, f"{entity['display']} has no evidence"
        assert all(entity["display"].casefold() in row["text"].casefold() for row in passages)


def test_one_chunk_cannot_flood_the_graph(store):
    """A contact list or an email footer is quadratic in names."""
    crowd = ". ".join(f"Person Number{n} Smith" for n in range(60))
    file_id = store.upsert_file("D:/docs/list.txt", size_bytes=len(crowd), mtime_ns=1,
                                ext="txt", parent_dir="D:/docs", source_kind="file")
    store.replace_chunks(file_id, [{"ordinal": 0, "text": crowd, "char_start": 0,
                                    "char_end": len(crowd), "page": None}])
    GraphBuilder(store, max_entities_per_chunk=10).build()
    # 10 entities is at most 45 pairs from that chunk, not 60*59/2 = 1,770.
    assert store.graph_stats()["edges"] < 200


def test_clearing_the_graph_leaves_the_index_untouched(store):
    GraphBuilder(store).build()
    store.clear_graph()
    assert store.graph_stats()["entities"] == 0
    assert store.stats()["chunks_total"] == len(TEXTS)


def test_a_deleted_file_takes_its_mentions_with_it(store):
    """The graph is derived; a chunk that no longer exists must not linger in it."""
    GraphBuilder(store).build()
    before = store.conn.execute("SELECT COUNT(*) AS n FROM entity_mentions").fetchone()["n"]
    file_id = store.get_file("D:/docs/doc0.txt").id
    store.delete_file(file_id)
    after = store.conn.execute("SELECT COUNT(*) AS n FROM entity_mentions").fetchone()["n"]
    assert after < before


def test_building_over_an_empty_index_is_not_an_error(tmp_path):
    with SqliteStore(tmp_path / "empty.db") as opened:
        result = GraphBuilder(opened).build()
        assert result.chunks_processed == 0
        assert opened.graph_stats()["entities"] == 0


# ---------------------------------------------------------------------------
# Merging a short name into a long one it never appears without.
#
# The real corpus showed "AVEVA Group" and "AVEVA Group Limited" as two nodes,
# joined to each other and to all the same neighbours. They are one company.
# The tests that matter here are the ones where it must NOT merge.
# ---------------------------------------------------------------------------

def seed_entities(store, entities: dict[str, list[int]]) -> dict[str, int]:
    """`{display: [chunk ids]}` -> written entities and mentions."""
    file_id = store.get_file("D:/docs/doc0.txt").id
    ids = store.commit_graph_batch(
        entities=[(name.casefold(), name, "name", "cooccurrence") for name in entities],
        mentions=[
            (name.casefold(), chunk_id, file_id, 1)
            for name, chunks in entities.items()
            for chunk_id in chunks
        ],
        pairs={},
        cursor=0,
    )
    store.recount_entities()
    return ids


def displays_in(store) -> set[str]:
    return {row["display"] for row in store.top_entities(500)}


def chunk_ids(store) -> list[int]:
    return [int(row["id"]) for row in store.conn.execute("SELECT id FROM chunks ORDER BY id")]


def test_a_short_name_never_used_alone_is_folded_into_the_long_one(store):
    chunks = chunk_ids(store)
    seed_entities(store, {
        "AVEVA Group": chunks[:3],
        "AVEVA Group Limited": chunks[:3],
    })

    assert store.merge_contained_entities() == 1

    remaining = displays_in(store)
    assert "AVEVA Group Limited" in remaining
    assert "AVEVA Group" not in remaining


def test_the_survivor_keeps_the_evidence_from_both(store):
    """A merge that loses mentions makes the node less traceable, not more."""
    chunks = chunk_ids(store)
    seed_entities(store, {
        "AVEVA Group": chunks[:3],
        "AVEVA Group Limited": chunks[:3],
    })
    store.merge_contained_entities()
    store.recount_entities()

    survivor = next(row for row in store.top_entities(50) if row["display"] == "AVEVA Group Limited")
    assert survivor["chunk_count"] == 3
    assert len(store.chunks_mentioning(survivor["id"], 10)) == 3


def test_a_short_name_used_on_its_own_anywhere_is_left_alone(store):
    """This is the case that makes the rule safe.

    "AVEVA" is a prefix of "AVEVA Group Limited" and is a more important entity
    in its own right. A textual prefix test would have destroyed it.
    """
    chunks = chunk_ids(store)
    seed_entities(store, {
        "AVEVA": chunks,                    # everywhere
        "AVEVA Group Limited": chunks[:2],  # only twice
    })

    store.merge_contained_entities()

    assert "AVEVA" in displays_in(store)
    assert "AVEVA Group Limited" in displays_in(store)


def test_a_substring_that_is_not_a_whole_word_is_not_containment(store):
    """"PI" is a substring of "PIPELINE" and that means nothing whatsoever."""
    chunks = chunk_ids(store)
    seed_entities(store, {
        "PI": chunks[:2],
        "PIPELINE DESIGN": chunks[:2],
    })

    assert store.merge_contained_entities() == 0
    assert {"PI", "PIPELINE DESIGN"} <= displays_in(store)


def test_two_unrelated_names_are_never_merged(store):
    chunks = chunk_ids(store)
    seed_entities(store, {
        "Barnsley Dairy": chunks[:2],
        "Leeds Bakery": chunks[:2],
    })

    assert store.merge_contained_entities() == 0


def test_the_build_reports_how_many_it_merged(store):
    result = GraphBuilder(store).build()
    assert "entities_merged" in result.as_dict()


def test_merging_runs_before_scoring_so_pmi_sees_the_merged_counts(store):
    """A survivor scored against half its own evidence is scored wrongly.

    Every PMI in the graph shifts if the merge happens afterwards, and all of
    them shift in the same direction - so nothing looks broken.
    """
    GraphBuilder(store).build()
    unscored = store.conn.execute(
        "SELECT COUNT(*) AS n FROM entity_edges WHERE pmi IS NULL"
    ).fetchone()["n"]
    assert unscored == 0
    for row in store.conn.execute("SELECT id, chunk_count FROM entities"):
        actual = store.conn.execute(
            "SELECT COUNT(*) AS n FROM entity_mentions WHERE entity_id = ?", (row["id"],)
        ).fetchone()["n"]
        assert row["chunk_count"] == actual


def test_merging_stays_affordable_on_a_large_entity_table(store):
    """The naive version does not finish on a real corpus.

    All-against-all containment is O(n^2) with a SQL query per surviving pair.
    On 200,000 emails the entity table runs to tens of thousands of rows -
    billions of comparisons - and the symptom is an index run that appears to
    hang at the very end, which is the hardest kind of failure to diagnose.

    Bucketing by first word makes it tractable. This drives 4,000 entities
    through it and asserts it completes quickly; the naive version would take
    minutes here and hours on the real thing.
    """
    import time

    chunks = chunk_ids(store)
    seed_entities(store, {
        f"Term{n} Something Long": chunks[: (n % 3) + 1] for n in range(4_000)
    })

    started = time.monotonic()
    store.merge_contained_entities()
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"merging 4,000 entities took {elapsed:.1f}s"


def test_a_pathological_first_word_bucket_is_skipped_rather_than_ground_through(store):
    """One word beginning thousands of names must not cost the run.

    Skipping costs a handful of merges. Not skipping costs the index.
    """
    from app.storage.sqlite_store import MERGE_BUCKET_LIMIT

    chunks = chunk_ids(store)
    seed_entities(store, {
        f"Common Prefix Number{n}": chunks[:1] for n in range(MERGE_BUCKET_LIMIT + 50)
    })

    import time
    started = time.monotonic()
    store.merge_contained_entities()
    assert time.monotonic() - started < 10


def test_containment_still_merges_within_a_normal_bucket(store):
    """The cap must not have quietly disabled the feature."""
    chunks = chunk_ids(store)
    seed_entities(store, {
        "AVEVA Group": chunks[:3],
        "AVEVA Group Limited": chunks[:3],
    })

    assert store.merge_contained_entities() == 1
    assert "AVEVA Group" not in displays_in(store)
