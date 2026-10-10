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
front of somebody's work is worse than no box. The first Escape empties the
box; the second closes it (the owner, 2026-10-08).

**Live counts per kind, and Tab cycles them.** Adoptions §5a: beside the
chips, "7 files · 2 emails" - read from the one response already in hand, so
cycling with Tab re-filters instantly and calls the engine exactly as many
times as a keystroke does, which is zero extra.

The owner, 2026-10-08 - *"improve that box behavior and layout to be world
class and modern and feature rich"* - and the four things chosen:

1. **It moves and remembers.** Dragged by its top bar to anywhere on any
   screen, resized from its edges, and reopened where it was left at that
   size (`mini_search_frame.py`; the place is saved through `state_writes`).
2. **Type chips** - All, Files, Mail, Photos, Code - under the box, beside
   the same `/` menu every search box has (`from:`, `after:`, ...). A chip
   filters the answer in hand at once; when that answer was cut off at the
   engine's depth, the chip's own search is run too, so "Photos" finds the
   photos a page of documents pushed out (`scope_request`).
3. **A preview beside the results** - the Search tab's own pane
   (`widgets/preview.py`), embedded: a document's text, a picture, an
   email's header card. Ctrl+P (Ctrl+Shift+P, the main window's key, too).
4. **Keys for everything, and recent searches.** Enter opens, Ctrl+Enter
   shows it in its folder, Ctrl+C copies the path, Ctrl+O opens an email in
   Outlook, Shift+Enter shows every result in the main window, and an empty
   box lists what was searched before. The keys are said along the bottom.

**Nothing here reads the disk or the index on the interface thread.**
Searches, the saved and recent lists and the preview run on workers; the
place is read with one keyed `get_state` and written by `state_writes`.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QSizeGrip, QSizePolicy, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from app.core.logging import logger
from app.ui.presenter.quick_search import (
    CHIP_ORDER, chip_label, kind_bucket, row_label,
)
from app.ui.widgets.mini_search_frame import CardWindow

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

#: The size it opens at the very first time; after that, where it was left.
WIDTH, HEIGHT = 720, 560

#: How many recent searches an empty box offers.
RECENT_SHOWN = 6

#: How many saved searches it offers under them.
SAVED_SHOWN = 3

#: How much wider the window grows when the preview opens, if it is narrow.
PREVIEW_ROOM = 400

#: An answer with this many passages may have been cut off at the engine's
#: depth, so a chip runs its own search too. `None` is the engine's own
#: `FUSED_LIMIT`, read when first needed.
FULL_ANSWER: Optional[int] = None

#: Stillness before a moved or resized window's place is saved.
SAVE_PLACE_MS = 600


def _full_answer() -> int:
    """The engine's own result cap, imported on first need (the engine is heavy)."""
    if FULL_ANSWER is not None:
        return int(FULL_ANSWER)
    from app.search.engine import FUSED_LIMIT

    return int(FUSED_LIMIT)


def _grouped(found: Any, details: Any) -> list:
    """A `run_search` answer as document groups. Pure; run on the worker."""
    from app.ui.presenter import group_results, to_rows

    terms = tuple(getattr(getattr(found.response, "parsed", None), "terms", ()) or ())
    return group_results(to_rows(found.results, terms), details=details)


