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
    # Marked, so what follows a run in the window can tell (`was_retry`).
    assert timed_out_retry.was_retry(stats)
    assert stats.resolved["retry_timed_out"] == ".txt, 10 times the usual limit"
    assert stats.as_dict()["resolved"]["retry_timed_out"]
    retry.store.close()


def test_an_ordinary_run_is_not_taken_for_a_retry(tmp_path) -> None:
    root = _corpus(tmp_path / "docs", files=1)
    pipeline = _pipeline(root, tmp_path / "index.db")
    stats, _ = _run(pipeline)
    assert not timed_out_retry.was_retry(stats)
    assert not timed_out_retry.was_retry(None)
    pipeline.store.close()


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

# ---------------------------------------------------------------------------
# The command line (non-negotiable 8)
# ---------------------------------------------------------------------------

def _cli(capsys, *argv):
    from app.cli import main

    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def cli_env(temp_env: Path) -> Path:
    """`temp_env` with a 1 s limit for text (the smallest a setting can hold)
    and nothing that would pause a run on a busy test machine."""
    with temp_env.open("a", encoding="utf-8") as handle:
        handle.write("\nINDEX_FILE_TIME_LIMIT_S=1\nINDEX_STALL_LIMIT_S=0\n"
                     "INDEX_CPU_PERCENT=0\nMIN_FREE_GB=0\nREQUIRED_FREE_GB=0\n")
    return temp_env


def _index_db(env_file: Path) -> Path:
    from app.core.config import load_settings

    return Path(load_settings(env_file).fts_db)


def test_the_flags_parse_as_documented() -> None:
    from app.cli import build_parser

    parse = build_parser().parse_args
    assert parse(["index"]).retry_timed_out is None
    assert parse(["index", "--retry-timed-out"]).retry_timed_out == "*"
    args = parse(["index", "--retry-timed-out", "pdf", "--time-limit-factor", "6"])
    assert args.retry_timed_out == "pdf" and args.time_limit_factor == 6.0
    # As the window's child process writes it, no-extension group included.
    assert parse(["index", "--retry-timed-out=", "--"]).retry_timed_out == ""
    assert parse(["timed-out"]).func.__name__ == "cmd_timed_out"
    assert parse(["timed-out", "pdf"]).type == "pdf"


@pytest.mark.parametrize("argv,said", [
    (["index", "--time-limit-factor", "4", "D:/x"], "only applies to a retry"),
    (["index", "--retry-timed-out", "D:/Docs"], "a file type, not a folder"),
    (["index", "--retry-timed-out", "pdf", "D:/Docs"], "a file type, not a folder"),
    (["index", "--retry-timed-out", "--time-limit-factor", "0"], "not between 1 and 100"),
    (["index", "--retry-timed-out", "--time-limit-factor", "500"], "not between 1 and 100"),
])
def test_what_cannot_be_meant_is_refused_with_the_fix(capsys, cli_env, argv, said) -> None:
    code, _out, err = _cli(capsys, "--env", str(cli_env), *argv)
    assert code == 1
    assert said in err and "ERR_CONFIG_INVALID" in err


def test_a_retry_with_nothing_timed_out_says_so_and_is_not_an_error(
        capsys, cli_env) -> None:
    code, out, _err = _cli(capsys, "--env", str(cli_env), "index", "--retry-timed-out")
    assert code == 0
    assert "Nothing to read again: no timed-out files." in out
    assert not _index_db(cli_env).exists(), "asking did not create an index"
    code, out, _err = _cli(capsys, "--env", str(cli_env), "timed-out")
    assert code == 0 and "No files are timed out." in out


