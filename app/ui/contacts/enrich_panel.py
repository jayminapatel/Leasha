"""Enrichment on a contact's page: "Enrich this contact", Look again, and what was turned down.

Layer: L5

Order 1s 7c and 7e. **Enrich this contact** asks for suggestions for this one contact only and
**Look again** does the same while asking the internet sources even if they were asked within the
repeat interval; both go through the page's one `EnrichRun` (the page does it, on a worker), and
the suggestions open in Enrich contacts to accept - nothing is written from here. **Suggestions
you turned down** lists what "Never suggest these" remembered for this contact, each with **Undo**,
which lets that value be suggested again. An email address turned down is not kept as text
(`app.contacts.enrich.declines`), so it is listed as "an email address".

Rule 5: the list is read, and each Undo written, on a worker (`loader.run_task`).
"""

from __future__ import annotations

import time
from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.ui.contacts import loader
from app.ui.theme import SPACE_XS, theme_colours
from app.ui.widgets.buttons import action_button, button_row
from app.ui.widgets.icons import icon

__all__ = ["EnrichPanel", "declined_words"]


def declined_words(row: Any) -> str:
    """One turned-down value in words: "Job title: Buyer (signature, 12 Oct 2026)"."""
    from app.ui.contacts_tools._common import KIND_LABELS, SCALAR_LABELS  # noqa: PLC0415 - the editor's words

    slot = str(row.field_key)
    kind = slot.split(":", 1)[-1]
    if slot == "photo":
        name = "Photo"
    elif slot.startswith("s:"):
        name = SCALAR_LABELS.get(kind, row.label or kind)
    else:
        name = KIND_LABELS.get(kind, row.label or kind)
    value = row.shown or ("an email address (the address itself is not kept)" if kind == "email" else "a value")
    when = time.strftime("%d %b %Y", time.localtime(row.at)).lstrip("0") if row.at else ""
    said = ", ".join(x for x in (row.source, when) if x)
    return f"{name}: {value}" + (f" ({said})" if said else "")


class EnrichPanel(QWidget):
    """Under a contact in the editor: ask enrichment about this contact, and undo a "Never suggest"."""

    #: (contact id, force) - the page runs it, for this contact only.
    enrich_requested = Signal(int, bool)
    error = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.hub: Any = None
        self.cid: Optional[int] = None
        self._gen = 0
        self.setAccessibleName("Enrichment")
        heading = QWidget()
        head = QHBoxLayout(heading)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(SPACE_XS)
        pic = QLabel()
        pic.setPixmap(icon("wand-sparkles", theme_colours().get("text_dim", "#888888")).pixmap(16, 16))
        title = QLabel("Enrichment")
        title.setObjectName("contactSectionTitle")
        head.addWidget(pic)
        head.addWidget(title)
        head.addStretch(1)
        self.enrich_button = action_button("Enrich this contact", "wand-sparkles", "secondary")
        self.enrich_button.setToolTip("Look for missing details for this contact only - nothing is kept until "
                                      "you accept it")
        self.enrich_button.setAccessibleName("Enrich this contact")
        self.again_button = action_button("Look again", "refresh-cw", "secondary")
        self.again_button.setToolTip("Look for this contact again, asking the internet sources you switched on "
                                     "even if they were asked recently")
        self.again_button.setAccessibleName("Look again for this contact")
        self.enrich_button.clicked.connect(lambda: self._ask(False))
        self.again_button.clicked.connect(lambda: self._ask(True))
        self.declined_title = QLabel("Suggestions you turned down")
        self.declined_title.setObjectName("resultsSummary")
        self.declined_title.setAccessibleName("Suggestions you turned down")
        self.declined_body = QVBoxLayout()
        self.declined_body.setContentsMargins(0, 0, 0, 0)
        self.declined_body.setSpacing(SPACE_XS)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SPACE_XS)
        outer.addWidget(heading)
        outer.addLayout(button_row(self.enrich_button, self.again_button))
        outer.addWidget(self.declined_title)
        outer.addLayout(self.declined_body)
        self.undo_buttons: list[Any] = []
        self._labels: list[QLabel] = []
        self.clear()

    def configure(self, hub: Any) -> None:
        self.hub = hub

    def clear(self) -> None:
        self._gen += 1
        self.cid = None
        self._wipe()
        self.setVisible(False)

    def show_for(self, cid: int) -> None:
        if self.hub is None:
            return
        self._gen += 1
        gen = self._gen
        self.cid = cid
        self.setVisible(True)
        store = self.hub.store
        from app.contacts.enrich import declines  # noqa: PLC0415

        loader.run_task(self, lambda: declines.for_contact(store, cid), lambda rows: self._landed(rows, gen),
                        component="ui.contacts.enrich")

    def declined_texts(self) -> list[str]:
        return [label.text() for label in self._labels]

    def _ask(self, force: bool) -> None:
        if self.cid is not None:
            self.enrich_requested.emit(int(self.cid), bool(force))

    def _wipe(self) -> None:
        while self.declined_body.count():
            item = self.declined_body.takeAt(0)
            row = item.widget()
            if row is not None:
                row.deleteLater()
        self.undo_buttons, self._labels = [], []
        self.declined_title.setVisible(False)

    def _landed(self, rows: list, gen: int) -> None:
        if gen != self._gen:
            return
        self._wipe()
        self.declined_title.setVisible(bool(rows))
        for r in rows:
            line = QWidget()
            lay = QHBoxLayout(line)
            lay.setContentsMargins(0, 0, 0, 0)
            text = QLabel(declined_words(r))
            text.setWordWrap(True)
            text.setAccessibleName(text.text())
            undo = action_button("Undo", "undo-2", "secondary")
            undo.setToolTip("Let enrichment suggest this value for this contact again")
            undo.setAccessibleName(f"Undo: {text.text()}")
            undo.clicked.connect(lambda _c=False, row=r: self._undo(row))
            lay.addWidget(text, 1)
            lay.addWidget(undo)
            self.declined_body.addWidget(line)
            self.undo_buttons.append(undo)
            self._labels.append(text)

    def _undo(self, row: Any) -> None:
        store, cid = self.hub.store, self.cid
        from app.contacts.enrich import declines  # noqa: PLC0415

        loader.run_task(self, lambda: declines.undo(store, row.contact_id, row.field_key, row.value_hash),
                        lambda _n: self.show_for(cid) if cid is not None and cid == self.cid else None,
                        component="ui.contacts.enrich")
