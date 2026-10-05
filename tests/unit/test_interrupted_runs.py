"""Work order `dates-live-log-and-interrupted-runs` 3a and 3d: a run that did not finish.

    3a  a run that ended without finishing (crash, power cut, the app killed)
        is recognised the next time the Indexing page opens. The page says so
        in plain words: when it stopped, how many files it had not reached, and
        that starting again carries on from there with nothing lost.
    3d  ... A killed run is detected as interrupted, and a clean stop is not.

**The kill is real.** `interrupted_run_child.py` takes the run lock the way
`app.cli index` does, runs a real `Pipeline` and ends its own process with
`os._exit(9)` part-way through - nothing unwinds, exactly as when the power goes.
A record written by hand would prove only that this module reads what a test
wrote; the child proves the pipeline leaves the evidence behind.

The lock directory is the test's own (`lock_dir`, and `TMPDIR` for the CLI):
the default is shared by every process on the machine, and a run in another
checkout would otherwise read as "a run is going on" here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core.run_lock import COMMAND_LINE, RUN_STATE_KEY, IndexRunLock, publish
from app.index.interrupted import read_unfinished_run
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from app.ui.presenter import (
    UNFINISHED_LABEL,
    stopped_when,
    unfinished_reach,
    unfinished_run_line,
    unfinished_run_rows,
)
from tests.unit.test_index_freshness import NullVectors, fake_embedder, write_aged

PROJECT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    for n in range(6):
        write_aged(root / f"note{n}.txt", f"Barnsley Dairy note {n} about the HACCP audit.")
    return root


@pytest.fixture()
def locks(tmp_path: Path) -> Path:
    folder = tmp_path / "locks"
    folder.mkdir()
    return folder


def _kill_mid_run(db: Path, root: Path, locks: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(PROJECT))
    return subprocess.run(
        [sys.executable, "-m", "tests.unit.interrupted_run_child", str(db), str(root),
         str(locks)],
        cwd=str(PROJECT), env=env, capture_output=True, text=True, timeout=180)


# --- detection: the store, the lock and a real process ----------------------

def test_a_killed_run_is_recognised_as_not_finished(tmp_path, corpus, locks) -> None:
    db = tmp_path / "index.db"
    before = time.time()
    done = _kill_mid_run(db, corpus, locks)
    assert done.returncode == 9, f"the child did not die mid-run:\n{done.stderr[-3000:]}"

    with SqliteStore(db) as store:
        found = read_unfinished_run(store, lock_dir=locks)
        record = json.loads(store.get_state(RUN_STATE_KEY))

    assert found is not None, "a run killed part-way left nothing to say so"
    # The pipeline's own checkpoint wrote it, folders and all - which is what
    # lets a scan's total become "files not reached".
    assert record["roots"] == [str(corpus)]
    assert found["owner"] == COMMAND_LINE
    assert found["indexed"] >= 1
    assert found["seen"] >= 1
    assert before - 1 <= found["stopped_at"] <= time.time()


def test_a_clean_stop_is_not_reported_as_interrupted(tmp_path, corpus, locks) -> None:
    """The Stop button, `request_stop` from another process and the disk guard
    all end here: the run returns, the lock is released, the record comes down."""
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[corpus]), workers=1,
                                checkpoint_every=1)
        pipeline = Pipeline(store, NullVectors(), fake_embedder(), config)

        def stop_early(stats) -> None:
            if stats.indexed >= 1:
                pipeline.request_stop()

        with IndexRunLock(store, owner=COMMAND_LINE, lock_dir=locks):
            stats = pipeline.run(on_progress=stop_early)
            # The record was there while it ran: the check below is not
            # passing merely because nothing was ever written.
            assert store.get_state(RUN_STATE_KEY)
        assert stats.indexed < 6, "the stop never landed; the test proves nothing"

        assert read_unfinished_run(store, lock_dir=locks) is None


def test_a_finished_run_is_not_reported(tmp_path, corpus, locks) -> None:
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[corpus]), workers=1)
        with IndexRunLock(store, owner=COMMAND_LINE, lock_dir=locks):
            Pipeline(store, NullVectors(), fake_embedder(), config).run()
        assert read_unfinished_run(store, lock_dir=locks) is None


def test_a_run_that_is_still_going_or_paused_is_not_reported(tmp_path, locks) -> None:
    """A pause holds the lock like any live run. The record alone is not enough."""
    with SqliteStore(tmp_path / "index.db") as store:
        with IndexRunLock(store, owner=COMMAND_LINE, lock_dir=locks):
            assert store.get_state(RUN_STATE_KEY)
            assert read_unfinished_run(store, lock_dir=locks) is None


def test_no_record_at_all_is_nothing_to_report(tmp_path, locks) -> None:
    with SqliteStore(tmp_path / "index.db") as store:
        assert read_unfinished_run(store, lock_dir=locks) is None


def test_the_files_not_reached_come_from_a_scan_of_the_same_folders(tmp_path, locks) -> None:
    from types import SimpleNamespace

    from app.index.scan import SCAN_STATE_KEY

    with SqliteStore(tmp_path / "index.db") as store:
        stats = SimpleNamespace(seen=300, indexed=250, walk_complete=False)
        publish(store, owner=COMMAND_LINE, started_at=time.time() - 60, stats=stats,
                roots=[Path("D:/Data")])
        store.set_state(SCAN_STATE_KEY, json.dumps(
            {"roots": ["D:/Data"], "files": 1504, "bytes": 1}))
        assert read_unfinished_run(store, lock_dir=locks)["not_reached"] == 1204

        # A scan of different folders is no count: the sentence must not guess.
        store.set_state(SCAN_STATE_KEY, json.dumps(
            {"roots": ["E:/Other"], "files": 1504, "bytes": 1}))
        assert read_unfinished_run(store, lock_dir=locks)["not_reached"] is None


def test_a_run_that_died_before_its_first_checkpoint_is_still_reported(tmp_path, locks) -> None:
    """The lock publishes on the way in, before the pipeline knows anything."""
    with SqliteStore(tmp_path / "index.db") as store:
        publish(store, owner=COMMAND_LINE, started_at=time.time() - 5, stats=None)
        found = read_unfinished_run(store, lock_dir=locks)
    assert found is not None and found["seen"] == 0
    assert unfinished_reach(found) == "It stopped before it had looked at any files."


def test_a_torn_record_costs_the_line_and_nothing_else(tmp_path, locks) -> None:
    with SqliteStore(tmp_path / "index.db") as store:
        store.set_state(RUN_STATE_KEY, "{not json")
        assert read_unfinished_run(store, lock_dir=locks) is None


def test_the_summary_payload_carries_it(tmp_path, locks, monkeypatch) -> None:
    """The Indexing page's worker body, which is where the page learns of it."""
    from app.ui.presenter import read_index_summary

    monkeypatch.setenv("TMPDIR", str(locks))
    with SqliteStore(tmp_path / "index.db") as store:
        publish(store, owner=COMMAND_LINE, started_at=time.time() - 5, stats=None)
        assert read_index_summary(store)["unfinished"]["owner"] == COMMAND_LINE


