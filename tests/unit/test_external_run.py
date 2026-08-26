r"""The window drawing a run it did not start, and the bar's own defects.

Layer: L5

Splitting the run lock from the window lock made `app.cli index` possible with
Leasha open, and created a state that had never existed: **an index genuinely
under way with nothing in this process knowing about it.** The window showed a
bar at zero and an enabled Start button, and pressing Start produced a lock
error for something it should simply have been showing.

Two of these pin defects that had nothing to do with the split and were found on
the way past. `_on_finished` painted a full green bar after a Stop, because
`pipeline.run` returns normally when it is asked to stop - so a run ended at 3%
finished at 100%, next to a headline saying it had been stopped. The identical
fault had already been found and fixed in `_on_failed` and not here.

The other is the reason the whole file exists. Every previous progress-bar test
is a string grep of `indexing_view.py` or a call to `progress_for` with a
hand-made dataclass; **nothing had ever instantiated the view**. Three bar bugs
have now shipped past a green suite. These build the real widget.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PyQt6")


def _qt():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _view():
    from app.ui.indexing_view import IndexingView

    _qt()
    return IndexingView()


def _record(**overrides):
    payload = {
        "pid": 4242,
        "owner": "the command line",
        "started_at": time.time() - 60,
        "updated_at": time.time(),
        "stats": {"seen": 500, "indexed": 300, "unchanged": 20, "skipped": 5,
                  "chunks": 900, "walk_complete": True},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# A run belonging to somebody else
# ---------------------------------------------------------------------------

def test_a_cli_run_is_drawn_in_the_window():
    r"""**The reported gap.** The bar sat at zero while an index was running."""
    view = _view()

    view.show_external(_record(), locked=True)

    assert view.bar.value() > 0, "the window shows nothing for a run in progress"
    assert view.bar.maximum() > 1
    assert not view.start_button.isEnabled()
    assert view.stop_button.isEnabled(), "Stop must reach the other process"


def test_the_headline_says_whose_run_it_is():
    """Stop reaches it now, so the sentence has to say what will be stopped."""
    view = _view()

    view.show_external(_record(owner="the command line"), locked=True)

    assert "the command line" in view.headline.text()


def test_a_record_without_the_lock_is_a_dead_process_not_a_run():
    r"""**The mutex is the authority; the record only supplies the words.**

    A process killed mid-run leaves its row behind and has its mutex released
    by the operating system. Trusting the row would refuse Start until somebody
    edited a database by hand - a paper lock, broken only at the worst moment.
    """
    view = _view()
    view.show_external(_record(), locked=True)
    assert not view.start_button.isEnabled()

    view.show_external(_record(), locked=False)

    assert view.start_button.isEnabled()
    assert view.bar.value() == 0


def test_a_record_too_old_to_believe_is_ignored():
    """The safety net for the gap between a process dying and anything noticing."""
    from app.ui.presenter import STALE_RUN_S

    view = _view()
    stale = _record(updated_at=time.time() - STALE_RUN_S - 60)

    view.show_external(stale, locked=True)

    assert view.start_button.isEnabled()


def test_is_running_spans_processes():
    r"""The scheduler asks this before starting a run on a timer.

    It was `self._worker is not None`, which stopped being the whole answer the
    moment a second process could be indexing. The lock would still refuse the
    run - so this is an error on a timer every hour rather than a corruption,
    which is its own kind of broken.
    """
    view = _view()
    assert not view.is_running()

    view.show_external(_record(), locked=True)

    assert view.is_running()


def test_the_window_does_not_fight_its_own_run_for_the_bar():
    """A live signal beats a polled one, and two of them flicker."""
    view = _view()
    view._worker = object()                      # pretend a local run is going
    view.bar.setRange(0, 100)
    view.bar.setValue(42)

    view.show_external(_record(), locked=True)

    assert view.bar.value() == 42, "the poll overwrote a live progress tick"


def test_going_idle_only_repaints_when_something_changed():
    """This runs on a timer for as long as the window is open."""
    view = _view()
    view.headline.setText("something a person is reading")

    view.show_external(None, locked=False)

    assert view.headline.text() == "something a person is reading"


# ---------------------------------------------------------------------------
# The bar's own defects
# ---------------------------------------------------------------------------

class _Finished:
    skipped_by_code: dict = {}
    skipped_roots: tuple = ()
    notices: tuple = ()
    stopped_early = None
    indexed = 12
    seen = 12
    unchanged = 0
    skipped = 0
    chunks = 40
    elapsed_s = 3.0
    bytes_read = 1024


def test_a_stopped_run_does_not_finish_at_a_hundred_percent():
    r"""**A run stopped at 3% painted a full green bar.**

    `pipeline.run` returns normally after `request_stop()`, so `finished` fires
    for a stop exactly as it does for a completed run - and this handler set the
    bar full unconditionally, next to a headline saying the run was stopped. The
    same fault was found and fixed in `_on_failed` and not here.
    """
    view = _view()
    view._stopping = True

    view._on_finished(_Finished())

    assert view.bar.value() == 0, "a stopped run reported itself complete"


def test_a_governor_abort_does_not_finish_at_a_hundred_percent():
    """Out of disk is not "finished" either, and takes the same path."""
    view = _view()

    class Aborted(_Finished):
        stopped_early = object()

    view._on_finished(Aborted())

    assert view.bar.value() == 0


def test_a_run_that_reached_the_end_is_full():
    """The case that must keep working."""
    view = _view()

    view._on_finished(_Finished())

    assert view.bar.value() == view.bar.maximum()


def test_the_busy_bar_is_given_a_chunk_width_so_it_animates():
    r"""**Indeterminate mode looked like a frozen full bar.**

    `setRange(0, 0)` is what an index shows for most of its length, because the
    size of the job is unknown until the walk ends. Under `QStyleSheetStyle` a
    styled `::chunk` with no width paints across the whole groove and does not
    move, so "we do not know yet" was indistinguishable from "finished, stuck".
    """
    from app.ui.theme import stylesheet

    sheet = stylesheet("dark")
    chunk = sheet.split("QProgressBar::chunk")[1].split("}")[0]

    assert "width:" in chunk, "a chunk with no width does not animate in busy mode"


def test_the_scan_button_exists_and_says_what_it_costs():
    """The bar cannot show a percentage without a total, and only a scan makes one."""
    view = _view()

    assert view.scan_button.isEnabled()
    tip = view.scan_button.toolTip().lower()
    assert "percentage" in tip
    assert "contents" in tip, "say that it reads no file contents"
