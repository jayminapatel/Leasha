"""A line above the results saying the search did a worse job than usual.

Layer: L5 (UI)

The standing rule, from the owner: **nothing fails silently.** The backend
detects degradation and puts it on `SearchResponse.notices`; the CLI prints it;
until now the window - the only interface the owner actually uses - said
nothing.

That is the failure this whole widget exists for. A search returning sixty
keyword hits and no vector hits looks *identical* to one that worked. The person
gets fewer, worse results and no reason, so they conclude the corpus is thin
rather than that half the engine is dead. There was a warning all along, in the
log, where nobody reads it.

**Its own widget, not a line in `search_view`.** Two reasons and both matter:
`search_view.py` sits at 249 of the 250 code lines the presenter guard allows,
and Files and Mail need exactly this too - the owner's other standing rule is
that a feature helping one search area is applied to the others.

**Warn colour, never the summary's.** `#resultsSummary` is `text_faint`, which
is what "20 results in 240ms" uses. A notice rendered in that is a notice that
looks like ordinary chrome, and `#statWarn` exists in `theme.py` precisely
because a figure the code had decided was worth warning about was rendering
identically to one that was fine. Repeating that here would be making the same
mistake inside the fix for it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from app.ui.presenter import notice_line
from app.ui.theme import SPACE_XS, SPACE_S

__all__ = ["NoticeBar"]

_FIRST_LINK = re.compile(r"<a\s[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)


class NoticeBar(QWidget):
    """Zero or more notices, or nothing at all when the search was healthy."""

    dismissed = Signal()
    #: A link inside a notice was clicked, with its `href`.
    chosen = Signal(str)

    def __init__(self, parent: Any | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("noticeBar")

        self.label = QLabel("")
        # **`#statWarn`, not `#resultsSummary`.** See the module docstring.
        self.label.setObjectName("statWarn")
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Preferred)
        # Selectable: the message names a command to run, and a message you
        # cannot copy is a message you have to retype.
        # **Links, because one of these notices is an offer.** The kind-word
        # suggestion is only honest if applying it is one visible click; a
        # sentence telling somebody to retype their query is not an offer.
        self.label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.label.setOpenExternalLinks(False)
        self.label.linkActivated.connect(self.chosen.emit)
        # On one line (`retain_line`) a link past the cut would be out of reach, so
        # the notice's first link is repeated here, beside the dismiss button.
        self.action = QLabel("")
        self.action.setObjectName("statWarn")
        self.action.setTextFormat(Qt.TextFormat.RichText)
        self.action.setOpenExternalLinks(False)
        self.action.linkActivated.connect(self.chosen.emit)
        self.action.hide()
        self._one_line = False

        self.close_button = QPushButton("Dismiss")
        self.close_button.setToolTip(
            "Hide this notice. It says nothing about the search itself, and the "
            "same notice returns if the same thing happens again.")
        self.close_button.setObjectName("noticeDismiss")
        self.close_button.setAccessibleName("Dismiss this notice")
        self.close_button.setFlat(True)
        from app.ui.widgets.icons import icon                       # noqa: PLC0415 - the same x as every close
        from app.ui.theme import theme_colours                      # noqa: PLC0415

        self.close_button.setIcon(icon("x", theme_colours().get("text_dim", "#888888")))
        self.close_button.clicked.connect(self._dismiss)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, SPACE_XS)
        row.setSpacing(SPACE_S)
        row.addWidget(self.label, 1)
        row.addWidget(self.action, 0)
        row.addWidget(self.close_button, 0)

        self.setAccessibleName("Search notices")
        self.hide()

    # -- the only method a view needs ---------------------------------------

    def show_notices(self, notices: Iterable[Any]) -> None:
        """Draw these, or hide if there are none.

        **Branches on `code`, never on `message`.** The contract is that the
        wording is free to change and the code is not, and `app/ui/` has a test
        asserting the UI does not parse error strings to decide anything. This
        widget does not decide anything at all - it draws what it is given -
        but `codes` is exposed so a view can.
        """
        items = list(notices or ())
        self.codes = tuple(getattr(n, "code", "") for n in items)

        # **The wording is decided in `presenter.notice_line`, which is
        # Qt-free and therefore tested.** This method is plumbing: what it
        # decides is whether to be visible, and that follows from the text.
        text = notice_line(items)
        self.label.setText(text)
        # On one line (`retain_line`) the end of a long notice is cut off, so
        # the whole of it is one hover away. Harmless where the bar wraps.
        self.setToolTip(text)
        self.setVisible(bool(text))
        self._place_action()

    def _place_action(self) -> None:
        """Show the first link beside the bar when the one line cuts the text short."""
        link = _FIRST_LINK.search(self.label.text()) if self._one_line else None
        if link is None:
            self.action.hide()
            return
        plain = QTextDocument()
        plain.setHtml(self.label.text())
        cut = self.label.fontMetrics().horizontalAdvance(plain.toPlainText()) > self.label.width()
        self.action.setText(link.group(0))
        self.action.setVisible(cut)

    def resizeEvent(self, event: Any) -> None:                  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._place_action()

    def retain_line(self) -> None:
        """One line, the same height shown or hidden - UI phase B, 2026-10-13.

        For a bar that sits above a list somebody is reading: a notice arriving
        after the rows were painted (a filter offer from its worker, the
        finished tier's spelling note) used to grow the bar and push every row
        down. Kept to one line and its height kept while hidden, it cannot.
        """
        self._one_line = True
        self.label.setWordWrap(False)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.layout().setContentsMargins(0, 0, 0, 0)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        policy.setRetainSizeWhenHidden(True)
        self.setSizePolicy(policy)

    def _dismiss(self) -> None:
        """Hide, and say so - a view may stop re-showing the same notice."""
        self.hide()
        self.dismissed.emit()