# --- the words ---------------------------------------------------------------

NOW = time.mktime((2026, 9, 27, 9, 30, 0, 0, 0, -1))


def _at(hour: int, minute: int, *, day: int = 27, month: int = 9, year: int = 2026) -> float:
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def test_when_it_stopped_is_said_the_way_a_person_says_it() -> None:
    assert stopped_when(_at(2, 14), now=NOW) == "about 02:14 today"
    assert stopped_when(_at(23, 5, day=26), now=NOW) == "about 23:05 yesterday"
    assert stopped_when(_at(14, 2, day=7), now=NOW) == "about 14:02 on 7 September"
    assert stopped_when(_at(14, 2, day=7, year=2025), now=NOW) == \
        "about 14:02 on 7 September 2025"
    assert stopped_when(None, now=NOW) == "at an unknown time"


def test_how_far_it_had_got_never_invents_a_number() -> None:
    assert unfinished_reach({"seen": 300, "not_reached": 1204}) == \
        "About 1,204 files had not been reached yet."
    assert unfinished_reach({"seen": 300, "not_reached": 1}) == \
        "About 1 file had not been reached yet."
    assert unfinished_reach({"seen": 12345, "not_reached": None}) == (
        "It had looked at 12,345 files and had not finished going through your folders.")
    assert unfinished_reach({"seen": 900, "walk_complete": True}) == (
        "It had found all 900 files and was part-way through reading them.")


