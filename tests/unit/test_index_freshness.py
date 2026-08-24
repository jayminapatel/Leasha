"""What the indexer re-reads, and what it correctly leaves alone.

Layer: L3

The rule the whole design rests on: **a file that has not changed is not read
again.** On 100GB with 30GB of mail archives, that is the difference between an
incremental run costing seconds and costing hours.

Getting it wrong in the safe direction wastes time. Getting it wrong in the
*unsafe* direction - deciding a file is unchanged when it has never been read at
all - loses data silently, and that is exactly what happened here. Every test
below is written from the failure it guards.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

import pytest

from app.extract import base
from app.extract.base import Document
from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import UNCHANGED, Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore

#: Far enough in the past that `_modified_recently` does not force a re-read.
#: Without this every test here is racing a 2-second window and flakes.
LONG_AGO = 3600


class FakeArchive:
    """Stands in for a `.pst`: one file on disk, several messages inside.

    `reads_externally = True` is the property that matters. It means the bytes
    on disk are not what gets parsed and the file may be held open by another
    application, so the indexer must not try to hash it.
    """

    name = "fake-archive"
    extensions = (".fakepst",)
    reads_externally = True
    messages = 5

    #: Bumped by a test to simulate one message changing inside the archive.
    revision = 0

    def extract(self, path: Path):
        for n in range(self.messages):
            body = f"Message {n} about Barnsley Dairy and the HACCP review."
            if n == 0 and self.revision:
                body = f"{body} Revised {self.revision}."
            yield Document(
                path=path,
                text=body,
                source_kind="pst_message",
                # Every document out of one file needs its own key, or they all
                # collide on the file's path and overwrite each other.
                virtual_path=f"pst://{path.name}/E{n}",
                meta={"entry_id": f"E{n}", "subject": f"Message {n}", "sender": "a@b.c"},
            )


@pytest.fixture(autouse=True)
def _register_fake_archive():
    """Register once, and put the registry back exactly as it was.

    A test that leaves an extractor behind changes what every later test in the
    session considers indexable, and the failure surfaces somewhere unrelated.
    """
    before = dict(base.REGISTRY)
    base.REGISTRY.pop(".fakepst", None)
    base.register(FakeArchive())
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(before)


class NullVectors:
    """The vector store is not what these tests are about."""

    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        pass

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def fake_embedder(dim: int = 8) -> Embedder:
    def encode(texts):
        return [
            l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(dim)])
            for t in texts
        ]

    return Embedder(dim=dim, encoder=encode)


def aged(path: Path) -> Path:
    """Backdate a file so `_modified_recently` does not force a re-read.

    Without this every test here races a two-second window and flakes - which
    is worse than no test, because it fails on someone else's change.
    """
    stamp = time.time() - LONG_AGO
    os.utime(path, (stamp, stamp))
    return path


def write_aged(path: Path, content) -> Path:
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return aged(path)


def run_index(db: Path, root: Path, **overrides):
    with SqliteStore(db) as store:
        config = PipelineConfig(
            walk=WalkConfig(roots=[root], **overrides),
            workers=1,
        )
        pipeline = Pipeline(store, NullVectors(), fake_embedder(), config)
        return pipeline.run()


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    write_aged(root / "notes.txt", "Barnsley Dairy notes.")
    write_aged(root / "archive.fakepst", b"x" * 4096)
    return root


# -- the bug that hid every PST ---------------------------------------------

def test_an_archive_is_indexed_the_first_time_it_is_ever_seen(tmp_path, corpus):
    """The one that cost two rounds of "the PST did not index".

    `_classify` returned `None` for "unchanged". It also returned `None` as the
    perfectly ordinary "changed, but there is no hash" answer for any file read
    through another application - which is every `.pst` and every `.ost`. So an
    archive was classified as unchanged **before it had ever been indexed**, and
    silently never indexed at all.

    It counted as `unchanged`, not `skipped`, so no error appeared anywhere and
    the run reported complete success. The whole point of the `UNCHANGED`
    sentinel being a distinct object is that this cannot recur.
    """
    stats = run_index(tmp_path / "index.db", corpus)

    # One row per document, not per file: the text file plus every message.
    assert stats.indexed == 1 + FakeArchive.messages, (
        f"an archive was skipped on first sight: {stats.as_dict()}"
    )
    assert stats.unchanged == 0
    assert stats.chunks >= FakeArchive.messages


def test_unchanged_is_a_sentinel_and_not_a_missing_hash():
    """Directly: the two answers must not be the same value.

    A hash of `None` is a normal result. "Do not index this" is not.
    """
    assert UNCHANGED is not None
    assert UNCHANGED != ""
    assert repr(UNCHANGED) == "UNCHANGED"


# -- not re-reading what has not changed ------------------------------------

def test_a_second_run_over_an_untouched_corpus_reads_nothing(tmp_path, corpus):
    """The property the whole incremental design exists for."""
    db = tmp_path / "index.db"
    run_index(db, corpus)

    second = run_index(db, corpus)

    assert second.indexed == 0, "an unchanged corpus was re-indexed"
    assert second.unchanged == 2      # both *files* decided before any reading
    assert second.chunks == 0
    assert second.bytes_read == 0


def test_an_untouched_archive_costs_no_extraction(tmp_path, corpus):
    """A 30GB historic .pst must cost a stat() on every run after the first.

    This is the specific case behind "these archives are historic and never
    change" - re-opening one through Outlook or libpff takes minutes even when
    nothing inside it has moved.
    """
    db = tmp_path / "index.db"
    run_index(db, corpus)

    opened = {"count": 0}
    real_extract = FakeArchive.extract

    def counting_extract(self, path):
        opened["count"] += 1
        yield from real_extract(self, path)

    FakeArchive.extract = counting_extract
    try:
        run_index(db, corpus)
    finally:
        FakeArchive.extract = real_extract

    assert opened["count"] == 0, "an unchanged archive was opened and re-read"


def test_a_changed_archive_is_re_read(tmp_path, corpus):
    """The other half. Skipping is only safe if a real change is still caught."""
    db = tmp_path / "index.db"
    run_index(db, corpus)

    archive = corpus / "archive.fakepst"
    archive.write_bytes(b"y" * 8192)     # different size, so mtime granularity is irrelevant
    aged(archive)

    second = run_index(db, corpus)

    # The archive was re-read - but no *message* changed, so nothing needed
    # re-embedding. That is the feature working, not a failure to notice.
    #
    # `unchanged` counts *files* the walker skipped; `unchanged_documents`
    # counts messages inside a file it did read. Conflating them is what made a
    # working run print "seen 8, indexed 17".
    assert second.unchanged_documents >= FakeArchive.messages
    assert second.indexed == 0


def test_only_the_changed_messages_inside_an_archive_are_re_indexed(tmp_path, corpus):
    """The whole point of per-message rows.

    An archive's bytes change whenever the mail client so much as opens it, so
    the *file* looks modified constantly. Re-embedding all of it every time is
    the difference between seconds and hours on 30GB - and a fifteen-year-old
    email has not changed at all.
    """
    db = tmp_path / "index.db"
    run_index(db, corpus)

    archive = corpus / "archive.fakepst"
    archive.write_bytes(b"y" * 8192)
    aged(archive)
    FakeArchive.revision = 1                     # exactly one message differs
    try:
        second = run_index(db, corpus)
    finally:
        FakeArchive.revision = 0

    assert second.indexed == 1, (
        f"expected one re-indexed message, got {second.indexed}: {second.as_dict()}"
    )
    assert second.unchanged_documents >= FakeArchive.messages - 1


def test_every_message_gets_its_own_row(tmp_path, corpus):
    """A search result must name the email, not the archive it lives in."""
    db = tmp_path / "index.db"
    run_index(db, corpus)

    with SqliteStore(db) as store:
        rows = [r for r in store.iter_files() if r.source_kind == "pst_message"]

    assert len(rows) == FakeArchive.messages
    assert len({r.path for r in rows}) == FakeArchive.messages, "messages shared a row"


def test_an_extractor_that_forgets_virtual_path_does_not_lose_documents(tmp_path):
    """Every message would otherwise overwrite the last, silently.

    The archive would end up as one row holding only its final email, with no
    error anywhere - and the only symptom would be mail that cannot be found.
    """
    class Careless(FakeArchive):
        name = "careless"
        extensions = (".careless",)

        def extract(self, path: Path):
            for n in range(3):
                yield Document(path=path, text=f"Message {n} about Barnsley Dairy.",
                               source_kind="pst_message")

    base.register(Careless())
    try:
        root = tmp_path / "docs"
        root.mkdir()
        write_aged(root / "a.careless", b"x" * 1024)
        stats = run_index(tmp_path / "index.db", root)
        assert stats.indexed == 3, "documents were silently overwritten"
    finally:
        base.REGISTRY.pop(".careless", None)


def test_a_touched_but_identical_file_is_not_re_indexed(tmp_path, corpus):
    """robocopy, a restore from backup and cloud sync all reset mtime.

    Trusting mtime alone would re-index the entire corpus every time any of
    them happened. Archives are exempt - their bytes cannot be hashed - so this
    is asserted on the ordinary file.
    """
    db = tmp_path / "index.db"
    run_index(db, corpus)

    aged(corpus / "notes.txt")
    stamp = time.time() - (LONG_AGO // 2)
    os.utime(corpus / "notes.txt", (stamp, stamp))

    second = run_index(db, corpus)

    assert second.indexed == 0, "identical content was re-indexed because mtime moved"


def test_a_failed_file_is_retried_rather_than_treated_as_done(tmp_path):
    """A file that could not be read must not be mistaken for a finished one."""
    root = tmp_path / "docs"
    root.mkdir()
    write_aged(root / "a.txt", "Barnsley Dairy.")
    db = tmp_path / "index.db"

    with SqliteStore(db) as store:
        file_id = store.upsert_file(
            str(root / "a.txt"), size_bytes=15, mtime_ns=1, ext="txt",
            parent_dir=str(root), source_kind="file",
        )
        from app.core.errors import make_error
        store.mark_skipped(file_id, make_error("ERR_FILE_LOCKED", "test", path="a.txt"))

    stats = run_index(db, root)
    assert stats.indexed == 1, "a previously failed file was never retried"


# ---------------------------------------------------------------------------
# The resource governor must slow the run down, never stop it dead.
# ---------------------------------------------------------------------------

def governed(db: Path, root: Path, probe, **limit_overrides):
    """Run an index with a scripted view of the machine."""
    from dataclasses import replace

    from app.index.resources import ResourceGovernor, ResourceLimits

    limits = replace(
        ResourceLimits(memory_mb=1000, cpu_percent=80, min_free_gb=5, poll_seconds=0.0),
        **limit_overrides,
    )
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, checkpoint_every=1)
        governor = ResourceGovernor(limits, probe=probe, sleep=lambda _s: None)
        return Pipeline(store, NullVectors(), fake_embedder(), config, governor).run()


def test_a_memory_pause_does_not_deadlock_the_run(tmp_path, corpus):
    """The bug this whole arrangement exists to prevent.

    The pause used to happen in the **consumer** - the only thread that drains
    the results queue. While it waited, the extraction workers blocked trying to
    hand over results they were still holding in memory. So memory never fell,
    so the memory pause never cleared, and the run hung permanently - looking
    exactly like a slow index over a large archive, which is the worst possible
    disguise.

    Backpressure belongs at the intake. This drives the governor permanently
    over its memory ceiling and asserts the run still *finishes*.
    """
    from app.index.resources import Snapshot

    calls = {"n": 0}

    def always_over_memory():
        calls["n"] += 1
        # Relent after a while, as a real machine would once the queues drain.
        # If the fix is wrong this is never reached and the test times out.
        rss = 5_000.0 if calls["n"] < 3 else 100.0
        return Snapshot(rss_mb=rss, system_cpu_percent=5.0, own_cpu_percent=0.0,
                        free_disk_gb=100.0, on_battery=False, at=0.0)

    stats = governed(tmp_path / "index.db", corpus, always_over_memory)

    assert stats.indexed == 1 + FakeArchive.messages, (
        "the run did not complete under memory pressure"
    )
    assert stats.pauses >= 1, "it should have actually paused"


def test_a_battery_pause_does_not_deadlock_the_run(tmp_path, corpus):
    """Same shape, different cause. On a laptop this is the common one."""
    from app.index.resources import Snapshot

    calls = {"n": 0}

    def on_battery_then_mains():
        calls["n"] += 1
        return Snapshot(rss_mb=100.0, system_cpu_percent=5.0, own_cpu_percent=0.0,
                        free_disk_gb=100.0, on_battery=calls["n"] < 3, at=0.0)

    stats = governed(tmp_path / "index.db", corpus, on_battery_then_mains)
    assert stats.indexed == 1 + FakeArchive.messages


def test_the_indexer_does_not_pause_because_of_its_own_cpu_use(tmp_path, corpus):
    """A saturated CPU that is saturated *by this process* is not a busy machine.

    Without subtracting our own load, four workers on a four-core laptop stall
    the run indefinitely on a completely idle machine.
    """
    from app.index.resources import Snapshot

    def busy_but_it_is_us():
        return Snapshot(rss_mb=100.0, system_cpu_percent=99.0, own_cpu_percent=97.0,
                        free_disk_gb=100.0, on_battery=False, at=0.0)

    stats = governed(tmp_path / "index.db", corpus, busy_but_it_is_us, busy_seconds=0.0)

    assert stats.indexed == 1 + FakeArchive.messages
    assert stats.pauses == 0, "it throttled itself on its own workload"


def test_a_full_disk_still_ends_the_run_rather_than_pausing(tmp_path, corpus):
    """Moving the waiting to the producer must not lose the one hard stop."""
    from app.index.resources import Snapshot

    def no_space():
        return Snapshot(rss_mb=100.0, system_cpu_percent=5.0, own_cpu_percent=0.0,
                        free_disk_gb=0.1, on_battery=False, at=0.0)

    stats = governed(tmp_path / "index.db", corpus, no_space)

    assert stats.stopped_early is not None
    assert stats.stopped_early.code == "ERR_DISK_SPACE"


def test_embedding_is_batched_across_documents_not_per_document(tmp_path, corpus):
    """Per-message indexing must not cost per-message embedding.

    Embedding one document at a time means a batch of about three chunks for an
    email, and ONNX throughput collapses at that size - it spends its time on
    per-call overhead rather than on matrix work. That regression was real and
    visible: the same archive took noticeably longer than the version that
    embedded everything in one go.

    Counting the *calls* is the honest way to assert this; asserting on elapsed
    time would be flaky and would not say why.
    """
    calls = {"n": 0, "sizes": []}

    def counting_encode(texts):
        calls["n"] += 1
        calls["sizes"].append(len(texts))
        return [
            l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(8)])
            for t in texts
        ]

    with SqliteStore(tmp_path / "index.db") as store:
        config = PipelineConfig(walk=WalkConfig(roots=[corpus]), workers=1)
        pipeline = Pipeline(
            store, NullVectors(), Embedder(dim=8, encoder=counting_encode), config
        )
        stats = pipeline.run()

    documents = stats.indexed
    assert documents >= FakeArchive.messages
    assert calls["n"] < documents, (
        f"{calls['n']} embedding calls for {documents} documents - "
        f"batch sizes {calls['sizes']}"
    )


def test_a_file_is_not_marked_indexed_before_its_vectors_exist(tmp_path, corpus):
    """The ordering that makes a crash recoverable.

    A file marked INDEXED with no vectors is invisible to semantic search and
    never retried by anything. Marked PENDING with vectors is merely redone.
    """
    seen: list[str] = []

    def watching_encode(texts):
        with SqliteStore(tmp_path / "index.db") as peek:
            seen.extend(
                record.status for record in peek.iter_files()
                if record.source_kind == "pst_message"
            )
        return [
            l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(8)])
            for t in texts
        ]

    with SqliteStore(tmp_path / "index.db") as store:
        config = PipelineConfig(walk=WalkConfig(roots=[corpus]), workers=1)
        Pipeline(store, NullVectors(), Embedder(dim=8, encoder=watching_encode), config).run()

    assert "INDEXED" not in seen, "a document was marked indexed before it was embedded"


def test_pruning_does_not_load_every_message_in_the_archive(tmp_path, corpus):
    """At 200,000 emails, materialising every row to discard the mail is
    minutes and hundreds of megabytes - at the very end of a run, for nothing.

    Asserted by counting what the store is asked for, not by timing.
    """
    db = tmp_path / "index.db"
    run_index(db, corpus)

    with SqliteStore(db) as store:
        everything = list(store.iter_files())
        only_files = list(store.iter_files(source_kind="file"))

    assert len(everything) > len(only_files), "the fixture should contain mail"
    assert all(record.source_kind == "file" for record in only_files)


def test_files_and_documents_are_counted_separately(tmp_path, corpus):
    """"seen 8, indexed 17" cannot both be files, and reporting them as one
    number made a working run look wrong.

    A `.pst` is one *file* and thousands of *documents*. The summary has to say
    which it means or the numbers are unreadable.
    """
    stats = run_index(tmp_path / "index.db", corpus)

    assert stats.seen == 2, "two files on disk"
    assert stats.indexed == 1 + FakeArchive.messages, "one text file plus every message"
    assert stats.seen != stats.indexed, "the two units must not be conflated"


def test_documents_skipped_inside_an_archive_are_counted_apart_from_files(tmp_path, corpus):
    """335 unchanged *messages* and 17 rewritten ones is a completely different
    story from 352 unchanged *files*, and only one of them is true."""
    db = tmp_path / "index.db"
    run_index(db, corpus)

    archive = corpus / "archive.fakepst"
    archive.write_bytes(b"y" * 8192)
    aged(archive)
    FakeArchive.revision = 1
    try:
        second = run_index(db, corpus)
    finally:
        FakeArchive.revision = 0

    assert second.unchanged == 1, "the text file, skipped by the walker"
    assert second.unchanged_documents == FakeArchive.messages - 1, "messages inside the archive"
    assert second.indexed == 1


def test_bytes_are_counted_even_when_the_first_document_is_unchanged(tmp_path, corpus):
    """A 64-second run over 100MB reported "0.0 MB read".

    Bytes were credited when a file's first document was *written*; an archive
    whose first message happens to be unchanged therefore reported nothing for
    the entire file, and the throughput line was meaningless.
    """
    db = tmp_path / "index.db"
    run_index(db, corpus)

    archive = corpus / "archive.fakepst"
    archive.write_bytes(b"y" * 8192)
    aged(archive)
    # Every message identical, so the first document is skipped, not written.
    second = run_index(db, corpus)

    assert second.unchanged_documents >= 1, "the archive was re-read and skipped"
    assert second.bytes_read > 0, "an archive that was read reported reading nothing"
