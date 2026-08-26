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

__all__ = ["build_controls"]


def build_controls(*, on_stop: Any, on_scan: Any, on_reset: Any):
    """Returns `(start, scan, stop, reset, layout)`.

    Start is left unconnected: the window owns what starting means, and wires it
    itself. The other three are self-contained requests.
    """
    start = QPushButton("Start indexing")
    start.setToolTip(
        "Read the folders listed in Settings and add what has changed.\n\n"
        "Safe to stop and safe to repeat: files already indexed are skipped by "
        "date and size, so a second run costs seconds rather than starting "
        "over. Your documents are only ever read.")

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

    # Destructive, so it is placed away from Start and asks before acting.
    reset = QPushButton("Reset index…")
    reset.setToolTip(
        "Delete everything indexed and start over.\n\n"
        "Your documents are never touched - the index is built from them and "
        "can always be rebuilt. What it costs is the time to index again.")
    reset.clicked.connect(lambda _c=False: on_reset())

    row = QHBoxLayout()
    row.addWidget(start)
    row.addWidget(scan)
    row.addWidget(stop)
    row.addStretch(1)
    row.addWidget(reset)
    return start, scan, stop, reset, row
