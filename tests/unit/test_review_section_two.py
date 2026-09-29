r"""Section 2 of the review-remediation order: before the next scale run.

Layer: L1, L2 and L3

`docs/WORKORDER-202626082352-review-remediation.md` §2. Everything here is a
fault that a small corpus cannot show you: a quadratic loop that is invisible at
20,000 words and fatal at 80,000, a prune that is fine for ten files and
pathological for ten thousand, a memory profile that only matters at five
million paths.

**Which is exactly why they need tests that state the scale.** The quadratic
chunker survived every release because nothing in the suite timed anything, and
the archive prune had never been pointed at an archive that was deleted. A test
of the mechanism would have passed in both cases.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# H9 - the chunker is linear
# ---------------------------------------------------------------------------

#: Words. Above the 80,000 the review measured at 73 seconds, so a regression to
#: the quadratic scan cannot hide under the budget.
PERF_WORDS = 100_000

#: Seconds. Generous - the fixed version does this in about 0.2s here - because
#: a perf floor that fails on a slow or loaded machine gets deleted, and a floor
#: nobody trusts protects nothing. Two orders of magnitude below the fault it
#: guards is enough to catch it.
PERF_BUDGET_S = 2.0


def test_chunking_a_hundred_thousand_words_is_not_quadratic():
    r"""**The measurement, as a test, because that is what was missing.**

    `_atomise` asked `any(... for position in paragraph_starts)` per word - a
    scan of every paragraph in the document for every word in it. Measured by
    the review: 20k words 4.2s, 80k words 73s. A 1MB log spent minutes here and
    a 10MB one hung the extraction worker.

    Sorted starts plus `bisect_right` makes it O(W log P).
    """
    from app.extract.chunker import chunk_text

    text = " ".join(
        f"word{n}" + ("\n\n" if n % 40 == 0 else "") for n in range(PERF_WORDS))

    # **This thread's processor time, not the wall clock.** The question is how
    # much work the chunker does, and only `thread_time` answers it alone. The
    # wall clock also counts every moment this thread waited for Python's lock
    # while another thread ran - and by this point in a whole-suite run, earlier
    # GUI tests have left thread-pool workers behind. On the Windows CI one run
    # measured 3.0s where the same code takes 0.24s here and passed there on the
    # run before (2026-09-29): not quadratic, which would be ~100s, but waiting.
    # The budget is unchanged; a quadratic scan still misses it fifty-fold.
    started = time.thread_time()
    chunks = chunk_text(text)
    elapsed = time.thread_time() - started

    assert chunks, "produced no chunks at all"
    assert elapsed < PERF_BUDGET_S, (
        f"{PERF_WORDS:,} words took {elapsed:.1f}s, budget {PERF_BUDGET_S}s - "
        f"the paragraph scan is quadratic again")


def test_paragraph_boundaries_are_still_found():
    """Speed is worthless if the answer changed. The bisect must agree with the
    scan it replaced about where paragraphs begin."""
    from app.extract.chunker import _atomise, _BREAK_PARAGRAPH

    text = "alpha beta\n\ngamma delta\n\nepsilon"
    atoms = _atomise(text, lambda word: 1.0)
    starts = [atom.start for atom in atoms if atom.break_level == _BREAK_PARAGRAPH]

    assert starts == [text.index("alpha"), text.index("gamma"),
                      text.index("epsilon")]


# ---------------------------------------------------------------------------
# H7 / H8 - pruning
# ---------------------------------------------------------------------------

class _CountingVectors:
    """Counts `delete_by_file_ids` calls - the LanceDB version count, in effect."""

    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    def delete_by_file_ids(self, ids):
        self.calls.append(list(ids))


def _pipeline(tmp_path, vectors=None, **overrides):
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[tmp_path]), **overrides)
    built = Pipeline(store=store, vectors=vectors or _CountingVectors(),
                     embedder=None, config=config)
    return store, built


def test_pruning_many_files_is_a_handful_of_deletes_not_one_each(tmp_path):
    r"""**H7.** One `delete_by_file_ids([file_id])` per file is one LanceDB
    dataset version per file - deleting a 10,000-file folder produced 10,000
    versions, the exact fragmentation `COMPACT_EVERY_ROWS` exists to prevent -
    plus one SQLite write transaction each.
    """
    from app.index.pipeline import Pipeline

    vectors = _CountingVectors()
    store, pipeline = _pipeline(tmp_path, vectors=vectors)
    try:
        doomed = []
        for number in range(Pipeline.PRUNE_BATCH * 2 + 5):
            doomed.append(store.upsert_file(
                rf"D:\gone\file{number}.txt", size_bytes=1, mtime_ns=1,
                ext="txt", parent_dir=r"D:\gone"))

        removed = pipeline._delete_in_batches(doomed)

        assert removed == len(doomed)
        assert len(vectors.calls) == 3, (
            f"{len(doomed):,} files took {len(vectors.calls)} vector deletes - "
            f"one Lance version each is the fault being fixed")
        assert store.get_file(r"D:\gone\file0.txt") is None
    finally:
        store.close()


def test_a_deleted_archive_takes_its_messages_with_it(tmp_path):
    r"""**H8.** `_prune_missing` only ever looked at `source_kind="file"`, and
    no other deletion path exists for archive rows - so deleting a 30GB `.pst`
    removed nothing, and 200,000 messages stayed searchable for ever, every one
    of them opening to a file that is not there.
    """
    store, pipeline = _pipeline(tmp_path)
    try:
        archive = tmp_path / "old.pst"        # deliberately never created
        marker = store.upsert_file(str(archive), size_bytes=10, mtime_ns=1,
                                   ext="pst", parent_dir=str(tmp_path),
                                   source_kind="archive")
        member = store.upsert_file(f"{archive}/message-1", size_bytes=1,
                                   mtime_ns=1, ext="", parent_dir=str(archive),
                                   source_kind="archive")

        doomed = pipeline._doomed_inside_archives(seen=set(), archived=None)

        assert set(doomed) == {marker, member}, (
            "a deleted archive must take its contents with it")
    finally:
        store.close()


def test_a_live_archive_keeps_its_members(tmp_path):
    r"""**The bug the first version of H8 introduced, caught by the suite.**

    `source_kind="archive"` is not "the archive file" - it is every row that
    came *out* of one, the container's marker and each member alike. Treating
    them all as containers found that `backup.zip/q3/plan.dwg` is not a path on
    disk and deleted the members of perfectly healthy archives.
    """
    store, pipeline = _pipeline(tmp_path)
    try:
        archive = tmp_path / "live.zip"
        archive.write_bytes(b"PK\x03\x04")     # a real file this time
        store.upsert_file(str(archive), size_bytes=4, mtime_ns=1, ext="zip",
                          parent_dir=str(tmp_path), source_kind="archive")
        store.upsert_file(f"{archive}/q3/plan.dwg", size_bytes=1, mtime_ns=1,
                          ext="dwg", parent_dir=str(archive),
                          source_kind="archive")

        assert pipeline._doomed_inside_archives(seen=set(), archived=None) == []
    finally:
        store.close()


def test_an_archive_on_a_folder_that_has_vanished_is_left_alone(tmp_path):
    r"""**One missing `.pst` is 200,000 rows, so the evidence has to be good.**

    A disconnected network drive or an unmounted volume makes every path under
    it stop existing at once. "The archive is not there" and "the whole share is
    not there" look identical to `Path.exists()`, and only one of them is a
    deletion. If the parent folder has gone too, this run knows nothing.
    """
    store, pipeline = _pipeline(tmp_path)
    try:
        # A folder that is deliberately never created, so both it and the
        # archive inside it are missing - which is what an unmounted share
        # looks like. Built from `tmp_path` rather than written as `Z:\...`
        # because a Windows path on POSIX has no separators at all: its parent
        # is `.`, which exists, and the guard would appear to fail.
        offline = tmp_path / "offline-share"
        store.upsert_file(str(offline / "mail.pst"), size_bytes=10,
                          mtime_ns=1, ext="pst", parent_dir=str(offline),
                          source_kind="archive")

        assert pipeline._doomed_inside_archives(seen=set(), archived=None) == [], (
            "pruned an archive whose entire folder is missing - that is an "
            "offline drive, not a deletion")
    finally:
        store.close()


def test_an_archive_this_walk_covered_is_never_pruned(tmp_path):
    """A run restricted to one root must not conclude that an archive it
    deliberately did not look at has been deleted."""
    store, pipeline = _pipeline(tmp_path)
    try:
        archive = tmp_path / "elsewhere.pst"
        store.upsert_file(str(archive), size_bytes=10, mtime_ns=1, ext="pst",
                          parent_dir=str(tmp_path), source_kind="archive")

        covered = {str(archive).lower()}
        assert pipeline._doomed_inside_archives(seen=covered, archived=None) == []
    finally:
        store.close()


def test_a_filename_containing_a_wildcard_only_matches_itself(tmp_path):
    r"""`%` and `_` are ordinary characters in a Windows filename and wildcards
    in `LIKE`. Unescaped, `Q1_2024%.pst` would match - and in a delete, remove -
    rows belonging to entirely unrelated files."""
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(tmp_path / "index.db") as store:
        store.upsert_file(r"D:\mail\Q1_2024%.pst#one", size_bytes=1, mtime_ns=1,
                          ext="", parent_dir=r"D:\mail", source_kind="archive")
        other = store.upsert_file(r"D:\mail\Q1X2024Z.pst#one", size_bytes=1,
                                  mtime_ns=1, ext="", parent_dir=r"D:\mail",
                                  source_kind="archive")

        found = store.file_ids_under_archive(r"D:\mail\Q1_2024%.pst")

        assert other not in found, "a literal % or _ matched an unrelated file"
        assert len(found) == 1


# ---------------------------------------------------------------------------
# M18 - files the walk cannot even look at
# ---------------------------------------------------------------------------

def test_a_path_too_long_for_windows_is_counted_rather_than_vanishing():
    r"""`except OSError: continue` gave no row, no skip, no count, no log line -
    the same silent absence `NAME_ONLY` exists to prevent, arriving through a
    different door. On stock Windows every path over 260 characters lands there.

    The reason matters as much as the count: *"4,812 files could not be read"*
    is not actionable; *"4,812 paths over 260 characters"* names the setting.
    """
    from app.index.walker import WalkConfig, _record_stat_failure

    config = WalkConfig(roots=[])
    long_path = Path("C:/" + "verylongfolder/" * 20 + "file.txt")
    assert len(str(long_path)) >= 260

    _record_stat_failure(config, long_path, OSError(2, "not found"))
    _record_stat_failure(config, Path("C:/short.txt"), PermissionError(13, "denied"))

    assert config.stat_failures["path over 260 characters"] == 1
    assert config.stat_failures["permission denied"] == 1


# ---------------------------------------------------------------------------
# M8 - the index build is off the write path
# ---------------------------------------------------------------------------

def test_writing_vectors_never_triggers_an_index_build():
    r"""Training IVF_PQ is minutes at 6.4M rows and tens of minutes at 12.8M,
    and it ran from `add()` - synchronously, on the consumer thread, with every
    extraction worker blocked behind bounded queues and nothing on screen saying
    a pause had begun. `Pipeline.run` builds the index at the end, where the
    wait is expected."""
    import ast
    import inspect
    import textwrap

    from app.storage.vector_store import VectorStore

    source = textwrap.dedent(inspect.getsource(VectorStore.add))
    called = {
        node.func.attr for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "maybe_create_index" not in called, (
        "add() builds the ANN index again - that blocks every worker behind it")
    assert "maybe_compact" in called, (
        "compaction must stay: it is incremental, and deferring it makes the "
        "rest of the run slower")


# ---------------------------------------------------------------------------
# M16 - PST folders are read one message at a time
# ---------------------------------------------------------------------------

def test_a_pst_folder_is_not_materialised_whole():
    """`list(folder.items())` held every message body in a folder at once, and a
    100,000-message Inbox is the corpus this exists for."""
    import ast
    import inspect

    from app.extract import email_pst

    tree = ast.parse(inspect.getsource(email_pst))
    offenders = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "list"
        and node.args
        and isinstance(node.args[0], ast.Call)
        and getattr(node.args[0].func, "attr", "") == "items"
    ]

    assert not offenders, (
        f"list(...items()) at line(s) {offenders} materialises a whole folder")


def test_a_folder_that_fails_part_way_keeps_what_it_read():
    """A COM enumerator can fail after yielding thousands of messages - Outlook
    closing mid-walk is the ordinary case. Everything read is kept, the folder
    is recorded as busy, and the next incremental pass retries it."""
    from app.extract import email_pst

    class _Folder:
        display_name = "Inbox"

        def items(self):
            yield "one"
            yield "two"
            raise RuntimeError("Outlook went away")

    recorded: list[str] = []
    original = email_pst._record_busy
    email_pst._record_busy = lambda store, folder, exc: recorded.append(str(exc))
    try:
        got = list(email_pst._iter_folder_items(object(), _Folder()))
    finally:
        email_pst._record_busy = original

    assert got == ["one", "two"], "work done before the failure was thrown away"
    assert recorded, "the folder failed and nothing recorded it"


# ---------------------------------------------------------------------------
# H6 - the keyword path, narrowed first and widened only when it must be
# ---------------------------------------------------------------------------

from app.storage.sqlite_store import SqliteStore  # noqa: E402 - used below only


def _keyword_corpus(store):
    """Two terms that do not co-occur, plus one document where they do."""
    for number, text in enumerate((
        "pump station commissioning and valve replacement",   # both
        "pump station commissioning notes",                    # pump only
        "valve replacement schedule",                          # valve only
        "unrelated prose about budgets",                       # neither
    )):
        file_id = store.upsert_file(rf"D:\c\f{number}.txt", size_bytes=10,
                                    mtime_ns=number, ext="txt", parent_dir=r"D:\c")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text, "page": None,
                                        "char_start": 0, "char_end": len(text)}])
        store.mark_indexed(file_id)


def test_a_thin_narrow_query_still_widens_to_or(tmp_path):
    r"""**Recall is not traded for speed, and this is the guard.**

    `AND_TERM_LIMIT = 1` was chosen against twenty real sentences: three left
    six of them returning nothing at all, four left eleven. Trying the AND form
    first is only acceptable because the OR form still runs whenever AND does
    not fill the page - so a description whose words do not all co-occur returns
    exactly what it always did.
    """
    from app.search import keyword
    from app.search.query import parse_query

    with SqliteStore(tmp_path / "index.db") as store:
        _keyword_corpus(store)

        # Both terms appear together in one document, so AND alone finds one -
        # far short of the page - and the wide query has to run.
        rows = keyword.search(store, parse_query("pump valve"), limit=10)

        found = {row["text"] for row in rows}
        assert len(found) >= 3, (
            f"widening did not happen - only {len(found)} document(s) came back, "
            f"so a description that does not all match now finds nothing")


def test_the_narrow_query_is_used_when_it_fills_the_page(tmp_path):
    """The whole point: when the terms genuinely co-occur, the expensive OR is
    never run. Measured on a synthetic corpus, 21.2ms against 5.4ms."""
    from app.search import keyword
    from app.search.query import parse_query

    with SqliteStore(tmp_path / "index.db") as store:
        _keyword_corpus(store)

        rows = keyword.search(store, parse_query("pump valve"), limit=1)

        assert len(rows) == 1
        assert "valve" in rows[0]["text"] and "pump" in rows[0]["text"], (
            "a one-result page should have been answered by the AND form, "
            "which only matches the document containing both")


def test_and_term_limit_was_not_quietly_raised():
    r"""**The value is measured, and the measurement is in its docstring.**

    Raising it is the obvious-looking fix for the latency this section is
    about, and it is the wrong one: three left six of twenty sentences empty,
    four left eleven. If a future change wants a different value it needs new
    numbers, not this test deleted.
    """
    from app.search.query import AND_TERM_LIMIT

    assert AND_TERM_LIMIT == 1, (
        "AND_TERM_LIMIT changed - the recall table in its docstring says what "
        "that costs. Re-measure before believing a new value.")
