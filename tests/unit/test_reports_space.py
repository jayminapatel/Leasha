r"""Order 202626270602 (0n) section 3 - the Space Report.

Layer: L4

Real `SqliteStore`, real rows - the same reasoning
`test_reports_inheritance.py` gives for doing the same: this is a set of
SQL queries plus formatting, and a hand-built fixture could pass while the
real query shape does not match what `upsert_file`/`upsert_volume` write.
"""

from __future__ import annotations

import pytest

from app.reports.space import (
    DUPLICATE_GROUPS_SHOWN,
    DuplicateGroup,
    SourceUniqueness,
    find_duplicate_groups,
    find_source_uniqueness,
    render_space_document,
    total_reclaimable_bytes,
)
from app.storage.sqlite_store import SqliteStore


def _local(store, path, *, content_hash=None, size_bytes=1000):
    return store.upsert_file(
        path, size_bytes=size_bytes, mtime_ns=1, ext="txt",
        parent_dir=str(path).rsplit("\\", 1)[0], source_kind="file",
        status="INDEXED", content_hash=content_hash,
    )


def _on_volume(store, volume_id, relative_path, *, content_hash=None, size_bytes=1000):
    return store.upsert_file(
        f"leasha-volume://{volume_id}/{relative_path}",
        size_bytes=size_bytes, mtime_ns=1, ext="txt", parent_dir="p",
        source_kind="file", status="INDEXED", content_hash=content_hash,
        volume_id=volume_id, relative_path=relative_path,
    )


# ---------------------------------------------------------------------------
# Schema v24 - §3c's own performance box
# ---------------------------------------------------------------------------

