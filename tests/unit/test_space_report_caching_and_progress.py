r"""Order 202626270602 (0n) section 3c - caching and progress.

Layer: L5

`_report_snapshot` (`app/ui/reports_view.py`) is the worker body `refresh()`
dispatches on every tab switch. Section 3c asks for two things beyond the
query indexes already built: the expensive queries skip entirely when
nothing has been indexed since the last load, and a stage is reported while
they run. Both are tested here directly against the function, without a
live `MainWindow` - it takes a store and two plain callables, nothing Qt
about it.
"""

from __future__ import annotations

from app.storage.sqlite_store import SqliteStore
from app.ui.reports_view import _report_snapshot


def _local(store, path, *, content_hash=None, size_bytes=1000):
    return store.upsert_file(
        path, size_bytes=size_bytes, mtime_ns=1, ext="txt",
        parent_dir=str(path).rsplit("\\", 1)[0], source_kind="file",
        status="INDEXED", content_hash=content_hash,
    )


def _set_indexed_at(store, file_id, value):
    with store.write() as conn:
        conn.execute("UPDATE files SET indexed_at = ? WHERE id = ?", (value, file_id))


# ---------------------------------------------------------------------------
# The cache: skip the expensive queries when nothing has moved
# ---------------------------------------------------------------------------

def test_a_first_call_has_no_cache_and_returns_the_full_snapshot(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        file_id = _local(store, r"D:\Docs\a.txt", content_hash="H1")
        _set_indexed_at(store, file_id, 1000)

        snapshot = _report_snapshot(store)

        assert snapshot is not None
        sources, generated_at, space = snapshot
        assert generated_at == 1000
        assert "Space Report" in space


def test_an_unchanged_data_timestamp_skips_the_queries_entirely(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        file_id = _local(store, r"D:\Docs\a.txt", content_hash="H1")
        _set_indexed_at(store, file_id, 1000)

        seen: list = []
        result = _report_snapshot(store, last_known_generated_at=1000, on_progress=seen.append)

        assert result is None, "the data timestamp had not moved - nothing to recompute"
        assert seen == [], "a cache hit must not run any stage - there is nothing to stage"


def test_a_new_index_run_invalidates_the_cache(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        file_id = _local(store, r"D:\Docs\a.txt", content_hash="H1")
        _set_indexed_at(store, file_id, 1000)

        stale_result = _report_snapshot(store, last_known_generated_at=999)
        assert stale_result is not None, "999 is not today's data timestamp - must recompute"
        assert stale_result[1] == 1000


# ---------------------------------------------------------------------------
# Progress: a stage is reported while the real queries run
# ---------------------------------------------------------------------------

def test_a_cache_miss_reports_more_than_one_stage_in_order(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        file_id = _local(store, r"D:\Docs\a.txt", content_hash="H1")
        _set_indexed_at(store, file_id, 1000)

        seen: list = []
        _report_snapshot(store, on_progress=seen.append)

        assert len(seen) >= 3, "at least sources, duplicates and uniqueness are separate stages"
        assert all(isinstance(stage, str) and stage for stage in seen), (
            "every stage is plain, non-empty text - the same shape a toast or a "
            "label can show verbatim, not a code or a partial object"
        )
        assert len(seen) == len(set(seen)) or True  # stages may legitimately repeat words
        assert seen == list(dict.fromkeys(seen)), "each stage fires once, not repeated"


def test_on_progress_is_optional(tmp_path):
    """Every other CallableWorker call site does not pass on_progress at
    all; the default must not crash."""
    with SqliteStore(tmp_path / "i.db") as store:
        file_id = _local(store, r"D:\Docs\a.txt", content_hash="H1")
        _set_indexed_at(store, file_id, 1000)

        snapshot = _report_snapshot(store)

        assert snapshot is not None
