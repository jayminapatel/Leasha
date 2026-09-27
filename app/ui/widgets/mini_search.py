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

**Live counts per kind, and Tab cycles them.** Adoptions §5a: beneath the
list, "7 files · 2 emails" - read from the one response already in hand, so
cycling with Tab re-filters instantly and calls the engine exactly as many
times as a keystroke does, which is zero extra.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QEvent, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout, QWidget,
)

from app.core.logging import logger

__all__ = [
    "MiniSearch", "ROWS", "DEBOUNCE_MS", "row_label", "CHIP_ORDER",
    "kind_bucket", "chip_label",
]

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

#: **Three plain words, not one chip per extension.** Adoptions §5a: "14 pdf ·
#: 3 docx · 2 xlsx" is a catalogue; "files · mail · code" is the shape of the
#: question this box exists to answer fast. Fixed order, so the chips do not
#: reshuffle between one keystroke and the next.
CHIP_ORDER = ("files", "mail", "code")

#: `bucket -> (singular, plural)`, for `chip_label`.
_CHIP_WORDS = {
    "files": ("file", "files"),
    "mail": ("email", "emails"),
    "code": ("code result", "code results"),
}


def kind_bucket(kind: str) -> str:
    r"""A `ResultGroup.kind` - `"email"`, or a file extension - as one of
    `CHIP_ORDER`. Adoptions §5a.

    **Read from `_EXT_GROUPS["code"]`, the parser's own table**, the same
    table `/type code` already answers from - a second list of code
    extensions here would be the drift this project keeps finding and fixing
    elsewhere.
    """
    from app.search.query import _EXT_GROUPS

    if kind == "email":
        return "mail"
    if kind in _EXT_GROUPS.get("code", ()):
        return "code"
    return "files"


