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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import QRect, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QPushButton, QScrollArea, QStackedWidget,
    QTextBrowser, QVBoxLayout, QWidget,
)

from app.core.logging import logger
from app.ui.presenter.opening import key_of
from app.ui.view_of_file import View, read_turn
from app.ui.widgets.buttons import style_all

__all__ = ["PreviewWindow", "GEOMETRY_KEY", "ON_TOP_KEY", "TEXT_ONLY_NOTE",
           "DWG_PREVIEW_ENABLED_KEY", "enabled_checkbox", "pop_out", "set_describe_options"]

_log = logger.bind(component="ui.preview.window")

#: Where a pop-out remembers its size and its pin. One shape for every window,
#: because two of them open on top of each other otherwise.
GEOMETRY_KEY = "ui:preview_window_geometry"
ON_TOP_KEY = "ui:preview_window_on_top"

#: Workspace §5c's off switch. §6: every new behaviour ships on and
#: individually off-able. Same `index_state` shape as the three switches §3
#: added (`ui:drag_out_enabled` and friends), read the same defensive way, on
#: by default. Read when a window opens, so turning it off stops Leasha
#: offering to run LibreDWG on the next drawing anybody pins.
DWG_PREVIEW_ENABLED_KEY = "ui:dwg_preview_enabled"

#: Describe's settings for every pop-out, told by the window
#: (`set_describe_options`); these are the settings' own defaults until then.
_DESCRIBE: dict = {"ollama_url": "http://127.0.0.1:11434",
                   "ollama_vision_model": "llava", "chat_engine": "onnx"}


def set_describe_options(*, ollama_url: str, ollama_vision_model: str,
                         chat_engine: str) -> None:
    """The window's Describe settings, for every pop-out it or the lightbox opens."""
    _DESCRIBE.update(ollama_url=ollama_url, ollama_vision_model=ollama_vision_model,
                     chat_engine=chat_engine)