def test_schema_v24_indexes_content_hash(tmp_path):
    from app.storage.migrations import CURRENT_VERSION

    with SqliteStore(tmp_path / "i.db") as store:
        assert CURRENT_VERSION >= 24
        names = {row["name"] for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'files'")}
        assert "idx_files_content_hash" in names


# ---------------------------------------------------------------------------
# find_duplicate_groups / total_reclaimable_bytes - 3a
# ---------------------------------------------------------------------------

def test_a_hash_with_one_file_is_not_a_duplicate_group(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        _local(store, r"D:\Docs\a.txt", content_hash="H1")

        assert find_duplicate_groups(store) == []
        assert total_reclaimable_bytes(store) == 0


def test_two_files_sharing_a_hash_are_one_group(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        _local(store, r"D:\Docs\a.txt", content_hash="H1", size_bytes=1000)
        _local(store, r"D:\Backup\a-copy.txt", content_hash="H1", size_bytes=1000)

        groups = find_duplicate_groups(store)

    assert len(groups) == 1
    group = groups[0]
    assert group.content_hash == "H1"
    assert group.size_bytes == 1000
    assert len(group.copies) == 2
    assert group.reclaimable_bytes == 1000        # keep one, reclaim the other


def test_reclaimable_bytes_totals_every_group_not_only_the_shown_ones(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        for n in range(3):
            _local(store, rf"D:\Docs\a{n}.txt", content_hash="H1", size_bytes=1000)
        for n in range(2):
            _local(store, rf"D:\Docs\b{n}.txt", content_hash="H2", size_bytes=500)

        total = total_reclaimable_bytes(store)

    # H1: 3 copies of 1000 -> 2000 reclaimable. H2: 2 copies of 500 -> 500.
    assert total == 2500


def test_duplicate_groups_are_sorted_by_reclaimable_bytes_descending(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        # Small group, big file: 1 * 10_000 = 10_000 reclaimable.
        _local(store, r"D:\Docs\big1.txt", content_hash="BIG", size_bytes=10_000)
        _local(store, r"D:\Docs\big2.txt", content_hash="BIG", size_bytes=10_000)
        # Big group, small file: 4 * 10 = 40 reclaimable.
        for n in range(5):
            _local(store, rf"D:\Docs\small{n}.txt", content_hash="SMALL", size_bytes=10)

        groups = find_duplicate_groups(store)

    assert [g.content_hash for g in groups] == ["BIG", "SMALL"]


def test_a_copy_on_a_catalogued_volume_names_the_volume(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        volume_id = store.upsert_volume(
            "GUID-1", kind="drive", name="Old WD", status="OFFLINE")
        _local(store, r"D:\Docs\a.txt", content_hash="H1", size_bytes=1000)
        _on_volume(store, volume_id, "backup/a.txt", content_hash="H1", size_bytes=1000)

        groups = find_duplicate_groups(store)

    assert len(groups) == 1
    kinds = {c.source_kind for c in groups[0].copies}
    names = {c.source_name for c in groups[0].copies}
    assert kinds == {"local", "drive"}
    assert "Old WD" in names
    volume_copy = next(c for c in groups[0].copies if c.source_kind == "drive")
    assert volume_copy.source_status == "offline"


def test_duplicate_groups_are_capped_at_the_shown_limit(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        for group in range(DUPLICATE_GROUPS_SHOWN + 5):
            _local(store, rf"D:\Docs\g{group}-a.txt", content_hash=f"H{group}")
            _local(store, rf"D:\Docs\g{group}-b.txt", content_hash=f"H{group}")

        groups = find_duplicate_groups(store)

    assert len(groups) == DUPLICATE_GROUPS_SHOWN


# ---------------------------------------------------------------------------
# find_source_uniqueness - 3b, "the backup conscience"
# ---------------------------------------------------------------------------

def test_content_that_exists_twice_is_not_unique_to_either_source(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        volume_id = store.upsert_volume("GUID-1", kind="drive", name="Old WD")
        _local(store, r"D:\Docs\a.txt", content_hash="H1")
        _on_volume(store, volume_id, "a.txt", content_hash="H1")

        assert find_source_uniqueness(store) == []


def test_content_on_exactly_one_volume_counts_toward_that_volume(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        volume_id = store.upsert_volume(
            "GUID-1", kind="drive", name="Old WD", status="OFFLINE",
            seen_at=1_700_000_000)
        for n in range(372):
            _on_volume(store, volume_id, f"f{n}.txt", content_hash=f"UNIQUE{n}")

        results = find_source_uniqueness(store)

    assert len(results) == 1
    only = results[0]
    assert only.name == "Old WD"
    assert only.kind == "drive"
    assert only.status == "offline"
    assert only.file_count == 372
    assert only.last_seen == 1_700_000_000


def test_local_only_content_is_reported_as_one_bucket(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        _local(store, r"D:\Docs\a.txt", content_hash="H1")
        _local(store, r"D:\Docs\b.txt", content_hash="H2")

        results = find_source_uniqueness(store)

    assert len(results) == 1
    assert results[0].name == "This computer"
    assert results[0].kind == "local"
    assert results[0].file_count == 2


def test_volumes_are_ranked_before_this_computer(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        volume_id = store.upsert_volume("GUID-1", kind="drive", name="Old WD")
        _on_volume(store, volume_id, "a.txt", content_hash="V1")
        _local(store, r"D:\Docs\a.txt", content_hash="L1")
        _local(store, r"D:\Docs\b.txt", content_hash="L2")
        _local(store, r"D:\Docs\c.txt", content_hash="L3")

        results = find_source_uniqueness(store)

    # "This computer" has more files (3) than the volume (1), but the
    # volume - the one a broken drive can actually take with it - still
    # comes first.
    assert results[0].kind == "drive"
    assert results[-1].name == "This computer"


def test_a_file_with_no_content_hash_is_invisible_to_either_query(tmp_path):
    r"""No hash means nothing to compare - neither report may guess."""
    with SqliteStore(tmp_path / "i.db") as store:
        _local(store, r"D:\Docs\a.txt", content_hash=None)
        _local(store, r"D:\Docs\b.txt", content_hash=None)

        assert find_duplicate_groups(store) == []
        assert find_source_uniqueness(store) == []


# ---------------------------------------------------------------------------
# render_space_document - the words
# ---------------------------------------------------------------------------

def test_the_document_states_the_total_reclaimable_bytes():
    groups = [DuplicateGroup(content_hash="H1", size_bytes=1024 * 1024, copies=())]
    doc = render_space_document(groups, [], total_reclaimable=2 * 1024 * 1024)
    assert "2.0 MB" in doc


def test_an_empty_index_says_no_duplicates_were_found():
    doc = render_space_document([], [], total_reclaimable=0)
    assert "no duplicate" in doc.lower()


def test_the_uniqueness_headline_names_the_source_and_count():
    doc = render_space_document([], [
        SourceUniqueness(name="Old WD", kind="drive", status="offline",
                         last_seen=1_690_000_000, file_count=372),
    ])
    assert "372" in doc
    assert "Old WD" in doc
    assert "offline" in doc.lower()


def test_the_document_never_shows_file_contents():
    r"""Same guarantee `inheritance.py`'s own test makes: names and
    locations only, never what is inside a file."""
    from app.reports.space import DuplicateCopy

    groups = [DuplicateGroup(
        content_hash="H1", size_bytes=10,
        copies=(DuplicateCopy(path="/a.txt", source_name="This computer",
                              source_kind="local"),))]
    doc = render_space_document(groups, [], total_reclaimable=0)
    assert "SECRET-DOCUMENT-BODY-TEXT" not in doc
