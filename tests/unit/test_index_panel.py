"""The Indexing page must always say something, and Reset must be safe.

Layer: L5

Reported as "the index page is blank". It was: `refresh_totals` ended in
`except: return`, so a store read that failed left an empty label with nothing
to explain it. A blank page is the worst possible answer to "is my index
working" - indistinguishable from an empty index, a broken one, and a bug in the
page.

So the rule these tests hold is: **every outcome produces rows, including
failure.** And the reset that was added alongside must delete the index without
touching a document or forgetting which folders to index.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.storage.sqlite_store import SqliteStore
from app.ui.presenter import index_summary, read_index_summary, when_text

HEALTHY = {
    "files_total": 355, "chunks_total": 3355, "chunks_embedded": 3355,
    "files": {"INDEXED": 355}, "skipped_by_code": {},
}


def labels(rows) -> list[str]:
    return [row.label for row in rows]


def find(rows, label: str):
    return next((row for row in rows if row.label == label), None)


# ---------------------------------------------------------------------------
# It always says something
# ---------------------------------------------------------------------------

def test_an_unreadable_store_produces_a_row_not_a_blank_page():
    """The reported bug. `except: return` left the panel empty, which reads as
    "no index" rather than "could not look"."""
    rows = index_summary(None, error="database is locked")

    assert rows, "a failure must still produce rows"
    assert rows[0].warn
    assert "locked" in rows[0].value


def test_an_empty_index_says_what_to_do_about_it():
    rows = index_summary({})
    assert rows
    assert "Settings" in rows[0].value


def test_a_healthy_index_reports_documents_and_passages():
    rows = index_summary(HEALTHY, {"rows": 3355})
    assert find(rows, "Documents").value == "355"
    assert find(rows, "Searchable passages").value == "3,355"


# ---------------------------------------------------------------------------
# The number that was invisible for weeks
# ---------------------------------------------------------------------------

def test_partial_embedding_is_reported_as_a_warning():
    """154 of 3,355 passages had vectors and nothing said so. Meaning-based
    search silently did one twentieth of its job."""
    rows = index_summary(
        {**HEALTHY, "chunks_embedded": 154}, {"rows": 154},
    )
    coverage = find(rows, "Meaning-based search covers")

    assert coverage is not None
    assert coverage.value == "5%"
    assert coverage.warn
    assert "exact words only" in coverage.note


def test_full_coverage_is_not_a_warning():
    coverage = find(index_summary(HEALTHY, {"rows": 3355}), "Meaning-based search covers")
    assert coverage.value == "100%"
    assert not coverage.warn


def test_more_vectors_than_passages_is_reported():
    """Orphans from an interrupted rebuild produce results that cannot be
    opened, which looks like results going missing."""
    rows = index_summary({**HEALTHY, "chunks_embedded": 100}, {"rows": 3355})
    orphans = find(rows, "Orphaned vectors")
    assert orphans is not None and orphans.warn


def test_the_index_location_is_shown():
    """"Is the index where I configured it" was asked, and was a question that
    needed the command line to answer."""
    rows = index_summary(HEALTHY, {"rows": 3355}, data_path=r"D:\KnowledgeGraphData")
    assert find(rows, "Index location").value == r"D:\KnowledgeGraphData"


def test_skipped_files_name_their_reasons():
    rows = index_summary(
        {**HEALTHY, "skipped_by_code": {"ERR_UNSUPPORTED_TYPE": 12, "ERR_FILE_LOCKED": 3}},
        {"rows": 3355},
    )
    skipped = find(rows, "Skipped")
    assert skipped.value == "15"
    assert "ERR_UNSUPPORTED_TYPE" in skipped.note


# ---------------------------------------------------------------------------
# Reading it never raises, whatever the store does
# ---------------------------------------------------------------------------

class BrokenStore:
    def stats(self):
        raise RuntimeError("database is locked")

    def get_state(self, _key, _default=None):
        return None


def test_a_broken_store_returns_an_error_payload_rather_than_raising():
    """It runs in a worker, and a worker that raises leaves the page showing
    "Reading…" for ever."""
    payload = read_index_summary(BrokenStore())
    assert payload["error"]
    assert "locked" in payload["error"]


def test_a_corrupt_timestamp_does_not_lose_the_whole_panel():
    """One line out of eight. Losing the other seven to it is a poor trade."""
    assert when_text("not a date") == ""
    assert when_text("") == ""


# ---------------------------------------------------------------------------
# Reset: destructive, and precisely scoped
# ---------------------------------------------------------------------------

@pytest.fixture
def store():
    folder = Path(tempfile.mkdtemp())
    with SqliteStore(folder / "reset.db") as opened:
        for number in range(5):
            file_id = opened.upsert_file(
                path=f"doc{number}.txt", size_bytes=10, mtime_ns=1, status="INDEXED",
            )
            opened.replace_chunks(file_id, [{
                "text": f"body {number}", "ordinal": 0,
                "char_start": 0, "char_end": 6, "page": None,
            }])
        opened.set_state("ui:roots", r"D:\Docs|D:\Mail")
        opened.set_state("ui:theme", "dark")
        opened.set_state("ui:index_schedule", "daily")
        opened.set_state("index:last_run", "2026-08-24T22:00:00")
        yield opened


def test_reset_removes_every_document_and_passage(store):
    removed = store.clear_index()

    assert removed == 5
    assert store.stats()["files_total"] == 0
    assert store.stats()["chunks_total"] == 0


def test_reset_keeps_the_settings_somebody_configured(store):
    """A reset that also forgot which folders to index would be one nobody
    could recover from without setting the application up again."""
    store.clear_index()

    assert store.get_state("ui:roots") == r"D:\Docs|D:\Mail"
    assert store.get_state("ui:theme") == "dark"
    assert store.get_state("ui:index_schedule") == "daily"


def test_reset_clears_the_cursors(store):
    """They point at chunk ids that no longer exist. Left behind, the next run
    resumes past the beginning of an empty index and indexes nothing."""
    store.clear_index()
    assert store.get_state("index:last_run") is None


def test_reset_is_safe_to_run_twice(store):
    store.clear_index()
    assert store.clear_index() == 0


def test_reset_leaves_the_index_searchable_afterwards(store):
    """The store must still work - a reset is "start over", not "break it"."""
    store.clear_index()
    file_id = store.upsert_file(path="new.txt", size_bytes=1, mtime_ns=1, status="INDEXED")
    store.replace_chunks(file_id, [{
        "text": "fresh content", "ordinal": 0, "char_start": 0, "char_end": 5, "page": None,
    }])
    assert store.stats()["files_total"] == 1