def test_the_command_line_lists_and_retries_a_group(
        capsys, cli_env, tmp_path, monkeypatch, fast_watchdog) -> None:
    env = ["--env", str(cli_env)]
    calls: list[str] = []
    root = _corpus(tmp_path / "docs", files=3)
    (root / "slow.txt").write_text("the slow pump station report", encoding="utf-8")
    _patch_extract(monkeypatch, "slow.txt", _slow_reader(2.5, calls))

    code, out, _err = _cli(capsys, *env, "index", str(root), "--quiet",
                           "--workers", "1", "--fake-embedder-for-bench")
    assert code == 0 and "ERR_FILE_TIMEOUT" in out

    # The listing: the counts, the command, and then the files of one type.
    code, out, _err = _cli(capsys, *env, "timed-out")
    assert code == 0
    assert "Timed out  1 file(s), by type" in out and ".txt" in out
    assert "app.cli index --retry-timed-out txt --time-limit-factor 4" in out
    _code, out, _err = _cli(capsys, *env, "--json", "timed-out", "txt")
    listed = json.loads(out)
    assert listed["total"] == 1 and listed["groups"][0]["ext"] == "txt"
    assert Path(listed["files"][0]["path"]).name == "slow.txt"
    assert "longer than 1 s" in listed["files"][0]["skip_detail"]

    # A type with nothing timed out: said, with what there is, and no run.
    code, out, _err = _cli(capsys, *env, "index", "--retry-timed-out", "pdf")
    assert code == 0
    assert "Nothing to read again: no timed-out .pdf files." in out
    assert "Timed out: .txt x1" in out

    # The retry: five times the 1 s limit is enough for a 2.5 s read.
    every: list[str] = []
    _spy_on_every_read(monkeypatch, every)
    code, out, _err = _cli(capsys, *env, "index", "--retry-timed-out", ".TXT",
                           "--time-limit-factor", "5", "--quiet",
                           "--workers", "1", "--fake-embedder-for-bench")
    assert code == 0
    assert "Reading 1 timed-out .txt file(s) again, each with 5 times its usual" in out
    assert "Indexed   1 document(s)" in out
    assert "Files     1 seen" in out
    assert every == ["slow.txt"], "no other file was opened"

    with SqliteStore(_index_db(cli_env)) as store:
        assert store.timed_out_groups() == []
        assert sum(1 for _ in store.iter_files()) == 4, "nothing was pruned"
    from app.core.config import load_settings

    assert load_settings(cli_env).index_file_time_limit_s == 1, "the setting is as it was"
    code, out, _err = _cli(capsys, *env, "timed-out")
    assert "No files are timed out." in out


def test_a_retry_from_the_command_line_that_times_out_again_says_the_limit(
        capsys, cli_env, tmp_path, monkeypatch, fast_watchdog) -> None:
    env = ["--env", str(cli_env)]
    root = _corpus(tmp_path / "docs", files=1)
    (root / "stuck.txt").write_text("never read", encoding="utf-8")
    _patch_extract(monkeypatch, "stuck.txt", _stuck_in_python)
    common = ("--quiet", "--workers", "1", "--fake-embedder-for-bench")
    _cli(capsys, *env, "index", str(root), *common)
    code, out, _err = _cli(capsys, *env, "--json", "index", "--retry-timed-out",
                           "--time-limit-factor", "2", *common)
    payload = json.loads(out)
    assert payload["skipped_by_code"] == {"ERR_FILE_TIMEOUT": 1}
    assert payload["seen"] == 1 and payload["deleted"] == 0
    with SqliteStore(_index_db(cli_env)) as store:
        (row,) = store.timed_out_files("txt")
    assert "on this retry (2 times the usual 1 s)" in row["skip_detail"]

# ---------------------------------------------------------------------------
# The separate indexing process
# ---------------------------------------------------------------------------

def test_the_flags_for_the_indexing_process_round_trip() -> None:
    from app.cli import build_parser
    from app.cli.index import _retry_asked
    from app.index.child_run import child_command

    assert timed_out_retry.child_arguments(None) == []
    for retry in (RetryTimedOut("pdf", 6), RetryTimedOut("", 4), RetryTimedOut(None, 2.5)):
        argv = child_command([], extra=timed_out_retry.child_arguments(retry))
        args = build_parser().parse_args(argv[argv.index("index"):])
        assert args.roots == []
        assert _retry_asked(args) == (retry, None), argv


