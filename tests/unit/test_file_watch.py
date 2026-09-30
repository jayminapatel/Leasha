r"""A time limit per file, and Force skip (work order 0z lane B).

Layer: L3

The owner: "especially as the pst scanning is way slow ... and is not
reliable, this has to have a robust design". What these pin, each with a
real `Pipeline` run:

* **A reader looping in Python** is timed out, the file is recorded as
  `ERR_FILE_TIMEOUT`, and the *same* thread carries on - measured, not assumed:
  the exception raised into the thread arrives.
* **A reader stuck in native code** (here: a blocking lock wait, which Python
  cannot interrupt) is left behind; the file is recorded anyway and a
  replacement thread keeps the run at full strength. When the stuck call
  finally returns, the old thread ends without taking more work.
* **In a reader process**, the process is ended and the thread moves on.
* **A slow mailbox that keeps making progress is not cut off**, however long
  it takes in total; one that stops making progress is, and keeps what it read.
* **Force skip** does the same for one reader, by hand, in-process and through
  the separate-process run's `skip <reader>` command.
* A timed-out file is **settled**: the next run leaves it alone.
"""

from __future__ import annotations

import mailbox
import os
import sys
import textwrap
import threading
import time
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.extract.base import REGISTRY
from app.index import file_watch
from app.index import pipeline as pipeline_module
from app.index.embedder import Embedder
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.read_process import ReaderProcess
from app.index.resources import ResourceGovernor, ResourceLimits, Snapshot
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore

PROJECT = Path(__file__).resolve().parents[2]
#: Every run here must finish well inside this, or a reader held its thread.
RUN_BUDGET_S = 60.0


class FakeVectors:
    """A vector store that counts, not writes - as `test_pause_button.py`."""

    def __init__(self) -> None:
        self.rows: dict[int, int] = {}

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        wanted = [int(one) for one in file_ids]
        for cid, fid in list(self.rows.items()):
            if fid in wanted:
                del self.rows[cid]

    def add(self, *, chunk_ids, file_ids, vectors) -> int:
        for cid, fid in zip(chunk_ids, file_ids, strict=True):
            self.rows[int(cid)] = int(fid)
        return len(list(chunk_ids))

    def maybe_compact(self, **_k) -> bool:
        return False

    def maybe_create_index(self, **_k) -> bool:
        return False

    def count(self) -> int:
        return len(self.rows)


def _corpus(root: Path, files: int = 5) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (root / f"note{index:02d}.txt").write_text(
            f"pump station {index} commissioning report", encoding="utf-8")
    return root


def _mbox(path: Path, messages: int) -> Path:
    box = mailbox.mbox(str(path))
    try:
        for number in range(messages):
            message = EmailMessage()
            message["From"] = f"sender{number}@example.com"
            message["To"] = "someone@example.com"
            message["Subject"] = f"Message number {number}"
            message["Date"] = "Mon, 01 Jan 2024 10:00:00 +0000"
            message["Message-ID"] = f"<m{number}@example.com>"
            message.set_content(f"Body of message {number} about volcanoes. " * 10)
            box.add(message)
    finally:
        box.close()
    return path


def _pipeline(root: Path, db: Path, *, workers: int = 1, file_limit: float = 0,
              stall_limit: float = 0, read_processes: bool = False) -> Pipeline:
    embedder = Embedder(dim=4, encoder=lambda texts: [
        [1.0, 0.0, 0.0, 0.0] for _ in texts])
    store = SqliteStore(db).connect()
    config = PipelineConfig(
        walk=WalkConfig(roots=[root]), workers=workers, min_free_gb=0,
        required_free_gb=0, read_processes=read_processes,
        file_time_limit_s=file_limit, stall_limit_s=stall_limit,
        limits=ResourceLimits(pause_on_battery=False, cpu_percent=0,
                              min_free_gb=0, poll_seconds=0.05,
                              low_priority=False),
    )
    pipeline = Pipeline(store, FakeVectors(), embedder, config)
    pipeline.governor = ResourceGovernor(
        config.resolved_limits(), probe=lambda: Snapshot(),
        manual_check=pipeline._pause_file_set)
    return pipeline


def _run(pipeline: Pipeline, on_progress=None):
    """`pipeline.run()` with a deadline, so a held thread fails, not hangs."""
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["stats"] = pipeline.run(on_progress=on_progress)
        except BaseException as exc:                # noqa: BLE001 - re-raised below
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(RUN_BUDGET_S)
    if thread.is_alive():
        pipeline.request_stop()
        pytest.fail(f"the run did not finish in {RUN_BUDGET_S:.0f}s - a stuck "
                    "reader held its thread")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["stats"], time.monotonic() - started


