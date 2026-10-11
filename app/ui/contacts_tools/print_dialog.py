"""Printing (order 1k 6q): address labels, envelopes and a printed address book.

Layer: L5

Three tabs, one preview. **Preview** writes the PDF to a temporary file on a worker and shows its
pages; **Save PDF…** copies it to the file the person chooses. Nothing is saved before the person
has seen the preview, and the preview is of the real file: Qt's PDF renderer when the build has it,
otherwise the layout drawn from where the writer says it put each label (millimetres from the
page's top-left; `PdfLayout`).

* **Address labels**: the sheet (Avery L-series for A4, 5160-family for Letter), which address to
  print (main, home, work, other) and which label to start at on a part-used sheet. Contacts with no
  such address are left out and named.
* **Envelopes**: the size, and the sender picked from the book for the return address.
* **Address book**: A to Z, the whole book or the contacts in view, with or without photos.

The contacts come from the scope list at the top: the whole book, the contacts in view or the
selection.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QScrollArea, QSpinBox, QTabWidget,
    QWidget,
)

from app.contacts.card import display_name
from app.ui.contacts_tools._common import ToolDialog, btn, ink, named, plural
from app.ui.widgets.icons import icon

__all__ = ["PrintDialog", "PrintPreview", "make_pdf", "draw_page", "ADDRESS_CHOICES"]

ADDRESS_CHOICES = (("The main address (or the first)", ""), ("Home", "home"), ("Work", "work"), ("Other", "other"))
_PX_PER_MM = 2.4


@dataclass
class PrintPreview:
    path: str = ""
    page_size_mm: tuple = (210.0, 297.0)
    pages: int = 0
    placements: list = field(default_factory=list)
    texts: dict = field(default_factory=dict)             # (page, index, kind) -> the lines drawn there
    skipped: list[str] = field(default_factory=list)      # names left out for want of an address
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    count: int = 0


def make_pdf(hub: Any, ids: Optional[list[int]], mode: str, out_path: Any, *, sheet: str = "L7163",
             address: str = "", start: int = 1, size: str = "DL", sender_id: Optional[int] = None,
             photos: bool = True) -> PrintPreview:
    """Write the PDF for `mode` ('labels', 'envelopes', 'book') and describe it. Reads the book and
    writes `out_path` only. A worker's job."""
    from app.contacts import printing                        # noqa: PLC0415

    if ids is None:
        cards = [c for _i, c in hub.all_cards()]
    else:
        cards = [c for c in (hub.get_card(i) for i in ids) if c is not None]
    if not cards:
        return PrintPreview(error="There are no contacts to print. Choose some contacts, or clear the filter, and try again.")
    try:
        if mode == "labels":
            lay = printing.labels_pdf(cards, sheet, address, out_path, start_position=start)
        elif mode == "envelopes":
            sender = hub.get_card(sender_id) if sender_id else None
            lay = printing.envelope_pdf(sender, cards, size, out_path)
        elif mode == "book":
            lay = printing.address_book_pdf(cards, out_path, with_photos=photos)
        else:
            return PrintPreview(error=f"Leasha does not know how to print {mode!r}.")
    except (printing.NothingToPrint, ValueError) as exc:
        return PrintPreview(error=str(exc))
    out = PrintPreview(str(out_path), lay.page_size_mm, lay.pages, list(lay.placements), count=len(cards),
                       warnings=list(getattr(lay, "warnings", [])))
    out.skipped = [display_name(cards[i]) or f"Contact {i + 1}" for i in getattr(lay, "skipped", [])]
    sender_card = hub.get_card(sender_id) if (mode == "envelopes" and sender_id) else None
    for p in lay.placements:
        key = (p.page, p.index, p.kind)
        if p.kind == "label":
            out.texts[key] = printing.label_lines(cards[p.index], address, p.h_mm)
        elif p.kind == "recipient":
            out.texts[key] = printing.label_lines(cards[p.index], "", 99.0)
        elif p.kind == "sender" and sender_card is not None:
            out.texts[key] = printing.label_lines(sender_card, "", 99.0)
        elif p.kind == "heading":
            out.texts[key] = [p.text]
        elif 0 <= p.index < len(cards):
            out.texts[key] = [display_name(cards[p.index])]
    return out


