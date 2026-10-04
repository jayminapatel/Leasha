r"""The one-word status: the vocabulary, the store's grouped counts, the funnel.

Layer: L0 (`app.core.file_state`), L1 (`SqliteStore.status_counts`) and L5
(the funnel on the Indexing page).

The owner: *"in the results it must show the single word status engine and
this should be visible in the results"* - with a Status column in every results
list and a live funnel of counts per status at the top of the Indexing page.
This file holds the words and the counts; `test_status_column.py` holds the
columns and the list totals.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core import file_state as fs
from app.core.errors import make_error
from app.storage.sqlite_store import FileStatus, SqliteStore

# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

def test_every_word_is_a_single_word_with_a_plain_sentence() -> None:
    assert fs.WORDS == ("Discovered", "Queued", "Reading", "Indexed", "Skipped",
                        "Failed", "TimedOut", "Offline", "NameOnly", "Duplicate",
                        "Deferred")
    for word in fs.WORDS:
        assert " " not in word
        sentence = fs.explain(word)
        assert sentence.endswith("."), word
        assert "ERR_" not in sentence, f"{word}'s tooltip names a code"
    assert set(fs.FUNNEL_ORDER) == set(fs.WORDS)
    assert fs.explain("") == fs.explain("Nonsense") == ""


@pytest.mark.parametrize(("status", "code", "offline", "word"), [
    ("INDEXED", None, False, "Indexed"),
    # Keyword-searchable now; only the vectors are still catching up.
    ("PARTIAL", None, False, "Indexed"),
    ("NAME_ONLY", None, False, "NameOnly"),
    ("PENDING", None, False, "Queued"),
    ("SKIPPED", "ERR_FILE_CORRUPT", False, "Skipped"),
    ("FAILED", "ERR_UNEXPECTED", False, "Failed"),
    ("SKIPPED", "ERR_OCR_HELD", False, "Deferred"),
    ("SKIPPED", "ERR_MEDIA_BACKLOG", False, "Deferred"),
    ("SKIPPED", "ERR_MEDIA_HELD", False, "Deferred"),
    ("SKIPPED", "ERR_FILE_LOCKED", False, "Deferred"),
    # The per-file time limit's code, mapped before anything writes it.
    ("SKIPPED", "ERR_FILE_TIMEOUT", False, "TimedOut"),
    ("FAILED", "ERR_FILE_TIMEOUT", False, "TimedOut"),
    # Offline beats everything: a file in a drawer cannot be opened.
    ("INDEXED", None, True, "Offline"),
    ("SKIPPED", "ERR_OCR_HELD", True, "Offline"),
    # A stale code on a row that is no longer skipped is not current.
    ("NAME_ONLY", "ERR_OCR_HELD", False, "NameOnly"),
    ("PENDING", "ERR_FILE_TIMEOUT", False, "Queued"),
    ("", None, False, ""),
    ("SOMETHING_NEW", None, False, ""),
])
def test_derive_maps_what_the_row_carries(status, code, offline, word) -> None:
    assert fs.derive(status, code, offline=offline) == word


def test_every_store_status_has_a_word() -> None:
    for status in FileStatus.ALL:
        assert fs.derive(status) in fs.WORDS, status


def test_deferred_is_the_pipelines_own_set() -> None:
    """Two copies, held together here: the pipeline is L3 and core cannot
    import it, and a code added to one and not the other would make a held
    file read "Skipped" in the results while the pipeline queues it."""
    from app.index.pipeline import Pipeline

    assert fs.DEFERRED_CODES == Pipeline.DEFERRED_SKIP_CODES


def test_the_deferred_and_timeout_codes_do_not_overlap() -> None:
    assert not fs.DEFERRED_CODES & fs.TIMEOUT_CODES
    assert "ERR_FILE_TIMEOUT" in fs.TIMEOUT_CODES


def test_funnel_counts_moves_rows_into_their_word_once() -> None:
    counts = fs.funnel_counts(
        {"INDEXED": 100, "PARTIAL": 5, "SKIPPED": 30, "FAILED": 2, "PENDING": 7,
         "NAME_ONLY": 9},
        [("SKIPPED", "ERR_OCR_HELD", 10), ("SKIPPED", "ERR_FILE_TIMEOUT", 3)],
        {"INDEXED": 4, "SKIPPED": 1},
        reading=2, discovered=50)
    assert counts["Indexed"] == 100 + 5 - 4
    assert counts["Deferred"] == 10
    assert counts["TimedOut"] == 3
    assert counts["Skipped"] == 30 - 10 - 3 - 1
    assert counts["Offline"] == 5
    assert counts["Reading"] == 2 and counts["Discovered"] == 50
    assert counts["Queued"] == 7 and counts["NameOnly"] == 9 and counts["Failed"] == 2
    assert set(counts) == set(fs.WORDS)
    # Nothing lost or invented: the store's rows, plus the two live numbers.
    assert sum(counts.values()) == 153 + 2 + 50


def test_funnel_line_reads_like_the_owners_example() -> None:
    line = fs.funnel_line({"Indexed": 448210, "Queued": 1200, "Reading": 4,
                           "Skipped": 310, "Failed": 12, "Deferred": 45})
    assert line.startswith("Indexed 448,210 \u00b7 ")
    for part in ("Queued 1,200", "Reading 4", "Skipped 310", "Failed 12", "Deferred 45"):
        assert part in line
    assert "Offline" not in line, "a zero count is left off the line"
    assert fs.funnel_line({}) == "Indexed 0"


# ---------------------------------------------------------------------------
# The store's grouped counts
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path: Path):
    s = SqliteStore(tmp_path / "index.db").connect()
    yield s
    s.close()


def _file(store: SqliteStore, name: str, *, status: str = FileStatus.PENDING,
          volume_id=None) -> int:
    return store.upsert_file(
        f"C:/corpus/{name}", size_bytes=10, mtime_ns=time.time_ns(), status=status,
        volume_id=volume_id, relative_path=name if volume_id else None)


def _corpus(store: SqliteStore) -> dict[str, int]:
    ids = {}
    for n in range(4):
        ids[f"doc{n}"] = _file(store, f"doc{n}.txt")
        store.mark_indexed(ids[f"doc{n}"])
    ids["film"] = _file(store, "film.mp4", status=FileStatus.NAME_ONLY)
    ids["pending"] = _file(store, "pending.txt")
    ids["broken"] = _file(store, "broken.pdf")
    store.mark_skipped(ids["broken"], make_error("ERR_FILE_CORRUPT", "test", path="broken.pdf"))
    ids["scan"] = _file(store, "scan.png")
    store.mark_skipped(ids["scan"], make_error("ERR_OCR_HELD", "test", path="scan.png"))
    ids["slow"] = _file(store, "slow.pdf")
    with store.write() as conn:        # the code the time-limit lane will write
        conn.execute("UPDATE files SET status = 'SKIPPED', skip_code = 'ERR_FILE_TIMEOUT' "
                     "WHERE id = ?", (ids["slow"],))
    volume = store.upsert_volume("guid-1", kind="drive", name="Projects 2019",
                                 status="OFFLINE")
    ids["away"] = _file(store, "away.docx", volume_id=volume)
    store.mark_indexed(ids["away"])
    ids["away_held"] = _file(store, "away.png", volume_id=volume)
    store.mark_skipped(ids["away_held"], make_error("ERR_OCR_HELD", "test", path="away.png"))
    ids["volume"] = volume
    return ids


def test_status_counts_feed_one_count_per_word(store) -> None:
    ids = _corpus(store)
    assert store.offline_volume_ids() == [ids["volume"]]
    grouped = store.status_counts(store.offline_volume_ids())
    counts = fs.funnel_counts(grouped["by_status"], grouped["coded"], grouped["offline"])
    assert counts["Indexed"] == 4
    assert counts["NameOnly"] == 1
    assert counts["Queued"] == 1
    assert counts["Skipped"] == 1
    assert counts["Deferred"] == 1, "the held picture on the offline drive counts once"
    assert counts["TimedOut"] == 1
    assert counts["Offline"] == 2
    assert sum(counts.values()) == 11


def test_the_funnel_statements_use_the_indexes_they_claim(store) -> None:
    """Held, not assumed: the docstring of `status_counts` names three
    indexes, and a funnel that full-scanned twenty million rows every five
    seconds during a run is exactly the cost it was written to avoid."""
    _corpus(store)
    plans: list[str] = []
    real = store.conn

    class Spy:
        def execute(self, sql, params=()):
            # 2026-10-04: `status_counts` asks `PRAGMA data_version` first, to
            # re-read only after a write; a PRAGMA has no plan to hold.
            if sql.lstrip().upper().startswith("PRAGMA"):
                return real.execute(sql, params)
            plans.append(" ".join(
                str(row[-1]) for row in real.execute("EXPLAIN QUERY PLAN " + sql, params)))
            return real.execute(sql, params)

    original = type(store).conn
    try:
        type(store).conn = property(lambda _self: Spy())
        store.status_counts([1])
    finally:
        type(store).conn = original
    assert len(plans) == 3
    # The whole-table count reads the narrow status index, never the table;
    # the other two are searches, bounded by the rows they are about.
    assert "SCAN files USING COVERING INDEX idx_files_status" in plans[0]
    assert "SEARCH files USING INDEX idx_files_skip" in plans[1]
    assert "SEARCH files USING INDEX idx_files_volume" in plans[2]


def test_file_states_is_one_read_for_a_page(store) -> None:
    ids = _corpus(store)
    states = store.file_states([ids["doc0"], ids["scan"], 999_999])
    assert set(states) == {ids["doc0"], ids["scan"]}
    assert states[ids["scan"]]["skip_code"] == "ERR_OCR_HELD"


def test_the_funnel_worker_adds_the_live_numbers(store) -> None:
    from types import SimpleNamespace

    from app.ui.tasks import status_funnel_counts

    _corpus(store)
    stats = SimpleNamespace(
        seen=40, indexed=20, unchanged=5, skipped=3,
        workers={"1": {"file": "big.pdf"}, "2": {"file": ""}, "3": {"file": "a.docx"}})
    counts = status_funnel_counts(store, stats)
    assert counts["Reading"] == 2
    # 2026-10-04: a file the scan has listed by name is a PENDING row, counted
    # as Queued by the store - so it is not also counted as Discovered.
    pending = store.conn.execute("SELECT COUNT(*) FROM files WHERE status = 'PENDING'").fetchone()[0]
    assert counts["Discovered"] == 40 - 28 - 2 - pending
    assert counts["Indexed"] == 4
    # Nothing running: the store's numbers only.
    idle = status_funnel_counts(store)
    assert idle["Reading"] == idle["Discovered"] == 0


def test_the_index_summary_carries_the_funnel(store) -> None:
    from app.ui.tasks import read_index_summary

    _corpus(store)
    payload = read_index_summary(store)
    assert payload["funnel"]["Indexed"] == 4
    assert payload["funnel"]["Deferred"] == 1


# ---------------------------------------------------------------------------
# The funnel on the Indexing page
# ---------------------------------------------------------------------------

def test_the_funnel_is_at_the_top_of_the_indexing_page(qtbot, store) -> None:
    from app.ui.indexing_view import IndexingView
    from app.ui.tasks import read_index_summary
    from app.ui.widgets.indexing_layout import paint_totals
    from app.ui.widgets.status_funnel import OBJECT_NAME

    _corpus(store)
    view = IndexingView()
    qtbot.addWidget(view)
    assert view.funnel.objectName() == OBJECT_NAME
    assert not view.funnel.isVisibleTo(view), "hidden until it has counts"
    paint_totals(view, read_index_summary(store))
    text = view.funnel.text()
    assert text.startswith("Indexed 4 \u00b7 ")
    for part in ("Queued 1", "Deferred 1", "NameOnly 1", "Skipped 1", "TimedOut 1",
                 "Offline 2"):
        assert part in text
    assert view.funnel.isVisibleTo(view)
    assert "Deferred: " + fs.explain("Deferred") in view.funnel.toolTip()
    # At the top of the page: straight after the headline and its "now" line,
    # above the totals and the bar.
    layout = view.headline.parentWidget().layout()
    assert layout.indexOf(view.funnel) == layout.indexOf(view.now_line) + 1
    assert layout.indexOf(view.funnel) < layout.indexOf(view.totals)


def test_a_failed_read_leaves_the_last_counts(qtbot) -> None:
    from app.ui.widgets.status_funnel import StatusFunnel

    funnel = StatusFunnel()
    qtbot.addWidget(funnel)
    funnel.show_counts({"Indexed": 3})
    funnel.show_counts(None)
    assert funnel.text() == "Indexed 3"


def test_a_progress_tick_refreshes_on_a_worker_throttled(qtbot, store) -> None:
    from types import SimpleNamespace

    from app.ui.widgets.status_funnel import StatusFunnel

    _corpus(store)
    funnel = StatusFunnel()
    qtbot.addWidget(funnel)
    view = SimpleNamespace(_worker=SimpleNamespace(pipeline=SimpleNamespace(store=store)))
    stats = SimpleNamespace(seen=0, indexed=0, unchanged=0, skipped=0,
                            workers={"1": {"file": "x.pdf"}})
    assert funnel.tick(view, stats) is True
    qtbot.waitUntil(lambda: "Reading 1" in funnel.text(), timeout=5000)
    # Straight away again: throttled, no second read.
    assert funnel.tick(view, stats) is False
    # No run in this process: nothing to read from.
    assert funnel.tick(SimpleNamespace(_worker=None), stats) is False


def test_a_run_in_a_separate_process_keeps_the_funnel_live(qtbot, store) -> None:
    """Order 0z A3, audit of 2026-09-30. With "Index in a separate process" on
    the run is a `ChildIndexRun`, whose `store` is None, so every tick was
    turned away and the line stood still until the run ended. It now reads
    through the window's own store, which the run carries as `read_store`.
    Fails on the code as it was (`tick` returned False)."""
    from types import SimpleNamespace

    from app.index.child_run import ChildIndexRun
    from app.ui.widgets.status_funnel import StatusFunnel

    _corpus(store)
    run = ChildIndexRun(["python", "-m", "app.cli", "index"])
    assert run.store is None and run.read_store is None
    funnel = StatusFunnel()
    qtbot.addWidget(funnel)
    view = SimpleNamespace(_worker=SimpleNamespace(pipeline=run))
    stats = SimpleNamespace(seen=0, indexed=0, unchanged=0, skipped=0,
                            workers={"1": {"file": "x.pdf"}})
    assert funnel.tick(view, stats) is False, "no store at all: still nothing to read"
    run.read_store = store
    assert funnel.tick(view, stats) is True
    qtbot.waitUntil(lambda: "Reading 1" in funnel.text(), timeout=5000)
    assert funnel.text().startswith("Indexed 4 · ")


def test_outlook_busy_is_retried_and_reads_as_deferred() -> None:
    """2026-10-04, the owner ("do the recommended"): the error promises a retry
    "on the next pass", and the run now gives one - from the one list the
    Status column and the indexer share."""
    from app.core.file_state import DEFERRED, DEFERRED_CODES, derive
    from app.index.pipeline import Pipeline

    assert derive("SKIPPED", "ERR_OUTLOOK_BUSY") == DEFERRED
    assert Pipeline.DEFERRED_SKIP_CODES is DEFERRED_CODES, "one list, not two copies"
    assert "ERR_OUTLOOK_BUSY" in Pipeline.DEFERRED_SKIP_CODES


def test_the_files_date_column_is_headed_date() -> None:
    """It shows a photo's taken date and an attachment's sent date, so "Date"
    (2026-10-04, was "Modified"); the key is unchanged for saved choices."""
    from app.ui.files_view import COLUMNS

    assert ("modified", "Date") in [(key, heading) for key, heading, *_ in COLUMNS]
