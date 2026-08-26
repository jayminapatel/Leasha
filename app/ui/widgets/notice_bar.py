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

from collections.abc import Iterable
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from app.ui.presenter import notice_line

__all__ = ["NoticeBar"]


class NoticeBar(QWidget):
    """Zero or more notices, or nothing at all when the search was healthy."""

    dismissed = pyqtSignal()
    #: A link inside a notice was clicked, with its `href`.
    chosen = pyqtSignal(str)

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

        self.close_button = QPushButton("Dismiss")
        self.close_button.setToolTip(
            "Hide this notice. It says nothing about the search itself, and the "
            "same notice returns if the same thing happens again.")
        self.close_button.setObjectName("noticeDismiss")
        self.close_button.setAccessibleName("Dismiss this notice")
        self.close_button.setFlat(True)
        self.close_button.clicked.connect(self._dismiss)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 4)
        row.setSpacing(8)
        row.addWidget(self.label, 1)
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
        self.setVisible(bool(text))

    def _dismiss(self) -> None:
        self.hide()
        self.dismissed.emit()
