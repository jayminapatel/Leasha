"""The progress bar, and what Stop means.

Layer: L5

Reported as *"the indexing start stop and progress needs thorough checking as it
does not feel right"*. It was right not to feel right.

**Two bugs, and they pointed in opposite directions.** The bar left out the
files being indexed - and once that was fixed it went to 100% within seconds,
because the denominator was `seen`, which a bounded queue keeps permanently
close to the numerator. See `progress_for`.

**The bar left out the files being indexed.** The numerator was
`unchanged + skipped`; a first index of a fresh corpus has nothing unchanged and
little skipped, so the bar sat near zero for hours while the log showed thousands
of files done. Nothing could have caught this - a bar that moves too slowly still
moves, and no test asserted what it should read.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ui.presenter import progress_for


@dataclass
class Stats:
    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    skipped: int = 0
    #: **These tests are about the arithmetic, so the walk is over.**
    #:
    #: While the walker is still running, `seen` is a running tally rather than
    #: a total, and `progress_for` deliberately refuses to compute a percentage
    #: from it - the bounded work queue keeps `seen` about a queue-length ahead
    #: of `done`, so the ratio read ~97% within the first minute of a run with
    #: the whole corpus left. That case is covered in
    #: `test_pause_is_visible.py`; here the denominator is known and the
    #: question is whether the numerator adds up.
    walk_complete: bool = True


def fraction(stats, **kwargs) -> float:
    value, total = progress_for(stats, **kwargs)
    return value / total


def test_indexed_files_count_towards_progress():
    """**The bug.** A thousand files indexed out of a thousand seen is finished,
    not zero - and this is the case on every first run."""
    assert fraction(Stats(seen=1000, indexed=1000)) == 1.0


def test_unchanged_files_count_too():
    """A re-index where nothing changed is a run that is fully done."""
    assert fraction(Stats(seen=500, unchanged=500)) == 1.0


def test_skipped_files_count_as_finished_with():
    """The walker is done with them either way. A corpus of scanned PDFs with
    no text layer would otherwise never reach the end of the bar."""
    assert fraction(Stats(seen=100, skipped=100)) == 1.0


def test_a_mixed_run_adds_all_three():
    value, total = progress_for(Stats(seen=100, indexed=40, unchanged=30, skipped=20))
    assert (value, total) == (90, 100)


def test_the_denominator_grows_with_the_walk():
    """No total is knowable before the walk finishes. A fixed one would be a
    lie; an indeterminate bar spins forever and reads as stuck."""
    assert progress_for(Stats(seen=50, indexed=10))[1] == 50
    assert progress_for(Stats(seen=5000, indexed=10))[1] == 5000


def test_an_estimate_is_used_when_it_is_bigger():
    """Early in a walk, `seen` is far below the truth and the bar would jump
    backwards as the denominator caught up."""
    assert progress_for(Stats(seen=10, indexed=10), total_estimate=1000)[1] == 1000


def test_the_estimate_is_ignored_once_the_walk_passes_it():
    """An estimate that turns out low must not cap the bar below what is really
    happening - the walker finds files the estimate never counted."""
    assert progress_for(Stats(seen=3000, indexed=2000), total_estimate=1000)[1] == 3000


def test_the_value_never_exceeds_the_maximum():
    """`seen` can lag `done` by a tick. A bar past its own maximum is a Qt
    warning on the console and a full bar while the run is plainly still going."""
    value, total = progress_for(Stats(seen=5, indexed=100))
    assert value <= total


def test_the_first_tick_does_not_divide_by_zero():
    value, total = progress_for(Stats())
    assert total >= 1 and value == 0


def test_missing_attributes_are_treated_as_zero():
    """Progress payloads have grown fields over time. A tick from an older
    shape must not take the panel down."""
    class Partial:
        seen = 10
        walk_complete = True
    value, total = progress_for(Partial())
    assert (value, total) == (0, 10)


def test_none_values_do_not_raise():
    class Nones:
        seen = None
        indexed = None
        unchanged = 5
        skipped = None
        walk_complete = True
    assert progress_for(Nones()) == (5, 5)


# ---------------------------------------------------------------------------
# What the buttons say
# ---------------------------------------------------------------------------

def source() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "app" / "ui" / "indexing_view.py").read_text(
        encoding="utf-8")


def test_the_button_says_stop_because_that_is_what_it_does():
    """It said "Pause" and there is no resume - the run ends and the next Start
    begins a new one. A button that promises to pause and then stops is a button
    people stop trusting."""
    text = source()
    assert 'QPushButton("Stop")' in text
    assert 'QPushButton("Pause")' not in text


def test_stopping_keeps_saying_what_it_is_doing():
    """Both buttons are disabled while a run winds down, which is correct -
    there is nothing useful to click - but it left the panel looking frozen."""
    assert "_stopping" in source()


def test_a_failed_run_resets_the_bar():
    """A run that failed at 40% left the bar at 40%, inviting the reading that
    it is still going - while the buttons came back a moment later, so the panel
    contradicted itself."""
    text = source()
    failed = text.split("def _on_failed")[1].split("def ")[0]
    assert "self.bar.setValue(0)" in failed


def test_a_payload_with_no_walk_complete_field_is_treated_as_still_walking():
    """**Failing towards the indeterminate bar is the safe direction.**

    An older progress payload, or a caller that has not been updated, does not
    know whether the walk has finished. Guessing "finished" puts the bar at a
    percentage that may be a lie; guessing "still going" shows a moving bar and
    the live counts beside it, which is true either way.
    """
    class Old:
        seen = 100
        indexed = 100

    assert progress_for(Old()) == (0, 0)
