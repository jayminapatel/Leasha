r"""Performance floors: the category the 2026-08-26 review said was missing.

Its closing point, and it is right: **H9 and H6 survived because nothing timed
anything.** The chunker was quadratic - 80,000 words took 73 seconds - and the
keyword path degraded with corpus size, and both passed every test in the suite
the whole time, because every test asked whether the answer was correct and
none asked how long it took to arrive.

**These are floors, not benchmarks.** Each one is set several times looser than
the measurement it was written against, so it fails on a regression of the kind
that has actually happened here - orders of magnitude, algorithmic - and not on
a slow morning in CI or a machine a third the speed of this one. A performance
test that fails intermittently gets deleted, and then the floor is gone.

Each floor records what it was measured at, on what, so the next person can
tell a real regression from a slower machine.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.storage.sqlite_store import SqliteStore

#: How much slack a floor gets over its measured value.
#:
#: 10x. The regressions this category exists for were 500x (the chunker) and
#: 4x (the keyword path), and the machines this runs on differ by perhaps 3x.
SLACK = 10


def elapsed_ms(work) -> float:
    """Best of three. The worst run on a shared machine says nothing."""
    runs = []
    for _ in range(3):
        started = time.perf_counter()
        work()
        runs.append((time.perf_counter() - started) * 1000)
    return min(runs)


# --- the chunker: H9 was 500x, and quadratic ---------------------------------

#: Measured 2026-08-27: **219ms** for 100,000 words, on the Linux container
#: this suite runs in. Before H9's fix the same input took minutes - the fault
#: was a `str.find` from the start of the document once per word, so the cost
#: grew with the square of the length and 80,000 words took 73 seconds.
CHUNKER_FLOOR_MS = 220 * SLACK


@pytest.mark.slow
def test_a_hundred_thousand_words_chunk_in_under_two_seconds() -> None:
    """The review asked for exactly this floor, and named the number.

    A document of a hundred thousand words is a book, a long transcript, or a
    year of one log file - all of which are in the corpus this is built for.
    """
    from app.extract.base import DocumentBuilder
    from app.extract.chunker import chunk_document

    words = " ".join(f"word{n % 997}" for n in range(100_000))
    builder = DocumentBuilder(Path("C:/docs/long.txt"))
    builder.add(words)
    document = builder.build()

    took = elapsed_ms(lambda: chunk_document(document))
    assert took < CHUNKER_FLOOR_MS, (
        f"chunking 100,000 words took {took:.0f}ms against a {CHUNKER_FLOOR_MS}ms "
        f"floor. The last time this got slow it was quadratic - see H9 and "
        f"`_atomise`."
    )


# --- the keyword path: H6, and the floor it was fixed to ---------------------

#: Measured 2026-08-27 on the fixture below - 20,000 chunks over 2,000 files:
#: **6.3ms** for a three-word query with H6's AND-first-then-widen path. H6's
#: own benchmark, on its corpus, was 21.2ms before the fix and 5.4ms after.
KEYWORD_FLOOR_MS = 7 * SLACK


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> SqliteStore:
    """Twenty thousand chunks. Small enough to build in a test, big enough that
    a linear scan is visible against an index seek."""
    path = tmp_path_factory.mktemp("perf") / "corpus.db"
    store = SqliteStore(path).connect()
    vocabulary = [f"term{n:04d}" for n in range(400)]
    for file_number in range(2_000):
        file_id = store.upsert_file(
            f"C:/corpus/doc{file_number:05d}.txt", size_bytes=1_000,
            mtime_ns=file_number, status="INDEXED", source_kind="file",
        )
        store.replace_chunks(file_id, [
            {
                "ordinal": ordinal,
                "text": " ".join(
                    vocabulary[(file_number * 7 + ordinal * 13 + offset) % 400]
                    for offset in range(40)
                ),
                "char_start": 0,
                "char_end": 200,
            }
            for ordinal in range(10)
        ])
    yield store
    store.close()


@pytest.mark.slow
def test_a_three_word_query_stays_off_the_linear_path(corpus: SqliteStore) -> None:
    """H6: the keyword path ORed every term and then ranked the union.

    The fix tries AND first and only widens when AND does not fill a page. The
    failure mode this floor watches for is that widening becoming the default
    again - which would not change a single answer, and would not fail any
    other test in this suite.
    """
    from app.search.keyword import search
    from app.search.query import parse_query

    parsed = parse_query("term0001 term0002 term0003")
    took = elapsed_ms(lambda: search(corpus, parsed, limit=20))
    assert took < KEYWORD_FLOOR_MS, (
        f"a three-word keyword search took {took:.1f}ms against a "
        f"{KEYWORD_FLOOR_MS}ms floor on 20,000 chunks. See H6: AND first, "
        f"widen to OR only when the page is not full."
    )


@pytest.mark.slow
def test_the_filename_lookup_stays_fast_enough_for_every_keystroke(
    corpus: SqliteStore
) -> None:
    """It runs on every character typed into the Files box, so it has a budget
    whether or not anybody wrote one down."""
    #: Measured at 0.4ms on this fixture. The floor is 20ms times the slack,
    #: because the number that matters is "not a scan", not "0.4".
    took = elapsed_ms(lambda: corpus.search_files_by_name("doc012", limit=100))
    assert took < 20 * SLACK, (
        f"a filename lookup took {took:.1f}ms; it runs per keystroke")


# --- the store's own counters ------------------------------------------------


@pytest.mark.slow
def test_asking_whether_anything_is_indexed_does_not_count_anything(
    corpus: SqliteStore
) -> None:
    """`has_any_files()` exists because `stats()` was being used for this.

    `stats()` is three COUNT(*), two of them over `chunks`: 93ms measured on a
    two-million-chunk fixture, so around 460ms at the ten million this is
    designed for, and it was running while a tab was drawn. This asserts the
    shape of the difference rather than a wall-clock number, because on a
    20,000-chunk fixture both are fast and only one of them stays that way.
    """
    cheap = elapsed_ms(corpus.has_any_files)
    counted = elapsed_ms(corpus.stats)
    assert cheap < counted, (
        "has_any_files() is not cheaper than stats() - if it has become a "
        "count, the reason it exists is gone")
    # 0.003ms against stats() at 4.4ms on this fixture, and the gap widens
    # with the index: stats() scans, this stops at the first row.
    assert cheap < 5, f"an existence check took {cheap:.2f}ms"