def test_a_retry_through_the_indexing_process(tmp_path) -> None:
    """The real `app.cli index --events jsonl` child, as the window starts it
    with "Index in a separate process" on: an ordinary run times a slow file
    out, and a retry run reads that file and only that file."""
    import textwrap
    import threading

    from app.index.child_run import ChildIndexRun, child_command
    from tests.unit.test_file_watch import PROJECT

    corpus = _corpus(tmp_path / "docs", files=3)
    (corpus / "slow.txt").write_text("the slow pump station report", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DATA_PATH={(tmp_path / 'data').as_posix()}\n"
        f"LOG_PATH={(tmp_path / 'logs').as_posix()}\n"
        "MIN_FREE_GB=0\nREQUIRED_FREE_GB=0\nINDEX_CPU_PERCENT=0\n"
        "INDEX_FILE_TIME_LIMIT_S=1\nINDEX_STALL_LIMIT_S=0\n", encoding="utf-8")
    locks = tmp_path / "locks"
    locks.mkdir()
    opened = tmp_path / "opened.txt"
    # `python -m app.cli ...` becomes `python -c <the same, with a slow reader
    # that also notes every file it is asked for>`.
    program = textwrap.dedent("""
        import sys, time
        import app.index.pipeline as pipeline_module
        real = pipeline_module.extract
        def extract(path, **kwargs):
            with open(OPENED, "a", encoding="utf-8") as handle:
                handle.write(str(path).replace(chr(92), "/").rsplit("/", 1)[-1] + "\\n")
            if str(path).endswith("slow.txt"):
                until = time.monotonic() + 3.0
                while time.monotonic() < until:
                    sum(range(1000))
            return real(path, **kwargs)
        pipeline_module.extract = extract
        from app.cli import main
        sys.exit(main(sys.argv[1:]))
    """).replace("OPENED", repr(str(opened)))

    def child(roots, extra):
        argv = child_command(roots, env_file=env_file, workers=1,
                             extra=[*extra, "--fake-embedder-for-bench"])
        assert argv[1:3] == ["-m", "app.cli"]
        argv = [argv[0], "-c", program, *argv[3:]]
        env = dict(os.environ, TMPDIR=str(locks), PYTHONPATH=str(PROJECT))
        run = ChildIndexRun(argv, env=env, cwd=PROJECT,
                            stderr_path=tmp_path / "child-stderr.log")
        outcome: dict = {}

        def target() -> None:
            try:
                outcome["stats"] = run.run(on_progress=lambda _stats: None)
            except BaseException as exc:            # noqa: BLE001
                outcome["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(240)
        if thread.is_alive():
            run.shutdown(grace_s=5)
            pytest.fail("the child run never finished")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["stats"]

    first = child([corpus], [])
    assert first.indexed == 3
    assert first.skipped_by_code.get("ERR_FILE_TIMEOUT") == 1

    opened.write_text("", encoding="utf-8")
    retry = timed_out_retry.child_arguments(RetryTimedOut("txt", 8))
    second = child([], retry)
    assert second.seen == 1 and second.indexed == 1
    assert not second.skipped_by_code
    assert opened.read_text(encoding="utf-8").split() == ["slow.txt"], (
        "the child read the group and nothing else")
    assert any("Reading 1 timed-out .txt file again" in str(n) for n in second.notices)
    assert timed_out_retry.was_retry(second) and not timed_out_retry.was_retry(first), (
        "the mark crosses the pipe with the rest of the stats")
    from app.core.config import load_settings

    with SqliteStore(load_settings(env_file).fts_db) as store:
        assert store.timed_out_groups() == []
        assert sum(1 for _ in store.iter_files()) == 4