def pop_out(row: Any, *, store: Any, state: Any, on_error: Any, remember: Any,
            closed: Any, body_provider: Any = None, siblings: Any = (),
            index: int = 0) -> "PreviewWindow":
    """Open `row` in a window of its own, shown. A pinned document and the
    lightbox are both built here (2026-10-04) - they had drifted: the lightbox
    opened a bare path and had no Describe settings. Open and Show in folder
    take the row on show to the one route (`workers.open_row_async`)."""
    from app.ui.workers import open_row_async

    window = PreviewWindow(row, state=state, body_provider=body_provider,
                           siblings=siblings, index=index, store=store)
    window.remember.connect(remember)
    window.open_requested.connect(
        lambda shown: open_row_async(store, shown, on_error=on_error))
    window.reveal_requested.connect(
        lambda shown: open_row_async(store, shown, reveal=True, on_error=on_error))
    window.closed.connect(closed)
    window.show()
    window.raise_()
    return window

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
    #: "Open the real file" / "Show in folder". §2g. 2026-10-04: they carry
    #: the ROW on show, not its path, so the one open route (`pop_out`) can
    #: resolve its drive, its moment and its line as every page does.
    open_requested = pyqtSignal(object)
    reveal_requested = pyqtSignal(object)

    def __init__(self, row: Any, *, state: Any = None,
                 body_provider: Any = None, siblings: Any = (),
                 index: int = 0, store: Any = None,
                 ollama_url: Optional[str] = None,
                 ollama_vision_model: Optional[str] = None,
                 chat_engine: Optional[str] = None) -> None:
        # No parent: a parented widget with a window flag still minimises with
        # its owner, and a pinned document that vanishes with the main window
        # is not pinned. The same reasoning `log_window` records.
        super().__init__(None)
        self._row = row
        # The whole path: a Code row's `path` is shortened for its column (2026-10-04).
        self._path = key_of(row)
        #: What `_render` actually reads. Equal to `self._path` until §4e's
        #: "Show full layout" swaps it for a cached converted PDF - `_path`
        #: itself never changes, because "Open the real file" and "Show in
        #: folder" have to keep pointing at the original document.
        self._display_path = self._path
        self._body_provider = body_provider
        #: **This window's own counter.** Nothing outside can move it, which
        #: is what stops a search in the main window blanking a pinned page.
        self._generation = 0
        self._kind = "none"
        self._image: Any = None
        #: Work order 0h §3b: the lightbox. Empty/single-item means no
        #: navigation at all - a document pinned from anywhere other than the
        #: thumbnail grid or the results list carries no siblings, and the
        #: arrow keys fall through to whatever they always did (scrolling).
        self._siblings = list(siblings) if siblings else []
        self._index = int(index) if self._siblings else 0
        #: Kept so `_navigate` can re-read a sibling's own remembered
        #: rotation the same way `__init__` reads this one's, below.
        self._state = state
        #: §5c's off switch, read once from the state this window was opened
        #: with - the same snapshot `_restore` reads geometry and the pin from.
        self._drawings_enabled = _flag(state, DWG_PREVIEW_ENABLED_KEY,
                                       default=True)
        # Work order 0i section 3a. `store` is None for any caller that has
        # not been updated to pass one (the test `Row`/harness included) -
        # the Describe button simply stays hidden then, the same "inert
        # until wired" shape `body_provider` already has. Never a required
        # parameter: a pop-out that cannot reach the database must still
        # open, view-only, exactly as it always has.
        self._store = store
        # Left out, the window's own (`set_describe_options`) - so the lightbox,
        # which had none, describes with the same settings a pinned window does.
        self._ollama_url = ollama_url or _DESCRIBE["ollama_url"]
        self._ollama_vision_model = ollama_vision_model or _DESCRIBE["ollama_vision_model"]
        #: `CHAT_ENGINE` (2026-09-29): Describe is Florence-2 inside Leasha
        #: unless this says `ollama`. Told, like the address, never read from disk.
        self._chat_engine = chat_engine or _DESCRIBE["chat_engine"]
        self._describe_file_id = getattr(row, "file_id", None)
        self._describe_generation = 0

        self._view = View(turn=read_turn(state, self._path),
                          page=int(getattr(row, "page", 0) or 0))

        self.setWindowTitle(self._title_for(row))
        # §2a: the title bar is the filename, the tooltip is the full path.
        # A title bar cannot hold `C:\Users\...\2019\surveys\...`, and the one
        # question somebody has about a pinned window is *which* copy it is.
        self.setToolTip(self._path or self.windowTitle())
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)

        self._build()
        self._restore(state)
        self.reload()
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)

    def _title_for(self, row: Any) -> str:
        """The filename, plus "(2 of 5)" when this window can navigate.

        Work order 0h §3b's only UI surface for "you can press an arrow key
        here" - a lightbox with no visible position indicator reads as a
        single photo with an accidental keyboard shortcut, not as a browser.
        """
        name = str(getattr(row, "name", "") or self._path or "Preview")
        if len(self._siblings) > 1:
            name = f"{name}  ({self._index + 1} of {len(self._siblings)})"
        return name

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

        # Workspace §4c: an EPUB as chapters, pinned like any other file.
        from app.ui.widgets.epub_view import EpubView

        self.epub = EpubView()

        self.stack = QStackedWidget()
        for widget in (self.text, self.scroll, self.card, self.spreadsheet,
                       self.epub):
            self.stack.addWidget(widget)

        self.note = QLabel("")
        self.note.setObjectName("resultMissing")
        self.note.setWordWrap(True)
        self.note.setVisible(False)

        # Workspace §4e: the better answer 2g's sentence points at. Hidden
        # until a text-rendered Office/ODF preview arrives *and* a converter
        # is detected - see `_loaded`.
        self.full_layout_button = self._button(
            "Show full layout",
            "Converts this document to PDF through LibreOffice and shows "
            "the real layout here - rotate, zoom and print all work on it. "
            "Converted once and cached; opening it again is instant.",
            self._show_full_layout)
        self.full_layout_button.setVisible(False)

        # Workspace §5c. Hidden until a drawing arrives *and* LibreDWG is on
        # this machine *and* the switch is on - see `_loaded`.
        self.simplified_button = self._button(
            "Show simplified view",
            "Draws this drawing here so you can see which one it is - the "
            "lines and the text, without hatching or dimensions. Drawn once "
            "and kept; opening it again is instant, and the drawing itself is "
            "never changed.",
            self._show_simplified)
        self.simplified_button.setVisible(False)

        # Work order 0i section 3a. Hidden until this preview turns out to be
        # a photo *and* a store was given to write the result into - see
        # `_loaded`. Its tooltip is replaced once the availability check
        # comes back, the same deferred-tooltip shape §6's rule already
        # tolerates for full_layout_button/simplified_button above.
        self.describe_button = self._button(
            "Describe",
            "Asks a local AI vision model to describe this photo in a "
            "sentence or two - a few seconds, and only for this one photo. "
            "Cached afterwards: opening it again shows the same words "
            "instantly, with no AI model asked twice.",
            self._describe)
        self.describe_button.setVisible(False)

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
            lambda: self.open_requested.emit(self._row))
        self.reveal_button = self._button(
            "Show in folder", "Opens the folder with this file selected.",
            lambda: self.reveal_requested.emit(self._row))
        self._offer_folder()

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

        note_row = QHBoxLayout()
        note_row.addWidget(self.note, 1)
        note_row.addWidget(self.full_layout_button)
        # Beside §4e's, never with it: one is offered for a document shown as
        # text, the other for a drawing, and no file is both.
        note_row.addWidget(self.simplified_button)
        note_row.addWidget(self.describe_button)

        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addLayout(note_row)
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
            KIND_EPUB, KIND_HTML, KIND_IMAGE, KIND_MARKDOWN, KIND_PDF,
            KIND_SPREADSHEET, OFFICE_CONVERTER_MISSING_NOTE,
        )

        self._kind = str(getattr(preview, "kind", "none"))
        # A fresh load starts from the original file - §4e's swap to a cached
        # PDF applies only until the next reload, exactly like rotation and
        # zoom apply only to what is currently on screen.
        self._display_path = self._path
        meta = getattr(preview, "meta", {}) or {}
        # §2g: say when the layout is missing rather than letting the document
        # look damaged. Only for what was *extracted* to text - a `.txt` file
        # shown as text has no layout to be missing.
        extracted = bool(meta.get("extracted"))
        self.note.setVisible(extracted)
        self.note.setText(TEXT_ONLY_NOTE)
        # §4e: the better answer 2g's own sentence points at. Offered only
        # where §2g's sentence already appears, and only when a converter is
        # actually there to run - `office_converter_available` was asked on
        # the worker that built this preview, never here.
        can_convert = extracted and bool(meta.get("office_converter_available"))
        self.full_layout_button.setVisible(can_convert)
        self.full_layout_button.setEnabled(True)
        if extracted and not can_convert:
            self.note.setText(f"{TEXT_ONLY_NOTE} {OFFICE_CONVERTER_MISSING_NOTE}")

        # §5c: a drawing. Its own sentence rather than §2g's - a `.dwg` is not
        # a document shown as text with its layout missing, it is a drawing
        # that has not been drawn yet, and the loader already wrote the line
        # that says which of the two reasons applies.
        drawing = bool(meta.get("drawing"))
        # Offered only where it could work: a drawing, LibreDWG present (asked
        # on the worker that built this preview, never here), and the switch
        # this window opened with turned on.
        can_draw = (drawing and self._drawings_enabled
                    and bool(meta.get("dwg_preview_available")))
        self.simplified_button.setVisible(can_draw)
        self.simplified_button.setEnabled(True)
        if drawing:
            # The loader's sentence points the *pane* at "Pin in a window",
            # which is the window somebody is already looking at - so where
            # the button is offered, the button is the answer and the sentence
            # would be one instruction to do what has already been done.
            notice = "" if can_draw else str(getattr(preview, "notice", "") or "")
            self.note.setText(notice)
            self.note.setVisible(bool(notice))

        # Work order 0i section 3a. Only for a photo, and only where a store
        # was given to persist the result into - see `__init__`'s note.
        self.describe_button.setVisible(
            self._kind == KIND_IMAGE and self._store is not None)
        if self._kind == KIND_IMAGE and self._store is not None:
            self._check_describe()

        if self._kind in (KIND_IMAGE, KIND_PDF):
            self._render()
            return
        if self._kind == KIND_SPREADSHEET:
            # No I/O here either - see the same comment in widgets/preview.py.
            self.spreadsheet.show_sheets(getattr(preview, "meta", {}).get("sheets"))
            self.stack.setCurrentWidget(self.spreadsheet)
            self._enable_picture_controls(False)
            return
        if self._kind == KIND_EPUB:
            self.epub.show_chapters(getattr(preview, "meta", {}).get("chapters"))
            self.stack.setCurrentWidget(self.epub)
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

    def _show_full_layout(self) -> None:
        """§4e: convert once through the existing LibreOffice converter
        route, cached beside the index. Worker, always - a real conversion
        can take seconds, and this button is pressed on the UI thread.
        """
        from app.ui.preview_loader import ensure_office_pdf
        from app.ui.workers import CallableWorker, run

        # Disabled rather than hidden: hiding it mid-conversion reads as the
        # button having done nothing, when it is working.
        self.full_layout_button.setEnabled(False)
        self._generation += 1
        generation = self._generation
        worker = CallableWorker(
            ensure_office_pdf, self._path, component="ui.preview.window")
        worker.signals.finished.connect(
            lambda preview, g=generation: self._full_layout_ready(preview, g))
        worker.signals.failed.connect(
            lambda _error, g=generation: self._show_card(
                "The full layout could not be prepared.", g))
        run(QThreadPool.globalInstance(), worker)

    def _full_layout_ready(self, preview: Any, generation: int) -> None:
        """UI thread. A converted PDF (cached, or just produced), or an
        error - never a traceback for a document somebody merely clicked a
        button on."""
        if generation != self._generation:
            return                               # a later request won
        self.full_layout_button.setEnabled(True)
        error = getattr(preview, "error", None)
        if error is not None:
            self._show_card(error.render(), generation)
            return

        from app.ui.preview_loader import KIND_PDF

        self._kind = KIND_PDF
        self._display_path = str(getattr(preview, "path", "") or self._display_path)
        self.full_layout_button.setVisible(False)
        self.note.setVisible(False)
        self._render()

    def _show_simplified(self) -> None:
        """§5c: draw this drawing, once, through LibreDWG's `dwg2SVG`.

        Worker, always - it starts a program, and this is a button press on
        the interface thread. Exactly §4e's shape one function above, for the
        same reason and with the same generation discipline.
        """
        from app.ui.preview_loader import ensure_dwg_svg
        from app.ui.workers import CallableWorker, run

        # Disabled rather than hidden: hiding it mid-conversion reads as the
        # button having done nothing, when it is working.
        self.simplified_button.setEnabled(False)
        self._generation += 1
        generation = self._generation
        worker = CallableWorker(
            ensure_dwg_svg, self._path, component="ui.preview.window")
        worker.signals.finished.connect(
            lambda preview, g=generation: self._simplified_ready(preview, g))
        worker.signals.failed.connect(
            lambda _error, g=generation: self._show_card(
                "This drawing could not be drawn.", g))
        run(QThreadPool.globalInstance(), worker)

    def _simplified_ready(self, preview: Any, generation: int) -> None:
        """UI thread. A cached SVG, or the drawing's own release line back
        again with a sentence saying why there is no picture - §5c's fallback,
        never a traceback and never a silent nothing."""
        if generation != self._generation:
            return                               # a later request won
        self.simplified_button.setEnabled(True)

        from app.ui.preview_loader import KIND_IMAGE

        notice = str(getattr(preview, "notice", "") or "")
        self.note.setText(notice)
        self.note.setVisible(bool(notice))

        if str(getattr(preview, "kind", "")) != KIND_IMAGE:
            # The fallback: the release line, in the text view it was already
            # showing, with the notice above saying what happened.
            self.text.setPlainText(str(getattr(preview, "body", "") or ""))
            self.stack.setCurrentWidget(self.text)
            self._enable_picture_controls(False)
            return

        self._kind = KIND_IMAGE
        self._display_path = str(getattr(preview, "path", "") or self._display_path)
        self.simplified_button.setVisible(False)
        self._render()

    # -- Describe (work order 0i section 3a) ----------------------------------

    def _check_describe(self) -> None:
        """Is Describe usable right now? Worker, always - `health()` is a
        network call, and non-negotiable #5 is the UI thread never does I/O.

        Re-run on every load, not cached on the window: Ollama can be started
        or a model pulled while a pop-out sits open, and a greyed button that
        never re-checks is a worse failure than the extra request.
        """
        from app.ui.workers import CallableWorker, run

        if self._store is None or self._describe_file_id is None:
            return
        self._describe_generation += 1
        generation = self._describe_generation
        worker = CallableWorker(
            _describe_status, self._store, self._describe_file_id,
            self._ollama_url, self._ollama_vision_model, self._chat_engine,
            component="ui.preview.describe")
        worker.signals.finished.connect(
            lambda status, g=generation: self._describe_status_ready(status, g))
        # A failed check leaves the button as `_loaded` set it - visible,
        # disabled reads as broken; visible and clickable at least offers a
        # retry, and `_describe` itself never raises into the UI either way.
        run(QThreadPool.globalInstance(), worker)

    def _describe_status_ready(self, status: "_DescribeStatus", generation: int) -> None:
        if generation != self._describe_generation:
            return                               # a later check won
        if status.already_described:
            self.describe_button.setEnabled(False)
            self.describe_button.setToolTip(
                "Already described - the words are in the text above.")
            return
        self.describe_button.setEnabled(status.available)
        if not status.available:
            self.describe_button.setToolTip(status.reason)

    def _describe(self) -> None:
        """Ask the vision model, once, and cache the answer. Worker, always."""
        from app.ui.workers import CallableWorker, run

        if self._store is None or self._describe_file_id is None:
            return
        self.describe_button.setEnabled(False)
        self.describe_button.setToolTip("Asking the AI model to describe this photo...")
        self._describe_generation += 1
        generation = self._describe_generation
        worker = CallableWorker(
            _describe_and_store, self._store, self._describe_file_id,
            self._display_path, self._ollama_url, self._ollama_vision_model,
            self._chat_engine, component="ui.preview.describe")
        worker.signals.finished.connect(
            lambda caption, g=generation: self._describe_done(caption, g))
        worker.signals.failed.connect(
            lambda _error, g=generation: self._describe_failed(g))
        run(QThreadPool.globalInstance(), worker)

    def _describe_done(self, caption: Optional[str], generation: int) -> None:
        if generation != self._describe_generation:
            return
        if caption is None:
            self._describe_failed(generation)
            return
        self.describe_button.setEnabled(False)
        self.describe_button.setToolTip(
            "Already described - the words are in the text above.")
        # The new chunk is part of this file's stored text now - a fresh
        # load shows it exactly the way any other labelled segment shows,
        # with no bespoke rendering path to keep in step.
        self.reload()

    def _describe_failed(self, generation: int) -> None:
        if generation != self._describe_generation:
            return
        self.describe_button.setEnabled(True)
        self.describe_button.setToolTip(
            "That did not work - the photo may be unreadable, or Ollama "
            "stopped answering. Try again.")

    def _render(self) -> None:
        """Ask for the page at this rotation and zoom. Worker, always."""
        from app.ui.render_page import render
        from app.ui.workers import CallableWorker, run

        self._generation += 1
        generation = self._generation
        target = (self.stack.width(), self.stack.height())
        worker = CallableWorker(
            render, self._display_path, kind=self._kind, view=self._view,
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

    def keyPressEvent(self, event: Any) -> None:             # noqa: N802 - Qt's name
        """Left/right walks the sibling set - work order 0h §3b, the lightbox.

        Only claimed when there is more than one sibling to move between;
        with none (every pop-out before this order, and any pinned from
        somewhere that does not know its own result set) the keys fall
        through to Qt's ordinary handling, unchanged.
        """
        if len(self._siblings) > 1 and event.key() in (
                Qt.Key.Key_Left, Qt.Key.Key_Right):
            self._navigate(-1 if event.key() == Qt.Key.Key_Left else 1)
            event.accept()
            return
        super().keyPressEvent(event)

    def _navigate(self, delta: int) -> None:
        """Move to the next/previous sibling, wrapping at either end.

        Wraps rather than stopping: a lightbox that dead-ends at the last
        photo makes somebody reach for the mouse to go back to the start,
        which is the one motion arrow keys exist to remove.

        Rebuilds exactly the state `__init__` built for the first photo -
        title, tooltip, rotation - then calls `reload()`, which already owns
        bumping the generation and dropping anything still in flight for the
        photo being left. No new load path; the existing one, for a
        different row.
        """
        self._index = (self._index + delta) % len(self._siblings)
        row = self._siblings[self._index]
        self._row = row
        self._path = key_of(row)
        self._display_path = self._path
        self._offer_folder()
        self._view = View(turn=read_turn(self._state, self._path),
                          page=int(getattr(row, "page", 0) or 0))
        self.setWindowTitle(self._title_for(row))
        self.setToolTip(self._path or self.windowTitle())
        self.reload()

    def _offer_folder(self) -> None:
        """2026-09-30: "Show in folder" only for a row that is a real file. A
        pinned message out of a mail archive has an address (`pst://...`) for
        a path and no file of its own; the button used to be on for it and
        ask Explorer for something that is not there."""
        from app.ui.presenter.rows import file_of_row

        self.reveal_button.setEnabled(bool(file_of_row(self._row)))

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


def _flag(state: Any, key: str, *, default: bool) -> bool:
    """One boolean out of an `index_state` snapshot. Never raises.

    The same spellings `result_tools._read_flag` accepts, because a preference
    written by one and read by the other must mean the same thing in both.
    """
    if not state:
        return default
    raw = state.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("off", "0", "false", "no")


@dataclass(frozen=True, slots=True)
class _DescribeStatus:
    """What `_check_describe`'s worker found. Plain data, so it crosses the
    worker/UI-thread boundary the same way every other `CallableWorker`
    result does - no Qt object touched off the UI thread."""

    available: bool
    reason: str
    already_described: bool


def _describe_client(ollama_url: str, ollama_model: str, engine: str) -> Any:
    """Worker thread: what Describe asks. `CHAT_ENGINE` (2026-09-29) decides -
    Florence-2 inside Leasha by default, or the Ollama vision model."""
    from types import SimpleNamespace

    from app.llm.engines import vision_model

    return vision_model(SimpleNamespace(chat_engine=engine), url=ollama_url, model=ollama_model)


def _describe_status(store: Any, file_id: int, ollama_url: str,
                      ollama_model: str, engine: str = "onnx") -> "_DescribeStatus":
    """Work order 0i section 3a. Runs off the UI thread - see `_check_describe`.

    Module-level, not a method: `CallableWorker` runs it in a thread pool
    thread, and a bound method would still work, but a plain function makes
    it obvious nothing here reaches back into the widget.
    """
    from app.extract.vision_caption import available, unavailable_reason

    try:
        already = bool(store.has_ai_caption(int(file_id)))
    except Exception:                             # noqa: BLE001 - a check, never a crash
        already = False
    client = _describe_client(ollama_url, ollama_model, engine)
    ok = available(client)
    return _DescribeStatus(
        available=ok, reason="" if ok else unavailable_reason(client),
        already_described=already)


def _describe_and_store(store: Any, file_id: int, path: str, ollama_url: str,
                         ollama_model: str, engine: str = "onnx") -> Optional[str]:
    """Work order 0i section 3a. Runs off the UI thread - see `_describe`.

    Returns the caption on success, `None` on anything else - a bad photo,
    Ollama going down mid-request, or the model refusing. `describe_image`
    already never raises; this stays defensive anyway because a worker
    result reaching `_describe_failed` must never be a traceback.
    """
    from app.extract.vision_caption import describe_image

    client = _describe_client(ollama_url, ollama_model, engine)
    try:
        result = describe_image(Path(path), client)
        if result is None:
            return None
        store.add_caption_chunk(int(file_id), result.caption)
        return result.caption
    except Exception:                             # noqa: BLE001 - one click, not a crash
        return None


def enabled_checkbox(store: Any, *, on_toggle: Any) -> QCheckBox:
    """§5c's off switch, in the shape the other four already use.

    On by default, read once from `index_state`, never raising against a
    locked or missing database - `timeline_strip.enabled_checkbox` and
    `pinned_panel.enabled_checkbox` word-for-word, one key along.
    """
    box = QCheckBox("Drawing previews")
    box.setToolTip(
        "Offer to draw a DWG drawing in its own window so you can see which "
        "one it is. Turn it off and drawings show their file name and "
        "AutoCAD version only."
    )
    try:
        raw = (store.get_state(DWG_PREVIEW_ENABLED_KEY, None)
               if store is not None else None)
    except Exception:                            # noqa: BLE001 - a preference
        raw = None
    box.setChecked(_flag({DWG_PREVIEW_ENABLED_KEY: raw},
                         DWG_PREVIEW_ENABLED_KEY, default=True))
    box.toggled.connect(on_toggle)
    return box
