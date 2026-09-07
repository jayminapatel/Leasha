r"""One render call for images and PDF pages. **Worker thread only.**

Layer: L5. Workspace §2d and §2e.

**One pipeline for both kinds**, which is the order's instruction and is what
makes rotate and zoom two parameters rather than two features. An image and a
PDF page both become a `QImage`; everything downstream — rotating, scaling,
printing, showing — treats them identically and does not ask which it has.

**`QImage`, never `QPixmap`.** Qt refuses to build a pixmap off the interface
thread and crashes on some platforms rather than refusing; `QImage` decodes
anywhere. Converting one to the other on the UI thread afterwards is a wrap,
not a second decode. That split is the M11 pattern this codebase already
follows in `preview_loader.decode_image`, and this is the same rule applied to
a page.

**Nothing here raises.** A page that will not render is an ordinary state — a
damaged PDF, an image that is really HTML, a drive that went to sleep — and
the pane says so in a card. None of it is worth an error dialog, and all of it
happens on a worker where an exception is a lost preview rather than a
traceback.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.logging import logger

__all__ = ["render", "page_count", "PDF_BASE_DPI"]

_log = logger.bind(component="ui.render")

#: What zoom 1.0 means for a PDF. 96 is the number Windows calls 100%, so a
#: page at 1.0 is the size it would print at, which is what "actual size"
#: means to the person holding the paper version.
PDF_BASE_DPI = 96.0

#: Never render bigger than this on either edge. A 4× zoom of an A0 drawing is
#: a hundred million pixels, and the window would ask for it before anybody
#: could stop it. The cap is silent because the alternative — a dialog about
#: raster dimensions — is not a sentence to put in front of somebody looking
#: at a drawing.
MAX_EDGE = 12_000


def render(path: Any, *, kind: str = "image", view: Any = None,
           fit_to: Any = None) -> Optional[Any]:
    """A `QImage` of this file at this rotation and zoom, or None.

    `kind` is `"image"` or `"pdf"`; `view` is a `view_of_file.View`; `fit_to`
    is a `(width, height)` the render should fit inside when the view asks
    for fit.
    """
    from app.ui.view_of_file import View

    found = view if isinstance(view, View) else View()
    try:
        is_pdf = str(kind) == "pdf"
        image = _pdf_page(path, found) if is_pdf else _image(path)
        if image is None or image.isNull():
            return None
        return _shape(image, found, fit_to, pre_zoomed=is_pdf)
    except Exception as exc:                     # noqa: BLE001 - see docstring
        _log.debug("could not render {}: {}", path, exc)
        return None


def page_count(path: Any) -> int:
    """How many pages a PDF has, or 0. Never raises. Worker thread."""
    try:
        # `pymupdf`, which is what the package calls itself now - `fitz` is
        # the legacy alias and `extract/pdf.py` stopped using it for the same
        # reason: an alias that a future release drops is an import that
        # breaks with no warning.
        import pymupdf

        with pymupdf.open(str(path)) as document:
            return int(document.page_count)
    except Exception as exc:                     # noqa: BLE001 - see docstring
        _log.debug("could not count pages in {}: {}", path, exc)
        return 0


# ---------------------------------------------------------------------------
# The two sources
# ---------------------------------------------------------------------------

def _image(path: Any) -> Optional[Any]:
    r"""An image, decoded exactly as `preview_loader.decode_image` decodes it.

    **Reused, not re-derived.** A bare `QImage(str(path))` is what this was
    before the pop-out had its own decode path: no EXIF orientation
    correction (a portrait photo rendered sideways) and no HEIC/HEIF route
    at all (Qt has no plugin for either, so `QImage` alone returns a null
    image). `decode_image` already carries both fixes - see its docstring
    and `_apply_orientation`/`_decode_heif` in `preview_loader.py` - and this
    is the pop-out's own image path, so the fix belongs here once rather
    than twice.
    """
    from app.ui.preview_loader import decode_image

    return decode_image(str(path))


def _pdf_page(path: Any, view: Any) -> Optional[Any]:
    r"""One page of a PDF, rendered to a `QImage`.

    **PyMuPDF rather than `QPdfView`.** §2e replaces the embedded viewer in
    the pop-out with a rendered page precisely so that rotation and zoom are
    the same two parameters an image gets - `QPdfView` has its own zoom, its
    own rotation and its own idea of what a page is, and reconciling three of
    those with one toolbar is how a window ends up with two zooms that
    disagree.

    **The cost is text selection**, and the order says so: PDF text selection
    is scoped out of the pop-out for now. The in-app pane keeps `QPdfView`
    and keeps selection with it, so nothing is lost that anybody had.
    """
    import pymupdf

    from PyQt6.QtGui import QImage

    zoom = 1.0 if view.is_fit else float(view.zoom)
    with pymupdf.open(str(path)) as document:
        if document.page_count <= 0:
            return None
        number = max(0, min(int(view.page), document.page_count - 1))
        page = document.load_page(number)
        matrix = pymupdf.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        # `copy()` because the `QImage` would otherwise reference the samples
        # buffer, which `pymupdf` frees when this block closes - a use-after-free
        # that shows as a corrupt page rather than a crash, some of the time.
        return QImage(pixmap.samples, pixmap.width, pixmap.height,
                      pixmap.stride, QImage.Format.Format_RGB888).copy()


# ---------------------------------------------------------------------------
# Rotation and scale, applied once
# ---------------------------------------------------------------------------

def _shape(image: Any, view: Any, fit_to: Any, *,
           pre_zoomed: bool = False) -> Any:
    """Rotate, then scale. **In that order**, and it matters.

    Scaling first and rotating after would fit the *unrotated* shape into the
    window, so a sideways A4 page rotated upright would come back too wide
    for the pane it was measured against.

    `pre_zoomed` is True for a PDF, whose zoom went into the render matrix -
    scaling it again here would be a second resample of the same pixels and
    would throw away exactly the sharpness the matrix bought. **An image has
    not been zoomed by anything**, and the first version of this forgot that:
    every image rendered at 100% whatever the zoom said, which is a control
    that visibly does nothing.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QTransform

    if view.turn:
        image = image.transformed(QTransform().rotate(view.turn),
                                  Qt.TransformationMode.SmoothTransformation)

    if view.is_fit:
        if not fit_to:
            return _capped(image)
        width, height = int(fit_to[0]), int(fit_to[1])
        if width <= 0 or height <= 0:
            return _capped(image)
        return image.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)

    if not pre_zoomed and abs(float(view.zoom) - 1.0) > 1e-9:
        image = image.scaled(
            max(1, int(image.width() * view.zoom)),
            max(1, int(image.height() * view.zoom)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
    return _capped(image)


def _capped(image: Any) -> Any:
    """Never wider or taller than `MAX_EDGE`. Silent, on purpose."""
    from PyQt6.QtCore import Qt

    if image.width() <= MAX_EDGE and image.height() <= MAX_EDGE:
        return image
    return image.scaled(MAX_EDGE, MAX_EDGE, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
