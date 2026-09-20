r"""The preview pane: read a result without opening the application that owns it.

Layer: L5

Off by default, `Ctrl+P` or the View menu, and persisted like every other view
preference.

**No `QWebEngineView`, and the reason is not performance.** It is a full
Chromium - multi-process, a GPU process, hundreds of megabytes - which would
reverse the founding decision of V2: one process, no services. Worse, it was
proposed for *HTML email*, which means remote image loading, which means
fifteen-year-old tracking pixels phoning home the moment somebody arrows past a
result. "Nothing leaves this machine" would quietly stop being true. Email is
sanitised by `app/ui/sanitise.py` and rendered by `QTextBrowser`, which cannot
run script and is never given a remote URL to fetch.

**Nothing is read on the interface thread.** `preview_loader` does the reading
on a worker; this only draws what comes back. Three things follow from that and
each is a rule the pane keeps:

* **Debounced.** Holding the down arrow through fifty results must queue one
  render, not fifty.
* **Cancelled.** A render that lands after the selection moved on is dropped.
  Painting it would put the wrong document beside the right row - which is
  worse than showing nothing, because it looks right.
* **Never fatal.** A failure is an `AppError` rendered as a sentence in the
  pane. Not a dialog, not a traceback, not a crash for a file somebody merely
  scrolled past.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QFontDatabase, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.core.logging import logger
from app.ui.preview_loader import (
    decode_image,
    KIND_EPUB,
    KIND_HTML,
    KIND_IMAGE,
    KIND_MARKDOWN,
    KIND_NONE,
    KIND_PDF,
    KIND_SPREADSHEET,
    KIND_TEXT,
    load_preview_for,
)
from app.ui.widgets.epub_view import EpubView
from app.ui.widgets.highlight import CodeHighlighter, language_for
from app.ui.widgets.spreadsheet_view import SpreadsheetView
from app.ui.workers import CallableWorker, run

__all__ = ["PreviewPane", "PREVIEW_DEBOUNCE_MS", "attach_preview"]


def attach_preview(results: Any, on_open: Any, on_error: Any, *, store: Any = None):
    """Build a pane for `results`, wire it, and return `(pane, splitter)`.

    Here rather than in the view because the pane's own docstring is where
    somebody looks to find out how it is used, and because the view it attaches
    to is already at the length a view is allowed to be.

    **The splitter exists whether or not the pane is shown.** Toggling
    visibility is then a repaint rather than a relayout, which is what makes
    `Ctrl+P` feel instant - and the divider somebody dragged is still where they
    left it when the pane comes back.

    `store` is Offline Media §3b: a row on a catalogued volume previews from
    the index when its drive is not connected, and resolving that needs the
    store. `None` (every caller that predates this, and any test's bare
    stand-in) leaves a volume-backed row unresolved exactly as before -
    additive, never a new failure mode for an existing caller.
    """
    pane = PreviewPane()
    pane.open_requested.connect(on_open)
    pane.error.connect(on_error)
    # §5b: "Show in folder" from the pane takes the results view's own
    # reveal path, so the window handles both the same way.
    reveal = getattr(results, "reveal_requested", None)
    if reveal is not None:
        pane.reveal_requested.connect(reveal.emit)
    pane.store = store
    results.selected.connect(pane.show_row)

    split = QSplitter(Qt.Orientation.Horizontal)
    split.addWidget(results)
    split.addWidget(pane)
    split.setStretchFactor(0, 3)
    split.setStretchFactor(1, 2)
    split.setChildrenCollapsible(False)
    pane.setVisible(False)
    return pane, split

_log = logger.bind(component="ui.preview")

#: Stillness before a render starts. Long enough that arrowing through a list
#: renders once at the end rather than at every step, short enough that a
#: deliberate click feels immediate.
PREVIEW_DEBOUNCE_MS = 200


def _theme_palette(widget: QWidget) -> Any:
    """The palette the code colours are mixed from: the *theme's* ground and ink.

    `widget.palette()` is the operating system's - nothing here sets one, the
    window is themed by stylesheet - so with the theme forced to dark on a
    light-mode machine the highlighter chose dark ink for a dark page and the
    numbers and keywords all but vanished (seen in the dark golden). The same
    trap `ResultDelegate.paint` documents; the same cure, the sheet's tokens.
    """
    from PyQt6.QtGui import QColor, QPalette

    from app.ui.theme import theme_colours

    colours = theme_colours()
    palette = QPalette(widget.palette())
    palette.setColor(QPalette.ColorRole.Base, QColor(colours["surface"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(colours["text"]))
    return palette


class PreviewPane(QWidget):
    """Shows the selected result. Draws only; the reading happens elsewhere."""

    #: The person asked to open the file properly, from the pane.
    open_requested = pyqtSignal(object)
    #: §5b: "Show in folder" from the pane - the same request the results'
    #: context menu makes, routed the same way by `attach_preview`.
    reveal_requested = pyqtSignal(object)
    #: Workspace §2: pin this document in a window of its own. Carries the
    #: pane's `body_provider` with it, because a mail row has no file on disk
    #: and the provider is the only thing that can read the message - §2h's
    #: "no special casing beyond the synthetic-path load that already exists".
    pop_out_requested = pyqtSignal(object, object)
    error = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._row: Any = None
        #: Incremented per request. A result carrying an older number is a
        #: render the selection has already moved past - see `_rendered`.
        self._generation = 0
        #: Optional, set by the view. Text for a row that carries none of its
        #: own - Mail reads the message from the store, because a PST is a
        #: hundred thousand messages in one file and there is nothing on disk to
        #: open for any one of them. **Called on the worker**, never here.
        self.body_provider: Any = None
        #: Optional, set by the view. A sentence about what the body is *not*
        #: showing - Mail uses it to say that a quoted thread was stripped at
        #: index time, which is otherwise invisible and makes a reply read as a
        #: message sent with no context. Called on the worker, like the body.
        self.notice_provider: Any = None
        #: Optional, set by `attach_preview`. Offline Media §3b: resolving a
        #: catalogued-volume row - online, through its current mount point;
        #: offline, to the stored text in the index - needs the store, and
        #: nothing else in this pane otherwise touches one.
        self.store: Any = None
        #: §5c: animate open/close. Pushed by the window from `ui:motion`;
        #: off unless somebody turned it on.
        self.motion: bool = False
        self._animation: Any = None

        self.title = QLabel("")
        self.title.setObjectName("resultName")
        self.title.setWordWrap(True)

        self.subtitle = QLabel("")
        self.subtitle.setObjectName("resultMeta")
        self.subtitle.setWordWrap(True)

        self.notice = QLabel("")
        self.notice.setObjectName("resultMissing")
        self.notice.setWordWrap(True)
        self.notice.setVisible(False)

        # UI Redesign (202626160950 §5a): the facts header. Filled from
        # `inspector.preview_facts`, Qt-free; rows it has no value for are
        # simply not drawn. Lives between the subtitle and the content.
        from PyQt6.QtWidgets import QGridLayout

        self.facts = QWidget()
        self.facts.setObjectName("inspectorFacts")
        self._facts_grid = QGridLayout(self.facts)
        self._facts_grid.setContentsMargins(0, 4, 0, 8)
        self._facts_grid.setHorizontalSpacing(14)
        self._facts_grid.setVerticalSpacing(3)
        self.facts.setVisible(False)

        # --- the renderers, one per kind, swapped rather than rebuilt
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(False)
        self.text.setOpenLinks(False)
        self.text.setAccessibleName("Preview")
        # **One highlighter, re-pointed per file.** Building one per selection
        # would leak a rule set for every row arrowed past; `setLanguage` is the
        # whole of what changes between one file and the next.
        self._highlighter = CodeHighlighter(self.text.document(), _theme_palette(self))

        self.image = QLabel("")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setAccessibleName("Image preview")

        self.card = QLabel("")
        self.card.setWordWrap(True)
        self.card.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.card.setAccessibleName("File details")

        # Workspace §4b: a spreadsheet as a grid, sheet tabs and all.
        self.spreadsheet = SpreadsheetView()
        # Workspace §4c: an EPUB as chapters, not a flattened wall of text.
        self.epub = EpubView()

        # **Every word in the pane can be selected and copied.** Asked for:
        # *"in the preview pane you should be able to select and copy"*.
        #
        # `QTextBrowser` already allowed it; the four `QLabel`s did not, and a
        # label is where the title, the file details and the "why this is not
        # showing" sentence live. A pane you can read but not copy from is the
        # wrong half of a preview - the usual reason to look at one is to take
        # a name, a path or a line out of it.
        #
        # Mouse *and* keyboard: the second is what makes Ctrl+C work without a
        # drag, and this application's specification requires keyboard-only
        # operation end to end.
        selectable = (Qt.TextInteractionFlag.TextSelectableByMouse
                      | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        for label in (self.title, self.subtitle, self.notice, self.card):
            label.setTextInteractionFlags(selectable)
            label.setCursor(Qt.CursorShape.IBeamCursor)

        self.stack = QStackedWidget()
        for widget in (self.text, self.image, self.card, self.spreadsheet,
                       self.epub):
            self.stack.addWidget(widget)
        self._pdf = self._make_pdf_view()
        if self._pdf is not None:
            self.stack.addWidget(self._pdf)

        # §5b: the three actions carry icons (the window tints them via
        # `retint`); their labels are the strings they always were, plus
        # "Show in folder", which reuses the context menu's own wording.
        self.reveal_button = QPushButton("Show in folder")
        self.reveal_button.setToolTip("Open the folder this file is in, with the file selected")
        self.reveal_button.setEnabled(False)
        self.reveal_button.clicked.connect(
            lambda _c=False: self._row is not None and self.reveal_requested.emit(self._row))

        self.pop_button = QPushButton("Pin in a window")
        self.pop_button.setToolTip(
            "Opens this document in its own window you can keep beside your "
            "work. Searching again here will not change it.")
        self.pop_button.setEnabled(False)
        self.pop_button.clicked.connect(
            lambda _c=False: self._row is not None
            and self.pop_out_requested.emit(self._row, self.body_provider))

        self.open_button = QPushButton("Open")
        self.open_button.setToolTip("Open the file in the application that owns it")
        self.open_button.clicked.connect(
            lambda _c=False: self._row is not None and self.open_requested.emit(self._row))

        # **Debounced, not immediate.** Arrowing down a list of fifty results
        # otherwise starts fifty reads, forty-nine of which nobody sees.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(PREVIEW_DEBOUNCE_MS)
        self._timer.timeout.connect(self._start)

        # **Workspace §2b, and the order asks for it here as well as in the
        # pop-out**: *"Also wire the same find into the in-app preview pane;
        # it is the last metre of every search."* One implementation, attached
        # twice, so the keystroke cannot behave two ways.
        #
        # Over `self.text` only, and that is honest rather than partial: an
        # image has no text to find, and the PDF view is Qt's own with its own
        # search. Ctrl+F over those does nothing rather than something odd.
        from app.ui.widgets.find_bar import attach_find

        # The main window's Escape closes this bar (`MainWindow._clear_search`); a
        # second Escape shortcut here made the two ambiguous and neither fired.
        self.find = attach_find(self, self.text, window_escape=False)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        layout.addWidget(self.facts)
        layout.addWidget(self.notice)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.find)

        buttons = QHBoxLayout()
        self.open_button.setProperty("primary", True)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.reveal_button)
        buttons.addWidget(self.pop_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.clear()

    # -- lifecycle -----------------------------------------------------------

    def shutdown(self) -> None:
        """Stop the debounce and stale anything in flight - see workers."""
        from app.ui.workers import stop_timers

        stop_timers(self)

    def _make_pdf_view(self) -> Optional[QWidget]:
        """Qt's own PDF view, when this build of Qt has it and it will build.

        `QtPdfWidgets` ships separately from `QtWidgets` and is absent on some
        installations, so PDFs fall back to the file card - which still names
        the file and offers to open it, most of the value.

        **Every failure is caught, not only `ImportError`.** This ran during
        `MainWindow.__init__`, so anything raised here took down the whole
        application before a window appeared - and it did: `QPdfView()` needs a
        parent in PyQt6, and the missing argument turned an optional preview
        that is off by default into a program that would not start. An optional
        component must never be able to do that, whatever goes wrong inside it.
        """
        try:
            from PyQt6.QtPdf import QPdfDocument
            from PyQt6.QtPdfWidgets import QPdfView

            view = QPdfView(self)
            self._pdf_document = QPdfDocument(self)
            view.setDocument(self._pdf_document)
            view.setAccessibleName("PDF preview")
            return view
        except Exception as exc:                 # noqa: BLE001 - see the docstring
            _log.debug("no PDF preview ({}); PDFs will show the file card", exc)
            return None

    # -- selection -----------------------------------------------------------

    def apply_preference(self, prefs: Any, selected: Any = None) -> None:
        """Show or hide from a `ViewPreferences`, previewing what is selected.

        **Previewing on show matters.** Without it the pane opens empty against
        a list with a row highlighted, which reads as the preview being broken
        rather than as nothing having been selected since it appeared.
        """
        wanted = bool(getattr(prefs, "preview", False))
        # The pane's own flag, not `isVisible()`: on the Search home state the
        # whole results pane is out of sight, `isVisible()` is False for a pane
        # that was asked to show, and switching it off again then returned
        # early - leaving it to reappear the moment results arrived.
        if wanted == (not self.isHidden()):
            return

        if self.motion and self._animate(wanted):
            pass                      # visibility is set by the animation
        else:
            self.setVisible(wanted)
        if wanted:
            self.show_row(selected)
        else:
            self.clear()             # stop a render nobody will see

    def _animate(self, wanted: bool) -> bool:
        """§5c: slide the splitter over 160ms. Returns False when there is no
        splitter to animate, so the caller falls back to a plain toggle."""
        from PyQt6.QtCore import QEasingCurve, QVariantAnimation

        split = self.parentWidget()
        if not isinstance(split, QSplitter) or split.indexOf(self) < 0:
            return False
        me = split.indexOf(self)
        sizes = split.sizes()
        total = sum(sizes) or split.width()
        if wanted:
            self.setVisible(True)
            target = split.sizes()
            if target[me] <= 0:
                target[me] = total * 2 // 5
                target[1 - me] = total - target[me]
            start, end = 0, target[me]
        else:
            start, end = sizes[me], 0
        anim = QVariantAnimation(self)
        anim.setDuration(160)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(start)
        anim.setEndValue(end)

        def step(value: Any) -> None:
            v = int(value)
            new = list(split.sizes())
            new[me] = v
            new[1 - me] = max(0, total - v)
            split.setSizes(new)

        anim.valueChanged.connect(step)
        if not wanted:
            anim.finished.connect(lambda: self.setVisible(False))
        self._animation = anim
        anim.start()
        return True

    def clear(self) -> None:
        self._row = None
        self._generation += 1
        self._timer.stop()
        self.title.setText("Nothing selected")
        self.subtitle.setText("Select a result to preview it here.")
        self.notice.setVisible(False)
        self._show_facts(())
        self.text.setPlainText("")
        self.stack.setCurrentWidget(self.text)
        self.open_button.setEnabled(False)
        self.reveal_button.setEnabled(False)
        self.pop_button.setEnabled(False)

    def show_row(self, row: Any) -> None:
        """Queue a preview of `row`. Safe to call on every arrow key."""
        if row is None:
            self.clear()
            return

        self._row = row
        self._generation += 1
        # A highlight from the last document painted over this one would be
        # nonsense, and a count of matches in a file nobody is looking at any
        # more is worse.
        self.find.clear()
        # Named immediately, rendered shortly: the heading must follow the
        # selection at once or the pane looks a step behind the list.
        self.title.setText(str(getattr(row, "name", "") or getattr(row, "path", "")))
        self.subtitle.setText("Loading…")
        self.notice.setVisible(False)
        from app.ui.inspector import preview_facts
        self._show_facts(preview_facts(row))
        self.open_button.setEnabled(True)
        self.reveal_button.setEnabled(bool(getattr(row, "path", "")))
        self.pop_button.setEnabled(True)
        self._timer.start()

    def _show_facts(self, facts: Any) -> None:
        """§5a: redraw the facts grid; hidden when there is nothing to say."""
        while self._facts_grid.count():
            item = self._facts_grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for n, (label, value) in enumerate(facts):
            key = QLabel(label)
            key.setObjectName("factLabel")
            val = QLabel(value)
            val.setObjectName("factValue")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                        | Qt.TextInteractionFlag.TextSelectableByKeyboard)
            self._facts_grid.addWidget(key, n, 0, Qt.AlignmentFlag.AlignTop)
            self._facts_grid.addWidget(val, n, 1)
        self.facts.setVisible(bool(facts))

    def retint(self, colours: dict) -> None:
        """§0.3: icons on the three buttons, in the palette's text colour."""
        from app.ui.widgets.icons import icon
        self._highlighter.setPalette(_theme_palette(self))
        self.open_button.setIcon(icon("external-link", colours.get("rail_on", "#ffffff")))
        self.reveal_button.setIcon(icon("folder-open", colours.get("text_dim", "#888888")))
        self.pop_button.setIcon(icon("bookmark", colours.get("text_dim", "#888888")))

    def _start(self) -> None:
        if self._row is None:
            return
        generation = self._generation
        worker = CallableWorker(
            load_preview_for, self._row,
            body_provider=self.body_provider,
            notice_provider=self.notice_provider,
            store=self.store,
            component="ui.preview",
        )
        worker.signals.finished.connect(
            lambda preview, g=generation: self._rendered(preview, g))
        worker.signals.failed.connect(
            lambda error, g=generation: self._failed(error, g))
        run(QThreadPool.globalInstance(), worker)

    # -- drawing -------------------------------------------------------------

    def _rendered(self, preview: Any, generation: int) -> None:
        if generation != self._generation:
            return                  # the selection moved on; this is stale

        self.subtitle.setText(preview.subtitle or "")
        notice = preview.notice
        if preview.truncated:
            notice = (notice + "  " if notice else "") + (
                "Showing the beginning of the file - it is longer than the "
                "preview reads."
            )
        self.notice.setText(notice)
        self.notice.setVisible(bool(notice))

        if preview.error is not None:
            self._show_card(preview.error.render())
            return

        if preview.kind == KIND_HTML:
            # `setHtml` on a document with no remote references left in it.
            # No highlighting: the document carries its own formatting, and a
            # regex painting keywords over an email would be vandalism.
            self._highlight_as(None)
            self.text.setHtml(preview.body)
            self.stack.setCurrentWidget(self.text)
        elif preview.kind == KIND_TEXT:
            # Set the language *before* the text: `setPlainText` triggers a
            # rehighlight, and doing it the other way round paints the file
            # twice - once with the previous file's grammar.
            self._highlight_as(preview.path)
            self.text.setPlainText(preview.body)
            self.stack.setCurrentWidget(self.text)
        elif preview.kind == KIND_MARKDOWN:
            # Workspace §4a. `setMarkdown` rather than `setPlainText`: a `.md`
            # file is prose with structure in it, and showing the literal `#`
            # and `**` characters is showing the markup rather than the
            # document. No highlighter - the document supplies its own
            # formatting, exactly as the HTML branch above already argues.
            self._highlight_as(None)
            self.text.document().setMarkdown(preview.body)
            self.stack.setCurrentWidget(self.text)
        elif preview.kind == KIND_IMAGE:
            self._show_image(preview.path)
        elif preview.kind == KIND_SPREADSHEET:
            # No I/O here - `preview.meta["sheets"]` is a list of `SheetGrid`
            # the worker already built; this is arithmetic over data in hand.
            self.spreadsheet.show_sheets(preview.meta.get("sheets"))
            self.stack.setCurrentWidget(self.spreadsheet)
        elif preview.kind == KIND_EPUB:
            # Same as above - every chapter's HTML was already sanitised on
            # the worker.
            self.epub.show_chapters(preview.meta.get("chapters"))
            self.stack.setCurrentWidget(self.epub)
        elif preview.kind == KIND_PDF and self._pdf is not None:
            self._show_pdf(preview.path, preview.page)
        else:
            self._show_card(
                f"{preview.title}\n{preview.subtitle}\n\n"
                "No preview for this type. Open it to read it."
            )

    def _failed(self, error: Any, generation: int) -> None:
        """A worker that raised. Reported in the pane, never as a dialog."""
        if generation != self._generation:
            return
        self._show_card(getattr(error, "render", lambda: str(error))())
        self.error.emit(error)

    def _highlight_as(self, path: Optional[str]) -> None:
        """Point the highlighter at a grammar, and set a font to match.

        **Monospace only for code.** Proportional text is easier to read and is
        right for everything else; code is the one case where column alignment
        carries meaning, so the font follows the grammar rather than being set
        once. An extension with no grammar gets neither - see `language_for`
        for why guessing is worse than leaving it alone.

        Taken from the path rather than from a field on `Preview`: a mail body
        arrives with a synthetic path and no extension, which is exactly the
        case that should end up unhighlighted.
        """
        suffix = (path or "").replace("\\", "/").rpartition("/")[2]
        language = language_for(suffix.rpartition(".")[2]) if "." in suffix else None
        self._highlighter.setLanguage(language)
        self.text.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            if language else self.font()
        )

    def _show_card(self, text: str) -> None:
        self.card.setText(text)
        self.stack.setCurrentWidget(self.card)

    def _show_image(self, path: str) -> None:
        r"""Draw an already-decoded image, or start decoding it.

        **`QPixmap(path)` reads and decodes the file on the calling thread**,
        and this is a slot - so a 40-megapixel scan, or any image on a network
        share, froze the window for as long as the decode took. The pane's own
        docstring promises the opposite, and every other slow thing in this
        widget already goes through a worker.

        `QPixmap` may only be constructed on the UI thread, but `QImage` may be
        decoded anywhere, so the split is: worker decodes to `QImage`, this
        converts it - which is a wrap, not a re-decode.
        """
        self._generation += 1
        generation = self._generation
        worker = CallableWorker(decode_image, path, component="ui.preview.image")
        worker.signals.finished.connect(
            lambda image, g=generation: self._draw_image(image, g))
        worker.signals.failed.connect(
            lambda _e, g=generation: self._draw_image(None, g))
        run(QThreadPool.globalInstance(), worker)

    def _draw_image(self, image: Any, generation: int) -> None:
        """UI thread. `image` is a `QImage` the worker decoded, or None."""
        if generation != self._generation:
            return                               # a later preview won
        pixmap = QPixmap.fromImage(image) if image is not None else QPixmap()
        if pixmap.isNull():
            self._show_card("This image could not be read.")
            return
        # Scaled to the pane, smoothly, and never enlarged past its own size -
        # a 64px icon blown up to fill a pane looks like a rendering fault.
        target = self.stack.size()
        self.image.setPixmap(pixmap.scaled(
            target, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        self.stack.setCurrentWidget(self.image)

    def _show_pdf(self, path: str, page: int) -> None:
        try:
            self._pdf_document.load(path)
            if page > 0:
                navigator = self._pdf.pageNavigator()
                if navigator is not None:
                    from PyQt6.QtCore import QPointF

                    # The matching page first: opening a 400-page report at
                    # page one, when the hit is on page 312, is a preview of
                    # the wrong thing.
                    navigator.jump(max(0, page - 1), QPointF(0, 0))
        except Exception as exc:                 # noqa: BLE001 - never fatal
            _log.debug("could not preview {}: {}", path, exc)
            self._show_card("This PDF could not be opened for preview.")
            return
        self.stack.setCurrentWidget(self._pdf)
