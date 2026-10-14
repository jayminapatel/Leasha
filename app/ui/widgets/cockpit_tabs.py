r"""The Indexing cockpit's bottom panel: the run's log, the system log, and the lists.

Layer: L5

**One panel of a fixed place, with tabs**, where the run log, the skipped
files, the archived folders, the timed-out files, the notices and "This index"
used to be stacked down two columns - and each one appearing or growing pushed
everything under it down the page (the owner, 2026-10-14: *"the sizes of boxes
change on content etc it is disturbing for the eye"*). Now a list that grows
scrolls inside its tab, an empty one says so in place, and the page never
moves. The tabs carry their counts ("Skipped (12)").

**The system log is on the page** (*"i want a option to add the system log so
it is accessible from that page too so you dont have to switch to settings"*):
the same `DebugPane` Settings shows, poppable into its own window. It is fed
from the application log *file* (`core.log_tail.LogTail`), not the window's
in-memory ring, because an index run in its own process logs there and never
into this window. The file is read on a worker (`LogTailFeed`), only while the
tab is on screen - the pane's own timer runs only then.

The widgets themselves are the view's (`view.run_log`, `view.skips`,
`view.stats_box`, `view.notices`, `view.archives`, `view.timed_out`,
`view.totals`); this file only gives each a tab. The funnel's counts stay at
the top of the page (the owner, 2026-10-14: "keep the counts on top").
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject, QThreadPool, Slot
from PySide6.QtWidgets import (
    QFrame, QLabel, QScrollArea, QSizePolicy, QTabWidget, QVBoxLayout, QWidget,
)

from app.core.log_tail import LogTail
from app.ui.presenter.cockpit import HELD_SENTENCE, held_summary, tab_title
from app.ui.theme import SPACE_S, theme_colours
from app.ui.widgets.debug_pane import DebugPane
from app.ui.widgets.icons import icon
from app.ui.workers import CallableWorker, run

__all__ = ["CockpitTabs", "LogTailFeed", "TABS", "TAB_TIPS"]

#: The tabs, in order: `(name, Lucide glyph)`. New words, 2026-10-14.
TABS: tuple[tuple[str, str], ...] = (
    ("Run log", "list"),
    ("System log", "file-text"),
    ("Skipped", "ban"),
    ("Held for later", "hourglass"),
    ("This index", "database"),
    ("Notices", "bell"),
)

#: What each tab holds, for its tooltip - in the order of `TABS`.
TAB_TIPS: tuple[str, ...] = (
    "What this index run has done so far, step by step, with the time of each.",
    "Every line the window and the indexing process write to today's log file, "
    "newest at the bottom.",
    "Files this run could not read, grouped by reason, each with what fixes it.",
    "Files saved for a later, slower pass - pictures and recordings - and how many "
    "are waiting.",
    "What the index holds now: files by status, documents, passages and where it "
    "lives.",
    "Things worth knowing that are not failures, folders not walked this time, and "
    "files that ran out of time.",
)
RUN_LOG, SYSTEM_LOG, SKIPPED, HELD, THIS_INDEX, NOTICES = range(len(TABS))


class LogTailFeed(QObject):
    """The System log pane's source: the log file's new lines, read off the UI thread.

    `lines(limit)` is what `DebugPane` calls once a second while it is on
    screen. It returns what was already read - a copy under a lock, no I/O - and
    asks a worker for the next piece, unless one is still out. So the file is
    only ever read while somebody is looking at the tab.
    """

    def __init__(self, tail: Optional[LogTail] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.tail = tail or LogTail()
        self._busy = False

    def lines(self, limit: int) -> list[str]:
        self.request()
        return self.tail.lines(limit)

    def request(self) -> bool:
        """Ask a worker to read what was appended. False while one is still out."""
        if self._busy:
            return False
        self._busy = True
        worker = CallableWorker(self.tail.poll, component="ui.index.system_log")
        worker.signals.done.connect(self._done)
        run(QThreadPool.globalInstance(), worker)
        return True

    @Slot()
    def _done(self) -> None:
        self._busy = False


def _scrolled(*widgets: QWidget, placeholder: Optional[QLabel] = None) -> QScrollArea:
    """`widgets` stacked top to bottom in a frameless scroll area, spare room below."""
    inner = QWidget()
    layout = QVBoxLayout(inner)
    layout.setContentsMargins(SPACE_S, SPACE_S, SPACE_S, SPACE_S)
    layout.setSpacing(SPACE_S)
    if placeholder is not None:
        layout.addWidget(placeholder)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    area = QScrollArea()
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidgetResizable(True)
    area.setWidget(inner)
    return area


def _quiet(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("settingsHint")
    label.setWordWrap(True)
    return label


class CockpitTabs(QTabWidget):
    """Run log, System log, Skipped, Held for later, This index, Notices."""

    def __init__(self, view: Any, *, feed: Optional[LogTailFeed] = None) -> None:
        super().__init__()
        self.setObjectName("cockpitTabs")
        self.setDocumentMode(False)
        self.setUsesScrollButtons(True)

        # Run log: the box always shows (its placeholder says the run has not
        # said anything yet), and takes the tab's whole height.
        run_log = view.run_log
        run_log.view.setMinimumHeight(0)
        run_log.view.setMaximumHeight(16777215)
        run_log.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(SPACE_S, SPACE_S, SPACE_S, SPACE_S)
        box.addWidget(run_log)
        # Shown only once it has a parent: shown before, it would open as a
        # window of its own (`test_window_opens`).
        run_log.setVisible(True)
        self.addTab(page, TABS[RUN_LOG][0])

        # System log: the Settings pane, fed from the log file.
        self.feed = feed or LogTailFeed(parent=self)
        view.system_log = DebugPane(source=self.feed.lines)
        view.system_log.view.setMinimumHeight(0)
        self.addTab(view.system_log, TABS[SYSTEM_LOG][0])

        # Skipped: the panel scrolls itself; an empty one says so in place.
        self.no_skips = _quiet("Nothing has been skipped.")
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(SPACE_S, SPACE_S, SPACE_S, SPACE_S)
        box.addWidget(self.no_skips)
        box.addWidget(view.skips, 1)
        self.addTab(page, TABS[SKIPPED][0])

        # Held for later: what the funnel calls Deferred, with its sentence.
        self.held_sentence = _quiet(HELD_SENTENCE)
        self.held_lines = QLabel("")
        self.held_lines.setObjectName("cockpitHeld")
        self.held_lines.setWordWrap(True)
        self.addTab(_scrolled(self.held_sentence, self.held_lines), TABS[HELD][0])

        # This index: the summary, scrolled, so a long summary never asks the
        # page for height. (The funnel's counts are at the top of the page.)
        self.addTab(_scrolled(view.totals, view.stats_box), TABS[THIS_INDEX][0])

        # Notices, archived folders, timed-out files: each still shows and
        # hides itself, now inside a tab that never changes size.
        self.no_notices = _quiet("Nothing to report.")
        self.addTab(_scrolled(view.notices, view.archives, view.timed_out,
                              placeholder=self.no_notices), TABS[NOTICES][0])
        for index, tip in enumerate(TAB_TIPS):
            self.setTabToolTip(index, tip)
        self.retint()

    # -- counts -----------------------------------------------------------

    def refresh(self, view: Any) -> None:
        """Retitle the tabs with their counts and show each empty state. No I/O."""
        skipped = sum(int(n or 0) for n in getattr(view.skips, "_shown", {}).values())
        self.setTabText(SKIPPED, tab_title(TABS[SKIPPED][0], skipped))
        self.no_skips.setVisible(view.skips.isHidden())

        funnel = getattr(view, "funnel", None)
        counts = getattr(funnel, "counts", None) or None
        held, lines = held_summary(getattr(view, "_run_skips", None), counts)
        self.setTabText(HELD, tab_title(TABS[HELD][0], held))
        text = "\n".join(lines)
        if self.held_lines.text() != text:
            self.held_lines.setText(text)

        notices = [line for line in view.notices.text().split("\n") if line.strip()]
        shown = not view.notices.isHidden()
        self.setTabText(NOTICES, tab_title(TABS[NOTICES][0], len(notices) if shown else 0))
        self.no_notices.setVisible(
            view.notices.isHidden() and view.archives.isHidden() and view.timed_out.isHidden())

    # -- the theme --------------------------------------------------------

    def changeEvent(self, event: Any) -> None:       # noqa: N802 - Qt's name
        super().changeEvent(event)
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            self.retint()

    def retint(self) -> None:
        """Draw the tab icons in the current theme."""
        colour = theme_colours().get("text_dim", "")
        for index, (_name, glyph) in enumerate(TABS):
            self.setTabIcon(index, icon(glyph, colour))