def _record(pipeline: Pipeline, name: str):
    for record in pipeline.store.iter_files():
        if Path(record.path).name == name:
            return record
    raise AssertionError(f"{name} has no row")


def _stuck_in_python(path, **kwargs):
    """A reader gone round in circles, in Python - the usual parser hang."""
    while True:
        sum(range(1000))


@pytest.fixture
def fast_watchdog(monkeypatch):
    """Look often and give up on a thread quickly, so each test takes seconds."""
    monkeypatch.setattr(file_watch, "TICK_S", 0.05)
    monkeypatch.setattr(file_watch, "GRACE_S", 1.0)


def _patch_extract(monkeypatch, stuck_name: str, reader) -> None:
    real = pipeline_module.extract

    def extract(path, **kwargs):
        if Path(path).name == stuck_name:
            return reader(path, **kwargs)
        return real(path, **kwargs)

    monkeypatch.setattr(pipeline_module, "extract", extract)


# ---------------------------------------------------------------------------
# Which limit applies
# ---------------------------------------------------------------------------

def test_every_named_reader_is_a_registered_reader() -> None:
    """A rename must not quietly empty a list - as `PROCESS_READERS` is checked."""
    registered = {type(extractor).__name__ for extractor in REGISTRY.values()}
    named = (file_watch.QUICK_READERS | file_watch.STALL_READERS
             | file_watch.UNLIMITED_READERS)
    assert named - registered == set()


@pytest.mark.parametrize("name,kind", [
    ("notes.txt", file_watch.LIMIT_QUICK), ("main.py", file_watch.LIMIT_QUICK),
    ("report.pdf", file_watch.LIMIT_LONG), ("deck.pptx", file_watch.LIMIT_LONG),
    ("Archive.pst", file_watch.LIMIT_STALL), ("mail.mbox", file_watch.LIMIT_STALL),
    ("backup.zip", file_watch.LIMIT_STALL), ("film.mp4", file_watch.LIMIT_NONE),
])
def test_each_kind_of_file_gets_its_own_limit(name, kind) -> None:
    assert file_watch.limit_kind(Path(name)) == kind


def test_the_message_says_which_file_how_long_and_what_to_do() -> None:
    from app.core.errors import ActionType, make_error

    error = make_error("ERR_FILE_TIMEOUT", "test", path="D:/Docs/report.pdf",
                       took="20 min 3 s", reason="reading it took too long")
    assert "report.pdf" in error.message and "20 min 3 s" in error.message
    assert "Time limit per file" in error.suggestion
    assert error.action_type is ActionType.SKIP_CONTINUE


# ---------------------------------------------------------------------------
# In-process
# ---------------------------------------------------------------------------