def test_the_page_row_says_when_how_far_and_that_nothing_is_lost() -> None:
    found = {"stopped_at": _at(2, 14), "seen": 300, "not_reached": 1204}
    [row] = unfinished_run_rows(found, now=NOW)
    assert row.label == UNFINISHED_LABEL == "Last run did not finish"
    assert row.value == "Stopped at about 02:14 today"
    assert row.note == (
        "About 1,204 files had not been reached yet. Starting the index again "
        "carries on from where it stopped, and nothing already indexed is lost.")
    # Calm: nothing is wrong, and a warning colour sends people to Reset.
    assert row.warn is False


def test_the_row_comes_down_while_a_run_is_going() -> None:
    found = {"stopped_at": _at(2, 14), "seen": 300}
    assert unfinished_run_rows(found, running=True) == []
    assert unfinished_run_rows(None) == []


def test_the_command_line_says_the_same_thing() -> None:
    found = {"stopped_at": _at(2, 14), "seen": 300, "not_reached": 1204}
    assert unfinished_run_line(found, now=NOW) == (
        "The last index run did not finish: it stopped at about 02:14 today. About "
        "1,204 files had not been reached yet. Starting the index again carries on "
        "from where it stopped, and nothing already indexed is lost.")
    assert unfinished_run_line(found, carrying_on=True, now=NOW).endswith(
        "This run carries on from where it stopped; nothing already indexed is lost.")
    assert unfinished_run_line(None) == ""
    assert unfinished_run_line(found, now=NOW).isascii()


# --- the CLI (non-negotiable #8) --------------------------------------------

@pytest.fixture()
def planted(temp_env: Path, locks: Path, monkeypatch) -> list[str]:
    """A record left behind by a run that died, in the CLI's own store."""
    from app.core.config import load_settings

    monkeypatch.setenv("TMPDIR", str(locks))
    settings = load_settings(temp_env)
    with SqliteStore(settings.fts_db) as store:
        publish(store, owner=COMMAND_LINE, started_at=time.time() - 600,
                stats=None)
    return ["--env", str(temp_env)]


def test_cli_stats_reports_a_run_that_did_not_finish(capsys, planted) -> None:
    from app.cli import main

    assert main([*planted, "stats"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "The last index run did not finish" in out
    assert "nothing already indexed is lost" in out

    assert main([*planted, "--json", "stats"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["unfinished_run"]["owner"] == COMMAND_LINE


def test_cli_index_says_it_is_carrying_on(capsys, planted, corpus, monkeypatch) -> None:
    from app.cli import main
    from tests.unit.test_cli_index import fake_embedder as cli_fake

    monkeypatch.setattr("app.index.embedder.Embedder", cli_fake)
    assert main([*planted, "index", str(corpus), "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "The last index run did not finish" in out
    assert "This run carries on from where it stopped" in out

    # And having carried on to the end, there is nothing left to say.
    assert main([*planted, "stats"]) == 0
    assert "did not finish" not in capsys.readouterr().out


# --- the page ----------------------------------------------------------------

@pytest.mark.gui
def test_the_indexing_page_shows_it_and_takes_it_down_when_a_run_starts(
        qtbot, tmp_path, locks, monkeypatch) -> None:
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QLabel

    from app.ui.indexing_view import IndexingView
    from app.ui.widgets.indexing_layout import repaint_totals

    monkeypatch.setenv("TMPDIR", str(locks))
    with SqliteStore(tmp_path / "index.db") as store:
        publish(store, owner=COMMAND_LINE, started_at=time.time() - 600, stats=None)

        view = IndexingView()
        qtbot.addWidget(view)
        view.show()
        view.refresh_totals(store)

        def shown() -> list[str]:
            return [w.text() for w in view.stats_box.findChildren(QLabel)]

        qtbot.waitUntil(lambda: UNFINISHED_LABEL in shown(), timeout=5000)
        assert any("nothing already indexed is lost" in text for text in shown())
        QThreadPool.globalInstance().waitForDone(2000)

        view._worker = object()          # what `start` sets; no pipeline needed
        try:
            repaint_totals(view)
            assert UNFINISHED_LABEL not in shown()
        finally:
            view._worker = None
