r"""The four buttons on the Indexing page, and what each of them promises.

Layer: L5

Here rather than in `indexing_view.py` because that file is at the 250-line
guard, and because a tooltip is copy rather than layout - the sentence saying
*what will happen* has more in common with the strings in `errors.py` than with
a box layout.

**Every one of these has a tooltip, and Start had none.** Asked as a presumption
- *"i presume tool tips are used every where to be helpful"* - which measured at
55% of interactive controls. The most important button in the application was in
the missing half, next to a Reset that deletes an index and three radio buttons
elsewhere whose choices are days of re-indexing apart. A label says what a
control *is*; a tooltip says what pressing it will *do*, and those are different
sentences.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import QHBoxLayout, QPushButton

from app.ui.widgets.buttons import ROW_SPACING, style_button

__all__ = ["build_controls"]


def build_controls(*, on_stop: Any, on_pause: Any, on_scan: Any, on_reset: Any):
    """Returns `(start, pause, stop, scan, reset, layout)`.

    **Order is Start, Pause, Stop, then the rest** - the three that act on a
    run *in progress* read left to right as a run would use them, before the
    two that are about a run not yet started (Scan) or the whole index
    (Reset). Start is left unconnected: the window owns what starting means,
    and wires it itself. The other four are self-contained requests.
    """
    start = QPushButton("Start indexing")
    start.setToolTip(
        "Read the folders listed in Settings and add what has changed.\n\n"
        "Safe to stop and safe to repeat: files already indexed are skipped by "
        "date and size, so a second run costs seconds rather than starting "
        "over. Your documents are only ever read.")

    # **Beside Stop, and deliberately not instead of it.** The page said it
    # could "pause and resume an index run" and could not: the only pausing
    # in the application was the resource governor's, which the person does
    # not control. Stop keeps its own meaning - it ends the run, cheaply -
    # and this one holds it without ending anything.
    pause = QPushButton("Pause")
    pause.setEnabled(False)
    pause.setToolTip(
        "Hold the run where it is and give the computer back, without "
        "ending it. Nothing is lost and nothing is redone: it keeps its "
        "place and carries on from there when you press Resume.\n\n"
        "Different from Stop, which ends the run. Different again from "
        "the pausing you may see on this page without pressing anything - "
        "that is the computer standing aside for itself when it is busy, "
        "on battery, or short of space, and it starts again on its own.")
    pause.clicked.connect(lambda _c=False: on_pause())

    # **"Stop", not "Pause".** It said Pause and there is no resume: the run
    # ends, and the next Start begins a new one. It is a cheap end - everything
    # already indexed is kept and nothing is redone - but a button that promises
    # to pause and then stops is a button people stop trusting. The automatic
    # pausing in the status line is the resource governor, which does resume.
    stop = QPushButton("Stop")
    stop.setEnabled(False)
    stop.setToolTip(
        "Stops after the current file. Everything already indexed is kept, and "
        "starting again picks up where this left off rather than redoing it.\n\n"
        "Works on a run started from the command line too.")
    stop.clicked.connect(lambda _c=False: on_stop())

    # **The bar cannot show a percentage without a total, and only a scan makes
    # one.** `app.cli scan` was the sole writer of that number, so a GUI-started
    # run had none and the bar was a busy indicator for its whole length.
    scan = QPushButton("Scan first")
    scan.setToolTip(
        "Count the files before indexing them, so the progress bar can show a "
        "real percentage instead of just spinning.\n\n"
        "Reads no file contents - it walks the folders and adds up sizes - but "
        "on a large corpus that walk still takes a while.")
    scan.clicked.connect(lambda _c=False: on_scan())

    # Destructive, so it is placed away from the run controls and asks before
    # acting - the stretch between Scan and here is that distance.
    reset = QPushButton("Reset index…")
    reset.setToolTip(
        "Delete everything indexed and start over.\n\n"
        "Your documents are never touched - the index is built from them and "
        "can always be rebuilt. What it costs is the time to index again.")
    reset.clicked.connect(lambda _c=False: on_reset())

    # **The button system** (widgets/buttons.py, owner 2026-09-27): Start is
    # the page's one primary action, Reset the one that deletes, the rest
    # secondary - each with its icon, its natural width, one height.
    for button in (start, pause, stop, scan, reset):
        style_button(button)
    row = QHBoxLayout()
    row.setSpacing(ROW_SPACING)
    row.addWidget(start)
    row.addWidget(pause)
    row.addWidget(stop)
    row.addWidget(scan)
    row.addStretch(1)
    row.addWidget(reset)
    return start, pause, stop, scan, reset, row