def test_a_reader_looping_in_python_is_timed_out_and_the_run_finishes(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    root = _corpus(tmp_path / "docs")
    (root / "stuck.txt").write_text("never read", encoding="utf-8")
    _patch_extract(monkeypatch, "stuck.txt", _stuck_in_python)

    pipeline = _pipeline(root, tmp_path / "index.db", file_limit=0.5)
    stats, took = _run(pipeline)

    assert stats.indexed == 5, "every other file was still read"
    assert stats.skipped_by_code.get("ERR_FILE_TIMEOUT") == 1
    record = _record(pipeline, "stuck.txt")
    assert record.status == FileStatus.SKIPPED
    assert record.skip_code == "ERR_FILE_TIMEOUT"
    # **Measured: the exception raised into the thread arrived.** No thread
    # had to be left behind and replaced for a hang in Python code.
    assert pipeline._replacement_workers == []
    assert took < 20, f"the run took {took:.1f}s - the limit was half a second"
    pipeline.store.close()


def test_a_reader_stuck_in_native_code_is_left_behind_and_replaced(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    """Python cannot interrupt a blocking native call - measured here with a
    lock wait. The file is recorded anyway, and a new thread does the rest."""
    root = _corpus(tmp_path / "docs", files=6)
    (root / "a-stuck.txt").write_text("never read", encoding="utf-8")
    gate = threading.Event()
    stuck_threads: list[threading.Thread] = []

    def stuck_in_native(path, **kwargs):
        stuck_threads.append(threading.current_thread())
        gate.wait()                       # a lock acquire, in C, with no timeout
        return iter(())

    _patch_extract(monkeypatch, "a-stuck.txt", stuck_in_native)
    pipeline = _pipeline(root, tmp_path / "index.db", file_limit=0.3)
    try:
        stats, _took = _run(pipeline)

        assert stats.indexed == 6, "the replacement read everything else"
        assert stats.skipped_by_code.get("ERR_FILE_TIMEOUT") == 1
        assert len(pipeline._replacement_workers) == 1
        assert _record(pipeline, "a-stuck.txt").skip_code == "ERR_FILE_TIMEOUT"
        assert stuck_threads and stuck_threads[0].is_alive(), (
            "the stuck thread is still inside the native call - it was left, "
            "not ended, which is the honest limit of a thread")
    finally:
        gate.set()
    # Let go at last, it sees it was replaced and ends without taking work.
    stuck_threads[0].join(timeout=10)
    assert not stuck_threads[0].is_alive()
    pipeline.store.close()


def test_a_timed_out_file_is_settled_and_not_read_again(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    root = _corpus(tmp_path / "docs", files=2)
    (root / "stuck.txt").write_text("never read", encoding="utf-8")
    calls: list[str] = []

    def counting(path, **kwargs):
        calls.append(Path(path).name)
        return _stuck_in_python(path, **kwargs)

    _patch_extract(monkeypatch, "stuck.txt", counting)
    first = _pipeline(root, tmp_path / "index.db", file_limit=0.3)
    _run(first)
    first.store.close()
    assert calls == ["stuck.txt"]

    second = _pipeline(root, tmp_path / "index.db", file_limit=0.3)
    stats, _ = _run(second)
    assert calls == ["stuck.txt"], "the second run left the settled skip alone"
    assert stats.settled_by_code.get("ERR_FILE_TIMEOUT") == 1
    second.store.close()


def test_a_slow_mailbox_that_keeps_going_is_not_cut_off(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    """Total time far past every limit; never a gap as long as the stall limit."""
    root = tmp_path / "docs"
    root.mkdir()
    _mbox(root / "slow.mbox", messages=8)
    real = pipeline_module.extract

    def slow(path, **kwargs):
        for document in real(path, **kwargs):
            time.sleep(0.3)
            yield document

    _patch_extract(monkeypatch, "slow.mbox", slow)
    # 0.1 s for text means 1 s for a document - the mailbox takes 2.4 s. It is
    # judged only on progress: 1 s with nothing new.
    pipeline = _pipeline(root, tmp_path / "index.db", file_limit=0.1,
                         stall_limit=1.0)
    stats, took = _run(pipeline)

    assert took > 2.0, "the mailbox really did take longer than every total limit"
    assert stats.skipped_by_code.get("ERR_FILE_TIMEOUT") is None
    assert stats.indexed >= 8, "every message was read"
    pipeline.store.close()


def test_a_mailbox_that_stops_making_progress_is_cut_off_and_keeps_its_mail(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    _mbox(root / "stalls.mbox", messages=6)
    real = pipeline_module.extract

    def stalls(path, **kwargs):
        for number, document in enumerate(real(path, **kwargs)):
            if number == 3:
                _stuck_in_python(path)
            yield document

    _patch_extract(monkeypatch, "stalls.mbox", stalls)
    pipeline = _pipeline(root, tmp_path / "index.db", file_limit=0,
                         stall_limit=0.5)
    stats, _ = _run(pipeline)

    assert stats.skipped_by_code.get("ERR_FILE_TIMEOUT") == 1
    assert stats.indexed == 3, "the three messages read before it stalled are kept"
    assert "nothing new was read" in _record(pipeline, "stalls.mbox").skip_detail
    pipeline.store.close()


def test_an_archive_working_through_one_messages_attachments_is_not_stalled() -> None:
    """Order 0z audit, 2026-09-30: lanes B and C were built side by side and
    never joined. Inside one message of a `.pst` the frame's `n` stands still
    while its attachments are read, and an attachment that is skipped, held, a
    duplicate or unreadable hands over no document - only `Frame.beat` moves.
    The watchdog did not look at `beat`, so a message with a long run of such
    attachments was "no progress" and the archive was cut off while working.
    Fails on the code as it was (cut off at the second look)."""
    from types import SimpleNamespace

    from app.extract import progress

    frame = progress.Frame("pst", "Archive2019.pst", unit="message")
    frame.n, frame.where = 1, "Inbox"
    ended: list[int] = []
    # A stand-in reader process, so letting go ends "it" rather than raising
    # an exception into the thread running this test.
    reader = SimpleNamespace(reading=True, kill_child=lambda: ended.append(1))
    now = [1_000.0]
    watchdog = file_watch.Watchdog(stall_limit_s=10, clock=lambda: now[0])
    watch = file_watch.FileWatch(slot=SimpleNamespace(id=1, item=0, frames=[frame]),
                                 reader=reader)
    watchdog.add(watch)
    watch.begin(SimpleNamespace(path=Path("Archive2019.pst")), None,
                file_watch.LIMIT_STALL)
    watch.enter()
    watchdog.check()

    # Five attachments, six seconds each, none of them a document: 30 s in
    # all against a limit of 10, and never 10 s without one of them ending.
    for _ in range(5):
        now[0] += 6
        frame.count(progress.STATUS_SKIPPED)
        watchdog.check()
        assert watch.cancel is None, "an item ended - that is progress"
    assert ended == []

    # And one that really does stop moving is still cut off.
    now[0] += 11
    watchdog.check()
    assert watch.cancel is not None and watch.cancel.code == "ERR_FILE_TIMEOUT"
    assert ended == [1]


def test_force_skip_in_process_skips_that_readers_file(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    root = _corpus(tmp_path / "docs", files=3)
    (root / "a-stuck.txt").write_text("never read", encoding="utf-8")
    _patch_extract(monkeypatch, "a-stuck.txt", _stuck_in_python)
    # No limits at all: only the person ends this file.
    pipeline = _pipeline(root, tmp_path / "index.db")
    pressed: list[bool] = []

    def progress(stats) -> None:
        # A snapshot, as the window takes one: it fills `workers` from the board.
        workers = getattr(stats.snapshot(), "workers", None) or {}
        for key, worker in workers.items():
            if worker.get("file") == "a-stuck.txt" and not pressed:
                pressed.append(pipeline.force_skip(key))

    stats, _ = _run(pipeline, on_progress=progress)

    assert pressed == [True]
    assert stats.indexed == 3
    record = _record(pipeline, "a-stuck.txt")
    assert record.skip_code == "ERR_FILE_TIMEOUT"
    assert "you pressed Force skip" in record.skip_detail
    assert pipeline.force_skip("1") is False, "no run, nothing to skip"
    pipeline.store.close()


# ---------------------------------------------------------------------------
# Reader processes
# ---------------------------------------------------------------------------

#: A reader process whose reader hangs on one file: the real child
#: (`read_process.main`), with `extract` wrapped the way the tests above wrap it
#: in-process. A `-c` program, because a monkeypatch cannot reach another
#: process.
_HANGING_CHILD = textwrap.dedent("""
    import sys, time
    import app.extract as extract_module
    import app.index.read_process as read_process
    real = extract_module.extract
    def extract(path, **kwargs):
        if str(path).endswith("stuck.txt"):
            while True:
                time.sleep(1)
        return real(path, **kwargs)
    extract_module.extract = extract
    sys.exit(read_process.main(sys.argv[1:]))
""")


class _HangingReader(ReaderProcess):
    def argv(self) -> list[str]:
        return [self.python, "-c", _HANGING_CHILD]


def test_a_hung_reader_process_is_ended_and_the_thread_moves_on(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    root = _corpus(tmp_path / "docs", files=3)
    (root / "a-stuck.txt").write_text("never read", encoding="utf-8")
    monkeypatch.setattr(pipeline_module, "ReaderProcess", _HangingReader)
    readers: list[ReaderProcess] = []
    real_start = _HangingReader.start

    def start(self) -> None:
        if self not in readers:
            readers.append(self)
        real_start(self)

    monkeypatch.setattr(_HangingReader, "start", start)
    kills: list[bool] = []
    real_kill = _HangingReader.kill_child

    def kill_child(self) -> None:
        kills.append(self.reading)
        real_kill(self)

    monkeypatch.setattr(_HangingReader, "kill_child", kill_child)
    monkeypatch.setenv("PYTHONPATH", str(PROJECT))
    pipeline = _pipeline(root, tmp_path / "index.db", file_limit=1.0,
                         read_processes=True)
    stats, _ = _run(pipeline)

    assert stats.indexed == 3
    assert _record(pipeline, "a-stuck.txt").skip_code == "ERR_FILE_TIMEOUT"
    assert pipeline._replacement_workers == [], "the thread itself moved on"
    assert kills == [True], "the hung process was ended, mid-read, once"
    # Whichever order the walk took, a file after the stuck one would have
    # needed a fresh child; `indexed == 3` above says every one was read.
    pipeline.store.close()


#: A reader process that takes 2.5 s to start - what a loaded machine does to
#: a fresh interpreter and its imports - and then reads perfectly well.
_SLOW_START_CHILD = textwrap.dedent("""
    import sys, time
    time.sleep(2.5)
    import app.index.read_process as read_process
    sys.exit(read_process.main(sys.argv[1:]))
""")


class _SlowStartReader(ReaderProcess):
    def argv(self) -> list[str]:
        return [self.python, "-c", _SLOW_START_CHILD]


def test_a_reader_process_s_start_up_is_not_charged_to_its_first_file(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    """The fault of 2026-09-30. The clock for a file started before its reader
    process had said it was ready, so a process slower to start than the limit
    timed out the file it was started for - and, a fresh process being started
    after every time-out, every file after it. Fails on the code as it was
    (nothing indexed, three `ERR_FILE_TIMEOUT`)."""
    root = _corpus(tmp_path / "docs", files=3)
    monkeypatch.setattr(pipeline_module, "ReaderProcess", _SlowStartReader)
    monkeypatch.setenv("PYTHONPATH", str(PROJECT))
    # 1 s for a text file; the process needs 2.5 s before it can read one.
    pipeline = _pipeline(root, tmp_path / "index.db", file_limit=1.0,
                         read_processes=True)
    stats, _ = _run(pipeline)

    assert stats.skipped_by_code.get("ERR_FILE_TIMEOUT") is None
    assert stats.indexed == 3
    pipeline.store.close()


class _NeverReadyReader(ReaderProcess):
    def argv(self) -> list[str]:
        return [self.python, "-c", "import time; time.sleep(600)"]


def test_a_reader_process_that_never_starts_is_given_up_on_and_nothing_is_skipped(
        tmp_path, monkeypatch, fast_watchdog) -> None:
    """The other half: taking start-up off the file's clock must not leave it
    on no clock. A process that never says it is ready is ended after its own
    limit, said so in a structured error, and that thread reads its files
    itself for the rest of the run - no file is blamed for it."""
    from app.index import read_process

    root = _corpus(tmp_path / "docs", files=3)
    monkeypatch.setattr(read_process, "START_LIMIT_S", 1.0)
    monkeypatch.setattr(pipeline_module, "ReaderProcess", _NeverReadyReader)
    pipeline = _pipeline(root, tmp_path / "index.db", file_limit=5.0,
                         read_processes=True)
    stats, took = _run(pipeline)

    assert stats.indexed == 3, "read in the thread instead"
    assert stats.skipped == 0
    assert stats.warned_by_code.get("ERR_READER_PROCESS_START") == 1
    assert took < 15, "one bounded wait, not one per file"
    warnings = [e.text for e in stats.activity.entries()
                if e.detail == "ERR_READER_PROCESS_START"]
    assert len(warnings) == 1 and "did not start" in warnings[0]
    pipeline.store.close()


def test_waiting_for_a_reader_process_is_bounded_and_says_why(monkeypatch) -> None:
    from app.core.errors import AppErrorException
    from app.index import read_process

    monkeypatch.setattr(read_process, "START_LIMIT_S", 0.5)
    reader = _NeverReadyReader(low_priority=False)
    started = time.monotonic()
    try:
        with pytest.raises(AppErrorException) as caught:
            reader.wait_ready()
    finally:
        reader.close()
    assert time.monotonic() - started < 5
    error = caught.value.error
    assert error.code == "ERR_READER_PROCESS_START"
    assert error.suggestion and "Read files in separate processes" in error.suggestion
    assert not reader.ready


def test_a_stop_is_not_kept_waiting_for_a_reader_process_to_start() -> None:
    reader = _NeverReadyReader(low_priority=False)
    started = time.monotonic()
    try:
        assert reader.wait_ready(cancelled=lambda: True) is False
    finally:
        reader.close()
    assert time.monotonic() - started < 5


def test_the_file_s_clock_starts_when_the_reader_process_is_ready(
        tmp_path, monkeypatch) -> None:
    """Read from the clock itself rather than from a time-out: the reader
    seconds charged to the first file exclude the 2.5 s the process took."""
    root = _corpus(tmp_path / "docs", files=1)
    monkeypatch.setattr(pipeline_module, "ReaderProcess", _SlowStartReader)
    monkeypatch.setenv("PYTHONPATH", str(PROJECT))
    charged: list[float] = []
    real_end = file_watch.FileWatch.end

    def end(self) -> None:
        if self.candidate is not None:
            with self.lock:
                self._stop_clock()
                charged.append(self.read_s)
        real_end(self)

    monkeypatch.setattr(file_watch.FileWatch, "end", end)
    pipeline = _pipeline(root, tmp_path / "index.db", file_limit=60.0,
                         read_processes=True)
    stats, took = _run(pipeline)

    assert stats.indexed == 1
    assert took > 2.0, "the process really was slow to start"
    assert charged and max(charged) < 1.5, (
        f"the file was charged {max(charged):.1f}s of reader time; the "
        "process's start-up is in it")
    pipeline.store.close()


# ---------------------------------------------------------------------------
# The separate-process run: `skip <reader>`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,word", [
    ("skip 2\n", "skip 2"), (" SKIP 02 ", "skip 2"), ("skip", None),
    ("skip two", None), ("skip 1 2", None),
])
def test_skip_is_a_command_with_a_reader_number(text, word) -> None:
    from app.index.run_events import parse_command

    assert parse_command(text) == word


def test_the_child_hands_skip_to_its_pipeline() -> None:
    from app.cli.index import _EventSession

    class Pipe:
        skipped: list = []

        def force_skip(self, reader):
            self.skipped.append(reader)
            return True

        def request_stop(self):
            pass

        def pause(self):
            pass

    session = _EventSession(open(os.devnull, "w", encoding="utf-8"))
    pipeline = Pipe()
    session.command("skip 2")               # before the run is live: nothing
    session.attach(pipeline)
    session._live = True
    session.command("skip 2")
    assert pipeline.skipped == ["2"]


def test_force_skip_through_the_indexing_process(tmp_path) -> None:
    """The real `app.cli index --events jsonl` child, one reader stuck on a
    file, skipped by `ChildIndexRun.force_skip` - the window's path."""
    from app.index.child_run import ChildIndexRun, child_command

    corpus = _corpus(tmp_path / "docs", files=4)
    (corpus / "a-stuck.txt").write_text("never read", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DATA_PATH={(tmp_path / 'data').as_posix()}\n"
        f"LOG_PATH={(tmp_path / 'logs').as_posix()}\n"
        "MIN_FREE_GB=0\nREQUIRED_FREE_GB=0\nINDEX_CPU_PERCENT=0\n"
        "INDEX_FILE_TIME_LIMIT_S=0\nINDEX_STALL_LIMIT_S=0\n", encoding="utf-8")
    locks = tmp_path / "locks"
    locks.mkdir()
    argv = child_command([corpus], env_file=env_file, workers=1,
                         extra=["--fake-embedder-for-bench"])
    # `python -m app.cli ...` becomes `python -c <the same, with a stuck reader>`.
    program = textwrap.dedent("""
        import sys
        from tests import private_locks
        private_locks.install()          # this is a real index run: not the machine's lock
        import app.index.pipeline as pipeline_module
        real = pipeline_module.extract
        def extract(path, **kwargs):
            if str(path).endswith("a-stuck.txt"):
                while True:
                    sum(range(1000))
            return real(path, **kwargs)
        pipeline_module.extract = extract
        from app.cli import main
        sys.exit(main(sys.argv[1:]))
    """)
    assert argv[1:3] == ["-m", "app.cli"]
    argv = [argv[0], "-c", program, *argv[3:]]
    env = dict(os.environ, TMPDIR=str(locks), PYTHONPATH=str(PROJECT))
    run = ChildIndexRun(argv, env=env, cwd=PROJECT,
                        stderr_path=tmp_path / "child-stderr.log")
    pressed: list[bool] = []

    def progress(stats) -> None:
        for key, worker in (getattr(stats, "workers", None) or {}).items():
            if worker.get("file") == "a-stuck.txt" and not pressed:
                pressed.append(run.force_skip(key))

    outcome: dict = {}

    def target() -> None:
        try:
            outcome["stats"] = run.run(on_progress=progress)
        except BaseException as exc:                # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(240)
    if thread.is_alive():
        run.shutdown(grace_s=5)
        pytest.fail("the child run never finished: Force skip did not reach it")
    if "error" in outcome:
        raise outcome["error"]
    stats = outcome["stats"]
    assert pressed == [True]
    assert stats.indexed == 4
    assert stats.skipped_by_code.get("ERR_FILE_TIMEOUT") == 1


def test_force_skip_is_refused_with_no_child() -> None:
    from app.index.child_run import ChildIndexRun

    assert ChildIndexRun([sys.executable, "-c", "pass"]).force_skip("1") is False
