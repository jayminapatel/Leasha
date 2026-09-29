"""Work order 0x §2b and §2c: the window's supervisor for an indexer process.

Layer: L3

`app/index/child_run.py` starts the indexer as a child process and stands in
for a `Pipeline`. These tests drive it against `fake_index_child.py`, a tiny
script that speaks the same protocol but misbehaves on request, so each way
the pipe can go wrong is exercised on purpose:

* a line split across two writes, a burst of hundreds, garbage on stdout;
* Pause, Resume and Stop reaching the child, including a Stop pressed before
  the child existed;
* the child dying mid-line, or exiting without saying it finished, and the
  plain-words error that results - naming the file it was reading;
* the window closing while a child runs: asked to stop, then ended, and never
  left behind.

The real `app.cli index` child is tested in `test_index_child_process.py`.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.index import child_run
from app.index.child_run import ChildIndexRun, end_all_children, live_children

PROJECT = Path(__file__).resolve().parents[2]
FAKE = Path(__file__).resolve().parent / "fake_index_child.py"


def _run(mode: str, tmp_path: Path) -> ChildIndexRun:
    return ChildIndexRun(
        [sys.executable, str(FAKE), mode], cwd=PROJECT,
        env=dict(os.environ, PYTHONPATH=str(PROJECT)),
        stderr_path=tmp_path / "child-stderr.log")


def _in_thread(run: ChildIndexRun, seen: list) -> tuple[threading.Thread, dict]:
    """`run.run` on a thread of its own, as `IndexWorker` calls it."""
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["stats"] = run.run(on_progress=seen.append)
        except BaseException as exc:            # noqa: BLE001 - inspected by the test
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


def _wait_for(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def _gone(pid: int) -> bool:
    import psutil

    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


# --- reading the child --------------------------------------------------------

def test_split_lines_bursts_and_garbage_are_read_as_events_and_nothing_else(tmp_path) -> None:
    seen: list = []
    stats = _run("normal", tmp_path).run(on_progress=seen.append)

    # Every complete event arrives, in order - the half line was waited for,
    # the stray print ignored. (Painting is throttled by the page, not here.)
    assert [s.indexed for s in seen] == list(range(0, 302))
    assert seen[0].phase == "model"
    assert seen[1].current == "a.txt"
    # A real IndexStats every time, so the presenter's properties work.
    assert seen[-1].files_per_minute == 0.0 and seen[-1].snapshot().indexed == 301
    assert stats.indexed == 301 and stats.phase == "word_index"


def test_pause_resume_and_stop_reach_the_child_in_order(tmp_path) -> None:
    run = _run("commands", tmp_path)
    seen: list = []
    thread, outcome = _in_thread(run, seen)
    _wait_for(lambda: seen)
    run.pause()
    _wait_for(lambda: any(s.paused_by_person for s in seen))
    assert run.paused_by_person
    run.resume()
    run.request_stop()
    thread.join(20)

    assert outcome["stats"].notices == ["pause", "resume", "stop"]
    assert not run.running and run.returncode == 0


def test_a_stop_pressed_before_the_child_existed_is_sent_when_it_does(tmp_path) -> None:
    run = _run("stop_ok", tmp_path)
    run.request_stop()                             # nothing started yet
    started = time.monotonic()
    stats = run.run()
    assert stats.indexed == 1
    assert time.monotonic() - started < 20


def test_a_command_to_a_child_that_has_gone_is_harmless(tmp_path) -> None:
    run = _run("normal", tmp_path)
    run.run()
    run.pause()
    run.resume()
    run.request_stop()                             # no exception, nothing to do


# --- when it goes wrong -----------------------------------------------------------

def test_a_child_that_dies_is_reported_in_plain_words_naming_its_file(tmp_path) -> None:
    run = _run("crash", tmp_path)
    with pytest.raises(AppErrorException) as caught:
        run.run()
    error = caught.value.error

    assert error.code == "ERR_INDEX_PROCESS_ENDED"
    assert error.message == "The indexing process stopped unexpectedly while reading big.pst."
    assert "Press Start to carry on" in error.suggestion          # how to fix it
    assert "exit code 3" in error.details
    assert "ran out of memory" in error.details                   # its last words
    assert run.returncode == 3 and not live_children()


def test_a_child_that_exits_without_finishing_is_not_taken_for_a_success(tmp_path) -> None:
    with pytest.raises(AppErrorException) as caught:
        _run("silent_exit", tmp_path).run()
    assert caught.value.error.code == "ERR_INDEX_PROCESS_ENDED"
    assert "half.docx" in caught.value.error.message
    assert "exit code 0" in caught.value.error.details


def test_a_child_that_never_says_anything_is_ended_and_reported(tmp_path, monkeypatch) -> None:
    """2026-09-29: the first Windows CI run waited ten minutes on a silent child."""
    monkeypatch.setattr(child_run, "FIRST_WORD_S", 1.0)
    run = ChildIndexRun([sys.executable, "-c", "import time; time.sleep(120)"],
                        stderr_path=tmp_path / "child-stderr.log")
    started = time.monotonic()
    with pytest.raises(AppErrorException) as caught:
        run.run()
    assert time.monotonic() - started < 15
    assert caught.value.error.code == "ERR_INDEX_PROCESS_ENDED"
    assert "sent nothing for 1s" in caught.value.error.details
    assert _gone(run.pid) and not live_children()


def test_a_child_that_goes_quiet_after_starting_is_ended_too(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(child_run, "SILENCE_S", 1.0)
    script = "import sys, time; print('hello', flush=True); time.sleep(120)"
    run = ChildIndexRun([sys.executable, "-c", script],
                        stderr_path=tmp_path / "child-stderr.log")
    with pytest.raises(AppErrorException) as caught:
        run.run()
    assert "sent nothing for 1s" in caught.value.error.details
    assert _gone(run.pid)


def test_a_child_that_could_not_start_its_run_passes_its_own_error_on(tmp_path) -> None:
    with pytest.raises(AppErrorException) as caught:
        _run("error", tmp_path).run()
    error = caught.value.error
    assert error.code == "ERR_INDEX_RUNNING"
    assert "the command line, since 14:02" in error.message


def test_with_no_file_known_the_sentence_is_still_true(tmp_path) -> None:
    run = _run("normal", tmp_path)
    error = run._ended_error()                     # noqa: SLF001 - the wording itself
    assert error.message == "The indexing process stopped unexpectedly."


# --- closing the window -------------------------------------------------------------

def test_closing_asks_a_child_to_stop_and_it_does(tmp_path) -> None:
    run = _run("stop_ok", tmp_path)
    seen: list = []
    thread, outcome = _in_thread(run, seen)
    _wait_for(lambda: seen)
    pid = run.pid

    assert run.shutdown(grace_s=10) == "stopped"
    thread.join(10)
    assert outcome["stats"].indexed == 1
    assert _gone(pid)


def test_closing_ends_a_child_that_will_not_stop_and_leaves_no_orphan(tmp_path) -> None:
    run = _run("ignore_stop", tmp_path)
    seen: list = []
    thread, outcome = _in_thread(run, seen)
    _wait_for(lambda: seen)
    pid = run.pid
    assert run in live_children()

    started = time.monotonic()
    assert end_all_children(grace_s=0.5) == 1
    assert time.monotonic() - started < child_run.TERMINATE_WAIT_S + 5
    thread.join(15)
    assert not thread.is_alive()
    assert _gone(pid), "the indexing process outlived the window"
    assert isinstance(outcome.get("error"), AppErrorException)
    assert "wedged.pst" in outcome["error"].error.message
    assert not live_children()


def test_closing_with_no_child_running_does_nothing() -> None:
    assert end_all_children() == 0


def test_the_child_notices_when_its_window_is_gone(tmp_path) -> None:
    """The pipe is the lifeline: closing the window's end ends the child."""
    run = _run("exit_on_eof", tmp_path)
    seen: list = []
    thread, outcome = _in_thread(run, seen)
    _wait_for(lambda: seen)
    pid = run.pid
    run._proc.stdin.close()                      # noqa: SLF001 - what a dead window does
    thread.join(20)
    assert _gone(pid)


