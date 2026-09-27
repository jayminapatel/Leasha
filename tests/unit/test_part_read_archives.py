"""Work order `dates-live-log-and-interrupted-runs` 3c: an archive read part-way says so.

    3c  an archive read part-way says so on the Indexing page: which one, and
        that it will carry on next run. This is kept distinct from "Mail
        archives partly read", which means damage, not interruption.

The fact comes from the archive's folder cursor (3b), which exists from the
moment a run stops inside an archive until a run reads it to the end - so the
row is exactly as long-lived as the truth it states. The damage row comes from a
different record (`warned_by_code`) and its wording is left as it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.index.interrupted import ARCHIVE_RESUME_PREFIX, read_part_read_archives
from app.storage.sqlite_store import SqliteStore
from app.ui.presenter.activity import activity_lines
from app.ui.presenter import (
    PART_READ_LABEL,
    PART_READ_LABEL_MANY,
    index_summary,
    part_read_rows,
    read_index_summary,
)
from tests.unit.test_pst_folder_resume import (  # noqa: F401 - fixtures, used by name
    _libpff_backend,
    archive,
    build_tree,
    index,
)
from tests.unit.test_pst_libpff import install_fake

DAMAGE_LABEL = "Mail archives partly read"
DAMAGE_NOTE = ("Everything readable is searchable. The log names what was "
               "missed; scanpst.exe repairs a damaged archive, then index again.")


def _cursor(store: SqliteStore, path: Path, key: str = "a") -> None:
    store.set_state(f"{ARCHIVE_RESUME_PREFIX}{key}", json.dumps(
        {"path": str(path), "size": 1, "mtime_ns": 1, "folder": 3, "read": 4, "seen": []}))


# --- reading it -------------------------------------------------------------

def test_an_archive_with_a_cursor_is_listed_by_name(tmp_path) -> None:
    pst = tmp_path / "2007.pst"
    pst.write_bytes(b"x")
    with SqliteStore(tmp_path / "i.db") as store:
        _cursor(store, pst)
        assert read_part_read_archives(store) == [{"path": str(pst), "name": "2007.pst"}]


def test_what_is_not_an_archive_cursor_or_no_longer_exists_is_left_out(tmp_path) -> None:
    with SqliteStore(tmp_path / "i.db") as store:
        _cursor(store, tmp_path / "deleted.pst", key="gone")
        store.set_state(f"{ARCHIVE_RESUME_PREFIX}torn", "{not json")
        store.set_state("resume:0123abcd", "17")           # an mbox message cursor
        assert read_part_read_archives(store) == []


def test_the_summary_payload_carries_it(tmp_path) -> None:
    pst = tmp_path / "2007.pst"
    pst.write_bytes(b"x")
    with SqliteStore(tmp_path / "i.db") as store:
        _cursor(store, pst)
        assert read_index_summary(store)["part_read"][0]["name"] == "2007.pst"


def test_cli_stats_names_it(tmp_path, temp_env, monkeypatch, capsys) -> None:
    from app.cli import main
    from app.core.config import load_settings

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    pst = tmp_path / "2007.pst"
    pst.write_bytes(b"x")
    with SqliteStore(load_settings(temp_env).fts_db) as store:
        _cursor(store, pst)
    assert main(["--env", str(temp_env), "stats"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "Mail archive not finished: 2007.pst. A run stopped part-way through it." in out

    assert main(["--env", str(temp_env), "--json", "stats"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["part_read_archives"][0]["name"] == "2007.pst"


# --- the words, kept apart from damage ----------------------------------------

def test_one_archive_is_named_and_told_it_carries_on() -> None:
    [row] = part_read_rows([{"path": "D:/Mail/2007.pst", "name": "2007.pst"}])
    assert row.label == PART_READ_LABEL == "Mail archive not finished"
    assert row.value == "2007.pst"
    assert row.note == ("A run stopped part-way through it. The next index run "
                        "carries on with it, and everything already read is "
                        "searchable now.")
    assert row.warn is False


def test_several_are_counted_and_the_first_few_named() -> None:
    names = [{"path": f"D:/{n}.pst", "name": f"{n}.pst"} for n in (2005, 2006, 2007, 2008)]
    [row] = part_read_rows(names)
    assert row.label == PART_READ_LABEL_MANY == "Mail archives not finished"
    assert row.value == "4"
    assert "2005.pst, 2006.pst, 2007.pst and 1 more" in row.note
    [two] = part_read_rows(names[:2])
    assert "2005.pst and 2006.pst." in two.note


def test_nothing_is_said_while_a_run_is_carrying_on_or_when_there_is_none() -> None:
    assert part_read_rows([{"path": "D:/2007.pst", "name": "2007.pst"}], running=True) == []
    assert part_read_rows(None) == [] and part_read_rows([]) == []


def test_it_never_uses_the_damage_rows_words() -> None:
    [row] = part_read_rows([{"path": "D:/2007.pst", "name": "2007.pst"}])
    assert "partly read" not in f"{row.label} {row.note}".lower()
    assert "scanpst" not in row.note


def test_the_damage_row_is_unchanged() -> None:
    """3c must not reword the existing row (standing rule)."""
    rows = index_summary({"files_total": 1, "chunks_total": 0},
                         warned={"ERR_PST_PARTIAL": 2})
    [damage] = [r for r in rows if r.label == DAMAGE_LABEL]
    assert damage.value == "2" and damage.note == DAMAGE_NOTE and damage.warn


# --- end to end: stop inside an archive, see it, carry on, see it go ------------

def test_a_run_stopped_inside_an_archive_is_shown_until_one_finishes_it(
        tmp_path, archive, monkeypatch) -> None:
    db = tmp_path / "index.db"
    install_fake(monkeypatch, build_tree()["root"])
    stopped = index(db, archive, stop_after=6)
    assert any("Stopped part-way through 2007.pst" in line
               for line in activity_lines(stopped.activity.entries()))
    with SqliteStore(db) as store:
        assert [a["name"] for a in read_index_summary(store)["part_read"]] == ["2007.pst"]

    install_fake(monkeypatch, build_tree()["root"])
    finished = index(db, archive)
    assert any("Carrying on with 2007.pst from where an earlier run stopped." in line
               for line in activity_lines(finished.activity.entries()))
    with SqliteStore(db) as store:
        assert read_index_summary(store)["part_read"] == []


@pytest.mark.gui
def test_the_indexing_page_shows_both_rows_apart(qtbot, tmp_path) -> None:
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QLabel

    from app.ui.indexing_view import IndexingView

    pst = tmp_path / "2007.pst"
    pst.write_bytes(b"x")
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file("C:/mail/a.pst", size_bytes=1, mtime_ns=1,
                                    status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": "one message"}])
        store.set_state("last_run_stats", repr({"warned_by_code": {"ERR_PST_PARTIAL": 1}}))
        _cursor(store, pst)

        view = IndexingView()
        qtbot.addWidget(view)
        view.show()
        view.refresh_totals(store)

        def shown() -> list[str]:
            return [w.text() for w in view.stats_box.findChildren(QLabel)]

        qtbot.waitUntil(lambda: PART_READ_LABEL in shown(), timeout=5000)
        texts = shown()
        assert DAMAGE_LABEL in texts, "the damage row went missing"
        assert texts[texts.index(PART_READ_LABEL) + 1] == "2007.pst"
        QThreadPool.globalInstance().waitForDone(2000)
