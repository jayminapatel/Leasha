"""Work order `pst-resilience` 3d: the Indexing tab says an archive was partly read.

`warned_by_code` is persisted in `last_run_stats` at the end of every run, and
only the command line read it - so the tab looked identical after a run that
skipped a fifth of an archive and after one that read every message ("indexed"
reads as "complete"). The count is now a row in the "This index" panel.

The scenario at the bottom drives the real view: a real `SqliteStore` holding the
stored record, `refresh_totals` reading it in a worker, the panel repainted.
"""

from __future__ import annotations

import pytest

from app.storage.sqlite_store import SqliteStore
from app.ui.presenter import index_summary, read_index_summary, warned_counts

HEALTHY = {
    "files_total": 355, "chunks_total": 3355, "chunks_embedded": 3355,
    "files": {"INDEXED": 355}, "skipped_by_code": {},
}
ROW = "Mail archives partly read"


def _find(rows, label):
    return next((row for row in rows if row.label == label), None)


def test_a_partial_archive_count_becomes_a_warning_row() -> None:
    rows = index_summary(HEALTHY, warned={"ERR_PST_PARTIAL": 3})
    row = _find(rows, ROW)
    assert row is not None and row.value == "3" and row.warn
    assert "scanpst" in row.note


def test_no_partial_archive_means_no_row() -> None:
    assert _find(index_summary(HEALTHY, warned={}), ROW) is None
    assert _find(index_summary(HEALTHY, warned={"ERR_MOSTLY_PICTURES": 9}), ROW) is None
    assert _find(index_summary(HEALTHY), ROW) is None


@pytest.mark.parametrize("raw", [None, "", "not a dict at all (", "[1, 2]", "{'chunks': 5}",
                                 "{'warned_by_code': 7}"])
def test_an_absent_or_unreadable_record_is_no_counts_not_an_error(raw) -> None:
    assert warned_counts(raw) == {}


def test_the_stored_record_is_read_back() -> None:
    raw = repr({"chunks": 10, "warned_by_code": {"ERR_PST_PARTIAL": 2, "ERR_X": "4", "bad": "z"}})
    assert warned_counts(raw) == {"ERR_PST_PARTIAL": 2, "ERR_X": 4}


def test_the_summary_payload_carries_the_last_runs_warnings(tmp_path) -> None:
    with SqliteStore(tmp_path / "i.db") as store:
        store.set_state("last_run_stats", repr({"warned_by_code": {"ERR_PST_PARTIAL": 1}}))
        assert read_index_summary(store)["warned"] == {"ERR_PST_PARTIAL": 1}


@pytest.mark.gui
def test_the_indexing_tab_shows_a_partly_read_archive(qtbot, tmp_path) -> None:
    """The scenario: open the tab over an index whose last run met a short archive."""
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QLabel

    from app.ui.indexing_view import IndexingView

    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file("C:/mail/a.pst", size_bytes=1, mtime_ns=1,
                                    status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": "one message"}])
        store.set_state("last_run_stats", repr({"warned_by_code": {"ERR_PST_PARTIAL": 2}}))

        view = IndexingView()
        qtbot.addWidget(view)
        view.show()
        view.refresh_totals(store)

        def shown() -> list[str]:
            return [w.text() for w in view.stats_box.findChildren(QLabel)]

        qtbot.waitUntil(lambda: ROW in shown(), timeout=5000)
        texts = shown()
        assert "2" in texts[texts.index(ROW) + 1:texts.index(ROW) + 2]
        QThreadPool.globalInstance().waitForDone(2000)      # the summary worker
