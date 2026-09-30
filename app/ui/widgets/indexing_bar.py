r"""The Indexing page's progress bar: it glides to each new value instead of
jumping, and it is a busy bar while nobody knows the total.

Layer: L5

Work order 0x §4c. **Why glide at all.** The page repaints at most four times a
second (`indexing_view.PROGRESS_PAINT_MIN_S`, 0.25 s), and a checkpoint can
land a few hundred files at once, so the bar used to move in visible hops -
still, then a jump, then still. A bar that moves smoothly reads as "working";
one that hops reads as "stuck, then caught up". This fills the gap between two
paints with a short slide, and nothing more: the numbers it slides between are
exactly the ones `presenter.progress_for` gave, so it never shows progress that
has not happened.

**The rules, in the order they are checked** (`glide_to`):

1. **No total yet -> a busy bar.** `total == 0` is Qt's indeterminate range, the
   moving block that says "working, size unknown". Order 0w already made that
   block actually move under the stylesheet (see the `QProgressBar::chunk`
   comment in `theme.py`) - that is kept exactly as it was, and Qt's own style
   animation drives it, not this file.
   *2026-09-30: no longer Qt's block - `ShimmerBar`, which this class now
   extends, paints and animates the busy segment itself.*
2. **The total changed -> jump, don't glide.** A new total means the old
   position means something else now (a busy bar became a counted one, or a
   phase ended). Sliding from the old number would draw a fraction that was
   never true, so the bar is set straight to the new value.
3. **Never backwards while the total stays the same.** The count of files
   finished only grows, so a smaller number with the same total can only be a
   stale tick arriving late. The bar holds where it is rather than twitching
   back.
4. **Otherwise, slide.** From where the bar is drawn now to the new value, over
   `GLIDE_S`, easing out (fast at first, gentle at the end), in steps of
   `FRAME_MS`.

**What it costs, and why that is within the 0.25 s paint throttle's intent.**
The throttle exists so that a fast run does not rebuild the skips panel, the
archived-folder list and the notices on the UI thread many times a second.
This touches none of that: each step is at most one `setValue` on one bar,
skipped when it would not move the bar by a pixel. The slide lasts one paint
interval, so there are at most `GLIDE_S / FRAME_MS` (five) steps per paint,
and the timer is **stopped** in between - an idle page runs no timer at all. Measured offscreen over a simulated 30 s run:
see `tests/unit/test_indexing_page_4.py::test_the_glide_costs_next_to_nothing`
and the 0x §4c note in the report for the figures.

**It stops when nobody can see it.** When the page is switched away from, the
Status shelf is swapped out, or the window is minimised, Qt sends the bar a hide
event; the slide is finished at once (the bar jumps to where it was heading) and
the timer stops. Each step also checks, in case a platform does not send one.

**Any other caller still gets a plain `QProgressBar`.** `setValue` and
`setRange` called from anywhere else - the start of a run, the end of one, a
failure, a run from the command line being drawn - stop any slide and set the
bar exactly, as they always did, so a test or a caller that sets 42 reads 42.
"""

from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QWidget

from app.ui.widgets.shimmer_bar import ShimmerBar

__all__ = ["GlidingBar", "GLIDE_S", "FRAME_MS"]

#: How long one slide takes. One paint interval (0.25 s): the next tick arrives
#: about as the slide ends, so a steady run looks like continuous movement and
#: never like a queue of slides. Shorter looks like the old hop; longer means
#: the bar is still catching up when the next number lands.
GLIDE_S = 0.25

#: Time between two steps of a slide: 20 steps a second, so five per slide.
#: Enough for the eye to read the bar as moving, not hopping - it is a bar
#: creeping forward, not a game - and a third of what a 60 Hz frame clock would
#: cost. First tried at 36 ms; the 30 s measurement (see the report for 0x §4c)
#: showed the steps, not the painting, were most of the cost, so fewer of them.
FRAME_MS = 50


