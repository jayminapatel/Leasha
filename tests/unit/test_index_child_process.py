"""Work order 0x §2a-2c with the real thing: `app.cli index --events jsonl` as a child.

Layer: L3

`test_run_events.py` holds the protocol and `test_child_run.py` the
supervisor against a fake. These start the **real** indexer as a child of the
test, through the same `ChildIndexRun` the window uses, on a small corpus,
with the labelled fake embedder (`--fake-embedder-for-bench`) so no model is
needed. What only the real child can prove:

* a whole run arrives as the same `IndexStats` the page draws, phases and
  activity log included, and names itself "the window" on the run record;
* Pause holds it and Stop ends it cleanly - and a clean stop is **not**
  reported by 0w's interrupted-run notice;
* a child that is killed leaves the evidence that notice reads, and the next
  run carries on;
* **no orphan**: if the window's process dies outright, the child notices its
  input has ended and stops by itself.

Each test starts a Python process that imports the pipeline, so each costs a
few seconds. The lock directory is the test's own (`TMPDIR`), as in
`test_interrupted_runs.py`, so a run elsewhere on the machine cannot interfere.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.core.run_lock import GUI, active_run
from app.index.child_run import ChildIndexRun, child_command
from app.index.interrupted import read_unfinished_run
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_index_freshness import write_aged

PROJECT = Path(__file__).resolve().parents[2]
FILES = 30


@pytest.fixture()
def setup(tmp_path: Path) -> dict:
    corpus = tmp_path / "docs"
    corpus.mkdir()
    for n in range(FILES):
        write_aged(corpus / f"note{n:02d}.txt",
                   f"Barnsley Dairy note {n} about the HACCP audit and the valves.\n" * 5)
    data = tmp_path / "data"
    env_file = tmp_path / ".env"
    # No free-space floor and no CPU ceiling: the sandbox disk is small and the
    # machine is shared, and neither is what these tests are about.
    env_file.write_text(
        f"DATA_PATH={data.as_posix()}\nLOG_PATH={(tmp_path / 'logs').as_posix()}\n"
        "MIN_FREE_GB=0\nREQUIRED_FREE_GB=0\nINDEX_CPU_PERCENT=0\n", encoding="utf-8")
    locks = tmp_path / "locks"
    locks.mkdir()
    return {"corpus": corpus, "env": env_file, "db": data / "fts" / "knowledge.db",
            "locks": locks, "tmp": tmp_path}


def _child(setup: dict) -> ChildIndexRun:
    argv = child_command([setup["corpus"]], env_file=setup["env"],
                         extra=["--fake-embedder-for-bench"])
    env = dict(os.environ, TMPDIR=str(setup["locks"]), PYTHONPATH=str(PROJECT))
    return ChildIndexRun(argv, env=env, cwd=PROJECT,
                         stderr_path=setup["tmp"] / "child-stderr.log")


def _wait_for(predicate, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def _start(run: ChildIndexRun, seen: list):
    import threading

    outcome: dict = {}

    def target() -> None:
        try:
            outcome["stats"] = run.run(on_progress=seen.append)
        except BaseException as exc:            # noqa: BLE001 - inspected below
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


def _finish(run: ChildIndexRun, seen: list, timeout: float = 300.0):
    """`run.run()`, but a run that never finishes fails **with its last state**.

    2026-09-29: on the first Windows CI run this test waited until the suite's
    own ten-minute limit ended everything, with nothing said about why. The
    child was alive (its heartbeats kept coming) and not indexing. Now the
    failure names the phase it was in and why it was paused, and the child is
    ended so the rest of the suite runs.
    """
    thread, outcome = _start(run, seen)
    thread.join(timeout)
    if thread.is_alive():
        last = seen[-1] if seen else None
        run.shutdown(grace_s=5)
        pytest.fail(
            f"the child run did not finish in {timeout:.0f}s; last phase "
            f"{getattr(last, 'phase', None)!r}, paused because "
            f"{getattr(last, 'pause_reason', '')!r}, "
            f"{getattr(last, 'indexed', 0)} of {FILES} indexed")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["stats"]


def test_a_whole_run_arrives_as_the_stats_the_page_draws(setup) -> None:
    seen: list = []
    stats = _finish(_child(setup), seen)

    assert stats.indexed == FILES and stats.seen == FILES
    assert stats.stopped_early is None
    phases = [s.phase for s in seen]
    assert "model" in phases and "reading" in phases     # 0w's phases cross
    assert any(e.kind == "phase" for e in stats.activity.entries())
    assert stats.resolved.get("workers")                  # §5c's record crosses
    # Nothing left behind to be mistaken for a run that died.
    with SqliteStore(setup["db"]) as store:
        assert read_unfinished_run(store, lock_dir=setup["locks"]) is None
        assert active_run(store) is None


def test_pause_holds_the_run_and_stop_ends_it_cleanly(setup) -> None:
    run = _child(setup)
    run.pause()                        # pressed before the model has even loaded
    seen: list = []
    thread, outcome = _start(run, seen)
    _wait_for(lambda: any(getattr(s, "paused_by_person", False) for s in seen))

    held_at = seen[-1].indexed
    time.sleep(1.0)
    assert seen[-1].indexed == held_at, "a paused run kept indexing"
    # While it runs, the record says whose run it is, in the page's words.
    with SqliteStore(setup["db"]) as store:
        assert (active_run(store) or {}).get("owner") == GUI

    run.request_stop()
    thread.join(90)
    assert "stats" in outcome, outcome.get("error")
    with SqliteStore(setup["db"]) as store:
        # A clean Stop is not "did not finish" (0w 3d).
        assert read_unfinished_run(store, lock_dir=setup["locks"]) is None


def test_a_killed_child_is_reported_and_the_next_run_carries_on(setup) -> None:
    run = _child(setup)
    run.pause()
    seen: list = []
    thread, outcome = _start(run, seen)
    _wait_for(lambda: any(getattr(s, "paused_by_person", False) for s in seen))
    run._proc.kill()                   # noqa: SLF001 - the crash, as the OS delivers it
    thread.join(60)

    error = outcome["error"]
    assert isinstance(error, AppErrorException)
    assert error.error.code == "ERR_INDEX_PROCESS_ENDED"
    with SqliteStore(setup["db"]) as store:
        unfinished = read_unfinished_run(store, lock_dir=setup["locks"])
    assert unfinished is not None and unfinished["owner"] == GUI   # 0w's notice

    again = _child(setup).run()
    assert again.indexed + again.unchanged == FILES
    with SqliteStore(setup["db"]) as store:
        assert read_unfinished_run(store, lock_dir=setup["locks"]) is None


_PARENT = textwrap.dedent("""
    import os, sys, threading, time
    sys.path.insert(0, {project!r})
    from app.index.child_run import ChildIndexRun
    argv = {argv!r}
    run = ChildIndexRun(argv, env=dict(os.environ, TMPDIR={locks!r}),
                        cwd={project!r})
    run.pause()
    said = threading.Event()
    def seen(stats):
        if stats.paused_by_person and not said.is_set():
            said.set()
            print(run.pid, flush=True)
    threading.Thread(target=run.run, kwargs={{"on_progress": seen}},
                     daemon=True).start()
    time.sleep(600)
