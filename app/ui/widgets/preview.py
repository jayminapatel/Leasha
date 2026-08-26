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
    KIND_HTML,
    KIND_IMAGE,
    KIND_NONE,
    KIND_PDF,
    KIND_TEXT,
    load_preview_for,
)
from app.ui.widgets.highlight import CodeHighlighter, language_for
from app.ui.workers import CallableWorker, run

__all__ = ["PreviewPane", "PREVIEW_DEBOUNCE_MS", "attach_preview"]


def attach_preview(results: Any, on_open: Any, on_error: Any):
    """Build a pane for `results`, wire it, and return `(pane, splitter)`.

    Here rather than in the view because the pane's own docstring is where
    somebody looks to find out how it is used, and because the view it attaches
    to is already at the length a view is allowed to be.

    **The splitter exists whether or not the pane is shown.** Toggling
    visibility is then a repaint rather than a relayout, which is what makes
    `Ctrl+P` feel instant - and the divider somebody dragged is still where they
    left it when the pane comes back.
    """
    pane = PreviewPane()
    pane.open_requested.connect(on_open)
    pane.error.connect(on_error)
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


class PreviewPane(QWidget):
    """Shows the selected result. Draws only; the reading happens elsewhere."""

    #: The person asked to open the file properly, from the pane.
    open_requested = pyqtSignal(object)
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

        # --- the renderers, one per kind, swapped rather than rebuilt
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(False)
        self.text.setOpenLinks(False)
        self.text.setAccessibleName("Preview")
        # **One highlighter, re-pointed per file.** Building one per selection
        # would leak a rule set for every row arrowed past; `setLanguage` is the
        # whole of what changes between one file and the next.
        self._highlighter = CodeHighlighter(self.text.document(), self.palette())

        self.image = QLabel("")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setAccessibleName("Image preview")

        self.card = QLabel("")
        self.card.setWordWrap(True)
        self.card.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.card.setAccessibleName("File details")

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
        for widget in (self.text, self.image, self.card):
            self.stack.addWidget(widget)
        self._pdf = self._make_pdf_view()
        if self._pdf is not None:
            self.stack.addWidget(self._pdf)

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

        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        layout.addWidget(self.notice)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.open_button)

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
        if wanted == self.isVisible():
            return

        self.setVisible(wanted)
        if wanted:
            self.show_row(selected)
        else:
            self.clear()             # stop a render nobody will see

    def clear(self) -> None:
        self._row = None
        self._generation += 1
        self._timer.stop()
        self.title.setText("Nothing selected")
        self.subtitle.setText("Select a result to preview it here.")
        self.notice.setVisible(False)
        self.text.setPlainText("")
        self.stack.setCurrentWidget(self.text)
        self.open_button.setEnabled(False)

    def show_row(self, row: Any) -> None:
        """Queue a preview of `row`. Safe to call on every arrow key."""
        if row is None:
            self.clear()
            return

        self._row = row
        self._generation += 1
        # Named immediately, rendered shortly: the heading must follow the
        # selection at once or the pane looks a step behind the list.
        self.title.setText(str(getattr(row, "name", "") or getattr(row, "path", "")))
        self.subtitle.setText("Loading…")
        self.notice.setVisible(False)
        self.open_button.setEnabled(True)
        self._timer.start()

    def _start(self) -> None:
        if self._row is None:
            return
        generation = self._generation
        worker = CallableWorker(
            load_preview_for, self._row,
            body_provider=self.body_provider,
            notice_provider=self.notice_provider,
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
        elif preview.kind == KIND_IMAGE:
            self._show_image(preview.path)
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
