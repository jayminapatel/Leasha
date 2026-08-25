r"""One file per run, and what has to be in it for the round trip to shorten.

Layer: L0

Asked for directly: *"for testing create detailed log files which for every run
you can check on the project folder"*. The value is not that lines get written -
`logs\app\` has always had those. It is that one run has a boundary, a header
saying what the settings actually were, and a footer saying how it ended.

The tests worth having here are the ones for the failures that would leave a run
log looking like it works: a file that stops after its first line, a tally that
counts occurrences rather than causes, and a footer that never mentions the
threads keeping the process alive.
"""

from __future__ import annotations

import threading

from loguru import logger

from app.core.runlog import KEEP_RUNS, RunLog, _prune, current, start_run


class FakeSettings:
    """Only `describe()` is used, and that is the whole contract."""

    def __init__(self, **values):
        self._values = values

    def describe(self):
        return dict(self._values)


def _text(run: RunLog) -> str:
    return run.path.read_text(encoding="utf-8")


# --- the boundary -----------------------------------------------------------

def test_the_file_is_named_after_the_run(tmp_path):
    """The folder sorts by time and reads as a list of what was done."""
    run = start_run(tmp_path, "index")
    try:
        assert run.path.parent.name == "runs"
        assert run.path.name.startswith("run-")
        assert run.path.name.endswith("-index.log")
    finally:
        run.finish(0)


def test_a_command_windows_cannot_put_in_a_filename_still_gets_a_file(tmp_path):
    """A run log that failed to open is the least useful outcome available."""
    run = start_run(tmp_path, 'search "a/b:c*"')
    try:
        assert run.path.is_file()
    finally:
        run.finish(0)


def test_the_run_in_progress_is_reachable_without_being_passed_around(tmp_path):
    """`current()` is how the indexer records a stage without threading the
    object through five layers. It is cleared on finish, so the next command
    does not write into the last one's file."""
    run = start_run(tmp_path, "index")
    assert current() is run

    run.finish(0)

    assert current() is None


# --- what lands in it -------------------------------------------------------

def test_everything_logged_while_it_is_open_lands_in_the_file(tmp_path):
    run = start_run(tmp_path, "search")
    try:
        logger.bind(component="test").info("a line from somewhere else")
    finally:
        run.finish(0)

    assert "a line from somewhere else" in _text(run)


def test_setup_logging_does_not_truncate_the_run(tmp_path):
    """**The regression this feature would otherwise ship with.**

    `setup_logging` calls `logger.remove()`, which takes every handler with it -
    including this one. It always runs *second*, because a command has to load
    its settings before it knows where logs live, so without `reattach` the file
    would hold a header, a footer, and none of the run. That failure looks
    exactly like a working feature until somebody opens one.
    """
    from app.core.logging import setup_logging

    run = start_run(tmp_path, "index")
    try:
        setup_logging(tmp_path / "logs", force=True)
        logger.bind(component="test").info("after the handlers were cleared")
    finally:
        run.finish(0)
        logger.remove()

    assert "after the handlers were cleared" in _text(run), (
        "the run log stopped at the first line of the run")


def test_the_settings_written_are_the_ones_in_force(tmp_path):
    """Not the defaults and not the `.env` file - the object the run is using.

    `doctor` reported its own hardcoded defaults for a week, and a measurement
    was attributed to the wrong model because of it.
    """
    run = start_run(tmp_path, "search")
    try:
        run.settings(FakeSettings(rerank_model="the-one-actually-loaded"))
    finally:
        run.finish(0)

    assert "the-one-actually-loaded" in _text(run)


def test_a_key_that_names_a_credential_is_masked(tmp_path):
    """The file is local and is not redacted, because `diagnose` is the command
    that produces something shareable. But "it never leaves the machine" stops
    being true the moment somebody attaches one to an email."""
    run = start_run(tmp_path, "search")
    try:
        run.settings(FakeSettings(ollama_url="http://127.0.0.1:11434",
                                  api_key="sk-not-in-the-file"))
    finally:
        run.finish(0)

    body = _text(run)
    assert "sk-not-in-the-file" not in body
    assert "<masked>" in body
    assert "11434" in body, "masking must not swallow the ordinary settings"


# --- the footer -------------------------------------------------------------

def test_errors_are_grouped_by_code_rather_than_counted(tmp_path):
    """**Four hundred of one problem is one problem.**

    A flat log of 400 `ERR_CONVERTER_MISSING` and one `ERR_DB_LOCKED` reads as
    401 things wrong. The tally says it is two, which is the number that
    decides what to do next.
    """
    run = start_run(tmp_path, "index")
    try:
        for _ in range(4):
            logger.bind(component="x", error_code="ERR_CONVERTER_MISSING").warning("no soffice")
        logger.bind(component="x", error_code="ERR_DB_LOCKED").error("locked")
    finally:
        run.finish(1)

    assert run.errors["ERR_CONVERTER_MISSING"] == 4
    assert run.errors["ERR_DB_LOCKED"] == 1
    assert "ERR_CONVERTER_MISSING" in _text(run)


