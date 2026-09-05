r"""A document pinned in its own window. Workspace §2.

Layer: L5

**A copy, not a move.** The in-app preview pane stays exactly where it is;
this is an independent, view-only window showing the same document. Multiples
are allowed and expected — comparing two versions of a drawing falls out for
free, and pinning the mail you are answering while you search for what it
mentions is the whole point.

**Its own generation stamp, and that is the load-bearing detail.** A new
search in the main window must never blank a pinned document; that is the
opposite of why it was pinned. Each window counts its own renders and ignores
anything that arrives carrying an older number, exactly as the pane does — but
from a counter nothing else can touch.

**View-only, everywhere.** Nothing here writes to the document. Rotation lives
in the app's own state, keyed by a hash of the path (`view_of_file`); the
bytes on disk are never opened for writing, and a test asserts it.

**Rendering is `render_page`'s job and happens on a worker**, per the M11
pattern: pages and images become a `QImage` off the interface thread, and this
window only ever wraps one in a `QPixmap` and draws it.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QRect, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QPushButton, QScrollArea, QStackedWidget,
    QTextBrowser, QVBoxLayout, QWidget,
)

from app.core.logging import logger
from app.ui.view_of_file import View, read_turn

__all__ = ["PreviewWindow", "GEOMETRY_KEY", "ON_TOP_KEY", "TEXT_ONLY_NOTE"]

_log = logger.bind(component="ui.preview.window")

#: Where a pop-out remembers its size and its pin. One shape for every window,
#: because two of them open on top of each other otherwise.
GEOMETRY_KEY = "ui:preview_window_geometry"
ON_TOP_KEY = "ui:preview_window_on_top"

MIN_WIDTH, MIN_HEIGHT = 520, 400

#: §2g. **One plain sentence**, on the kinds that are shown as text because
#: that is all the extractor produced. Somebody looking at a Word document
#: with no layout needs to know it is *this window's* limitation and not the
#: document's — otherwise the natural conclusion is that the file is damaged.
TEXT_ONLY_NOTE = "Shown as text — open the file for the full layout."

#: How far the font moves per zoom press for the text kinds. §2d asks for a
#: font-size bump rather than a scale: scaling text renders it blurry, and the
#: thing somebody wants from "bigger" in a document is more readable, not
#: larger pixels.
FONT_STEP = 1


class PreviewWindow(QWidget):
    """One document, pinned, view-only."""

    #: `{key: value}` worth remembering — geometry, pin, rotation.
    remember = pyqtSignal(dict)
    #: This window closed. The opener drops its reference.
    closed = pyqtSignal(object)
    #: "Open the real file" / "Show in folder". §2g.
    open_requested = pyqtSignal(str)
    reveal_requested = pyqtSignal(str)

    def __init__(self, row: Any, *, state: Any = None,
                 body_provider: Any = None) -> None:
        # No parent: a parented widget with a window flag still minimises with
        # its owner, and a pinned document that vanishes with the main window
        # is not pinned. The same reasoning `log_window` records.
        super().__init__(None)
        self._row = row
        self._path = str(getattr(row, "path", "") or "")
        self._body_provider = body_provider
        #: **This window's own counter.** Nothing outside can move it, which
        #: is what stops a search in the main window blanking a pinned page.
        self._generation = 0
        self._kind = "none"
        self._image: Any = None
        self._view = View(turn=read_turn(state, self._path),
                          page=int(getattr(row, "page", 0) or 0))

        name = str(getattr(row, "name", "") or self._path or "Preview")
        self.setWindowTitle(name)
        # §2a: the title bar is the filename, the tooltip is the full path.
        # A title bar cannot hold `C:\Users\...\2019\surveys\...`, and the one
        # question somebody has about a pinned window is *which* copy it is.
        self.setToolTip(self._path or name)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)

        self._build()
        self._restore(state)
        self.reload()

    # -- the furniture --------------------------------------------------------

    def _build(self) -> None:
        from app.ui.widgets.find_bar import attach_find

        self.text = QTextBrowser()
        self.text.setOpenLinks(False)
        # §2f. View-only still means copy-out works: a preview you cannot
        # quote from sends people to open the file for a sentence.
        self.text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard)

        self.picture = QLabel("")
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.picture)
        self.scroll.setWidgetResizable(False)

        self.card = QLabel("")
        self.card.setWordWrap(True)
        self.card.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Workspace §4b: a spreadsheet as a grid, pinned like any other file.
        from app.ui.widgets.spreadsheet_view import SpreadsheetView

        self.spreadsheet = SpreadsheetView()

        self.stack = QStackedWidget()
        for widget in (self.text, self.scroll, self.card, self.spreadsheet):
            self.stack.addWidget(widget)

        self.note = QLabel("")
        self.note.setObjectName("resultMissing")
        self.note.setWordWrap(True)
        self.note.setVisible(False)

        self.on_top = QCheckBox("Keep on top")
        self.on_top.setToolTip(
            "Keeps this window in front of everything else, so it stays "
            "visible while you work in another application.")
        self.on_top.toggled.connect(self._pin)

        self.rotate = self._button(
            "Rotate", "Turns the whole document a quarter turn. Remembered "
            "for this file; the file itself is never changed.", self._turn)
        self.zoom_in = self._button("Zoom in", "Show it larger (Ctrl+wheel).",
                                    lambda: self._zoom(out=False))
        self.zoom_out = self._button("Zoom out", "Show it smaller (Ctrl+wheel).",
                                     lambda: self._zoom(out=True))
        self.fit = self._button("Fit width",
                                "Scale it to fit this window.", self._fit)
        self.print_button = self._button(
            "Print", "Prints exactly what is shown, at this rotation. "
            "Choose 'Microsoft Print to PDF' to save it as a PDF instead.",
            self._print)
        self.open_button = self._button(
            "Open the real file", "Opens it in the application that owns it.",
            lambda: self.open_requested.emit(self._path))
        self.reveal_button = self._button(
            "Show in folder", "Opens the folder with this file selected.",
            lambda: self.reveal_requested.emit(self._path))

        bar = QHBoxLayout()
        for widget in (self.rotate, self.zoom_out, self.zoom_in, self.fit,
                       self.print_button):
            bar.addWidget(widget)
        bar.addStretch(1)
        bar.addWidget(self.on_top)

        exits = QHBoxLayout()
        exits.addWidget(self.open_button)
        exits.addWidget(self.reveal_button)
        exits.addStretch(1)

        self.find = attach_find(self, self.text)

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.note)
        layout.addWidget(self.stack, stretch=1)
        layout.addWidget(self.find)
        layout.addLayout(exits)

    def _button(self, label: str, tip: str, on_click: Any) -> QPushButton:
        """Every control states its effect — §6's rule, and §6a's test."""
        button = QPushButton(label)
        button.setToolTip(tip)
        button.clicked.connect(lambda _checked=False: on_click())
        return button

    # -- loading --------------------------------------------------------------

    def reload(self) -> None:
        """Read the document again, at the current rotation and zoom.

        **Every load carries this window's generation.** A reply stamped with
        an older number is one this window has already moved past - a second
        rotate pressed while the first was still rendering - and is dropped.
        """
        from app.ui.preview_loader import load_preview_for
        from app.ui.workers import CallableWorker, run

        self._generation += 1
        generation = self._generation
        worker = CallableWorker(
            load_preview_for, self._row, body_provider=self._body_provider,
            component="ui.preview.window")
        worker.signals.finished.connect(
            lambda preview, g=generation: self._loaded(preview, g))
        worker.signals.failed.connect(
            lambda _error, g=generation: self._show_card(
                "This document could not be read.", g))
        run(QThreadPool.globalInstance(), worker)

    def _loaded(self, preview: Any, generation: int) -> None:
        if generation != self._generation:
            return                               # a later request won
        from app.ui.preview_loader import (
            KIND_HTML, KIND_IMAGE, KIND_MARKDOWN, KIND_PDF, KIND_SPREADSHEET,
        )

        self._kind = str(getattr(preview, "kind", "none"))
        # §2g: say when the layout is missing rather than letting the document
        # look damaged. Only for what was *extracted* to text - a `.txt` file
        # shown as text has no layout to be missing.
        self.note.setVisible(bool(getattr(preview, "meta", {}).get("extracted")))
        self.note.setText(TEXT_ONLY_NOTE)

        if self._kind in (KIND_IMAGE, KIND_PDF):
            self._render()
            return
        if self._kind == KIND_SPREADSHEET:
            # No I/O here either - see the same comment in widgets/preview.py.
            self.spreadsheet.show_sheets(getattr(preview, "meta", {}).get("sheets"))
            self.stack.setCurrentWidget(self.spreadsheet)
            self._enable_picture_controls(False)
            return
        body = str(getattr(preview, "body", "") or "")
        if self._kind == KIND_HTML:
            self.text.setHtml(body)
        elif self._kind == KIND_MARKDOWN:
            # §4a, same as the in-app pane: rendered, not shown as raw markup.
            self.text.document().setMarkdown(body)
        else:
            self.text.setPlainText(body)
        self.stack.setCurrentWidget(self.text)
        self._enable_picture_controls(False)

    def _render(self) -> None:
        """Ask for the page at this rotation and zoom. Worker, always."""
        from app.ui.render_page import render
        from app.ui.workers import CallableWorker, run

        self._generation += 1
        generation = self._generation
        target = (self.stack.width(), self.stack.height())
        worker = CallableWorker(
            render, self._path, kind=self._kind, view=self._view,
            fit_to=target, component="ui.preview.render")
        worker.signals.finished.connect(
            lambda image, g=generation: self._drawn(image, g))
        worker.signals.failed.connect(
            lambda _error, g=generation: self._show_card(
                "This page could not be drawn.", g))
        run(QThreadPool.globalInstance(), worker)

    def _drawn(self, image: Any, generation: int) -> None:
        """UI thread. Wraps the worker's `QImage`; never decodes."""
        if generation != self._generation:
            return
        if image is None:
            self._show_card("This page could not be drawn.", generation)
            return
        pixmap = QPixmap.fromImage(image)
        self.picture.setPixmap(pixmap)
        self.picture.resize(pixmap.size())
        self.stack.setCurrentWidget(self.scroll)
        self._enable_picture_controls(True)

    def _show_card(self, text: str, generation: int) -> None:
        if generation != self._generation:
            return
        self.card.setText(text)
        self.stack.setCurrentWidget(self.card)
        self._enable_picture_controls(False)

    def _enable_picture_controls(self, drawing: bool) -> None:
        """Rotate and fit mean nothing to a wall of text, so they say so.

        Zoom stays on for both: for text it moves the font, which is §2d's
        own instruction and what "bigger" means in a document.
        """
        for button in (self.rotate, self.fit):
            button.setEnabled(bool(drawing))

    # -- the controls ---------------------------------------------------------

    def _turn(self) -> None:
        self._view = self._view.turned()
        self.remember.emit(self._view.as_state(self._path))
        self._render()

    def _zoom(self, *, out: bool) -> None:
        if self._is_picture():
            self._view = self._view.zoomed(out=out)
            self._render()
            return
        # Text: a font bump, not a scale. Scaled text is blurry text, and
        # what somebody wants from "bigger" here is more readable.
        font = self.text.font()
        font.setPointSize(max(6, font.pointSize() + (-FONT_STEP if out
                                                     else FONT_STEP)))
        self.text.setFont(font)

    def _fit(self) -> None:
        self._view = self._view.fitted()
        self._render()

    def _is_picture(self) -> bool:
        from app.ui.preview_loader import KIND_IMAGE, KIND_PDF

        return self._kind in (KIND_IMAGE, KIND_PDF)

    def _pin(self, wanted: bool) -> None:
        """Stay-on-top. `show()` after the flag, or Qt leaves it hidden."""
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(wanted))
        self.show()
        self.remember.emit({ON_TOP_KEY: "true" if wanted else "false"})

    def _print(self) -> None:
        r"""Print what is shown, at the shown rotation. §2c.

        **No separate export is built**, and the order says why: Windows'
        print-to-PDF makes this an export feature for free, and a second
        code path that wrote a file would be the one thing §6 forbids.

        Never raises: no printer, no permission, a dialog cancelled - all
        ordinary, none worth taking the window down for.
        """
        try:
            from PyQt6.QtPrintSupport import QPrintDialog, QPrinter

            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            dialog = QPrintDialog(printer, self)
            if dialog.exec() != QPrintDialog.DialogCode.Accepted:
                return
            if self._is_picture():
                self._print_picture(printer)
            else:
                self.text.document().print(printer)
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.warning("could not print {}: {}", self._path, exc)

    def _print_picture(self, printer: Any) -> None:
        r"""The pixmap on the page, scaled to fit, aspect kept.

        **`QPrinter` is imported here, not borrowed from `_print`.** It was
        imported in the caller and used here, where the name does not exist -
        a `NameError` the moment anybody printed a picture, which under PyQt6
        is not an error message but a dead process. Found by `ruff --select
        F821`, which had never been run over `app/`; `test_no_undefined_names`
        now runs it on every suite.
        """
        from PyQt6.QtGui import QPainter
        from PyQt6.QtPrintSupport import QPrinter

        pixmap = self.picture.pixmap()
        if pixmap is None or pixmap.isNull():
            return
        painter = QPainter(printer)
        try:
            page = printer.pageRect(QPrinter.Unit.DevicePixel).toRect()
            scaled = pixmap.scaled(page.size(),
                                   Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(
                page.x() + (page.width() - scaled.width()) // 2,
                page.y() + (page.height() - scaled.height()) // 2, scaled)
        finally:
            painter.end()

    # -- Qt -------------------------------------------------------------------

    def wheelEvent(self, event: Any) -> None:               # noqa: N802 - Qt's name
        """Ctrl+wheel zooms; a plain wheel scrolls, as it always did."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._zoom(out=event.angleDelta().y() < 0)
            event.accept()
            return
        super().wheelEvent(event)

    def _restore(self, state: Any) -> None:
        """Size and pin, from the app's own state. Never raises."""
        from app.ui.widgets.log_window import geometry_from

        try:
            found = geometry_from((state or {}).get(GEOMETRY_KEY))
            if found is not None:
                self.setGeometry(_offset(found))
            if str((state or {}).get(ON_TOP_KEY, "")).lower() in (
                    "1", "true", "yes"):
                self.on_top.setChecked(True)
        except Exception:                        # noqa: BLE001 - a preference
            return

    def moveEvent(self, event: Any) -> None:                # noqa: N802 - Qt's name
        super().moveEvent(event)
        self._remember_geometry()

    def resizeEvent(self, event: Any) -> None:              # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._remember_geometry()
        if self._view.is_fit and self._is_picture():
            self._render()

    def closeEvent(self, event: Any) -> None:               # noqa: N802 - Qt's name
        self._remember_geometry()
        self.closed.emit(self)
        super().closeEvent(event)

    def _remember_geometry(self) -> None:
        from app.ui.widgets.log_window import geometry_text

        if not self.isVisible():
            return
        text = geometry_text(self.geometry())
        if text:
            self.remember.emit({GEOMETRY_KEY: text})


#: How far each new window is nudged from the last remembered position.
#: Without it, pinning three documents stacks three windows exactly on top of
#: each other and the feature looks broken on its second use.
CASCADE = 28


def _offset(rect: QRect) -> QRect:
    """The remembered box, nudged so a second window is not hidden."""
    return QRect(rect.x() + CASCADE, rect.y() + CASCADE,
                 rect.width(), rect.height())