class MiniSearch(CardWindow):
    """A frameless, stay-on-top search box. Summoned, used, gone."""

    #: A result was chosen. The window opens it.
    chosen = Signal(object)
    #: Somebody asked for the whole application instead. The window comes up
    #: with this query in the box - the way out of a box that is too small
    #: for the question being asked.
    expanded = Signal(str)

    def __init__(self, engine: Any, parent: Optional[QWidget] = None, *,
                 preferences: Any = None, offer_recent: Any = None) -> None:
        # Frameless *and* a Tool window: a Tool has no taskbar entry, which is
        # what makes this feel like a summoned thing rather than a second
        # application somebody now has to close.
        super().__init__(Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self._engine = engine
        #: Returns the Settings search switches, read on every search so a
        #: switch changed in Settings applies at once; `None` is the Search
        #: surface's defaults (2026-10-04 - the box used to ignore them).
        self._preferences = preferences if callable(preferences) else (lambda: preferences)
        #: Whether to offer recent searches - the Settings switch, read at summon.
        self._offer_recent = offer_recent if callable(offer_recent) else (
            lambda: True if offer_recent is None else bool(offer_recent))
        self._generation = 0
        self._rows: list = []
        #: Every group the last response produced, before the chip filter -
        #: what "the result set already in hand" means for §5a. `_rows` is
        #: the filtered, display-capped view of this.
        self._all_groups: list = []
        #: `file_id -> mail details` for the groups in hand: a message's
        #: subject and sender, so a row is never called by its `pst://` key.
        self._details: dict = {}
        #: `bucket -> groups`, from a chip's own search when the answer in hand
        #: was cut off at the engine's depth (item 2, 2026-10-08).
        self._deep: dict = {}
        #: The last answer had as many passages as the engine returns at most.
        self._answer_full = False
        #: The words searched for, for the preview's highlights.
        self._terms: tuple = ()
        #: `bucket -> count`, over `_all_groups`. Empty when there is nothing
        #: to show a chip for.
        self._chip_counts: dict = {}
        #: The buckets with a count chip showing, in `CHIP_ORDER`. Parallel to
        #: the buttons in `self._chip_buttons`.
        self._chip_kinds: list = []
        #: `None` for "all", or one of `CHIP_ORDER`.
        self._active_chip: Optional[str] = None
        #: The saved searches, read once per summon on a worker; `None` until
        #: that read lands (`run_search` then reads them itself). 2026-10-04,
        #: code review: every 180 ms pause re-read them from the store.
        self._saved: Optional[tuple] = None
        #: The saved searches the last answer expanded - counted as run only
        #: when Enter or a click opens one of its results (2026-10-04, code
        #: review: each typing pause wrote a counter).
        self._saved_names: tuple = ()
        #: What was searched before, newest first, read on a worker at summon.
        self._recent: tuple = ()
        #: The place: read once (`_load_place`), saved after a move and on close.
        self._place: Any = None
        self._place_read = False
        self._place_saved = ""
        self._preview_wanted = False
        self._preview_width = 0
        self._grown = 0
        self.preview: Any = None

        self.setObjectName("miniSearch")
        from app.ui.presenter.quick_search import MIN_SIZE

        self.setMinimumSize(*MIN_SIZE)
        self.resize(WIDTH, HEIGHT)
        self._build()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._search)

        self._place_timer = QTimer(self)
        self._place_timer.setSingleShot(True)
        self._place_timer.setInterval(SAVE_PLACE_MS)
        self._place_timer.timeout.connect(self._save_place)

    # -- building ---------------------------------------------------------------

    def _build(self) -> None:
        from app.ui.presenter import quick_search as words
        from app.ui.widgets.buttons import icon_button
        from app.ui.widgets.mini_search_rows import MiniRowDelegate

        card = QVBoxLayout(self.card)
        card.setContentsMargins(4, 4, 4, 4)
        card.setSpacing(0)

        # --- the top bar: the handle, the box, three buttons. Dragged to move.
        self.header = QWidget()
        self.header.setObjectName("miniHeader")
        head = QVBoxLayout(self.header)
        head.setContentsMargins(14, 6, 10, 8)
        head.setSpacing(4)
        self.handle = QFrame()
        self.handle.setObjectName("miniHandle")
        self.handle.setFixedSize(40, 4)
        self.handle.setToolTip(words.MOVE_TIP)
        self.handle.setCursor(Qt.CursorShape.OpenHandCursor)
        head.addWidget(self.handle, 0, Qt.AlignmentFlag.AlignHCenter)
        line = QHBoxLayout()
        line.setSpacing(8)
        self.search_icon = QLabel()
        self.search_icon.setObjectName("miniSearchIcon")
        self.search_icon.setFixedSize(22, 22)
        line.addWidget(self.search_icon)

        self.box = QLineEdit()
        self.box.setObjectName("miniBox")
        self.box.setPlaceholderText(words.PLACEHOLDER)
        self.box.setAccessibleName("Search")
        self.box.setFrame(False)
        self.box.textEdited.connect(self._typed)
        self.box.returnPressed.connect(self._take)
        # **Tab cycles the chips instead of leaving the box.** A plain
        # QLineEdit hands Tab to Qt's focus-next-widget machinery before
        # `keyPressEvent` ever sees it, so intercepting it here - on the box
        # itself, via an event filter - is the one place that works. The same
        # filter takes the action keys before the box spends them (Ctrl+C,
        # Ctrl+Enter). Installed before the `/` menu's, so the menu - whose
        # filter then runs first - keeps Tab and Enter while it is open.
        self.box.installEventFilter(self)
        line.addWidget(self.box, 1)

        self.preview_button = icon_button("Preview", tooltip=words.PREVIEW_TIP,
                                          name="Show or hide the preview")
        self.preview_button.setCheckable(True)
        self.preview_button.clicked.connect(lambda _c=False: self.toggle_preview())
        self.expand_button = icon_button("Show all results", tooltip=words.EXPAND_TIP)
        self.expand_button.clicked.connect(lambda _c=False: self._expand())
        self.close_button = icon_button("Close", tooltip=words.CLOSE_TIP,
                                        name="Close the search box")
        self.close_button.clicked.connect(lambda _c=False: self.dismiss())
        for button in (self.preview_button, self.expand_button, self.close_button):
            button.setObjectName("miniHeaderButton")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            line.addWidget(button)
        head.addLayout(line)
        card.addWidget(self.header)
        self.add_drag_handle(self.header)
        self.add_drag_handle(self.handle)

        # **The same `/` menu as the Search tab's box** - order "dates" §1c
        # asked for `/date` here, and this box had no menu at all, so `/after`
        # was typed blind and then searched for as the word "after". The
        # Search tab's own catalogue (2026-10-08): the box runs `run_search`,
        # which expands `saved:` and runs the history half, so `/saved` and
        # the history switches do here what they do there. See `_take` for
        # the one thing the menu changes here.
        from app.ui.widgets.code_commands import (
            SEARCH_CATALOGUE, search_command_for, search_matching,
        )
        from app.ui.widgets.command_popup import attach_to

        self._popup = attach_to(self.box, store=getattr(self._engine, "store", None),
                                catalogue=SEARCH_CATALOGUE, matcher=search_matching,
                                resolve=search_command_for)

        # --- the chip bar: the type chips, then §5a's live counts.
        bar = QWidget()
        bar.setObjectName("miniChipBar")
        chips_line = QHBoxLayout(bar)
        chips_line.setContentsMargins(12, 2, 12, 8)
        chips_line.setSpacing(6)
        self.scopes = QWidget()
        self.scopes.setObjectName("miniScopes")
        scope_line = QHBoxLayout(self.scopes)
        scope_line.setContentsMargins(0, 0, 0, 0)
        scope_line.setSpacing(6)
        self._scope_buttons: dict = {}
        for bucket, label, glyph in words.SCOPE_CHIPS:
            chip = QPushButton(label)
            chip.setObjectName("miniScope")
            chip.setProperty("glyph", glyph)
            chip.setToolTip(words.chip_tip(bucket))
            chip.setAccessibleName(f"{label} - {words.chip_tip(bucket)}")
            chip.setCheckable(True)
            chip.setFlat(True)
            chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _checked=False, k=bucket: self._select_chip(k))
            scope_line.addWidget(chip)
            self._scope_buttons[bucket] = chip
        self.scopes.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        chips_line.addWidget(self.scopes)
        chips_line.addStretch(1)

        #: §5a's chip row. Built fresh on every response, since which kinds
        #: appear - and whether any chip is worth showing at all - changes
        #: with the query.
        self.chips = QWidget()
        self.chips.setObjectName("miniChips")
        self._chips_layout = QHBoxLayout(self.chips)
        self._chips_layout.setContentsMargins(0, 0, 0, 0)
        self._chips_layout.setSpacing(2)
        self._chip_buttons: list = []
        self.chips.hide()
        chips_line.addWidget(self.chips)
        card.addWidget(bar)

        rule = QFrame()
        rule.setObjectName("miniRule")
        rule.setFixedHeight(1)
        card.addWidget(rule)

        # --- the body: the list (or the recent searches, or a sentence), then
        # the preview, in a splitter so its width can be dragged.
        self._delegate = MiniRowDelegate(self)
        self.list = QListWidget()
        self.list.setObjectName("miniList")
        self.list.setAccessibleName("Results")
        self.list.setItemDelegate(self._delegate)
        self.list.setMouseTracking(True)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.itemActivated.connect(lambda _item: self._take())
        self.list.itemClicked.connect(lambda _item: self._take())
        self.list.currentRowChanged.connect(self._row_changed)

        self.recent_page = QWidget()
        self.recent_page.setObjectName("miniRecent")
        recent = QVBoxLayout(self.recent_page)
        recent.setContentsMargins(0, 0, 0, 0)
        recent.setSpacing(2)
        self.recent_heading = QLabel(words.RECENT_HEADING)
        self.recent_heading.setObjectName("miniSection")
        recent.addWidget(self.recent_heading)
        self.recent = QListWidget()
        self.recent.setObjectName("miniList")
        self.recent.setAccessibleName("Recent searches")
        self.recent.setItemDelegate(self._delegate)
        self.recent.setMouseTracking(True)
        self.recent.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.recent.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent.itemClicked.connect(lambda item: self._run_recent(item))
        self.recent.itemActivated.connect(lambda item: self._run_recent(item))
        recent.addWidget(self.recent, 1)

        self.message = QLabel("")
        self.message.setObjectName("miniMessage")
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.recent_page)
        self.stack.addWidget(self.message)

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setObjectName("miniSplit")
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(1)
        holder = QWidget()
        holder.setObjectName("miniBody")
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(8, 6, 8, 6)
        inner.addWidget(self.stack)
        self.split.addWidget(holder)
        card.addWidget(self.split, 1)

        rule = QFrame()
        rule.setObjectName("miniRule")
        rule.setFixedHeight(1)
        card.addWidget(rule)

        # --- the footer: what is happening, the keys, the resize grip.
        footer = QWidget()
        footer.setObjectName("miniFooter")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(14, 6, 2, 2)
        foot.setSpacing(6)
        self.status = QLabel("")
        self.status.setObjectName("miniStatus")
        foot.addWidget(self.status)
        foot.addStretch(1)
        self.hints = QWidget()
        self.hints.setObjectName("miniHints")
        self._hints_layout = QHBoxLayout(self.hints)
        self._hints_layout.setContentsMargins(0, 0, 0, 0)
        self._hints_layout.setSpacing(4)
        foot.addWidget(self.hints)
        self.grip = QSizeGrip(footer)
        self.grip.setToolTip(words.RESIZE_TIP)
        foot.addWidget(self.grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        card.addWidget(footer)
        self._show_mode("empty")

    # -- the theme --------------------------------------------------------------

    def _apply_theme(self) -> None:
        """The window's own sheet, from the theme the main window is using.

        **This box is a window of its own**, so the sheet the main window set
        on itself never reached it - it was drawn in the operating system's
        default look, light in the dark theme. Built from the same template
        and palette (`theme.stylesheet`), then the box's own rules after it.
        """
        from app.ui import theme
        from app.ui.widgets.buttons import retint_all
        from app.ui.widgets.icons import icon

        colours = theme.theme_colours()
        scheme = next((name for name, palette in theme.PALETTES.items()
                       if all(colours.get(k) == v for k, v in palette.items())), "")
        try:
            base = theme.stylesheet(scheme) if scheme else ""
        except Exception as exc:                 # noqa: BLE001 - the box's own rules still apply
            _log.debug("no window sheet for the mini search: {}", exc)
            base = ""
        sizes = theme.font_sizes()
        sizes["field"] = f"{round(float(sizes['body'][:-2]) * 1.6, 1)}pt"
        self.setStyleSheet(base + _SHEET.format(**colours, **sizes))
        self.set_shadow_strength(scheme == "dark")
        self._delegate.retint(colours)
        retint_all(colours, roots=[self])
        for button in (self.preview_button, self.expand_button, self.close_button):
            button.setIconSize(QSize(18, 16))
        dim = colours.get("text_dim", "#888888")
        self.search_icon.setPixmap(icon("search", colours.get("text_faint", dim)).pixmap(22, 22))
        for chip in self._scope_buttons.values():
            ink = colours.get("accent_text", dim) if chip.isChecked() else dim
            chip.setIcon(icon(str(chip.property("glyph")), ink))
            chip.setIconSize(QSize(14, 14))
        self._colours = colours
        if self.preview is not None and hasattr(self.preview, "retint"):
            try:
                self.preview.retint(colours)
            except Exception:                    # noqa: BLE001 - a tint
                pass

    def _tint_scopes(self) -> None:
        """Recolour the type chips: the chosen one in the accent, the rest dim."""
        from app.ui.widgets.icons import icon

        colours = getattr(self, "_colours", None) or {}
        dim = colours.get("text_dim", "#888888")
        for bucket, chip in self._scope_buttons.items():
            chip.setChecked(bucket == self._active_chip)
            ink = colours.get("accent_text", dim) if chip.isChecked() else dim
            chip.setIcon(icon(str(chip.property("glyph")), ink))

    # -- appearing and disappearing -------------------------------------------

    def summon(self, prefill: str = "") -> None:
        """Show it where it was left (or centred, the first time), ready to type.

        **Remembered, and put back on a screen that exists** (the owner,
        2026-10-08: it should be movable and reopen where it was left). A
        box left on a monitor that has since been unplugged comes back on the
        screen under the pointer.

        **`prefill`, selected rather than merely present.** Adoptions §4a: a
        text selection in the foreground application arrives here already
        read, and is never searched by itself - it only replaces the "type
        your query" step with "one keystroke replaces what's highlighted",
        which is what makes it safe to always show even when it is wrong.
        """
        self._apply_theme()
        self._fit_minimum()
        self._load_place()
        self._place_window()
        self.box.clear()
        self.list.clear()
        self._rows = []
        self._reset_chips()
        self._active_chip = None
        self._tint_scopes()
        self._load_saved()
        self._load_recent()
        self.show()
        self._fit_minimum()
        self.raise_()
        self.activateWindow()
        self.box.setFocus()
        if self.preview is not None and not self.preview.isHidden():
            # Put back at the saved size above; open again from there.
            self.preview.hide()
            self._grown = 0
        if self._preview_wanted:
            self.toggle_preview()
        if prefill:
            self.box.setText(prefill)
            self.box.selectAll()
        self._show_mode("empty")

    def _fit_minimum(self) -> None:
        """No narrower than its type chips need, in the font this machine draws.

        `MIN_SIZE` is a floor, not the answer: the chips row can need more. On
        GitHub's Windows runner, whose fonts are wider, "All" was squeezed below
        its own text at 560px (2026-10-08) - a box narrower than its chips cuts
        them. Measured after the theme is applied, since that sets the font, and
        again once shown, when the row's place in the window is known.

        **The chips row only**, plus its margins either side: the footer hints
        and the result counts give way when room is short, by design, so the
        window's overall minimum is not the measure.
        """
        from PySide6.QtCore import QPoint

        from app.ui.presenter.quick_search import MIN_SIZE

        self.ensurePolished()
        # **Each chip as wide as it is when chosen.** A chosen chip is drawn
        # semibold, which is a pixel or two wider; without room kept for it,
        # choosing a chip in a box at its narrowest cut that chip short (55px
        # for a 56px "All" on GitHub's runner) and nudged its neighbours.
        for chip in self._scope_buttons.values():
            chosen = chip.isChecked()
            widths = []
            for state in (False, True):
                chip.setChecked(state)
                chip.style().unpolish(chip)
                chip.style().polish(chip)
                widths.append(chip.sizeHint().width())
            chip.setChecked(chosen)
            chip.style().unpolish(chip)
            chip.style().polish(chip)
            chip.setMinimumWidth(max(widths))
        if self.layout() is not None:
            self.layout().activate()
        left = max(0, self.scopes.mapTo(self, QPoint(0, 0)).x())
        self.setMinimumWidth(max(MIN_SIZE[0], self.scopes.sizeHint().width() + 2 * left))

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
        self._show_mode("empty")

    def dismiss(self) -> None:
        """Gone, and holding nothing. Escape, focus loss, or a chosen result."""
        self._timer.stop()
        self._generation += 1                    # anything in flight is stale
        self._popup.popup().hide()               # a menu outliving its box
        if self.isVisible():
            self._save_place()
        self.hide()
        self.box.clear()
        self.list.clear()
        self._rows = []
        self._saved_names = ()
        self._reset_chips()
        if self.preview is not None:
            self.preview.clear()

    def changeEvent(self, event: Any) -> None:              # noqa: N802 - Qt's name
        """Clicking away closes it. See the module docstring.

        Checked a moment later rather than at once: activation passes through
        nothing for an instant while the `/` menu opens, and a box that closed
        on its own menu would be worse than one that stayed.
        """
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            QTimer.singleShot(150, self._closed_by_clicking_away)

    def _closed_by_clicking_away(self) -> None:
        r"""Close it if somebody really went elsewhere: another application
        (no active window here at all) or another of this application's
        windows - never the box's own `/` menu, which takes activation for
        itself on some platforms."""
        try:
            if (not self.isVisible() or self.isActiveWindow()
                    or self._dragging is not None or self._resizing is not None):
                return
            from PySide6.QtWidgets import QApplication

            popup = self._popup.popup()
            active = QApplication.activeWindow()
            if popup.isVisible() or active is popup or (
                    active is not None and self.isAncestorOf(active)):
                return
            self.dismiss()
        except RuntimeError:                     # the box itself has gone
            pass

    # -- the place: where it was left -------------------------------------------

    def _store(self) -> Any:
        """The engine's store, or None - every store call goes through here."""
        return getattr(self._engine, "store", None)

    def _load_place(self) -> None:
        r"""Read the saved place once. **A keyed read**, one row by primary key
        on this thread's own connection, which under WAL never waits for the
        indexer - the reading rule `state_writes` states. Never raises."""
        if self._place_read:
            return
        self._place_read = True
        store = self._store()
        if store is None or not hasattr(store, "get_state"):
            return
        from app.ui.presenter.quick_search import PLACE_KEY, read_place

        try:
            # The one store read on the UI thread in this file - a keyed row, once
            # per process, deliberately; see the docstring above.
            text = store.get_state(PLACE_KEY, "") or ""
        except Exception as exc:                 # noqa: BLE001 - centred, then
            _log.debug("the mini search's place was not read: {}", exc)
            return
        self._place_saved = str(text)
        rect, preview, width = read_place(text)
        self._place, self._preview_wanted, self._preview_width = rect, preview, width

    def _place_window(self) -> None:
        """Where to show it: the saved place on a screen that exists, else
        centred on the screen under the pointer, a quarter of the way down."""
        from app.ui.presenter.quick_search import default_place, fit_on_screens
        from app.ui.widgets.mini_search_frame import screens_now

        try:
            screens, here = screens_now()
            if not screens:
                return
            if self._place is not None:
                fitted = fit_on_screens(self._place, screens, here)
            else:
                fitted = default_place(screens[here], (self.width(), self.height()))
            if fitted is not None:
                self.setGeometry(*fitted)
        except Exception as exc:                 # noqa: BLE001 - placement
            _log.debug("the mini search could not be placed: {}", exc)

    def moved_by_hand(self) -> None:
        """A drag or resize ended: save the place once it settles (`SAVE_PLACE_MS`)."""
        self._place_timer.start()

    def moveEvent(self, event: Any) -> None:                # noqa: N802 - Qt's name
        super().moveEvent(event)
        if self.isVisible():
            self._place_timer.start()

    def resizeEvent(self, event: Any) -> None:              # noqa: N802 - Qt's name
        super().resizeEvent(event)
        if self.isVisible():
            self._place_timer.start()
            self._show_hints()
            self._fit_counts()

    def _save_place(self) -> None:
        """Queue the place behind every other state write. Only when it changed."""
        self._place_timer.stop()
        from app.ui.presenter.quick_search import PLACE_KEY, place_text

        open_preview = self.preview is not None and not self.preview.isHidden()
        width = self.width() - self._grown if open_preview else self.width()
        rect = (self.x(), self.y(), width, self.height())
        if open_preview:
            sizes = self.split.sizes()
            self._preview_width = sizes[1] if len(sizes) > 1 else self._preview_width
        text = place_text(rect, preview=open_preview, preview_width=self._preview_width)
        self._place = rect
        self._preview_wanted = open_preview
        if text == self._place_saved:
            return
        self._place_saved = text
        store = self._store()
        if store is None:
            return
        from app.ui.state_writes import save_state

        save_state(store, PLACE_KEY, text, component="ui.mini.place")

    # -- keys ----------------------------------------------------------------------

    def keyPressEvent(self, event: Any) -> None:            # noqa: N802 - Qt's name
        if self._key(event):
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj: Any, event: Any) -> bool:     # noqa: N802 - Qt's name
        r"""The keys, caught on the box before it spends them.

        **On the box, not on this frame.** A `QLineEdit` answers Tab, Enter
        and Ctrl+C itself, underneath anything `keyPressEvent` could
        intercept - so the box is the only place they can be caught rather
        than merely observed. While the `/` menu is open it keeps them.
        """
        if obj is getattr(self, "box", None) and event.type() == QEvent.Type.KeyPress:
            if self._popup.popup().isVisible():
                return False
            return self._key(event)
        return super().eventFilter(obj, event)

    def _key(self, event: Any) -> bool:
        """One key: True when it was this box's to act on."""
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if key == Qt.Key.Key_Escape:
            self._escape()
            return True
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self._cycle_chip(backwards=key == Qt.Key.Key_Backtab or shift)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if ctrl:
                self._reveal_current()
                return True
            if shift:
                self._expand()
                return True
            return False                         # the box's returnPressed -> _take
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
            step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1,
                    Qt.Key.Key_PageDown: ROWS, Qt.Key.Key_PageUp: -ROWS}[key]
            return self._move(step)
        if ctrl and key == Qt.Key.Key_C and not self.box.hasSelectedText():
            return self._copy_current()
        if ctrl and key == Qt.Key.Key_O:
            self._outlook_current()
            return True
        if ctrl and key == Qt.Key.Key_P:
            self.toggle_preview()
            return True
        if ctrl and Qt.Key.Key_1 <= key <= Qt.Key.Key_5:
            from app.ui.presenter.quick_search import SCOPE_CHIPS

            self._select_chip(SCOPE_CHIPS[int(key) - int(Qt.Key.Key_1)][0])
            return True
        return False

    def _move(self, step: int) -> bool:
        """Arrows move the list even while the cursor is in the box, which is
        what everybody's muscle memory expects of a search prompt."""
        listing = self.recent if self.stack.currentWidget() is self.recent_page else self.list
        count = listing.count()
        if not count:
            return False
        row = listing.currentRow()
        listing.setCurrentRow(max(0, min(count - 1, row + step)))
        return True

    def _escape(self) -> None:
        r"""The first Escape empties the box; the second closes it.

        The owner, 2026-10-08. Somebody who typed the wrong thing wants it
        gone, not the whole box; somebody looking at an empty box wants out.
        """
        if self.box.text():
            self._timer.stop()
            self._generation += 1
            self.box.clear()
            self.list.clear()
            self._rows = []
            self._reset_chips()
            if self.preview is not None:
                self.preview.clear()
            self._show_mode("empty")
            return
        self.dismiss()

    # -- searching ------------------------------------------------------------

    def _typed(self, text: str) -> None:
        """Each keystroke: an empty box clears at once, anything else restarts the debounce."""
        if not str(text or "").strip():
            self._timer.stop()
            self._generation += 1
            self.list.clear()
            self._rows = []
            self._reset_chips()
            self._show_mode("empty")
            return
        self._timer.start()

    def _load_saved(self) -> None:
        """The saved searches, once per summon, on a worker. Never raises."""
        store = self._store()
        if store is None:
            self._saved = ()
            return
        try:
            from app.search.run import load_saved
            from app.ui.later import when_done
            from app.ui.workers import CallableWorker, run

            worker = CallableWorker(load_saved, store, component="ui.mini.saved")
            when_done(self, worker, finished=self._took_saved)   # dropped if the box is gone
            run(QThreadPool.globalInstance(), worker)
        except Exception as exc:                 # noqa: BLE001 - run_search reads them instead
            _log.debug("saved searches not read ahead: {}", exc)

    def _took_saved(self, saved: Any) -> None:
        """UI thread: the saved searches landed; redraw the empty box if it is showing."""
        self._saved = tuple(saved or ())
        if self.stack.currentWidget() is not self.list:
            self._show_mode("empty")

    def _load_recent(self) -> None:
        """What was searched before, on a worker - when Settings allows it."""
        self._recent = ()
        store = self._store()
        try:
            wanted = bool(self._offer_recent())
        except Exception:                        # noqa: BLE001 - a switch
            wanted = True
        if store is None or not wanted or not hasattr(store, "recent_searches"):
            return
        try:
            from app.ui.first_contact import FETCH_MULTIPLE, recent
            from app.ui.later import when_done
            from app.ui.workers import CallableWorker, run

            def fetch() -> tuple:
                rows = store.recent_searches(limit=RECENT_SHOWN * FETCH_MULTIPLE)
                return recent(rows, limit=RECENT_SHOWN)

            worker = CallableWorker(fetch, component="ui.mini.recent")
            when_done(self, worker, finished=self._took_recent)
            run(QThreadPool.globalInstance(), worker)
        except Exception as exc:                 # noqa: BLE001 - no list, then
            _log.debug("recent searches not read: {}", exc)

    def _took_recent(self, found: Any) -> None:
        """UI thread: the recent searches landed; redraw the empty box if it is showing."""
        self._recent = tuple(found or ())
        if self.stack.currentWidget() is not self.list and not self.box.text().strip():
            self._show_mode("empty")

    def _note_saved(self) -> None:
        """Count a run of the saved searches the chosen answer used, on a worker."""
        names, store = self._saved_names, self._store()
        self._saved_names = ()
        if not names or store is None:
            return
        try:
            from app.search.run import note_saved_runs
            from app.ui.workers import CallableWorker, run

            run(QThreadPool.globalInstance(),
                CallableWorker(note_saved_runs, store, names, component="ui.mini.saved"))
        except Exception as exc:                 # noqa: BLE001 - a counter
            _log.debug("saved-search run not counted: {}", exc)

    def _search(self, *, deepen: Optional[str] = None) -> None:
        r"""One tier, on a worker, carrying a generation. Never raises.

        `deepen` is a chip whose own search is wanted on top of the answer in
        hand (`_select_chip`); otherwise the chip that is on is searched too,
        on the same worker, whenever the plain answer comes back full.
        """
        from app.ui.workers import CallableWorker, run

        query = self.box.text().strip()
        if not query or self._engine is None:
            self.list.clear()
            self._rows = []
            self._reset_chips()
            self._show_mode("empty")
            return

        self._generation += 1
        generation = self._generation
        engine = self._engine
        preferences = self._preferences()
        saved = self._saved
        chip = deepen if deepen is not None else self._active_chip
        full_at = _full_answer()
        if deepen is None:
            self.status.setText(_words().SEARCHING)

        def ask() -> Any:
            from app.search.policy import SEARCH
            from app.search.run import run_search
            from app.ui.presenter import mail_details
            from app.ui.presenter.quick_search import scope_request

            store = getattr(engine, "store", None)
            found = details = None
            if deepen is None:
                # **The Search tab's search, step for step** (2026-10-04, the
                # owner's decision that every surface searches as that tab
                # does): `/date 2017` and `saved:name` expanded, "mail from
                # 2017" read as filters, the Settings switches for surface
                # SEARCH - the kid-safe policy §3a asks for. `run_search` is
                # the same code the command line and the MCP server run.
                # 2026-10-04, code review: the box's saved list, read once per
                # summon, and no counter written per typing pause.
                found = run_search(engine, query, surface=SEARCH, preferences=preferences,
                                   saved=saved, marks=False, note_saved=False)
                # **One more query, batched, on the same worker.** The rule
                # `mail_details` states: never one lookup per row.
                details = mail_details(store, found.results)
            deep = None
            full = found is None or len(getattr(found.response, "results", None) or []) >= full_at
            if chip is not None and full:
                scope, text = scope_request(chip, query)
                deeper = run_search(engine, text, surface=SEARCH, preferences=preferences,
                                    saved=saved, scope=scope, marks=False, note_saved=False)
                deep = (deeper, mail_details(store, deeper.results))
            return found, details, deep, chip, full

        worker = CallableWorker(ask, component="ui.mini.search")
        # A plain connect, not `when_done`: this box lives as long as the
        # application, so a late answer cannot find it deleted.
        worker.signals.finished.connect(
            lambda payload, g=generation: self._show(payload, g))
        worker.signals.failed.connect(
            lambda error, g=generation: self._failed(error, g))
        run(QThreadPool.globalInstance(), worker)

    def _show(self, payload: Any, generation: int) -> None:
        """UI thread: a search landed. Dropped when a later keystroke moved the
        generation on. Grouping is cheap arithmetic over rows already in hand.
        """
        if generation != self._generation:
            return                               # a later keystroke won
        try:
            found, details, deep, chip, full = payload
            groups = _grouped(found, details) if found is not None else None
            deep_groups = _grouped(deep[0], deep[1]) if deep is not None else None
        except Exception as exc:                 # noqa: BLE001 - a list
            _log.debug("could not draw the mini results: {}", exc)
            return

        if groups is not None:
            self._saved_names = tuple(getattr(found, "saved_names", ()) or ())
            self._terms = tuple(getattr(getattr(found.response, "parsed", None),
                                        "terms", ()) or ())
            self._all_groups = list(groups)
            self._details = dict(details or {})
            self._deep = {}
            self._answer_full = bool(full)
            self._rebuild_chips()
        if deep_groups is not None and chip is not None:
            self._deep[chip] = [group for group in deep_groups if kind_bucket(group.kind) == chip]
            self._details.update(dict(deep[1] or {}))
        self._apply_chip_filter()

    def _failed(self, error: Any, generation: int) -> None:
        """UI thread: the search raised. The worker already logged the error; the
        box goes back to empty rather than showing a dialog over someone's work.
        """
        if generation != self._generation:
            return
        _log.debug("mini search failed: {}", error)
        self.list.clear()
        self._rows = []
        self._reset_chips()
        self._show_mode("empty")

    # -- §5a: live counts per kind, and the type chips ---------------------------

    def _reset_chips(self) -> None:
        """Nothing to count and nothing to show. Summon, dismiss, a failure."""
        self._all_groups = []
        self._details = {}
        self._deep = {}
        self._answer_full = False
        self._chip_counts = {}
        self._rebuild_chips()

    def _apply_chip_filter(self) -> None:
        """The displayed rows: `_all_groups` (or a chip's own search), filtered
        and capped. Never an engine call - this only redraws answers in hand."""
        from app.ui.presenter.quick_search import nothing_found, results_count

        chip = self._active_chip
        if chip is not None and chip in self._deep:
            groups = self._deep[chip]
        else:
            groups = self._all_groups
            if chip is not None:
                groups = [group for group in groups if kind_bucket(group.kind) == chip]
        self._rows = groups[:ROWS]
        self.list.clear()
        from app.ui.presenter.quick_search import row_lines
        from app.ui.widgets.mini_search_rows import ROLE_LINES

        for group in self._rows:
            lines = row_lines(group, self._details.get(getattr(group, "file_id", 0)))
            item = QListWidgetItem(row_label(group))
            item.setData(ROLE_LINES, lines)
            item.setToolTip(lines.tip)
            self.list.addItem(item)
        query = self.box.text().strip()
        if self._rows:
            self.list.setCurrentRow(0)
            self._show_mode("results")
            self.status.setText(results_count(len(groups)) if len(groups) <= ROWS
                                else f"{ROWS} of {results_count(len(groups))}")
        elif query:
            self.message.setText(nothing_found(query))
            self._show_mode("nothing")
            self.status.setText("")
        else:
            self._show_mode("empty")

    def _rebuild_chips(self) -> None:
        """One count chip per kind present, counted from `_all_groups`.

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
                widget.hide()                    # gone now, not at the next idle moment
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
        self._fit_counts()
        for bucket in present:
            button = QPushButton(chip_label(bucket, counts[bucket]))
            button.setObjectName(f"miniChip_{bucket}")
            # Found by 0s's own plain-words/tooltip guard
            # (test_tooltips.py::test_every_control_explains_itself): the
            # chip's label already says the count and the kind, but not
            # what clicking it *does* - narrow the list to only that kind,
            # which is the one fact the label alone doesn't carry.
            button.setToolTip("Show only these results")
            button.setProperty("miniCount", True)
            button.setCheckable(True)
            button.setFlat(True)
            button.setChecked(bucket == self._active_chip)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, k=bucket: self._select_chip(k))
            self._chips_layout.addWidget(button)
            self._chip_buttons.append(button)

    def _fit_counts(self) -> None:
        """The type chips always fit; the counts beside them only when there is
        room - in a box made narrow they would squeeze the chips unreadable."""
        if not self._chip_kinds:
            return
        room = self.card.width() - 40
        wanted = self.scopes.sizeHint().width() + self.chips.sizeHint().width()
        self.chips.setVisible(wanted <= room or not self.card.width())

    def _select_chip(self, bucket: Optional[str]) -> None:
        r"""Show only that kind.

        A filter over the rows already in hand, at once - never a re-search
        for what is already there (§5a). **When the answer in hand was cut off
        at the engine's depth**, the chip's own search runs as well (item 2,
        2026-10-08): a page of fifty documents can hold no photo at all while
        the index holds hundreds, and "Photos" showing nothing would be wrong.
        """
        self._active_chip = bucket
        for button, kind in zip(self._chip_buttons, self._chip_kinds):
            button.setChecked(kind == bucket)
        self._tint_scopes()
        self._apply_chip_filter()
        if (bucket is not None and self._answer_full and bucket not in self._deep
                and self.box.text().strip()):
            self._search(deepen=bucket)

    def _cycle_chip(self, *, backwards: bool = False) -> None:
        r"""Tab steps through the kinds present and back to "all" (Adoptions
        §5a); with one kind or none in hand, through every type chip."""
        kinds = self._chip_kinds or list(CHIP_ORDER)
        order = [None, *kinds]
        try:
            index = order.index(self._active_chip)
        except ValueError:
            index = 0
        self._select_chip(order[(index + (-1 if backwards else 1)) % len(order)])

    # -- what is on screen ----------------------------------------------------------

    def _show_mode(self, mode: str) -> None:
        """`results`, `nothing` (a sentence), or `empty` (recent searches, or a
        sentence) - and the hints for it."""
        from app.ui.presenter.quick_search import empty_words

        if mode == "results":
            self.stack.setCurrentWidget(self.list)
        elif mode == "nothing":
            self.stack.setCurrentWidget(self.message)
        else:
            self._fill_recent()
            if self.recent.count() and not self.box.text().strip():
                self.stack.setCurrentWidget(self.recent_page)
                mode = "recent"
            else:
                self.message.setText(empty_words(False))
                self.stack.setCurrentWidget(self.message)
            self.status.setText("")
        self._mode = mode
        self._show_hints()
        if self.preview is not None and not self.preview.isHidden():
            self._row_changed(self.list.currentRow())

    def _fill_recent(self) -> None:
        """The empty box's list: recent searches first, then a few saved ones."""
        from app.ui.presenter.quick_search import SAVED_NOTE
        from app.ui.widgets.mini_search_rows import ROLE_LINES

        self.recent.clear()
        for text in self._recent[:RECENT_SHOWN]:
            item = QListWidgetItem(text)
            item.setData(ROLE_LINES, {"text": text, "note": "", "kind": "recent",
                                      "query": text})
            self.recent.addItem(item)
        for saved in tuple(self._saved or ())[:SAVED_SHOWN]:
            name = str(getattr(saved, "name", "") or "")
            if not name:
                continue
            note = f"{SAVED_NOTE} · {getattr(saved, 'query', '')}"
            item = QListWidgetItem(name)
            item.setData(ROLE_LINES, {"text": name, "note": note, "kind": "saved",
                                      "query": f"saved:{name}"})
            self.recent.addItem(item)
        if self.recent.count():
            self.recent.setCurrentRow(0)

    def _show_hints(self) -> None:
        """The keys for what is on screen, as many as fit - the least needed
        dropped first (`fit_hints`), never squeezed into each other."""
        from app.ui.presenter.quick_search import fit_hints, hints

        while self._hints_layout.count():
            item = self._hints_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        mode = getattr(self, "_mode", "empty")
        current = self._current()
        mail = current is not None and self._is_mail(current)
        shown = self.preview is not None and not self.preview.isHidden()
        found = hints(mode="results" if mode == "results" else
                      "recent" if mode == "recent" else "empty", mail=mail, preview=shown)
        built = []
        for hint in found:
            keys = QLabel(hint.keys, self.hints)
            keys.setObjectName("miniKey")
            words = QLabel(hint.words, self.hints)
            words.setObjectName("miniKeyWords")
            for label in (keys, words):
                label.hide()
                label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                label.ensurePolished()
            built.append((keys, words))
        widths = [keys.sizeHint().width() + words.sizeHint().width() + 8
                  for keys, words in built]
        budget = self.card.width() - self.status.sizeHint().width() - 70
        kept = set(fit_hints(found, widths, budget))
        for hint, (keys, words) in zip(found, built):
            if hint in kept:
                self._hints_layout.addWidget(keys)
                self._hints_layout.addWidget(words)
                keys.show()
                words.show()
            else:
                keys.deleteLater()
                words.deleteLater()

    def _row_changed(self, row: int) -> None:
        """The selection moved: the preview follows, and Ctrl+O's hint."""
        if getattr(self, "_mode", "") == "results":
            self._show_hints()
        if self.preview is None or self.preview.isHidden():
            return
        group = self._rows[row] if 0 <= row < len(self._rows) else None
        best = getattr(group, "best", None) if group is not None else None
        self.preview.show_row(best if best is not None else group)

    # -- the preview ----------------------------------------------------------------

    def toggle_preview(self, *, grow: bool = True) -> None:
        r"""Show or hide the selected result beside the list. Ctrl+P.

        **The Search tab's own pane**, built the first time it is asked for:
        a document's text with the searched words marked, a picture, an
        email's header card - read on its own worker, never here.
        """
        if self.preview is None:
            self._build_preview()
        showing = self.preview.isHidden()
        screen_room = self._screen_width()
        if showing:
            self.preview.show()
            room = 0
            if grow and self.width() < 980:
                room = min(PREVIEW_ROOM, max(0, screen_room - self.width()))
                if room:
                    self.resize(self.width() + room, self.height())
            self._grown += room
            pane = self._preview_width or max(320, (self.width() - 2 * 18) * 2 // 5)
            total = sum(self.split.sizes()) or self.width()
            self.split.setSizes([max(260, total - pane), pane])
            self._row_changed(self.list.currentRow())
        else:
            sizes = self.split.sizes()
            if len(sizes) > 1 and sizes[1] > 0:
                self._preview_width = sizes[1]
            self.preview.hide()
            self.preview.clear()
            if self._grown:
                self.resize(max(self.minimumWidth(), self.width() - self._grown), self.height())
                self._grown = 0
        self.preview_button.setChecked(not self.preview.isHidden())
        self._show_hints()
        if self.isVisible():
            self._place_timer.start()

    def _screen_width(self) -> int:
        """Room to the right edge of this screen, or a large number when there is no screen."""
        screen = self.screen()
        if screen is None:
            return 4000
        area = screen.availableGeometry()
        return max(0, area.right() - self.x())

    def _build_preview(self) -> None:
        """Make the preview pane on first use. The pane reads on its own worker and
        reports an error as a sentence in the status line, never a dialog.
        """
        from app.ui.widgets.preview import PreviewPane

        pane = PreviewPane()
        pane.setObjectName("miniPreview")
        pane.store = self._store()
        pane.terms_provider = lambda: self._terms
        pane.open_requested.connect(self._open_from_preview)
        pane.reveal_requested.connect(pane.reveal_row)
        pane.error.connect(lambda error: self.status.setText(
            str(getattr(error, "message", "") or "")))
        # Pinning needs the main window, which this box does not have.
        pane.pop_button.hide()
        pane.setMinimumWidth(260)
        pane.hide()
        self.split.addWidget(pane)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.preview = pane
        colours = getattr(self, "_colours", None)
        if colours and hasattr(pane, "retint"):
            try:
                pane.retint(colours)
            except Exception:                    # noqa: BLE001 - a tint
                pass

    def _open_from_preview(self, row: Any) -> None:
        self._note_saved()
        self.dismiss()
        self.chosen.emit(row)

    # -- choosing -------------------------------------------------------------

    def _current(self) -> Any:
        """The highlighted result group, or None."""
        row = self.list.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _is_mail(self, group: Any) -> bool:
        """Whether a group is a message or an attachment - what Ctrl+O can send to Outlook."""
        from app.ui.presenter.quick_search import is_message_group

        detail = self._details.get(getattr(group, "file_id", 0))
        return bool(is_message_group(group, detail) or getattr(group, "is_attachment", False))

    def _run_recent(self, item: Any) -> None:
        """A recent or saved search: put it in the box and search now."""
        from app.ui.widgets.mini_search_rows import ROLE_LINES

        data = item.data(ROLE_LINES) if item is not None else None
        query = str((data or {}).get("query", "") or "")
        if not query:
            return
        self.box.setText(query)
        self.box.setFocus()
        self._timer.stop()
        self._search()

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

        **An empty box runs the highlighted recent search** instead.
        """
        if self._popup.popup().isVisible():
            return
        if not self.box.text().strip() and self.stack.currentWidget() is self.recent_page:
            self._run_recent(self.recent.currentItem())
            return
        chosen = self._current()
        if chosen is not None:
            # A saved search's run counts here, once (2026-10-04, code review).
            # Handed to the window instead, the window's box counts it.
            self._note_saved()
            self.dismiss()
            self.chosen.emit(chosen)
            return
        self._expand()

    def _expand(self) -> None:
        """Every result, in the main window. Shift+Enter, or the header button."""
        query = self.box.text().strip()
        self.dismiss()
        if query:
            self.expanded.emit(query)

    def _reveal_current(self) -> None:
        """Ctrl+Enter: show the highlighted result in its folder, by the one
        open route every page uses (`workers.open_row_async`), and close."""
        chosen = self._current()
        if chosen is None:
            return
        from app.ui import workers

        self._note_saved()
        self.dismiss()
        workers.open_row_async(self._store(), chosen, reveal=True)

    def _copy_current(self) -> bool:
        """Ctrl+C with nothing selected in the box: the highlighted result's
        real path on the clipboard (`workers.copy_path_async`). The box stays."""
        chosen = self._current()
        if chosen is None:
            return False
        from app.ui import workers
        from app.ui.presenter.quick_search import COPIED

        workers.copy_path_async(chosen, store=self._store())
        self.status.setText(COPIED)
        return True

    def _outlook_current(self) -> None:
        r"""Ctrl+O: the highlighted email, in Outlook - by the one open route,
        which opens a message in Outlook (the owner, 2026-10-04). For an
        attachment, the message it came with."""
        chosen = self._current()
        if chosen is None:
            return
        from app.ui.presenter.quick_search import OUTLOOK_ONLY

        if not self._is_mail(chosen):
            self.status.setText(OUTLOOK_ONLY)
            return
        from app.core.row_facts import attachment_of
        from app.ui import workers
        from app.ui.presenter.opening import Place

        parent, _name = attachment_of(getattr(chosen, "path", ""))
        target = Place(parent) if parent else chosen
        self._note_saved()
        self.dismiss()
        workers.open_row_async(self._store(), target)


def _words() -> Any:
    """The presenter's wording module, imported on use."""
    from app.ui.presenter import quick_search

    return quick_search


#: The box's own rules, after the window's sheet. Theme tokens only.
_SHEET = """
#miniSearch {{ background: transparent; border: none; }}
#miniCard {{
    background: {surface}; border: 1px solid {border_strong}; border-radius: 14px;
}}
#miniCard QWidget#miniHeader, #miniCard QWidget#miniChipBar, #miniCard QWidget#miniFooter,
#miniCard QSplitter, #miniCard QStackedWidget, #miniCard QListWidget#miniList,
#miniCard QWidget#miniChips, #miniCard QWidget#miniScopes, #miniCard QWidget#miniHints,
#miniCard QWidget#miniBody, #miniCard QWidget#miniRecent, #miniCard QLabel#miniMessage {{
    background: transparent; border: none;
}}
#miniHandle {{ background: {border_strong}; border-radius: 2px; }}
QLineEdit#miniBox {{
    background: transparent; border: none; padding: 6px 2px;
    font-size: {field}; color: {text};
    selection-background-color: {accent}; selection-color: {selection_text};
}}
QPushButton#miniHeaderButton {{
    background: transparent; border: 1px solid transparent; border-radius: 8px;
    padding: 5px 4px 5px 7px; min-height: 18px;
}}
QPushButton#miniHeaderButton:hover {{ background: {surface_hover}; border-color: {border}; }}
QPushButton#miniHeaderButton:checked {{ background: {accent_soft}; border-color: {focus_ring}; }}
QPushButton#miniScope {{
    background: {surface_alt}; color: {text_dim}; border: 1px solid {border};
    border-radius: 13px; padding: 4px 12px 4px 9px; min-height: 16px; font-size: {body};
}}
QPushButton#miniScope:hover {{ background: {surface_hover}; color: {text}; }}
QPushButton#miniScope:checked {{
    background: {accent_soft}; color: {accent_text}; border-color: {focus_ring};
    font-weight: 600;
}}
QPushButton[miniCount="true"] {{
    background: transparent; color: {text_faint}; border: none; border-radius: 10px;
    padding: 3px 7px; font-size: {small};
}}
QPushButton[miniCount="true"]:hover {{ color: {text}; background: {surface_hover}; }}
QPushButton[miniCount="true"]:checked {{ color: {accent_text}; background: {accent_soft}; }}
#miniRule {{ background: {divider}; border: none; }}
QLabel#miniSection {{
    color: {text_faint}; font-size: {small}; font-weight: 600; padding: 4px 6px 2px 6px;
}}
QLabel#miniMessage {{ color: {text_dim}; font-size: {body}; padding: 24px; }}
QLabel#miniStatus {{ color: {text_faint}; font-size: {small}; }}
QLabel#miniKey {{
    color: {text_dim}; background: {surface_alt}; border: 1px solid {border};
    border-bottom-width: 2px; border-radius: 4px; padding: 0px 5px; font-size: {small};
}}
QLabel#miniKeyWords {{ color: {text_faint}; font-size: {small}; padding-right: 6px; }}
QSplitter#miniSplit::handle {{ background: {divider}; }}
#miniPreview {{ background: {surface}; }}
QSizeGrip {{ background: transparent; width: 12px; height: 12px; }}
"""
