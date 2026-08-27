r"""The box that appears anywhere. Workspace §3a.

Layer: L5

**This is the feature the product is demonstrated with**, and the order says
to treat its polish accordingly: press a key in any application, type, press
Enter, the document opens and the box is gone. Ten seconds, no window to find.

**It runs the Search tab's policy, not its own.** The kid-safe surface from
the search-experience order — spelling corrected and said so, relaxation on
empty, chips — because somebody who summoned a box from inside Excel is the
*least* likely person to be in the mood to debug a query. A second policy
here would be a second set of answers to the same question.

**It is not a second search engine.** One `SearchEngine`, handed in; this
draws a list and gets out of the way. Everything about ranking, filters and
notices happens where it already happens.

**Escape and losing focus both close it.** A stay-on-top box left behind in
front of somebody's work is worse than no box, and the one thing nobody will
forgive is a window they cannot get rid of.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from app.core.logging import logger

__all__ = ["MiniSearch", "ROWS", "DEBOUNCE_MS", "row_label"]

_log = logger.bind(component="ui.mini")

#: How many results the box shows.
#:
#: **Seven, and it is a deliberate ceiling rather than a screenful.** This is
#: not a results page - it is "the thing I was thinking of, now". A list long
#: enough to scroll is a list somebody reads instead of recognising, and the
#: whole promise is that the answer is already visible.
ROWS = 7

#: Stillness before searching. Shorter than the main box's 400ms: there is no
#: second tier here and no model call, and somebody who summoned a box with a
#: keystroke is in a hurry by definition.
DEBOUNCE_MS = 180

#: The size it opens at. Wide enough for a filename and a folder, short enough
#: that it never covers the document somebody is reading.
WIDTH, HEIGHT = 620, 320


def row_label(row: Any) -> str:
    r"""One line for one result: what it is called, and where it lives.

    Two facts and no more. A snippet here would make each row three lines
    tall and turn a recogniser into a reader - and the person already knows
    what they are looking for, or they would have opened the window.
    """
    name = str(getattr(row, "name", "") or "").strip()
    folder = str(getattr(row, "folder", "") or "").strip()
    path = str(getattr(row, "path", "") or "").strip()
    if not name:
        name = path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path
    return f"{name}   —   {folder}" if folder else name


class MiniSearch(QFrame):
    """A frameless, stay-on-top search box. Summoned, used, gone."""

    #: A result was chosen. The window opens it.
    chosen = pyqtSignal(object)
    #: Somebody asked for the whole application instead. The window comes up
    #: with this query in the box - the way out of a box that is too small
    #: for the question being asked.
    expanded = pyqtSignal(str)

    def __init__(self, engine: Any, parent: Optional[QWidget] = None) -> None:
        # Frameless *and* a Tool window: a Tool has no taskbar entry, which is
        # what makes this feel like a summoned thing rather than a second
        # application somebody now has to close.
        super().__init__(None, Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self._engine = engine
        self._generation = 0
        self._rows: list = []

        self.setObjectName("miniSearch")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.resize(WIDTH, HEIGHT)

        self.box = QLineEdit()
        self.box.setPlaceholderText("Search everything — Enter opens it")
        self.box.setAccessibleName("Search")
        self.box.textEdited.connect(self._typed)
        self.box.returnPressed.connect(self._take)

        self.list = QListWidget()
        self.list.setAccessibleName("Results")
        self.list.itemActivated.connect(lambda _item: self._take())
        self.list.itemClicked.connect(lambda _item: self._take())

        layout = QVBoxLayout(self)
        layout.addWidget(self.box)
        layout.addWidget(self.list, stretch=1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._search)

    # -- appearing and disappearing -------------------------------------------

    def summon(self) -> None:
        """Show it, centred on the active screen, ready to type.

        **Centred rather than remembered.** A box that appears where it was
        last time is a box somebody has to look for; one that is always in the
        middle of the screen they are using is one they can aim at without
        thinking. This is the opposite decision from the pinned windows, and
        deliberately: those are furniture, this is a prompt.
        """
        try:
            from PyQt6.QtGui import QGuiApplication

            screen = (QGuiApplication.screenAt(self.cursor().pos())
                      or QGuiApplication.primaryScreen())
            if screen is not None:
                area = screen.availableGeometry()
                self.move(area.center().x() - self.width() // 2,
                          area.top() + area.height() // 4)
        except Exception:                        # noqa: BLE001 - placement
            pass
        self.box.clear()
        self.list.clear()
        self._rows = []
        self.show()
        self.raise_()
        self.activateWindow()
        self.box.setFocus()

    def dismiss(self) -> None:
        """Gone, and holding nothing. Escape, focus loss, or a chosen result."""
        self._timer.stop()
        self._generation += 1                    # anything in flight is stale
        self.hide()
        self.box.clear()
        self.list.clear()
        self._rows = []

    def keyPressEvent(self, event: Any) -> None:            # noqa: N802 - Qt's name
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.dismiss()
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self._rows:
            # Arrows move the list even while the cursor is in the box, which
            # is what everybody's muscle memory expects of a search prompt.
            row = self.list.currentRow()
            step = 1 if key == Qt.Key.Key_Down else -1
            self.list.setCurrentRow(
                max(0, min(len(self._rows) - 1, row + step)))
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: Any) -> None:            # noqa: N802 - Qt's name
        """Clicking away closes it. See the module docstring."""
        super().focusOutEvent(event)
        if not self.isActiveWindow():
            self.dismiss()

    # -- searching ------------------------------------------------------------

    def _typed(self, _text: str) -> None:
        self._timer.start()

    def _search(self) -> None:
        """One tier, on a worker, carrying a generation. Never raises."""
        from app.ui.presenter import file_rows
        from app.ui.workers import CallableWorker, run

        query = self.box.text().strip()
        if not query or self._engine is None:
            self.list.clear()
            self._rows = []
            return

        self._generation += 1
        generation = self._generation
        engine = self._engine

        def ask() -> Any:
            from app.search.policy import SEARCH, for_surface

            # **The Search tab's policy, by name.** §3a is explicit: the
            # kid-safe surface. Somebody who summoned this from inside Excel
            # is the least likely person to want to debug a query.
            return engine.search(query, limit=ROWS,
                                 policy=for_surface(SEARCH))

        worker = CallableWorker(ask, component="ui.mini.search")
        worker.signals.finished.connect(
            lambda response, g=generation: self._show(response, g))
        worker.signals.failed.connect(
            lambda error, g=generation: self._failed(error, g))
        run(QThreadPool.globalInstance(), worker)

    def _show(self, response: Any, generation: int) -> None:
        if generation != self._generation:
            return                               # a later keystroke won
        from app.ui.presenter import group_results, to_rows

        try:
            terms = tuple(getattr(getattr(response, "parsed", None),
                                  "terms", ()) or ())
            groups = group_results(to_rows(
                getattr(response, "results", []) or [], terms))[:ROWS]
        except Exception as exc:                 # noqa: BLE001 - a list
            _log.debug("could not draw the mini results: {}", exc)
            return

        self._rows = list(groups)
        self.list.clear()
        for group in self._rows:
            self.list.addItem(QListWidgetItem(row_label(group)))
        if self._rows:
            self.list.setCurrentRow(0)

    def _failed(self, error: Any, generation: int) -> None:
        if generation != self._generation:
            return
        _log.debug("mini search failed: {}", error)
        self.list.clear()
        self._rows = []

    # -- choosing -------------------------------------------------------------

    def _take(self) -> None:
        r"""Open what is highlighted, and disappear.

        **With nothing highlighted, hand the query to the main window.** That
        is the way out of a box too small for the question being asked, and it
        is better than doing nothing: somebody who pressed Enter meant
        something to happen.
        """
        row = self.list.currentRow()
        if 0 <= row < len(self._rows):
            chosen = self._rows[row]
            self.dismiss()
            self.chosen.emit(chosen)
            return
        query = self.box.text().strip()
        self.dismiss()
        if query:
            self.expanded.emit(query)
