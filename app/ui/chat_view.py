"""Chat: talk to Leasha - it reads your files first, and answers in plain words.

Layer: L5 view - thin. Decisions are in `app/ui/presenter/chat.py`; the pieces
are in `app/ui/widgets/chat_*.py`; the engine, the worker and the saving are in
`app/ui/controllers/chat_controller.py`. This file only lays them out and
turns the person's keys and clicks into signals.

Work order 202626270611 section 3. The layout is three columns: your
conversations, the conversation itself (bubbles, the documents on the shelf,
the box you type in), and a side panel of the local sources the answer stands on
and their preview (2026-10-08, `widgets/chat_side_panel.py`). **No banners:**
the receipts are the honesty - every source number in the prose opens the passage
it came from. The message actions (Copy, Regenerate, Try again, Edit) live on the
messages themselves (`chat_bubbles.py`); this file only forwards them.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

from app.ui.presenter.chat import NOT_BUILT_LINE, PREVIEW_TAB, THINKING_LINE, unavailable_text
from app.ui.widgets.chat_answer_run import AnswerRun, NoShelf
from app.ui.widgets.chat_bubbles import AnswerBubble, BubbleList, UserBubble, centred
from app.ui.widgets.chat_head import build_head
from app.ui.widgets.chat_message_box import MessageBox
from app.ui.widgets.chat_session_list import SessionList
from app.ui.widgets.chat_shelf import ShelfBar
from app.ui.widgets.chat_side_panel import PanelSwitch
from app.ui.widgets.chat_sources import SourcesPane
from app.ui.widgets.model_picker import ModelPicker
from app.ui.widgets.page_frame import page_row

__all__ = ["ChatView"]


class ChatView(QWidget):
    """The Chat tab's layout and signals. Thin: see the module docstring."""
    question_submitted = Signal(str)
    stop_requested = Signal()
    recheck_requested = Signal()
    speed_changed = Signal(str)
    shelf_changed = Signal()
    open_requested = Signal(str)            # a path (a shelf chip)
    result_opened = Signal(object)          # a result row (a source, a hit)
    result_revealed = Signal(object)
    #: The Sources pane's right-click menu: the row that was clicked.
    pin_requested = Signal(object)
    reindex_requested = Signal(object)
    similar_requested = Signal(object)
    error = Signal(object)
    #: The window is closing: the controller stops any answer still running.
    closing = Signal()
    #: The last answer's Regenerate / Try again, and the last message's Edit.
    regenerate_requested = Signal()
    retry_requested = Signal()
    edit_requested = Signal()
    #: The Web chip was switched for this conversation.
    web_toggled = Signal(bool)
    #: The person answered "Search the web for ...?" - `True` is Allow.
    web_decided = Signal(bool)
    #: 2026-10-04: the Sources column's preview was shown (True) or hidden.
    preview_toggled = Signal(bool)
    #: UI phase B: the last message, brought back by Up and sent changed.
    question_edited = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """Build the three columns and wire the widgets' signals. UI thread, no store."""
        super().__init__(parent)
        self.sessions = SessionList()
        self.bubbles = BubbleList()
        self.shelf = ShelfBar()
        self.box = MessageBox()
        self.sources = SourcesPane()
        #: 2026-10-08: the side panel's switch; it gives the view `side_tabs` and `toggles`.
        self.panel = PanelSwitch(self)
        self._run: Optional[AnswerRun] = None
        self._owner: Any = None                  # whose sources the pane shows

        notice_row, head = build_head(self)       # the notice and the speed box
        # 2026-10-04: which model answers; filled by the controller, hidden with fewer than two.
        self.model_picker = ModelPicker()
        # Order 1o D4: the model and speed are the box's own controls, along its bottom edge.
        self.box.add_to_footer(self.model_picker.warning, self.model_picker, self.speed)
        centre = QWidget()
        column = QVBoxLayout(centre)
        # 10 a side: the three panes of the splitter met with a hairline between
        # them, so the footer line, the question box and "Send" all touched it.
        # 2026-10-08: one 8px rhythm down the column (it had Qt's default gaps).
        column.setContentsMargins(10, 0, 10, 0)
        column.setSpacing(8)
        column.addLayout(notice_row)
        column.addLayout(head)
        column.addWidget(self.speed_note)
        column.addWidget(self.bubbles, stretch=1)
        column.addWidget(composer := centred(self.shelf, self.box))       # order 1o D1
        self.shelf.accept_drops_on(composer)     # it hides when empty; a drag shows it

        self.split = QSplitter(Qt.Orientation.Horizontal)
        for part, weight in ((self.sessions, 1), (centre, 4), (self.sources, 2)):
            self.split.addWidget(part)
            self.split.setStretchFactor(self.split.indexOf(part), weight)
        self.split.setChildrenCollapsible(False)
        self.panel.lay_out(page_row(self), self.split)     # UI phase B: one frame for every page

        self.box.submitted.connect(self.question_submitted)
        self.box.stop_requested.connect(self.stop_requested)
        self.box.web_toggled.connect(self.web_toggled)
        self.box.walk_requested.connect(self.sources.walk)
        self.box.escaped.connect(self._back_to_conversation)
        self.box.recall_requested.connect(self._recall)
        self.box.edit_submitted.connect(self.question_edited)
        self.bubbles.starter_chosen.connect(self.box.send_text)
        self.shelf.changed.connect(self.shelf_changed)
        self.shelf.open_requested.connect(self.open_requested)
        self.sources.opened.connect(self._opened)
        self.sources.revealed.connect(self.result_revealed)
        self.sources.error.connect(self.error)
        self.sources.results.pin_requested.connect(self.pin_requested)
        self.sources.results.reindex_requested.connect(self.reindex_requested)
        self.sources.results.similar_requested.connect(self.similar_requested)

    # -- state the controller sets --------------------------------------------
    def focus(self) -> None:
        self.box.focus()

    def shutdown(self) -> None:
        self.sources.shutdown()
        self.closing.emit()

    def set_store(self, store: Any) -> None:
        # 2026-10-04: the preview reads a message from it. 2026-10-08: and the side
        # panel is put back as it was left (`PanelSwitch.restore`).
        self.sources.set_store(store)
        self.panel.restore(store)

    # -- the side panel (2026-10-08): the window's keys and the toolbar ---------------
    def toggle_panel(self) -> None:          # Ctrl+B: the panel away, or back
        self.panel.toggle()

    def toggle_preview(self) -> None:        # Ctrl+Shift+P, as on every list
        self.panel.tab_clicked(PREVIEW_TAB)

    def show_preview(self, on: bool) -> None:   # also the controller's remembered state
        self.panel.toolbar(PREVIEW_TAB, on)

    @property
    def preview(self) -> Any:
        """The Sources column's preview pane, as the other tabs expose theirs."""
        return self.sources.preview

    def show_available(self, ok: bool, reason: str = "", *, built: bool = True) -> None:
        text = "" if ok else (unavailable_text(reason) if built else NOT_BUILT_LINE)
        self.notice.setText(text)
        self.notice.setVisible(not ok)
        self.recheck_button.setVisible(not ok and built)
        self.box.set_unavailable("" if ok else text)
        self.speed.setEnabled(ok)
        # Not the model drop-down: picking another model is how an unavailable one is left.

    def show_speed_note(self, text: str) -> None:
        self.speed_note.setText(text)
        self.speed_note.setVisible(bool(text))

    def set_busy(self, busy: bool) -> None:
        # UI phase B: the list is not locked - the answer is its own conversation's.
        self.box.set_busy(busy)

    def set_web(self, available: bool, on: bool = False) -> None:
        """The Web chip: shown only when Settings allows the web, set for this conversation."""
        self.box.set_web(available, on)

    def _opened(self, row: Any) -> None:
        """A source was opened. A web page opens in the person's browser; a file goes
        to the window, which opens it the way every other page does."""
        path = str(getattr(row, "path", "") or "")
        if path.lower().startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(path))
            return
        self.result_opened.emit(row)

    def open_link(self, href: str) -> None:
        if str(href).lower().startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(href))

    def set_speed(self, value: str) -> None:
        index = self.speed.findData(value)
        if index >= 0:
            self.speed.blockSignals(True)
            self.speed.setCurrentIndex(index)
            self.speed.blockSignals(False)

    # -- the conversation --------------------------------------------------------
    def add_user(self, text: str) -> None:
        self.bubbles.add(self._user_bubble(text))
        self.bubbles.scroll_to_end()

    def _user_bubble(self, text: str) -> UserBubble:
        bubble = UserBubble(text)
        bubble.edit_requested.connect(self.edit_requested)
        return bubble

    def begin_answer(self) -> AnswerRun:
        """A fresh answer bubble; the Sources pane starts again for it."""
        bubble = self._answer_bubble()
        bubble.narrate(THINKING_LINE)                # a slim state before the first word
        self._owner = bubble
        self.sources.clear()
        self.bubbles.add(bubble)
        self._run = AnswerRun(bubble, self.sources, self.shelf)
        return self._run

    def end_answer(self) -> None:
        """The answer is finished: the newest answer takes Regenerate, the newest
        message Edit. One finished while its conversation was not on screen is let go;
        its turn is saved, and opening the conversation draws it."""
        if self._run is not None and self._run.parked:
            self._run.bubble.deleteLater()
            self._run = None
        self.bubbles.refresh_last()

    def _recall(self) -> None:
        """Up in the empty box: the last message comes back to be changed (UI phase B)."""
        users = [b for b in self.bubbles.bubbles() if isinstance(b, UserBubble) and b.is_last]
        if users:
            self.box.begin_edit(users[-1].text)

    def _answer_bubble(self) -> AnswerBubble:
        bubble = AnswerBubble()
        bubble.receipt_activated.connect(lambda n, b=bubble: self._activate(b, n))
        bubble.receipt_hovered.connect(lambda n, b=bubble: self._hovered(b, n))
        bubble.result_opened.connect(self._opened)
        bubble.result_revealed.connect(self.result_revealed)
        bubble.result_selected.connect(self.panel.preview_result)
        bubble.regenerate_requested.connect(self.regenerate_requested)
        bubble.retry_requested.connect(self.retry_requested)
        bubble.link_activated.connect(self.open_link)
        bubble.web_decided.connect(self.web_decided)
        return bubble

    def show_turns(self, turns: list, *, live: bool = False) -> None:
        """Redraw a stored conversation. `live`: it is the one being answered, and
        the answer being written goes back under it, as far as it has got.

        UI phase B (2026-10-13): an answer still being written when another
        conversation is opened is parked (`AnswerRun.park`), not deleted - its
        worker keeps writing into it - so switching mid-answer strands nothing."""
        run = self._run if self._run is not None and not self._run.finished else None
        if run is not None and not run.parked:
            self.bubbles.take(run.bubble)
            run.park(self.shelf.shelf)
        self.bubbles.clear()
        self.sources.clear()
        self._run, self._owner = run, None
        last = None
        for turn in turns:
            if turn.role == "user":
                self.bubbles.add(self._user_bubble(turn.text))
                continue
            bubble = self._answer_bubble()
            AnswerRun(bubble, None, NoShelf()).finish(turn)   # not `run`: that is the parked answer
            self.bubbles.add(bubble)
            last = bubble
        if run is not None and live:
            self.bubbles.add(run.bubble)
            run.resume(self.shelf)
            last = run.bubble
        if last is not None:
            self._show_sources_of(last)
        self.bubbles.refresh_last()
        self.bubbles.scroll_to_end()

    # -- sources -----------------------------------------------------------------
    def _show_sources_of(self, bubble: AnswerBubble) -> None:
        if self._owner is bubble:
            return
        self._owner = bubble
        # An answer still streaming adds its cards to the pane as they come; while
        # an older answer's sources are up it keeps them to itself (`bubble.shown`),
        # and they are all drawn again when its own sources come back.
        if self._run is not None and not self._run.finished:
            self._run.sources = self.sources if bubble is self._run.bubble else None
        self.sources.clear()
        self.sources.set_details(bubble.details)
        for number in sorted(bubble.shown):
            self.sources.add(number, bubble.shown[number])

    def _activate(self, bubble: AnswerBubble, number: int) -> None:
        # A source number in the prose: its card picked, its passage shown, and
        # (2026-10-08) the document itself on the Preview page.
        self._show_sources_of(bubble)
        if self.sources.select_number(number):
            self.panel.show_tab(PREVIEW_TAB)

    def _hovered(self, bubble: AnswerBubble, number: int) -> None:
        # 2026-10-12: wired with its bubble, as a click is. Without it a number in
        # an older answer showed the passage the newest answer gave that number.
        self._show_sources_of(bubble)
        self.sources.hover_number(number)

    def _back_to_conversation(self) -> None:
        self.sources.deselect()
        self.bubbles.scroll_to_end()
        self.box.focus()