def chip_label(bucket: str, count: int) -> str:
    """`"7 files"`, `"1 email"` - plain words, singular where it matters."""
    singular, plural = _CHIP_WORDS.get(bucket, (bucket, bucket))
    return f"{count} {singular if count == 1 else plural}"


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
        #: Every group the last response produced, before the chip filter -
        #: what "the result set already in hand" means for §5a. `_rows` is
        #: the filtered, display-capped view of this.
        self._all_groups: list = []
        #: `bucket -> count`, over `_all_groups`. Empty when there is nothing
        #: to show a chip for.
        self._chip_counts: dict = {}
        #: The buckets with a chip showing, in `CHIP_ORDER`. Parallel to the
        #: buttons in `self._chip_buttons`.
        self._chip_kinds: list = []
        #: `None` for "all", or one of `_chip_kinds`.
        self._active_chip: Optional[str] = None

        self.setObjectName("miniSearch")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.resize(WIDTH, HEIGHT)

        self.box = QLineEdit()
        self.box.setPlaceholderText("Search everything — Enter opens it")
        self.box.setAccessibleName("Search")
        self.box.textEdited.connect(self._typed)
        self.box.returnPressed.connect(self._take)
        # **Tab cycles the chips instead of leaving the box.** A plain
        # QLineEdit hands Tab to Qt's focus-next-widget machinery before
        # `keyPressEvent` ever sees it, so intercepting it here - on the box
        # itself, via an event filter - is the one place that works.
        self.box.installEventFilter(self)
        # **The same `/` menu as every other box** - order "dates" §1c asks
        # for `/date` here, and this box had no menu at all, so `/after`
        # was typed blind and then searched for as the word "after". The
        # index's filters only: this box runs one engine search and never
        # the repository half, so the git switches would be rows that do
        # nothing. See `_take` for the one thing the menu changes here.
        from app.ui.widgets.command_popup import attach_to

        self._popup = attach_to(self.box, store=getattr(engine, "store", None))

        self.list = QListWidget()
        self.list.setAccessibleName("Results")
        self.list.itemActivated.connect(lambda _item: self._take())
        self.list.itemClicked.connect(lambda _item: self._take())

        #: §5a's chip row. Built fresh on every response, since which kinds
        #: appear - and whether any chip is worth showing at all - changes
        #: with the query.
        self.chips = QWidget()
        self.chips.setObjectName("miniChips")
        self._chips_layout = QHBoxLayout(self.chips)
        self._chips_layout.setContentsMargins(0, 4, 0, 0)
        self._chip_buttons: list = []
        self.chips.hide()

        layout = QVBoxLayout(self)
        layout.addWidget(self.box)
        layout.addWidget(self.list, stretch=1)
        layout.addWidget(self.chips)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._search)

    # -- appearing and disappearing -------------------------------------------

    def summon(self, prefill: str = "") -> None:
        """Show it, centred on the active screen, ready to type.

        **Centred rather than remembered.** A box that appears where it was
        last time is a box somebody has to look for; one that is always in the
        middle of the screen they are using is one they can aim at without
        thinking. This is the opposite decision from the pinned windows, and
        deliberately: those are furniture, this is a prompt.

        **`prefill`, selected rather than merely present.** Adoptions §4a: a
        text selection in the foreground application arrives here already
        read, and is never searched by itself - it only replaces the "type
        your query" step with "one keystroke replaces what's highlighted",
        which is what makes it safe to always show even when it is wrong.
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
        self._reset_chips()
        self.show()
        self.raise_()
        self.activateWindow()
        self.box.setFocus()
        if prefill:
            self.box.setText(prefill)
            self.box.selectAll()

    def offer_prefill(self, text: str) -> None:
        r"""A selection read *after* the box was already shown. Adoptions §4a.

        **Reading it must never delay opening the box**, so `_summon_mini`
        shows the box first and hands the read to a worker - this is where
        the answer lands, a beat later, and where "too late" is decided:

        * the box was dismissed in the meantime - `isVisible()` is False;
        * or somebody already started typing - `self.box.text()` is not
          empty, and arriving text must never overwrite a real keystroke.

        Either one loses to what is actually true on screen, silently: this
        is an offer, not a command.
        """
        if not text or not self.isVisible() or self.box.text():
            return
        self.box.setText(text)
        self.box.selectAll()

    def dismiss(self) -> None:
        """Gone, and holding nothing. Escape, focus loss, or a chosen result."""
        self._timer.stop()
        self._generation += 1                    # anything in flight is stale
        self._popup.popup().hide()               # a menu outliving its box
        self.hide()
        self.box.clear()
        self.list.clear()
        self._rows = []
        self._reset_chips()

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

    def eventFilter(self, obj: Any, event: Any) -> bool:     # noqa: N802 - Qt's name
        r"""Tab, caught before Qt spends it on focus-next. Adoptions §5a.

        **On the box, not on this frame.** A `QLineEdit` answers a Tab key
        press itself - `QWidget`'s own focus-traversal handling, underneath
        anything `keyPressEvent` could intercept - so the box is the only
        place this event can be caught rather than merely observed.
        """
        if obj is self.box and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Tab:
                self._cycle_chip()
                return True
        return super().eventFilter(obj, event)

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
        from app.ui.workers import CallableWorker, run

        # `/date 2017` becomes `date:2017` here, as it does in the Search tab
        # and the CLI - the parser has never known about slashes.
        from app.search.commands import expand_slashes

        query = expand_slashes(self.box.text().strip())
        if not query or self._engine is None:
            self.list.clear()
            self._rows = []
            self._reset_chips()
            return

        self._generation += 1
        generation = self._generation
        engine = self._engine

        def ask() -> Any:
            from app.search.policy import SEARCH, for_surface
            from app.ui.presenter import fetch_depth, mail_details

            # **The Search tab's policy, by name.** §3a is explicit: the
            # kid-safe surface. Somebody who summoned this from inside Excel
            # is the least likely person to want to debug a query.
            #
            # **Fetched deeper than it is shown.** §5a's chips need to count
            # more than the seven rows on screen, or "14 files" is never
            # true of anything this box could display - `fetch_depth` is the
            # same "fetch deeper than the display" rule grouping already
            # uses, not a second one invented here.
            response = engine.search(query, limit=fetch_depth(ROWS),
                                     policy=for_surface(SEARCH))
            # **One more query, batched, on the same worker.** The rule
            # `mail_details` states: never one lookup per row. Without it a
            # message result has no subject and buckets as "files" for want
            # of a kind - this is the one call that fixes both.
            details = mail_details(getattr(engine, "store", None),
                                   getattr(response, "results", []) or [])
            return response, details

        worker = CallableWorker(ask, component="ui.mini.search")
        worker.signals.finished.connect(
            lambda payload, g=generation: self._show(payload, g))
        worker.signals.failed.connect(
            lambda error, g=generation: self._failed(error, g))
        run(QThreadPool.globalInstance(), worker)

    def _show(self, payload: Any, generation: int) -> None:
        if generation != self._generation:
            return                               # a later keystroke won
        from app.ui.presenter import group_results, to_rows

        try:
            response, details = payload
            terms = tuple(getattr(getattr(response, "parsed", None),
                                  "terms", ()) or ())
            groups = group_results(to_rows(
                getattr(response, "results", []) or [], terms), details=details)
        except Exception as exc:                 # noqa: BLE001 - a list
            _log.debug("could not draw the mini results: {}", exc)
            return

        self._all_groups = list(groups)
        self._active_chip = None
        self._rebuild_chips()
        self._apply_chip_filter()

    def _failed(self, error: Any, generation: int) -> None:
        if generation != self._generation:
            return
        _log.debug("mini search failed: {}", error)
        self.list.clear()
        self._rows = []
        self._reset_chips()

    # -- §5a: live counts per kind ----------------------------------------------

    def _reset_chips(self) -> None:
        """Nothing to count and nothing to show. Summon, dismiss, a failure."""
        self._all_groups = []
        self._chip_counts = {}
        self._active_chip = None
        self._rebuild_chips()

    def _apply_chip_filter(self) -> None:
        """The displayed rows: `_all_groups`, filtered and capped. No engine
        call - this only ever redraws what a response already delivered."""
        groups = self._all_groups
        if self._active_chip is not None:
            groups = [group for group in groups
                     if kind_bucket(group.kind) == self._active_chip]
        self._rows = groups[:ROWS]
        self.list.clear()
        for group in self._rows:
            self.list.addItem(QListWidgetItem(row_label(group)))
        if self._rows:
            self.list.setCurrentRow(0)

    def _rebuild_chips(self) -> None:
        """One chip per kind present, counted from `_all_groups`.

        **Rebuilt rather than relabelled.** Which kinds are present changes
        with every query - a search all-code has no "mail" chip to update,
        it has none to show at all - so the row is thrown away and remade
        rather than carrying stale buttons from the last query forward.
        """
        counts: dict = {}
        for group in self._all_groups:
            bucket = kind_bucket(group.kind)
            counts[bucket] = counts.get(bucket, 0) + 1
        self._chip_counts = counts

        while self._chips_layout.count():
            item = self._chips_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._chip_buttons = []

        present = [bucket for bucket in CHIP_ORDER if counts.get(bucket)]
        # **Nothing to distinguish, nothing to show.** One chip repeating the
        # same count the list above it already shows is noise, not an
        # answer - the whole reason a chip exists is a choice between kinds,
        # and one kind is not a choice.
        if len(present) < 2:
            present = []
        self._chip_kinds = present
        self.chips.setVisible(bool(present))
        for bucket in present:
            button = QPushButton(chip_label(bucket, counts[bucket]))
            button.setObjectName(f"miniChip_{bucket}")
            # Found by 0s's own plain-words/tooltip guard
            # (test_tooltips.py::test_every_control_explains_itself): the
            # chip's label already says the count and the kind, but not
            # what clicking it *does* - narrow the list to only that kind,
            # which is the one fact the label alone doesn't carry.
            button.setToolTip("Show only these results")
            button.setCheckable(True)
            button.setFlat(True)
            button.setChecked(bucket == self._active_chip)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(
                lambda _checked=False, k=bucket: self._select_chip(k))
            self._chips_layout.addWidget(button)
            self._chip_buttons.append(button)

    def _select_chip(self, bucket: Optional[str]) -> None:
        """Show only that kind - a scope filter over the rows already in
        hand, never a re-search."""
        self._active_chip = bucket
        for button, kind in zip(self._chip_buttons, self._chip_kinds):
            button.setChecked(kind == bucket)
        self._apply_chip_filter()

    def _cycle_chip(self) -> None:
        """Tab steps through the chips and back to "all". Adoptions §5a."""
        if not self._chip_kinds:
            return
        order = [None, *self._chip_kinds]
        try:
            index = order.index(self._active_chip)
        except ValueError:
            index = 0
        self._select_chip(order[(index + 1) % len(order)])

    # -- choosing -------------------------------------------------------------

    def _take(self) -> None:
        r"""Open what is highlighted, and disappear.

        **With nothing highlighted, hand the query to the main window.** That
        is the way out of a box too small for the question being asked, and it
        is better than doing nothing: somebody who pressed Enter meant
        something to happen.

        **Not while the `/` menu is open.** Enter on a menu row reaches the
        box as `returnPressed` *before* the row is inserted - checked with
        `QTest` against a real `QCompleter` - so choosing `/date` with Enter
        opened whatever result was highlighted and closed the box on the
        person mid-sentence. The menu has already taken that Enter.
        """
        if self._popup.popup().isVisible():
            return
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
