r"""Storage review S2, 2026-10-10: the vector backlog goes newest file first.

Layer: L1

`unembedded_by_file` handed the start-of-run backlog back oldest-stored first
(`ORDER BY file_id, id`) while a run reads files newest first, so with a backlog
of days the files a person is likeliest to look for got vectors last. Now:
files under the `first_folders` first, in their order, then newest modified
first; a file is never split across batches (the invariant of 2026-10-08).
"""

from __future__ import annotations

from app.storage.sqlite_store import SqliteStore


def _file(store, path: str, mtime_ns: int, passages: int) -> tuple[int, list[int]]:
    file_id = store.upsert_file(path=path, size_bytes=1, mtime_ns=mtime_ns)
    ids = store.replace_chunks(file_id, [{"text": f"{path} passage {n}"} for n in range(passages)])
    return file_id, ids


def _file_order(batches) -> list[int]:
    seen: list[int] = []
    for batch in batches:
        for _chunk, file_id in batch:
            if not seen or seen[-1] != file_id:
                seen.append(file_id)
    return seen


def test_newest_file_first_and_no_file_cut(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        old, _ = _file(store, r"D:\Docs\old.txt", 100, 3)
        new, _ = _file(store, r"D:\Docs\new.txt", 300, 3)
        mid, _ = _file(store, r"D:\Docs\mid.txt", 200, 3)
        batches = store.unembedded_by_file(batch_size=2)
    assert _file_order(batches) == [new, mid, old]
    assert [len(batch) for batch in batches] == [3, 3, 3]   # whole files, never cut
    for batch in batches:
        chunk_ids = [chunk for chunk, _ in batch]
        assert chunk_ids == sorted(chunk_ids)                # reading order inside a file


def test_first_folders_come_before_everything_in_their_order(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        newest, _ = _file(store, r"D:\Other\newest.txt", 900, 1)
        work, _ = _file(store, r"D:\Work\plan.txt", 100, 1)
        work_new, _ = _file(store, r"D:\Work\sub\later.txt", 150, 1)
        school, _ = _file(store, r"D:\School\homework.txt", 50, 1)
        lookalike, _ = _file(store, r"D:\School2\not-it.txt", 800, 1)
        # Folder given in another case and with a trailing separator.
        batches = store.unembedded_by_file(
            batch_size=1, first_folders=[r"d:\school" + "\\", r"D:\Work"])
    assert _file_order(batches) == [school, work_new, work, newest, lookalike]


def test_embedded_and_keyword_only_passages_are_not_in_the_backlog(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        _a, ids_a = _file(store, r"D:\a.txt", 1, 2)
        b, ids_b = _file(store, r"D:\b.txt", 2, 2)
        _c, ids_c = _file(store, r"D:\c.txt", 3, 2)
        store.mark_embedded(ids_a)
        store.mark_keyword_only(ids_c)
        batches = store.unembedded_by_file()
    assert batches == [[(ids_b[0], b), (ids_b[1], b)]]


def test_the_plan_reads_the_pending_index_and_sorts_nothing_in_sql(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        plan = " | ".join(str(row[3]) for row in store.conn.execute(
            "EXPLAIN QUERY PLAN SELECT c.id, c.file_id, f.mtime_ns FROM chunks c "
            "LEFT JOIN files f ON f.id = c.file_id WHERE c.embedded = 0"))
    assert "idx_chunks_pending" in plan, plan
    assert "INTEGER PRIMARY KEY" in plan, plan
    assert "TEMP B-TREE" not in plan, plan