class GlidingBar(ShimmerBar):
    """A `QProgressBar` with one extra method, `glide_to(value, total)`.

    2026-09-30: painted as a `ShimmerBar` (gradient fill, a sweep while a run
    is going, a gliding segment while busy) - see `widgets/shimmer_bar.py`."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # One timer, made once, running only while a slide is under way.
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._step)
        #: Where the current slide started, where it is going, and when it began.
        self._from = 0
        self._to = 0
        self._began = 0.0
        #: How many timer steps have run since the bar was made. Read by the
        #: cost test; costs one addition per step.
        self.steps = 0
        #: The pixel column the filled part last ended at, so a step that would
        #: not move it by a pixel skips `setValue` (and its repaint) entirely.
        self._last_column = -1

    # -- what the page calls ----------------------------------------------

    def glide_to(self, value: int, total: int) -> None:
        """Move towards `value` out of `total`; see the module docstring's rules."""
        value, total = int(value or 0), max(0, int(total or 0))
        if total == 0:
            # Rule 1: a busy bar. Qt animates it itself.
            self.setRange(0, 0)
            return
        if (self.minimum(), self.maximum()) != (0, total):
            # Rule 2: a new total - straight there.
            self.setRange(0, total)
            self.setValue(min(value, total))
            return
        target = max(min(value, total), self._to)      # rule 3: never back
        if target == self._to:
            return            # already there, or already sliding there
        if not self._can_animate():
            self.setValue(target)
            return
        # Rule 4: slide from where the bar is *drawn* now, so a new number
        # arriving mid-slide carries on smoothly rather than restarting.
        self._from = super().value()
        self._to = target
        self._began = time.monotonic()
        if not self._timer.isActive():
            self._timer.start()

    def gliding(self) -> bool:
        """Is a slide under way? For tests and for the cost measurement."""
        return self._timer.isActive()

    # -- plain QProgressBar calls from anywhere else ----------------------

    def setValue(self, value: int) -> None:  # noqa: N802 - Qt's name
        """Set the bar exactly, stopping any slide. Every caller but
        `glide_to` means "show this number now"."""
        self._timer.stop()
        self._to = int(value)
        super().setValue(int(value))

    def setRange(self, minimum: int, maximum: int) -> None:  # noqa: N802
        """Change the range, stopping any slide: its numbers belonged to the
        old range."""
        self._timer.stop()
        super().setRange(int(minimum), int(maximum))
        # Qt clamps the value into the new range; the slide's target follows.
        self._to = super().value() if super().value() >= 0 else 0

    # -- the slide itself -------------------------------------------------

    def _can_animate(self) -> bool:
        """Only while somebody could be looking at it."""
        return self.isVisible() and not self.window().isMinimized()

    def _finish(self) -> None:
        """End the slide where it was heading."""
        self._timer.stop()
        self._last_column = -1
        super().setValue(self._to)

    def _step(self) -> None:
        """One step of the slide. UI thread; one `setValue`, nothing else."""
        self.steps += 1
        if not self._can_animate():
            self._finish()
            return
        done = (time.monotonic() - self._began) / GLIDE_S
        if done >= 1.0:
            self._finish()
            return
        # Ease out: most of the distance early, the last bit gently, so the bar
        # settles rather than stopping dead.
        eased = 1.0 - (1.0 - done) ** 3
        value = int(round(self._from + (self._to - self._from) * eased))
        # **A step nobody could see is not taken.** On a slow run a whole slide
        # may cover two or three pixels; `setValue` still formats the
        # percentage text and asks the style whether to repaint, so skipping
        # the steps that land on the same pixel column is most of the saving.
        span = max(1, self.maximum() - self.minimum())
        column = self.width() * (value - self.minimum()) // span
        if column == self._last_column:
            return
        self._last_column = column
        super().setValue(value)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt's name
        """Page switched away or window minimised: no slide nobody can see."""
        if self._timer.isActive():
            self._finish()
        super().hideEvent(event)