# --- the command line it builds -------------------------------------------------------

def test_the_command_is_a_list_with_every_choice_the_window_made(tmp_path) -> None:
    argv = child_run.child_command(
        [tmp_path / "Docs", "-odd name"], env_file=tmp_path / ".env", prune=False,
        recheck_archives=True, workers=3, cloud_content_keys={"d:\\cloud"},
        python="/py")
    assert argv[:4] == ["/py", "-m", "app.cli", "index"]
    assert ["--events", "jsonl"] == argv[4:6]
    assert "--run-owner" in argv and argv[argv.index("--run-owner") + 1] == "window"
    assert argv[argv.index("--env") + 1] == str(tmp_path / ".env")
    assert "--no-prune" in argv and "--recheck-archives" in argv
    assert argv[argv.index("--workers") + 1] == "3"
    assert argv[argv.index("--cloud-content-key") + 1] == "d:\\cloud"
    # The folders come after `--`, so one starting with a dash is a folder.
    assert argv[argv.index("--") + 1:] == [str(tmp_path / "Docs"), "-odd name"]


def test_the_childs_settings_are_the_windows_live_settings(tmp_path) -> None:
    """Every key `load_settings` reads, from the window's copy - including a
    change the Tuning shelf made in memory and a value still at its default."""
    from app.core.config import SETTING_KEYS, load_settings

    env_file = tmp_path / ".env"
    env_file.write_text(f"DATA_PATH={(tmp_path / 'data').as_posix()}\n"
                        f"LOG_PATH={(tmp_path / 'logs').as_posix()}\n", encoding="utf-8")
    window = load_settings(env_file).model_copy(
        update={"index_workers": 3, "index_two_phase": False,
                "index_separate_process": True})
    env = child_run.settings_environment(window)
    assert set(env) == set(SETTING_KEYS)

    saved = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        child = load_settings(env_file)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    for key in SETTING_KEYS:
        assert getattr(child, key.lower()) == getattr(window, key.lower()), key