def test_the_tally_reads_the_code_field_not_the_message(tmp_path):
    """The code is a structured field precisely so nothing has to parse prose.
    A message merely *mentioning* a code is not an occurrence of it."""
    run = start_run(tmp_path, "index")
    try:
        logger.bind(component="x").warning("this mentions ERR_DB_LOCKED in passing")
    finally:
        run.finish(0)

    assert run.errors["ERR_DB_LOCKED"] == 0
    assert run.errors["(uncoded)"] == 1


def test_a_healthy_run_says_none_rather_than_leaving_the_section_empty(tmp_path):
    """An empty heading reads as a section that failed to fill."""
    run = start_run(tmp_path, "search")
    run.finish(0)

    assert "none" in _text(run).split("Errors by code")[1]


def test_threads_still_alive_are_named_and_the_non_daemon_ones_marked(tmp_path):
    """**The diagnosis of a window that closes without the process ending.**

    The interpreter joins every non-daemon thread before it exits. That was
    worked out by reasoning about `concurrent.futures`; the footer says it.
    """
    release = threading.Event()
    worker = threading.Thread(target=release.wait, args=(5,),
                              name="a-lingering-worker", daemon=False)
    worker.start()

    run = start_run(tmp_path, "window")
    try:
        run.finish(0)
    finally:
        release.set()
        worker.join(5)

    body = _text(run)
    assert "a-lingering-worker" in body
    assert "NON-DAEMON" in body
    assert "hangs after" in body, "it must say what a lingering thread means"


def test_the_stages_say_where_the_time_went(tmp_path):
    """A run that took nine minutes is not a finding. A run that spent eight of
    them in one stage is."""
    run = start_run(tmp_path, "index")
    try:
        with run.timer("walk"):
            pass
        with run.timer("embed"):
            pass
    finally:
        run.finish(0)

    assert [name for name, _s in run.stages] == ["walk", "embed"]
    assert "Timings" in _text(run)


def test_a_stage_that_raised_is_recorded_as_one_and_the_error_still_escapes(tmp_path):
    """How long something took before it broke is usually the question, and a
    timing is never a reason to swallow a fault."""
    run = start_run(tmp_path, "index")
    try:
        try:
            with run.timer("embed"):
                raise ValueError("boom")
        except ValueError:
            pass
    finally:
        run.finish(1)

    assert run.stages[0][0] == "embed (failed)"


def test_an_exception_nobody_caught_is_recorded(tmp_path):
    """The sink never sees these, so without it the file ends at whatever line
    happened to be logged last - the least useful place to stop."""
    run = start_run(tmp_path, "index")
    try:
        run.unhandled(RuntimeError("the thing that actually happened"))
    finally:
        run.finish("crash")

    assert "UNHANDLED RuntimeError: the thing that actually happened" in _text(run)


def test_the_exit_code_is_in_the_footer(tmp_path):
    """How it ended, at the end. Scrolling to the bottom is the one navigation
    everybody already knows."""
    run = start_run(tmp_path, "search")
    run.finish(2)

    footer = _text(run).split("Result")[-1]
    assert "exit code" in footer and "2" in footer
    assert "elapsed" in footer


# --- it must never be the cause of a failure --------------------------------

def test_a_folder_it_cannot_create_does_not_fail_the_run(tmp_path):
    """A run log that raises turns a small fault into a crash, inside the very
    run somebody is trying to understand."""
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory", encoding="utf-8")

    run = start_run(blocker, "index")
    run.note("still", "usable")
    run.finish(0)          # must not raise


def test_finishing_twice_is_harmless(tmp_path):
    """`closeEvent` fires more than once, and a CLI command that raises must
    not leave the sink attached for whatever runs next."""
    run = start_run(tmp_path, "window")
    run.finish(0)
    run.finish(0)

    assert _text(run).count("Threads at exit") == 1


def test_nothing_is_written_after_it_is_finished(tmp_path):
    """A detached sink that still writes is how one run's lines end up in
    another run's file."""
    run = start_run(tmp_path, "search")
    run.finish(0)
    before = _text(run)

    logger.bind(component="test").info("belongs to the next run, not this one")

    assert _text(run) == before


# --- housekeeping -----------------------------------------------------------

def test_only_the_newest_runs_are_kept(tmp_path):
    folder = tmp_path / "runs"
    folder.mkdir()
    for n in range(KEEP_RUNS + 5):
        (folder / f"run-2026010{n % 9}-00000{n % 9}-cmd{n}.log").write_text(
            "x", encoding="utf-8")

    _prune(folder, keep=3)

    assert len(list(folder.glob("run-*.log"))) == 3


def test_pruning_leaves_everything_that_is_not_a_run_log(tmp_path):
    """It shares `logs\\` with folders somebody else owns. Deleting by pattern
    is the difference between housekeeping and data loss."""
    folder = tmp_path / "runs"
    folder.mkdir()
    (folder / "run-20260101-000001-index.log").write_text("x", encoding="utf-8")
    (folder / "notes.txt").write_text("keep me", encoding="utf-8")

    _prune(folder, keep=0)

    assert (folder / "notes.txt").is_file()


def test_a_folder_that_cannot_be_tidied_is_not_a_failure(tmp_path):
    _prune(tmp_path / "does-not-exist", keep=1)   # must not raise
