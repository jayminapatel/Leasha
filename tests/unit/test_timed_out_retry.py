r"""Retry with a longer time limit (work order 0z, item F3).

Layer: L3

What these pin, the pipeline ones each with a real `Pipeline` run:

* **The store lists the timed-out files by type with one indexed query**, and
  hands back one group's files.
* **A retry reads only its group.** Nothing else in the corpus is opened, and
  nothing is pruned.
* **The longer limit is for that run only.** The file that timed out is read
  under the longer limit; the settings are not written; the next ordinary run
  times a slow file out at the usual limit again.
* **A second timeout stays TimedOut** and its sentence says what it was given.
* **A file that has changed since** is read with the usual limit.
* **The command line** lists the groups and runs the retry.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.core.errors import make_error
from app.index import file_watch, timed_out_retry
from app.index import pipeline as pipeline_module
from app.index.embedder import Embedder
from app.index.pipeline import PipelineConfig
from app.index.resources import ResourceGovernor, ResourceLimits, Snapshot
from app.index.timed_out_retry import RetryTimedOut
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore
from tests.unit.test_file_watch import (  # noqa: F401 - `fast_watchdog` is a fixture
    FakeVectors, _corpus, _patch_extract, _pipeline, _record, _run,
    _stuck_in_python, fast_watchdog,
)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path: Path):
    s = SqliteStore(tmp_path / "index.db").connect()
    yield s
    s.close()


def _row(store: SqliteStore, name: str, *, code: str = "", volume_id=None,
         source_kind: str = "file") -> int:
    ext = Path(name).suffix.lstrip(".").lower()
    file_id = store.upsert_file(
        f"C:/corpus/{name}", size_bytes=10, mtime_ns=1_000, ext=ext,
        status=FileStatus.PENDING, source_kind=source_kind,
        volume_id=volume_id, relative_path=name if volume_id else None)
    if code:
        store.mark_skipped(file_id, make_error(
            code, "test", path=name, took="2 min", reason="it took too long"))
    else:
        store.mark_indexed(file_id)
    return file_id


def _ledger(store: SqliteStore) -> None:
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        _row(store, name, code="ERR_FILE_TIMEOUT")
    _row(store, "mail.pst", code="ERR_FILE_TIMEOUT")
    _row(store, "Makefile", code="ERR_FILE_TIMEOUT")
    _row(store, "fine.pdf")
    _row(store, "broken.pdf", code="ERR_FILE_CORRUPT")
    _row(store, "held.png", code="ERR_OCR_HELD")
    # A message inside a mailbox, and a file on a catalogued drive: neither
    # is a file an index run can be pointed at.
    _row(store, "inside.eml", code="ERR_FILE_TIMEOUT", source_kind="pst_message")
    volume = store.upsert_volume("guid-1", kind="drive", name="Projects 2019")
    _row(store, "away.pdf", code="ERR_FILE_TIMEOUT", volume_id=volume)


def test_the_store_lists_timed_out_files_by_type(store) -> None:
    _ledger(store)
    groups = store.timed_out_groups()
    assert [(g["ext"], g["count"]) for g in groups] == [
        ("pdf", 3), ("", 1), ("pst", 1)], "most files first, then by name"
    assert groups[0]["example"] == "C:/corpus/a.pdf"
    assert sum(g["count"] for g in groups) == 5


def test_the_store_hands_back_one_groups_files(store) -> None:
    _ledger(store)
    pdfs = store.timed_out_files("pdf")
    assert [Path(row["path"]).name for row in pdfs] == ["a.pdf", "b.pdf", "c.pdf"]
    assert pdfs[0]["size_bytes"] == 10 and pdfs[0]["mtime_ns"] == 1_000
    assert "it took too long" in pdfs[0]["skip_detail"]
    assert [Path(r["path"]).name for r in store.timed_out_files("")] == ["Makefile"]
    assert len(store.timed_out_files()) == 5, "None is every group"
    assert store.timed_out_files("docx") == []


def test_an_index_with_no_timeouts_has_no_groups(store) -> None:
    _row(store, "fine.pdf")
    assert store.timed_out_groups() == []
    assert store.timed_out_files() == []


def test_the_timed_out_statements_use_the_skip_index(store) -> None:
    """Held, not assumed: both read the rows that carry the code - the partial
    `idx_files_skip` - and never the whole of `files`."""
    _ledger(store)
    plans: list[str] = []
    real = store.conn

    class Spy:
        def execute(self, sql, params=()):
            plans.append(" ".join(
                str(row[-1]) for row in real.execute("EXPLAIN QUERY PLAN " + sql, params)))
            return real.execute(sql, params)

    original = type(store).conn
    try:
        type(store).conn = property(lambda _self: Spy())
        store.timed_out_groups()
        store.timed_out_files("pdf")
    finally:
        type(store).conn = original
    assert len(plans) == 2, "one statement each"
    for found in plans:
        assert "SEARCH files USING INDEX idx_files_skip" in found
        assert "SCAN files" not in found


# ---------------------------------------------------------------------------
# The words
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typed,group", [
    (".PDF", "pdf"), ("pdf", "pdf"), ("*", None), ("all", None), (None, None),
    ("", ""), (" .Pst ", "pst"),
])
def test_a_group_is_read_as_the_store_names_it(typed, group) -> None:
    assert timed_out_retry.normalise_group(typed) == group


@pytest.mark.parametrize("given,factor", [
    (4, 4.0), ("8", 8.0), (0, 1.0), (-3, 1.0), (1000, 100.0), ("x", 4.0),
    (None, 4.0), (1.5, 1.5),
])
def test_a_factor_is_kept_within_bounds(given, factor) -> None:
    assert timed_out_retry.clamp_factor(given) == factor


def test_the_opening_sentence_says_what_is_read_again_and_how() -> None:
    from app.index.walker import Candidate

    one = Candidate(path=Path("a.pdf"), size_bytes=1, mtime_ns=1)
    found = timed_out_retry.RetryPlan(candidates=[one, one, one], changed=1, missing=2)
    text = timed_out_retry.said(RetryTimedOut("pdf", 4), found)
    assert text.startswith("Reading 3 timed-out .pdf files again, with 4 times "
                           "the usual time limit.")
    assert "1 has changed since, and is read with the usual limit." in text
    assert "2 could not be found where they were" in text
    nothing = timed_out_retry.said(RetryTimedOut(None, 4), timed_out_retry.RetryPlan())
    assert nothing == "No timed-out files were found to read again."


# ---------------------------------------------------------------------------
# The watchdog's arithmetic
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _watched(monkeypatch, *, name: str, factor: float, file_limit: float = 0,
             stall_limit: float = 0):
    """A watch inside its reader, on a clock the test moves."""
    clock = _Clock()
    monkeypatch.setattr(file_watch.time, "monotonic", clock)
    monkeypatch.setattr(file_watch, "_set_async_exc", lambda *_a: True)
    dog = file_watch.Watchdog(file_limit_s=file_limit, stall_limit_s=stall_limit,
                              clock=clock)
    watch = file_watch.FileWatch()
    dog.add(watch)
    watch.begin(Path(f"D:/Docs/{name}"), None, file_watch.limit_kind(Path(name)),
                factor)
    watch.enter()
    return dog, watch, clock


def test_a_retried_file_is_given_the_factor_times_its_limit(monkeypatch) -> None:
    # A PDF: ten times the 120 s text limit is 20 minutes; four times that, 80.
    dog, watch, clock = _watched(monkeypatch, name="big.pdf", factor=4, file_limit=120)
    clock.now += 1_300                       # past the usual 20 minutes
    dog.check()
    assert watch.cancel is None, "the usual limit no longer applies to this file"
    clock.now += 3_600                       # 4,900 s: past 80 minutes
    dog.check()
    assert watch.cancel is not None and watch.cancel.code == "ERR_FILE_TIMEOUT"
    assert ("reading it took longer than 1 h 20 min, the limit for a document "
            "of this kind on this retry (4 times the usual 20 min)") in watch.cancel.message
    assert "retry x4 of 1200s" in watch.cancel.details


def test_an_ordinary_file_is_timed_out_in_the_words_it_always_was(monkeypatch) -> None:
    dog, watch, clock = _watched(monkeypatch, name="big.pdf", factor=1, file_limit=120)
    clock.now += 1_300
    dog.check()
    assert watch.cancel is not None
    assert watch.cancel.message.endswith(
        "reading it took longer than 20 min, the limit for a document of this kind.")
    assert "retry" not in watch.cancel.message + watch.cancel.details


def test_a_retried_mailbox_is_given_longer_with_no_progress(monkeypatch) -> None:
    dog, watch, clock = _watched(monkeypatch, name="Archive.pst", factor=3,
                                 stall_limit=600)
    dog.check()                              # the first look records where it is
    clock.now += 700
    dog.check()
    assert watch.cancel is None
    clock.now += 1_200
    dog.check()
    assert "nothing new was read from it for 30 min" in watch.cancel.message
    assert "on this retry (3 times the usual 10 min)" in watch.cancel.message


def test_a_limit_that_is_switched_off_stays_off_on_a_retry(monkeypatch) -> None:
    dog, watch, clock = _watched(monkeypatch, name="notes.txt", factor=4, file_limit=0)
    clock.now += 1_000_000
    dog.check()
    assert watch.cancel is None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def _retry_run(root: Path, db: Path, retry: RetryTimedOut, *, file_limit: float):
    """The retry an entry point builds: `_pipeline`'s config, handed to
    `retry_pipeline` in place of `Pipeline`."""
    embedder = Embedder(dim=4, encoder=lambda texts: [
        [1.0, 0.0, 0.0, 0.0] for _ in texts])
    store = SqliteStore(db).connect()
    config = PipelineConfig(
        walk=WalkConfig(roots=[root]), workers=1, min_free_gb=0,
        required_free_gb=0, file_time_limit_s=file_limit, stall_limit_s=0,
        limits=ResourceLimits(pause_on_battery=False, cpu_percent=0,
                              min_free_gb=0, poll_seconds=0.05,
                              low_priority=False),
    )
    pipeline = timed_out_retry.retry_pipeline(retry)(
        store, FakeVectors(), embedder, config)
    pipeline.governor = ResourceGovernor(
        config.resolved_limits(), probe=lambda: Snapshot(),
        manual_check=pipeline._pause_file_set)
    return pipeline


def _slow_reader(seconds: float, calls: list[str]):
    """A reader that takes `seconds` in Python, then reads the file."""
    real = pipeline_module.extract

    def read(path, **kwargs):
        calls.append(Path(path).name)
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            sum(range(1000))
        return real(path, **kwargs)

    return read


def _spy_on_every_read(monkeypatch, calls: list[str]) -> None:
    real = pipeline_module.extract

    def extract(path, **kwargs):
        calls.append(Path(path).name)
        return real(path, **kwargs)

    monkeypatch.setattr(pipeline_module, "extract", extract)


def _first_run_times_slow_out(tmp_path, monkeypatch, calls, *, seconds=1.5,
                              extra=()):
    """Five notes, `slow.txt` and any `extra` files; one ordinary run at a
    0.4 s limit, which `slow.txt` (a `seconds`-long read) does not finish in."""
    root = _corpus(tmp_path / "docs")
    (root / "slow.txt").write_text("the slow pump station report", encoding="utf-8")
    for name in extra:
        (root / name).write_text("def pump(): return 'station'", encoding="utf-8")
    _patch_extract(monkeypatch, "slow.txt", _slow_reader(seconds, calls))
    first = _pipeline(root, tmp_path / "index.db", file_limit=0.4)
    stats, _ = _run(first)
    assert stats.skipped_by_code.get("ERR_FILE_TIMEOUT") == 1
    assert _record(first, "slow.txt").skip_code == "ERR_FILE_TIMEOUT"
    first.store.close()
    return root


def test_a_retry_reads_the_file_under_the_longer_limit_and_nothing_else(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    calls: list[str] = []
    root = _first_run_times_slow_out(tmp_path, monkeypatch, calls)
    assert calls == ["slow.txt"]
    every: list[str] = []
    _spy_on_every_read(monkeypatch, every)          # now sees every file opened

    retry = _retry_run(root, tmp_path / "index.db", RetryTimedOut("txt", 10),
                       file_limit=0.4)
    stats, _ = _run(retry)

    assert every == ["slow.txt"], "only the group was read - no note was opened"
    assert stats.seen == 1 and stats.indexed == 1 and stats.skipped == 0
    record = _record(retry, "slow.txt")
    assert record.status == FileStatus.INDEXED and record.skip_code is None
    assert retry.store.timed_out_groups() == []
    assert stats.deleted == 0, "a run that walked nothing prunes nothing"
    assert sum(1 for _ in retry.store.iter_files()) == 6
    assert any("Reading 1 timed-out .txt file again, with 10 times the usual "
               "time limit." in str(n) for n in stats.notices)
    retry.store.close()


def test_the_longer_limit_is_for_that_run_only(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    """The retry writes no setting and changes no limit: a fresh slow file,
    read by the next ordinary run, is timed out at the usual limit."""
    calls: list[str] = []
    root = _first_run_times_slow_out(tmp_path, monkeypatch, calls)
    retry = _retry_run(root, tmp_path / "index.db", RetryTimedOut(None, 10),
                       file_limit=0.4)
    assert retry.config.file_time_limit_s == 0.4, "the run's own limit is the usual one"
    _run(retry)
    assert _record(retry, "slow.txt").status == FileStatus.INDEXED
    retry.store.close()

    (root / "slow.txt").write_text("the slow report, edited", encoding="utf-8")
    third = _pipeline(root, tmp_path / "index.db", file_limit=0.4)
    stats, _ = _run(third)
    assert stats.skipped_by_code.get("ERR_FILE_TIMEOUT") == 1
    assert "on this retry" not in _record(third, "slow.txt").skip_detail
    third.store.close()


def test_a_retry_leaves_the_other_groups_alone(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    calls: list[str] = []
    root = _corpus(tmp_path / "docs", files=2)
    (root / "slow.txt").write_text("the slow report", encoding="utf-8")
    (root / "slow.py").write_text("def pump(): return 1", encoding="utf-8")
    real = pipeline_module.extract
    slow = _slow_reader(1.5, calls)

    def extract(path, **kwargs):
        return (slow if Path(path).name.startswith("slow.") else real)(path, **kwargs)

    monkeypatch.setattr(pipeline_module, "extract", extract)
    first = _pipeline(root, tmp_path / "index.db", file_limit=0.4)
    _run(first)
    assert [(g["ext"], g["count"]) for g in first.store.timed_out_groups()] == [
        ("py", 1), ("txt", 1)]
    first.store.close()
    calls.clear()

    retry = _retry_run(root, tmp_path / "index.db", RetryTimedOut("py", 10),
                       file_limit=0.4)
    _run(retry)
    assert calls == ["slow.py"], "the .txt group was not read"
    assert _record(retry, "slow.py").status == FileStatus.INDEXED
    assert _record(retry, "slow.txt").skip_code == "ERR_FILE_TIMEOUT"
    retry.store.close()


def test_a_second_timeout_stays_timed_out_and_says_what_it_was_given(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    calls: list[str] = []
    root = _corpus(tmp_path / "docs", files=1)
    (root / "stuck.txt").write_text("never read", encoding="utf-8")
    _patch_extract(monkeypatch, "stuck.txt", _stuck_in_python)
    first = _pipeline(root, tmp_path / "index.db", file_limit=0.3)
    _run(first)
    before = _record(first, "stuck.txt").skip_detail
    assert "on this retry" not in before
    first.store.close()

    retry = _retry_run(root, tmp_path / "index.db", RetryTimedOut("txt", 3),
                       file_limit=0.3)
    stats, took = _run(retry)
    record = _record(retry, "stuck.txt")
    assert record.status == FileStatus.SKIPPED
    assert record.skip_code == "ERR_FILE_TIMEOUT"
    assert "on this retry (3 times the usual 0 s)" in record.skip_detail
    assert stats.skipped_by_code.get("ERR_FILE_TIMEOUT") == 1
    assert took >= 0.9, "it was given three times 0.3 s before being stopped"
    assert [g["ext"] for g in retry.store.timed_out_groups()] == ["txt"]
    assert calls == []
    retry.store.close()


def test_a_file_that_has_changed_since_is_read_with_the_usual_limit(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    root = _corpus(tmp_path / "docs", files=1)
    (root / "stuck.txt").write_text("never read", encoding="utf-8")
    _patch_extract(monkeypatch, "stuck.txt", _stuck_in_python)
    first = _pipeline(root, tmp_path / "index.db", file_limit=0.3)
    _run(first)
    first.store.close()

    (root / "stuck.txt").write_text("edited since, and longer", encoding="utf-8")
    retry = _retry_run(root, tmp_path / "index.db", RetryTimedOut("txt", 50),
                       file_limit=0.3)
    stats, took = _run(retry)
    assert took < 10, "fifty times the limit would have been 15 s"
    assert retry._retry_plan.changed == 1 and not retry._retry_plan.longer
    assert "on this retry" not in _record(retry, "stuck.txt").skip_detail
    assert any("1 has changed since" in str(n) for n in stats.notices)
    retry.store.close()


def test_a_file_that_has_gone_is_left_for_the_clean_up(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    calls: list[str] = []
    root = _first_run_times_slow_out(tmp_path, monkeypatch, calls)
    os.remove(root / "slow.txt")
    retry = _retry_run(root, tmp_path / "index.db", RetryTimedOut("txt", 10),
                       file_limit=0.4)
    stats, _ = _run(retry)
    assert stats.seen == 0 and stats.indexed == 0
    assert _record(retry, "slow.txt").skip_code == "ERR_FILE_TIMEOUT", "not pruned here"
    assert any("No timed-out .txt files were found to read again. 1 could not "
               "be found where it was" in str(n) for n in stats.notices)
    assert not any("No folders are set up" in str(n) for n in stats.notices)
    retry.store.close()


def test_a_retry_config_walks_and_prunes_nothing(tmp_path) -> None:
    retry = _retry_run(tmp_path, tmp_path / "index.db", RetryTimedOut(None, 1000),
                       file_limit=1)
    assert retry.config.walk.roots == []
    assert retry.config.prune_missing is False
    assert retry.config.archives is False and retry.config.retry_locked is False
    assert retry.retry.factor == timed_out_retry.MAX_FACTOR
    retry.store.close()
