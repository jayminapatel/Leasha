r"""The row of buttons under the Indexing page's log: Start, Scan first, Stop,
Pause and Reset - and the rules for which of them can be pressed when.

Layer: L5

Work order 0x §4a. **Why this is its own file.** `indexing_view.py` was 299
code lines against the 250-line guard in `test_presenter.py`, because the Pause
button (2026-09-20) added its label, its long tooltip and three handlers to a
file that had already been split once. The guard is right about what it is for
- a view that keeps growing is where logic starts to live - so the row moved
here, the same way `settings_shelves.py` and `results_items.py` were carved out
of their views. **The guard was not raised.**

What stayed behind in the view, on purpose:

* `start()` and `stop()` themselves. `test_pages_reorg.py` checks that four
  sentences - "Indexing…", "Stopping after the current file…", "Everything
  indexed so far is kept." and "Nothing indexed yet." - are still written *in
  `indexing_view.py`*, word for word, because that is the file they were in
  before the reorganisation. Those sentences are said by `start()` and `stop()`,
  so the two methods stay and only their button bookkeeping comes here.
* `refresh_totals` and the `signals.progress.connect` line, which
  `test_ui_never_blocks.py` reads in the view to prove that the long jobs start
  a worker and that progress arrives through a signal.

So the view keeps every sentence a test pins to it; this file keeps the
buttons, their tooltips and their on/off rules. Nothing was reworded on the way:
the four older buttons are still built by `index_controls.build_controls`,
unchanged, and the Pause button's label and tooltip below are the same
characters they were in the view.

**One row, one widget.** The row used to be a bare `QHBoxLayout` handed to the
page layout. It is now a `QWidget` holding that layout, so the page can place
it with `addWidget` and a test can ask the row for its buttons. The buttons
keep their old names on the view (`view.start_button`, `view.pause_button`
and so on), because `shell.py`, the controllers and the tests all reach for
those names.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtWidgets import QWidget

from app.ui.widgets.index_controls import build_controls
from app.ui.widgets.indexing_headline import show_now

__all__ = [
    "IndexControls",
    "hold_run",
    "let_run_go",
    "PAUSE_LABEL",
    "RESUME_LABEL",
    "PAUSED_HEADLINE",
    "PAUSED_DETAIL",
]

#: 2026-09-20. The two words on the one button. It is one control rather than
#: two because a run is either held or it is not, and a Resume sitting greyed
#: out beside a live Pause is a second thing to read for no extra answer.
PAUSE_LABEL = "Pause"
RESUME_LABEL = "Resume"

#: What a run the PERSON paused says, as against one the machine paused -
#: which the presenter already words as "Paused - waiting for the machine".
#: Two different situations: one of them ends when they press Resume, and the
#: other ends by itself. A single "Paused" for both leaves somebody waiting
#: for a run that is waiting for them.
PAUSED_HEADLINE = "Paused"
PAUSED_DETAIL = ("Held at your request. Nothing is lost - press Resume and it "
                 "carries on from here.")


class IndexControls(QWidget):
    """The five buttons in one row, and which of them are live right now.

    The class never decides *what* a click means - Start is wired by the
    window, Stop, Scan and Reset call back into the view, and Pause calls
    `on_pause`. It only owns the buttons and their enabled/label state, so the
    four places that used to flip buttons by hand (start, stop, pause, and the
    end of a run) now each make one call here and cannot disagree.
    """

    def __init__(self, *, on_stop: Any, on_scan: Any, on_reset: Any,
                 on_pause: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # All five buttons and their row come from `build_controls`, in the
        # order a run uses them: Start, Pause, Stop, Scan, Reset (see that
        # module for why). Pause is built there too, with the same label and
        # tooltip it had here.
        (self.start_button, self.pause_button, self.stop_button,
         self.scan_button, self.reset_button, row) = build_controls(
            on_stop=on_stop, on_pause=on_pause, on_scan=on_scan,
            on_reset=on_reset)

        # No margins of its own: the row sat directly in the page's layout
        # before, and the page's spacing is what should decide the gaps.
        row.setContentsMargins(0, 0, 0, 0)
        self.setLayout(row)

        # **Tab goes left to right, the way the row reads.** Qt's default tab
        # order is the order widgets were made in, and Pause is made after
        # Reset (it joined the row later), so Tab used to jump from Stop to
        # the far-right Reset and back to Pause. Keyboard users get the order
        # their eyes see.
        order = (self.start_button, self.pause_button, self.stop_button,
                 self.scan_button, self.reset_button)
        for first, second in zip(order, order[1:]):
            QWidget.setTabOrder(first, second)
        #: The buttons left to right, as the row reads. **The one place this
        #: order is written down**: the page's layout reads it from here when
        #: it slots the run log in before the row. It used to keep its own
        #: copy, and when the row was reordered (Start, Pause, Stop - 3ddb128)
        #: the copy still said Start, Scan, Stop, Pause and overrode this.
        self.in_order = order

        #: Whether *this person* has the run held. Not the same question as
        #: `stats.paused`, which is true for the governor's pause as well.
        self.paused = False

    # -- which buttons are live -------------------------------------------

    def show_running(self) -> None:
        """A run has just started: Stop and Pause live, Start not."""
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.paused = False
        self.pause_button.setText(PAUSE_LABEL)
        self.pause_button.setEnabled(True)

    def show_stopping(self) -> None:
        """Stop was pressed: nothing useful to click until the run ends.

        **Stop beats Pause, from a held run as much as from a running one.**
        `Pipeline.request_stop` lets go of the pause itself, so this only has
        to stop offering a Resume that would now mean nothing.
        """
        self.stop_button.setEnabled(False)
        self.paused = False
        self.pause_button.setEnabled(False)
        self.pause_button.setText(PAUSE_LABEL)

    def show_idle(self) -> None:
        """No run: Start is live again, Stop and Pause are not."""
        self.paused = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.pause_button.setText(PAUSE_LABEL)

    def show_held(self, held: bool) -> None:
        """Pause was pressed (`held`) or Resume was: flip the one button's word."""
        self.paused = bool(held)
        self.pause_button.setText(RESUME_LABEL if held else PAUSE_LABEL)


# -- what pressing Pause and Resume does -------------------------------------
#
# Functions that take the view, in the style of `indexing_layout.py`: they set
# the same attributes the view's own methods always set (`view.headline`,
# `view.detail`), so nothing that reads those changes. The view keeps
# `pause_run` and `resume_run` as one-line methods because the window and the
# tests call them by those names.


def hold_run(view: Any) -> None:
    """Hold the run where it is. Nothing is ended and nothing is lost."""
    pipeline = getattr(view._worker, "pipeline", None)
    if pipeline is None or view._stopping:
        return
    pipeline.pause()
    view.controls.show_held(True)
    # Said now rather than at the next progress tick: the run may already
    # be between files, and a button that appears to do nothing for two
    # seconds is a button pressed twice.
    view.headline.setText(PAUSED_HEADLINE)
    view.detail.setText(PAUSED_DETAIL)
    show_now(view, None)


def let_run_go(view: Any) -> None:
    """Let the run carry on. The computer's own pauses still apply."""
    pipeline = getattr(view._worker, "pipeline", None)
    view.controls.show_held(False)
    if pipeline is None:
        return
    pipeline.resume()
    # **Not "Indexing…" - that would be a claim.** The machine may still
    # be busy, and the governor may keep the run waiting for its own
    # reasons; the next progress tick says which, truthfully.
    view.headline.setText("Carrying on…")