""")


def test_an_indexer_whose_window_dies_stops_by_itself(setup) -> None:
    """The window killed outright - Task Manager, a crash - must not leave an
    indexer running with no window: the "closed but still running" incident."""
    import psutil

    argv = child_command([setup["corpus"]], env_file=setup["env"],
                         extra=["--fake-embedder-for-bench"])
    script = _PARENT.format(project=str(PROJECT), argv=argv, locks=str(setup["locks"]))
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                              cwd=str(PROJECT), text=True)
    try:
        pid = int(parent.stdout.readline().strip())
        child = psutil.Process(pid)
        assert child.is_running()
    finally:
        parent.kill()                  # the window, gone without closing anything
        parent.wait(10)

    # Its input ends, it stops cleanly - well inside ORPHAN_GRACE_S.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if child.status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.1)
    else:
        child.kill()
        pytest.fail("the indexing process outlived its window")
    with SqliteStore(setup["db"]) as store:
        # It stopped cleanly rather than dying, so nothing reads as interrupted.
        assert read_unfinished_run(store, lock_dir=setup["locks"]) is None


def test_the_benchmark_measures_the_child_the_way_the_window_runs_it(tmp_path) -> None:
    """Order 0x §2d's "after" number: `bench-pipeline --child-process` indexes
    the same corpus through `ChildIndexRun` and says so in its report."""
    from app.index.pipeline_bench import BenchOptions, run_pipeline_bench

    report = run_pipeline_bench(BenchOptions(
        corpus_folder=tmp_path / "corpus", size="tiny", embedder="fake",
        child_process=True, full_speed=True, work_dir=tmp_path / "work"))
    results = report["results"]
    assert results["documents"] == results["expected_documents"]
    assert "CHILD process" in report["conditions"]["pipeline"]["entry"]
    assert report["conditions"]["pipeline"]["memory_of"].startswith("the child")
    assert "CHILD PROCESS" in report["label"]
