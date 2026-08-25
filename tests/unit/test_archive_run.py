r"""An archival root, through a real pipeline run.

Layer: L3

`test_archives.py` tests the decision. This tests that the decision is *acted
on* - that a skipped root is not walked, that its rows are not pruned, that its
count and date are recorded, and that a run which did not finish records
nothing.

**The last of those is the dangerous one.** Recording a pass for a run that
stopped a third of the way through marks an archive as fully indexed when two
thirds of it has never been read - and then nothing looks at it again, ever.
That failure is silent and permanent, so it gets a test rather than a comment.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

import pytest

from app.index.archives import (
    ARCHIVE,
    MODE_STATE_KEY,
    RECORD_STATE_KEY,
    dump_modes,
    load_records,
    normalise,
)
from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore

LONG_AGO = 3600


class NullVectors:
    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _embedder(dim: int = 8) -> Embedder:
    def encode(texts):
        return [
            l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(dim)])
            for t in texts
        ]

    return Embedder(dim=dim, encoder=encode)


def _write(path: Path, text: str = "Barnsley Dairy notes.") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - LONG_AGO
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture()
def corpus(tmp_path):
    archive = tmp_path / "archive"
    live = tmp_path / "live"
    _write(archive / "old.txt", "Twelve-year-old project notes.")
    _write(live / "new.txt", "This week's notes.")
    return archive, live


def _run(db: Path, roots, *, store=None, **config):
    def go(opened):
        pipeline = Pipeline(
            opened, NullVectors(), _embedder(),
            PipelineConfig(walk=WalkConfig(roots=list(roots)), workers=1, **config),
        )
        return pipeline.run()

    if store is not None:
        return go(store)
    with SqliteStore(db) as opened:
        return go(opened)


def test_the_first_run_walks_an_archive_and_records_what_it_found(tmp_path, corpus):
    archive, live = corpus
    db = tmp_path / "index.db"

    with SqliteStore(db) as store:
        store.set_state(MODE_STATE_KEY, dump_modes({str(archive): ARCHIVE}))
        stats = _run(db, [archive, live], store=store)
        records = load_records(store.get_state(RECORD_STATE_KEY, "") or "")

    assert stats.indexed == 2                      # both files, first time
    assert not stats.skipped_roots
    saved = records[normalise(archive)]
    assert saved.files == 1
    assert saved.archived_at > 0
    assert saved.mtime_ns > 0
    assert normalise(live) not in records          # a live root gets no record


def test_the_second_run_does_not_walk_it_at_all(tmp_path, corpus):
    r"""**The whole point, measured by the file that was added and missed.**

    A file dropped *inside* a subfolder of an archive - leaving the root's own
    mtime untouched - is invisible to the cheap check, and this asserts that:
    the run does not find it. That is not a bug being tested, it is the trade
    being made explicit, and it is why the panel says which folders were
    skipped and why `--recheck-archives` exists.
    """
    archive, live = corpus
    db = tmp_path / "index.db"

    with SqliteStore(db) as store:
        store.set_state(MODE_STATE_KEY, dump_modes({str(archive): ARCHIVE}))
        _run(db, [archive, live], store=store)

        # Creating `deep/` moves the root's mtime, which is exactly what the
        # tripwire is for - so it is put back, to reproduce the case the
        # tripwire genuinely cannot see: a change with no trace at the top.
        before = os.stat(archive)
        _write(archive / "deep" / "added.txt", "Added later, deep inside.")
        os.utime(archive, ns=(before.st_atime_ns, before.st_mtime_ns))
        stats = _run(db, [archive, live], store=store)

    assert [row["root"] for row in stats.skipped_roots] == [str(archive)]
    assert stats.skipped_roots[0]["files"] == 1
    assert stats.seen == 1                         # only the live root was walked


def test_a_folder_dropped_into_the_archive_is_caught(tmp_path, corpus):
    """The case the mtime tripwire exists for, and the common one."""
    archive, live = corpus
    db = tmp_path / "index.db"

    with SqliteStore(db) as store:
        store.set_state(MODE_STATE_KEY, dump_modes({str(archive): ARCHIVE}))
        _run(db, [archive, live], store=store)

        # Creating an entry in a directory moves that directory's mtime.
        _write(archive / "added.txt", "Somebody dropped this in.")
        stats = _run(db, [archive, live], store=store)

    assert not stats.skipped_roots
    assert stats.indexed == 1                      # the new file, and only it


def test_recheck_walks_it_in_full(tmp_path, corpus):
    archive, live = corpus
    db = tmp_path / "index.db"

    with SqliteStore(db) as store:
        store.set_state(MODE_STATE_KEY, dump_modes({str(archive): ARCHIVE}))
        _run(db, [archive, live], store=store)
        before = os.stat(archive)
        _write(archive / "deep" / "added.txt", "Added later.")
        os.utime(archive, ns=(before.st_atime_ns, before.st_mtime_ns))

        stats = _run(db, [archive, live], store=store, recheck_archives=True)

    assert not stats.skipped_roots
    assert stats.indexed == 1


def test_all_roots_ignores_the_modes_without_touching_the_records(tmp_path, corpus):
    """The escape hatch. It must not silently extend an archive's trust - that
    would make a diagnostic run change what later runs skip."""
    archive, live = corpus
    db = tmp_path / "index.db"

    with SqliteStore(db) as store:
        store.set_state(MODE_STATE_KEY, dump_modes({str(archive): ARCHIVE}))
        _run(db, [archive, live], store=store)
        before = store.get_state(RECORD_STATE_KEY, "")

        stats = _run(db, [archive, live], store=store, archives=False)
        after = store.get_state(RECORD_STATE_KEY, "")

    assert not stats.skipped_roots
    assert stats.seen == 2                         # everything walked
    assert after == before                         # and nothing re-dated


def test_rows_under_a_skipped_archive_are_not_pruned(tmp_path, corpus):
    r"""**And are not `stat`'d either, which is the point.**

    The `exists()` test in `_prune_missing` would keep them anyway - they are
    still on disk - but it is one syscall per row, and on a 1.5TB archive that
    is several million syscalls at the end of every incremental run to confirm
    that nothing changed. Skipping the walk and then paying that would have
    saved almost nothing.
    """
    archive, live = corpus
    db = tmp_path / "index.db"
    looked_at: list[str] = []

    with SqliteStore(db) as store:
        store.set_state(MODE_STATE_KEY, dump_modes({str(archive): ARCHIVE}))
        _run(db, [archive, live], store=store)

        import app.index.pipeline as pipeline_module

        real = pipeline_module.Path

        class Watched(type(Path())):                # a Path that records exists()
            def exists(self, *args, **kwargs):
                looked_at.append(str(self))
                return real(str(self)).exists()

        pipeline_module.Path = Watched
        try:
            stats = _run(db, [archive, live], store=store)
        finally:
            pipeline_module.Path = real

    assert stats.deleted == 0
    assert not any(normalise(archive) in normalise(seen) for seen in looked_at), (
        "a row under a skipped archive was stat'd during the prune"
    )


def test_an_interrupted_run_records_no_pass(tmp_path, corpus):
    r"""**The silent, permanent failure this module could have.**

    A run stopped a third of the way through has read a third of the archive.
    Recording a pass for it would mark the whole folder as fully indexed, and
    every later run would skip it - so two thirds of the corpus would be
    missing from search for ever, with the panel cheerfully reporting the
    folder as an archive that was last read today.
    """
    archive, live = corpus
    db = tmp_path / "index.db"
    for index in range(40):
        _write(archive / f"f{index:02d}.txt", f"Archived note {index}.")

    with SqliteStore(db) as store:
        store.set_state(MODE_STATE_KEY, dump_modes({str(archive): ARCHIVE}))
        pipeline = Pipeline(
            store, NullVectors(), _embedder(),
            PipelineConfig(walk=WalkConfig(roots=[archive, live]), workers=1,
                           checkpoint_every=1),
        )
        # Stopped part-way, the way the Stop button and the disk guard stop it.
        # `run()` clears the flag on entry, so it has to be set from inside.
        pipeline.run(on_progress=lambda _stats: pipeline.request_stop())
        records = load_records(store.get_state(RECORD_STATE_KEY, "") or "")

    assert records == {}


def test_an_unreadable_mode_record_walks_everything(tmp_path, corpus):
    """A preference that cannot be parsed must cost time, never coverage."""
    archive, live = corpus
    db = tmp_path / "index.db"

    with SqliteStore(db) as store:
        store.set_state(MODE_STATE_KEY, "{{{ not json")
        stats = _run(db, [archive, live], store=store)

    assert stats.seen == 2
    assert not stats.skipped_roots
