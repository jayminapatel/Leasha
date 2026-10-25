r"""The scan reads the index's file rows once, and decides exactly as before.

Layer: L3

2026-10-25, the owner: "when you stop and start index the resume is too slow
... we have the data in the database already". The scan's change check asked
the store about each file it found, one `get_file` each; on a restart every one
finds a row. It now reads the rows once (`SqliteStore.rows_for_change_check`)
and answers from them (`Pipeline._known_row`) - except a `PARTIAL` row, which
the feeder may promote while the scan runs, and a catalogued drive's key. These
pin that every answer `_classify` gives is the one it gave with a lookup per
file.
"""

from __future__ import annotations

import dataclasses
import os

from app.index.pipeline import UNCHANGED, Pipeline
from app.storage.sqlite_store import FileStatus, SqliteStore
from tests.unit.test_pst_change_marker import _candidate, _classifier
import pytest

HOUR_NS = 3_600_000_000_000


def _file(tmp_path, name, text="words"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    old = path.stat().st_mtime_ns - 2 * HOUR_NS       # not "touched just now"
    os.utime(path, ns=(old, old))
    return path


def _row(store, path, *, status, moved=False, skip_code=None, content_hash="h"):
    stat = path.stat()
    file_id = store.upsert_file(
        str(path), size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns - (HOUR_NS if moved else 0),
        ext=path.suffix.lstrip("."), parent_dir=str(path.parent),
        content_hash=content_hash, status=status)
    if skip_code:
        store.conn.execute("UPDATE files SET skip_code = ? WHERE id = ?", (skip_code, file_id))
        store.conn.commit()
    return file_id


def _cases(store, tmp_path):
    """One file in each state the check tells apart."""
    made = {}
    for name, status, kw in [
        ("indexed.txt", FileStatus.INDEXED, {}),
        ("moved.txt", FileStatus.INDEXED, {"moved": True}),
        ("partial.txt", FileStatus.PARTIAL, {}),
        ("pending.txt", FileStatus.PENDING, {}),
        ("skipped.txt", FileStatus.SKIPPED, {"skip_code": "ERR_TOO_LARGE"}),
        ("name_only.mp4", FileStatus.NAME_ONLY, {}),
        ("failed.txt", FileStatus.FAILED, {"skip_code": "ERR_PARSE"}),
    ]:
        path = _file(tmp_path, name)
        _row(store, path, status=status, **kw)
        made[name] = path
    made["new.txt"] = _file(tmp_path, "new.txt")      # no row at all
    return made


def _answers(pipeline, paths, *, hash_now=False):
    out = {}
    for name, path in paths.items():
        candidate = _candidate(path)
        if name.endswith(".mp4"):
            candidate = dataclasses.replace(candidate, readable=False)
        answer = pipeline._classify(candidate, hash_now=hash_now)
        out[name] = "UNCHANGED" if answer is UNCHANGED else repr(answer)
    return out


def test_every_answer_is_the_one_a_lookup_per_file_gave(tmp_path):
    store, pipeline = _classifier(tmp_path)
    try:
        paths = _cases(store, tmp_path)
        for hash_now in (False, True):
            per_file = _answers(pipeline, paths, hash_now=hash_now)
            pipeline._load_scan_rows()
            assert pipeline._scan_rows is not None
            once = _answers(pipeline, paths, hash_now=hash_now)
            pipeline._scan_rows = None
            assert once == per_file
        assert per_file["indexed.txt"] == "UNCHANGED"
        assert per_file["new.txt"] != "UNCHANGED"
    finally:
        store.close()


def test_the_rows_are_the_store_rows(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        path = _file(tmp_path, "a.txt")
        _row(store, path, status=FileStatus.INDEXED, skip_code="ERR_X")
        store.upsert_file("pst://store/1", size_bytes=1, mtime_ns=1, ext="", parent_dir="x",
                          source_kind="pst_message")
        rows = store.rows_for_change_check()
        assert set(rows) == {str(path)}, "a key with :// is left to get_file"
        record, row = store.get_file(str(path)), rows[str(path)]
        for field in row._fields:
            assert getattr(row, field) == getattr(record, field), field


def test_a_partial_row_is_asked_of_the_store_while_the_scan_runs(tmp_path):
    """The feeder promotes PARTIAL to INDEXED as the scan goes. Where the two get
    different answers - an unreadable file, a mail archive - the store is asked;
    elsewhere the snapshot's PARTIAL stands, and the answer is the same."""
    store, pipeline = _classifier(tmp_path)
    try:
        archive = _file(tmp_path, "2007.pst")
        unreadable = _file(tmp_path, "film.mp4")
        text = _file(tmp_path, "p.txt")
        ids = [_row(store, path, status=FileStatus.PARTIAL)
               for path in (archive, unreadable, text)]
        before = {p.name: _answers(pipeline, {p.name: p})[p.name]
                  for p in (archive, unreadable, text)}
        pipeline._load_scan_rows()
        for file_id in ids:
            store.mark_indexed(file_id)               # the feeder, meanwhile
        assert pipeline._known_row(_candidate(archive)).status == FileStatus.INDEXED
        film = dataclasses.replace(_candidate(unreadable), readable=False)
        assert pipeline._known_row(film).status == FileStatus.INDEXED
        assert pipeline._known_row(_candidate(text)).status == FileStatus.PARTIAL
        # And every answer is the one a lookup per file gives now.
        with_snapshot = {p.name: _answers(pipeline, {p.name: p})[p.name]
                         for p in (archive, unreadable, text)}
        pipeline._scan_rows = None
        per_file = {p.name: _answers(pipeline, {p.name: p})[p.name]
                    for p in (archive, unreadable, text)}
        assert with_snapshot == per_file
        assert before["film.mp4"] != per_file["film.mp4"], "the case the store is asked for"
        pipeline._load_scan_rows()
        # A row in any other state is answered from the snapshot.
        other = _file(tmp_path, "i.txt")
        _row(store, other, status=FileStatus.INDEXED)
        assert pipeline._known_row(_candidate(other)) is None, "written after the snapshot"
    finally:
        store.close()


@pytest.mark.slow
def test_a_scan_asks_the_store_per_file_no_longer(tmp_path, monkeypatch):
    """A restart over files the index knows: one read, not one lookup a file."""
    from tests.unit.test_enrichment_backlog import FakeVectors, _embedder, _run

    root = tmp_path / "docs"
    root.mkdir()
    for n in range(30):
        _file(root, f"doc{n}.txt", f"pump {n} valve report")
    vectors = FakeVectors()
    with SqliteStore(tmp_path / "index.db") as store:
        first = _run(store, root, vectors, _embedder())
        assert first.indexed == 30
        asked = []
        real = SqliteStore.get_file
        monkeypatch.setattr(SqliteStore, "get_file",
                            lambda self, path: asked.append(path) or real(self, path))

        second = _run(store, root, vectors, _embedder(), force=False)

        assert second.unchanged == 30
        assert not [p for p in asked if p.startswith(str(root))], (
            f"{len(asked)} per-file lookups in a scan of rows the index already held")


def test_the_snapshot_ends_with_the_scan(tmp_path):
    store, pipeline = _classifier(tmp_path)
    try:
        assert getattr(pipeline, "_scan_rows", None) is None
        pipeline.config.candidate_source = lambda *_a: iter(())
        pipeline._load_scan_rows()
        assert pipeline._scan_rows is None, "a run given its files asks per file"
    finally:
        store.close()


def test_archive_check_order_changes_no_answer(tmp_path):
    """`_archive_only_touched` asks the row before the name - still False for
    every row that is not a read archive, True for one that is."""
    store, pipeline = _classifier(tmp_path)
    try:
        note = _file(tmp_path, "note.txt")
        assert Pipeline._archive_only_touched(pipeline, _candidate(note), None) is False
    finally:
        store.close()
