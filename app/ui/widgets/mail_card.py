r"""The header card above a previewed message: who, to whom, when, what, with what.

Layer: L5 widget

Order 0y section 4a. A message used to be previewed as typed text - `From: ...`,
`To: ...`, a row of dashes, then the body. This draws the same facts the way a
mail program does, so the pane is recognised as an email at a glance: the
subject as a heading, the sender's name large with the address beside it, the
recipients, the date in words, and each attachment as a chip.

**Draws only.** Every word comes from `presenter.mail.MailCard`, decided
without Qt and read on a worker (`preview_loader.mail_preview`). Nothing here
touches a store, the disk or another program.

**Every word can be selected and copied**, like the rest of the pane. And what
Select All and Copy produce from the message underneath is still plain text
with the `From: ...` block on top - `MailBody` sees to that, because the card
is a drawing and a drawing cannot be pasted into a reply.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QMimeData, Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.ui.presenter.mail import RECIPIENTS_LABEL, RECIPIENTS_TIP, MailCard
from app.ui.widgets.flow_layout import FlowLayout

__all__ = ["MailCardView", "MailBody"]

_SELECTABLE = (Qt.TextInteractionFlag.TextSelectableByMouse
               | Qt.TextInteractionFlag.TextSelectableByKeyboard)


def _label(name: str, *, wrap: bool = False) -> QLabel:
    label = QLabel("")
    label.setObjectName(name)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(_SELECTABLE)
    label.setCursor(Qt.CursorShape.IBeamCursor)
    # Plain text, always: a subject or a sender's name comes from a stranger's
    # email, and a label left on automatic would render `<b>` or `<img>` in it.
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class MailCardView(QFrame):
    """The card. `show_card` fills it; an empty part is not drawn."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("mailCard")

        self.subject = _label("mailSubject", wrap=True)
        self.subject.setAccessibleName("Subject")

        self.sender = _label("mailSender")
        self.sender.setAccessibleName("From")
        self.address = _label("resultMeta")
        self.address.setAccessibleName("Sender's address")
        who = QHBoxLayout()
        who.setContentsMargins(0, 0, 0, 0)
        who.setSpacing(8)
        who.addWidget(self.sender)
        who.addWidget(self.address, 1, Qt.AlignmentFlag.AlignBottom)

        self.recipients_row = QWidget()
        to_row = QHBoxLayout(self.recipients_row)
        to_row.setContentsMargins(0, 0, 0, 0)
        to_row.setSpacing(8)
        to_label = QLabel(RECIPIENTS_LABEL)
        to_label.setObjectName("factLabel")
        to_label.setToolTip(RECIPIENTS_TIP)
        self.recipients = _label("factValue", wrap=True)
        self.recipients.setAccessibleName(RECIPIENTS_LABEL)
        self.recipients.setToolTip(RECIPIENTS_TIP)
        to_row.addWidget(to_label, 0, Qt.AlignmentFlag.AlignTop)
        to_row.addWidget(self.recipients, 1)

        self.date = _label("resultMeta")
        self.date.setAccessibleName("Sent")

        self.chips = QWidget()
        self.chips.setAccessibleName("Attachments")
        self._chip_flow = FlowLayout(self.chips, spacing=6)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 8)
        layout.setSpacing(3)
        layout.addWidget(self.subject)
        layout.addLayout(who)
        layout.addWidget(self.recipients_row)
        layout.addWidget(self.date)
        layout.addWidget(self.chips)
        self.setVisible(False)

    def show_card(self, card: MailCard) -> None:
        """Draw `card`. Arithmetic over words already in hand - no I/O."""
        self.subject.setText(card.subject)
        self.sender.setText(card.sender_name)
        self.sender.setVisible(bool(card.sender_name))
        self.address.setText(card.sender_address)
        self.address.setVisible(bool(card.sender_address))
        self.recipients.setText(", ".join(card.recipients))
        self.recipients_row.setVisible(bool(card.recipients))
        self.date.setText(card.date_words)
        self.date.setVisible(bool(card.date_words))
        self._show_chips(card.attachments)
        self.setVisible(True)

    def chip_texts(self) -> list[str]:
        """The attachment chips, as drawn. For a test, and for a screen reader's
        description of the row."""
        return [self._chip_flow.itemAt(n).widget().text()
                for n in range(self._chip_flow.count())]

    def _show_chips(self, names: Any) -> None:
        while self._chip_flow.count():
            item = self._chip_flow.takeAt(0)
            chip = item.widget() if item is not None else None
            if chip is not None:
                chip.hide()
                chip.deleteLater()
        for name in names or ():
            chip = _label("mailChip")
            chip.setText(str(name))
            chip.setToolTip(f"Attached: {name}")
            self._chip_flow.addWidget(chip)
        self.chips.setVisible(bool(names))
        self._chip_flow.invalidate()


class MailBody(QTextBrowser):
    r"""The preview's text area, which copies a message as text with its headers.

    **The card is drawn, so it cannot be copied with the message** - and copying
    a whole message out of the preview is an ordinary thing to do. When the
    selection is the whole document and `copy_header` is set, the text placed on
    the clipboard starts with that block (`From: ...`, `To: ...`, `Sent: ...`,
    `Subject: ...`), exactly as the preview typed it before the card existed. A
    selection of part of the message copies that part and nothing else.

    With `copy_header` empty - every preview that is not a message - this is a
    `QTextBrowser` and nothing more.
    """

    copy_header: str = ""

    def createMimeDataFromSelection(self) -> QMimeData:       # noqa: N802 - Qt's name
        data = super().createMimeDataFromSelection()
        if not self.copy_header:
            return data
        cursor = self.textCursor()
        whole = (cursor.selectionStart() == 0
                 and cursor.selectionEnd() >= self.document().characterCount() - 1)
        if not whole:
            return data
        plain = QMimeData()
        plain.setText(self.copy_header + self.toPlainText())
        return plain
