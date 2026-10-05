r"""Order 202626270602 (0n) - "each report degrades gracefully when a dependency's
data is thin (states plainly what it can't yet say, never errors)."

Layer: L4 + L5

**The bug this file was written to catch** (found 2026-09-20): the Space Report
on an index where nothing had been read for its contents yet - a name-only pass,
or a first run still going - printed *"No duplicate files were found."* That is
a statement of fact about the owner's files, and it was not true; it was a
statement about a column that was empty. The report has to say what it cannot
yet say instead.
"""

from __future__ import annotations

import pytest

from app.reports import space
from app.reports.inheritance import catalogue_sources, render_inheritance_document
from app.storage.sqlite_store import SqliteStore
from tests.unit.timeline_env import add_file, noon


def _index(tmp_path, *, hashed: int, unhashed: int):
    store = SqliteStore(tmp_path / "i.db").connect()
    for i in range(hashed):
        add_file(store, rf"D:\Docs\h{i}.txt", mtime=noon(2015, 6, 1 + i), content_hash=f"hash{i}")
    for i in range(unhashed):
        add_file(store, rf"D:\Docs\u{i}.txt", mtime=noon(2015, 7, 1 + i))
    return store


def _document(store) -> str:
    return str(space.document_for(space.SpaceFindings(
        groups=tuple(space.find_duplicate_groups(store)),
        uniqueness=tuple(space.find_source_uniqueness(store)),
        total_reclaimable=space.total_reclaimable_bytes(store), **space.hash_coverage(store))))


def test_nothing_read_yet_is_not_reported_as_no_duplicates(tmp_path):
    store = _index(tmp_path, hashed=0, unhashed=5)
    try:
        doc = _document(store)
        assert "No duplicate files were found" not in doc
        assert "None of the 5 files has been compared" in doc
        assert "cannot say what is duplicated" in doc
        assert "Nothing in the index exists on exactly one source yet" not in doc
    finally:
        store.close()


def test_partly_read_says_how_much_of_the_index_the_findings_cover(tmp_path):
    store = _index(tmp_path, hashed=3, unhashed=7)
    try:
        doc = _document(store)
        assert "compared 3 of 10 files" in doc and "covers only the ones that have" in doc
    finally:
        store.close()


def test_fully_read_says_nothing_extra_and_still_says_none_found(tmp_path):
    store = _index(tmp_path, hashed=4, unhashed=0)
    try:
        doc = _document(store)
        assert "No duplicate files were found." in doc
        assert "compared" not in doc.lower()
    finally:
        store.close()


def test_an_empty_index_does_not_claim_coverage_it_cannot_measure(tmp_path):
    with SqliteStore(tmp_path / "e.db") as store:
        assert space.hash_coverage(store) == {"files_total": 0, "files_compared": 0}
        doc = _document(store)
        assert "compared" not in doc.lower()          # nothing to compare is not a thin-data case


def test_the_table_headline_says_it_too(tmp_path):
    from app.ui.presenter.space_rows import space_headline

    store = _index(tmp_path, hashed=0, unhashed=5)
    try:
        findings = space.SpaceFindings(**space.hash_coverage(store))
        headline = space_headline(findings)
        assert "No duplicate files were found" not in headline
        assert "None of the 5 files has been compared" in headline
    finally:
        store.close()


def test_the_command_line_and_the_window_read_the_same_coverage(tmp_path, capsys):
    import json

    from tests.unit.test_timeline_cli import env_file, run
    from app import cli

    env = env_file(tmp_path)
    cli.cmd_init(cli.build_parser().parse_args(["init", "--env", env]))
    with SqliteStore(tmp_path / "data" / "index.db") as store:
        add_file(store, r"D:\Docs\a.txt", mtime=noon(2015, 6, 1))
    capsys.readouterr()
    assert run(["report", "space", "--env", env]) == cli.EXIT_OK
    assert "None of the 1 file has been compared" in capsys.readouterr().out
    assert run(["report", "space", "--json", "--env", env]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["files_total"] == 1 and payload["files_compared"] == 0


def test_the_windows_snapshot_carries_the_coverage(tmp_path):
    from app.ui.reports_view import _report_snapshot

    store = _index(tmp_path, hashed=0, unhashed=2)
    try:
        _sources, _at, document = _report_snapshot(store)
        assert (document.findings.files_total, document.findings.files_compared) == (2, 0)
        assert "None of the 2 files has been compared" in document
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The other two reports say what they cannot say, too
# ---------------------------------------------------------------------------

def test_an_empty_catalogue_says_so_rather_than_printing_an_empty_map(tmp_path):
    with SqliteStore(tmp_path / "e.db") as store:
        doc = render_inheritance_document(catalogue_sources(store))
    assert "This index has nothing recorded yet." in doc


def test_a_timeline_of_photographs_with_no_camera_dates_says_so(qtbot, tmp_path):
    from app.ui.timeline_view import TimelineView

    pytest.importorskip("PySide6")
    store = SqliteStore(tmp_path / "t.db").connect()
    for i in range(3):
        add_file(store, rf"D:\Photos\p{i}.jpg", mtime=noon(2019, 1, 1 + i))
    view = TimelineView(store)
    qtbot.addWidget(view)
    view.show()
    view.refresh()
    qtbot.waitUntil(lambda: view._overview is not None, timeout=8000)
    assert view.summary.text() == "3 items from 2019."
    assert not view.notes.isHidden()
    assert "None of your 3 photos has a date from the camera yet" in view.notes.text()
    assert "often the day they were copied" in view.notes.text()
    from PySide6.QtCore import QThreadPool
    QThreadPool.globalInstance().waitForDone(5000)
    store.close()


def test_an_empty_timeline_says_there_is_nothing_yet(qtbot, tmp_path):
    from app.ui.timeline_view import TimelineView

    with SqliteStore(tmp_path / "e.db") as store:
        view = TimelineView(store)
        qtbot.addWidget(view)
        view.show()
        view.refresh()
        qtbot.wait(300)
        assert view.summary.text().startswith("There is nothing on the timeline yet")
        assert view.notes.isHidden() and view.picker.year_box.count() == 0
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(5000)
