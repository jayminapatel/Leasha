r"""A paused run says so, instead of looking hung.

The resource governor waits for memory to settle or for the machine to be idle.
One observed pause ran for six minutes: the progress bar did not move, the text
did not change, and nothing anywhere said why. That is indistinguishable from a
hang, and the correct response to a hang is to kill the run - losing the work.
"""

from __future__ import annotations

from app.index.pipeline import IndexStats
from app.index.resources import ResourceGovernor
from app.ui.presenter import progress_text


def test_the_governor_reports_whether_it_is_waiting_now() -> None:
    """`paused_seconds` is a total and answers a different question."""
    governor = ResourceGovernor()
    assert governor.paused is False
    assert governor.pause_reason == ""
    assert hasattr(governor, "paused_seconds"), "the cumulative total is still there"


def test_a_paused_run_says_so_and_says_why() -> None:
    stats = IndexStats(
        seen=400, indexed=120,
        paused=True,
        pause_reason="Indexing has added 2,483MB (now 2,962MB), above the 1,500MB it is allowed to add.",
    )
    headline, detail = progress_text(stats)

    assert "Paused" in headline
    assert "2,483MB" in detail, "the real reason and the real number, not a generic message"
    assert "120" in detail, "what has been done so far still matters while waiting"


def test_it_says_the_run_will_resume_on_its_own() -> None:
    """Otherwise the reasonable thing to do is press Stop."""
    _headline, detail = progress_text(IndexStats(paused=True, pause_reason="Waiting."))
    assert "continue on its own" in detail


def test_a_running_run_is_unaffected() -> None:
    headline, _detail = progress_text(IndexStats(seen=10, indexed=4))
    assert "Paused" not in headline


def test_stopping_still_wins_over_paused() -> None:
    """Somebody who pressed Stop is told about Stop, not about memory."""
    stats = IndexStats(paused=True, pause_reason="memory", indexed=3)
    headline, _detail = progress_text(stats, stopping=True)
    assert "Stopping" in headline


# --- the bar that read 100% within seconds -----------------------------------

def test_the_bar_is_indeterminate_while_the_walk_is_still_running() -> None:
    """**The bug: it went straight to 100%.**

    The work queue is bounded, so the walker can never get more than a
    queue-length ahead of the workers. `seen` is therefore always about `done`
    plus a queue, and `done / seen` reaches ~97% within the first minute with
    the whole corpus still to come. The bar was showing how full the queue was.
    """
    from app.ui.presenter import progress_for

    early = IndexStats(seen=256, indexed=0, unchanged=0, skipped=0)
    assert progress_for(early) == (0, 0), "a determinate bar here is a lie"

    later = IndexStats(seen=10_256, indexed=10_000)
    assert progress_for(later) == (0, 0), "still walking, still unknowable"


def test_it_becomes_a_real_percentage_once_the_walk_finishes() -> None:
    """`seen` is a total only after the walker has stopped finding files."""
    from app.ui.presenter import progress_for

    stats = IndexStats(seen=1_000, indexed=400, walk_complete=True)
    value, total = progress_for(stats)
    assert (value, total) == (400, 1_000)


def test_a_known_total_gives_a_percentage_from_the_first_tick() -> None:
    """A caller that has counted the files first does not have to wait."""
    from app.ui.presenter import progress_for

    value, total = progress_for(IndexStats(seen=50, indexed=20), total_estimate=1_000)
    assert (value, total) == (20, 1_000)


def test_the_pipeline_marks_the_walk_complete() -> None:
    """The flag has to be set by the code that finishes walking, or the bar
    stays indeterminate for ever - which is the opposite failure."""
    import inspect

    from app.index import pipeline

    assert "stats.walk_complete = True" in inspect.getsource(pipeline)


def test_the_command_line_says_paused_too():
    """**The window got this and the CLI did not, so a real run read as stuck.**

    `cmd_index` builds its own progress line rather than going through
    `progress_text`, so the pause branch added for the panel did not reach it.
    A governor pause held the line unchanged for minutes and was reported as a
    hang; it was working.
    """
    import inspect

    from app import cli

    show = inspect.getsource(cli.cmd_index)
    assert "PAUSED" in show, "the command-line progress line never mentions a pause"
    assert "pause_reason" in show, "it should say why, not just that"