def draw_page(preview: PrintPreview, page: int, px_per_mm: float = _PX_PER_MM) -> QPixmap:
    """One page drawn from the layout: the paper, an outline for each label or entry, its text."""
    w_mm, h_mm = preview.page_size_mm
    pixmap = QPixmap(int(w_mm * px_per_mm), int(h_mm * px_per_mm))
    pixmap.fill(QColor("#ffffff"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen_edge, pen_text = QColor("#b8b8b8"), QColor("#202020")
    for p in preview.placements:
        if p.page != page:
            continue
        rect = QRectF(p.x_mm * px_per_mm, p.y_mm * px_per_mm, p.w_mm * px_per_mm, p.h_mm * px_per_mm)
        lines = preview.texts.get((p.page, p.index, p.kind), [])
        if p.kind in ("label", "recipient", "sender"):
            painter.setPen(pen_edge)
            painter.drawRoundedRect(rect, 2, 2)
        font = QFont()
        font.setPixelSize(max(7, int(3.2 * px_per_mm)))
        font.setBold(p.kind == "heading")
        painter.setFont(font)
        painter.setPen(pen_text)
        text = "\n".join(lines)
        painter.drawText(rect.adjusted(3, 2, -3, -2), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                                                          | Qt.TextFlag.TextWordWrap), text)
    painter.end()
    return pixmap


def render_pdf_page(path: str, page: int, width_px: int) -> Optional[QPixmap]:
    """The real PDF page through Qt's renderer, or None when this build has no `QtPdf`."""
    try:
        from PySide6.QtPdf import QPdfDocument               # noqa: PLC0415
    except ImportError:
        return None
    doc = QPdfDocument()
    if doc.load(path) != QPdfDocument.Error.None_ or not 0 <= page - 1 < doc.pageCount():
        return None
    size = doc.pagePointSize(page - 1)
    scale = width_px / max(1.0, size.width())
    image = doc.render(page - 1, QSize(width_px, int(size.height() * scale)))
    doc.close()
    return QPixmap.fromImage(image)


class PrintDialog(ToolDialog):
    def __init__(self, ctx: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(ctx, parent, title="Print contacts", glyph="printer",
                         subtitle="Address labels, envelopes or a printed address book. You see the pages first, then "
                                  "save a PDF to print.",
                         size=(1000, 720), name="contacts-print")
        from app.contacts import printing                     # noqa: PLC0415

        self.hub = ctx.hub
        self.preview: Optional[PrintPreview] = None
        self.page = 1
        self._tmp = Path(tempfile.mkdtemp(prefix="leasha-print-"))
        self._seq = 0
        selected = list(getattr(ctx, "selected_ids", []) or [])
        visible = list(getattr(ctx, "visible_ids", []) or [])

        scope_row = QHBoxLayout()
        label = QLabel("Print:")
        named(label, "Scope label", "Which contacts to print")
        self.scope = QComboBox()
        named(self.scope, "Which contacts", "The whole book, the contacts in view, or the ones you selected")
        self.scope.setToolTip("The whole book, the contacts in view, or the ones you selected")
        self.scope.addItem("The whole book", None)
        if visible:
            self.scope.addItem(f"The contacts in view ({len(visible)})", visible)
        if selected:
            self.scope.addItem(f"The selected contacts ({len(selected)})", selected)
            self.scope.setCurrentIndex(self.scope.count() - 1)
        scope_row.addWidget(label)
        scope_row.addWidget(self.scope, 1)
        self.body.addLayout(scope_row)

        self.tabs = QTabWidget()
        named(self.tabs, "What to print", "Switch between labels, envelopes and the address book")
        self.body.addWidget(self.tabs)

        labels = QWidget()
        form = QFormLayout(labels)
        self.sheet = QComboBox()
        for code, sheet in printing.LABEL_SHEETS.items():
            self.sheet.addItem(sheet.name, code)
        named(self.sheet, "Label sheet", "The make and size of your sticky label sheets")
        self.sheet.setToolTip("The make and size of your sticky label sheets")
        self.address = QComboBox()
        for words, value in ADDRESS_CHOICES:
            self.address.addItem(words, value)
        named(self.address, "Which address", "Which of each contact's addresses to print")
        self.address.setToolTip("Which of each contact's addresses to print")
        self.start = QSpinBox()
        self.start.setMinimum(1)
        named(self.start, "First label to use", "Number of the first label to print on, counting left to right then down. "
                                                 "Use it for a part-used sheet")
        self.sheet.currentIndexChanged.connect(lambda _i: self._sheet_changed())
        form.addRow("Sheet:", self.sheet)
        form.addRow("Address:", self.address)
        form.addRow("Start at label:", self.start)
        self.tabs.addTab(labels, icon("tag", ink()), "Address labels")

        env = QWidget()
        eform = QFormLayout(env)
        self.env_size = QComboBox()
        for code, (w, h) in printing.ENVELOPE_SIZES.items():
            self.env_size.addItem(f"{code} ({w:g} x {h:g} mm)", code)
        named(self.env_size, "Envelope size", "The size of your envelopes")
        self.env_size.setToolTip("The size of your envelopes")
        self.sender = QComboBox()
        self.sender.setEditable(False)
        self.sender.addItem("No return address", None)
        named(self.sender, "Sender", "Pick yourself from your book to print a return address")
        self.sender.setToolTip("Pick yourself from your book to print a return address")
        eform.addRow("Envelope:", self.env_size)
        eform.addRow("Return address from:", self.sender)
        self.tabs.addTab(env, icon("mail", ink()), "Envelopes")

        book = QWidget()
        bform = QFormLayout(book)
        self.photos = QCheckBox("Include photos")
        self.photos.setChecked(True)
        named(self.photos, "Include photos", "Print each contact's photo beside their details")
        self.photos.setToolTip("Print each contact's photo beside their details")
        bform.addRow(self.photos)
        self.tabs.addTab(book, icon("book-open-text", ink()), "Address book")
        self.tabs.currentChanged.connect(lambda _i: self._invalidate())
        for widget in (self.scope, self.sheet, self.address, self.env_size, self.sender):
            widget.currentIndexChanged.connect(lambda _i: self._invalidate())
        self.start.valueChanged.connect(lambda _v: self._invalidate())
        self.photos.toggled.connect(lambda _on: self._invalidate())

        self.page_label = QLabel("No preview yet. Press Preview.")
        named(self.page_label, "Page", "Which page of the preview is shown")
        self.page_view = QLabel()
        self.page_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        named(self.page_view, "Page preview", "How the printed page will look")
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.page_view)
        self.scroll.setWidgetResizable(True)
        named(self.scroll, "Preview area", "The pages that will be saved")
        nav = QHBoxLayout()
        self.prev_button = btn("Previous", "chevron-up", "secondary", "Show the previous page", "Previous page")
        self.next_button = btn("Next", "chevron-down", "secondary", "Show the next page", "Next page")
        self.prev_button.clicked.connect(lambda: self.show_page(self.page - 1))
        self.next_button.clicked.connect(lambda: self.show_page(self.page + 1))
        nav.addWidget(self.prev_button)
        nav.addWidget(self.next_button)
        nav.addWidget(self.page_label, 1)
        self.body.addLayout(nav)
        self.body.addWidget(self.scroll, 1)

        self.preview_button = btn("Preview", "eye", "secondary", "Make the pages and show them here", "Preview")
        self.save_button = btn("Save PDF…", "save", "primary", "Save the previewed pages as a PDF file to print",
                               "Save PDF")
        self.preview_button.clicked.connect(self.make_preview)
        self.save_button.clicked.connect(self.choose_and_save)
        self.add_footer_buttons(self.preview_button, self.save_button)
        self._sheet_changed()
        self._invalidate()
        hub = self.hub
        self.job(lambda: [(r.id, r.display_name) for r in hub.list_rows() if r.display_name][:5000], self._senders)

    # -- options -------------------------------------------------------------
    def _senders(self, rows: list) -> None:
        for cid, name in rows:
            self.sender.addItem(name, cid)

    def _sheet_changed(self) -> None:
        from app.contacts import printing                     # noqa: PLC0415

        sheet = printing.LABEL_SHEETS[self.sheet.currentData()]
        self.start.setMaximum(sheet.per_page)
        self.start.setToolTip(f"Number of the first label to print on (1 to {sheet.per_page}). Use it for a part-used sheet")

    def mode(self) -> str:
        return ("labels", "envelopes", "book")[self.tabs.currentIndex()]

    def _invalidate(self) -> None:
        """An option changed: the preview on screen is no longer what would be saved."""
        self.preview = None
        self.save_button.setEnabled(False)
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)

    # -- preview -------------------------------------------------------------
    def make_preview(self) -> None:
        self._seq += 1
        out = self._tmp / f"preview-{self._seq}.pdf"
        hub, ids, mode = self.hub, self.scope.currentData(), self.mode()
        opts = dict(sheet=self.sheet.currentData(), address=self.address.currentData(), start=self.start.value(),
                    size=self.env_size.currentData(), sender_id=self.sender.currentData(), photos=self.photos.isChecked())
        self.busy(True, "Making the pages…")
        self.preview_button.setEnabled(False)
        self.job(lambda: make_pdf(hub, ids, mode, out, **opts), self._made)

    def _made(self, preview: PrintPreview) -> None:
        self.preview_button.setEnabled(True)
        if preview.error:
            self.show_error(preview.error)
            return
        self.preview = preview
        note = [plural(preview.pages, "page")]
        if preview.skipped:
            shown = ", ".join(preview.skipped[:5]) + ("…" if len(preview.skipped) > 5 else "")
            note.append(f"{plural(len(preview.skipped), 'contact')} left out for want of that address ({shown})")
        if preview.warnings:
            note.append("; ".join(preview.warnings))
        self.busy(False, "Preview ready: " + "; ".join(note) + ". Check it, then Save PDF.")
        self.save_button.setEnabled(True)
        self.show_page(1)

    def show_page(self, page: int) -> None:
        pv = self.preview
        if pv is None:
            return
        page = max(1, min(pv.pages, page))
        self.page = page
        pixmap = render_pdf_page(pv.path, page, 520) or draw_page(pv, page)
        self.page_view.setPixmap(pixmap)
        self.page_label.setText(f"Page {page} of {pv.pages}")
        self.prev_button.setEnabled(page > 1)
        self.next_button.setEnabled(page < pv.pages)

    # -- saving --------------------------------------------------------------
    def choose_and_save(self) -> None:
        if self.preview is None:
            return
        path, _f = QFileDialog.getSaveFileName(self, "Save the PDF as", "contacts.pdf", "PDF (*.pdf)")
        if path:
            self.save_to(path if path.lower().endswith(".pdf") else path + ".pdf")

    def save_to(self, path: str) -> None:
        """Copy the previewed PDF to `path` (a worker). Only possible after a preview."""
        pv = self.preview
        if pv is None:
            return
        src = pv.path
        self.busy(True, "Saving…")
        self.job(lambda: (shutil.copyfile(src, path), path)[1], self._saved)

    def _saved(self, path: str) -> None:
        self.busy(False, f"Saved to {path}. Open it and print from there.")

    def done(self, result: int) -> None:                       # noqa: D401 - Qt override
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().done(result)
