r"""RAW camera images: extract and OCR the embedded JPEG preview.

Layer: L2

**Why not decode the full RAW?** A RAW file is pixels only, no metadata—it is
the camera's sensor dump. The embedded JPEG preview is all a file search needs
to find a photo: it has the same content, was made at the same moment, and
costs seconds instead of minutes. Decoding full RAW adds no text value and
holds up an index run.

**The ladder applies to the preview.** Once extracted, the preview is a JPEG
and goes through the normal OCR routing, so every caller gets the same uniform
behaviour: thumbnail stats, detection-only probe, or full recognition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from app.core.errors import raise_error
from app.core.format_health import Requirement
from app.core.logging import logger
from app.extract.base import Document, DocumentBuilder, SourceKind, register

__all__ = ["RawExtractor"]

log = logger.bind(component="extract.raw")


def extract_preview(path: Path) -> Optional[bytes]:
    """The embedded JPEG preview from a RAW file, or None.

    Never raises. A file that looks like RAW but isn't is left to OCR to
    refuse rather than silently becoming name-only.
    """
    try:
        import rawpy

        with rawpy.imread(str(path)) as raw:
            # RawPy cannot write JPEG directly; extract the buffer and
            # let PIL encode it, which is what pillow-heif does anyway.
            preview_data = raw.extract_thumb()
            if preview_data.shape[0] == 0:
                # No preview in this file - let OCR handle it
                return None
            return preview_data.tobytes()
    except Exception as exc:  # noqa: BLE001 - not a RAW file, not installed
        log.debug("could not extract RAW preview from {}: {}: {}",
                  path.name, type(exc).__name__, exc)
        return None


class RawExtractor:
    """RAW camera formats: extract the embedded JPEG and OCR it."""

    name = "raw"
    extensions = frozenset({
        ".cr2", ".nef", ".dng", ".arw",
    })
    reads_externally = False
    requires = (
        Requirement("rawpy", "rawpy",
                    provides="embedded JPEG extraction from RAW files",
                    hard=False),
    )

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """Extract the embedded JPEG preview and OCR it.

        If rawpy is not installed or the file has no preview, yield nothing
        so the document is indexed by filename only.
        """
        preview_bytes = extract_preview(path)
        if preview_bytes is None:
            return

        # Now OCR the preview as if it were the original image
        from app.extract.ocr import ocr_image

        result = ocr_image(preview_bytes)
        if result.engine_missing:
            # Engine could not load - not available on this machine
            raise_error(
                "ERR_OCR_UNAVAILABLE", "extract.raw", path=str(path),
                details="the OCR engine could not be loaded on this run",
            )
            return

        if result.empty:
            # No text in the preview - document is name-only
            log.debug("no text in RAW preview from {} after {:.1f}s",
                      path.name, result.elapsed_s)
            return

        builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
        builder.add(result.text, label="Text read from RAW preview")

        # **EXIF date is THE date for photos.** Use the camera's recorded time
        # rather than file mtime, which lies after copies.
        from app.extract.exif import read_datetime
        exif_date = read_datetime(path)
        if exif_date is not None:
            builder.date = exif_date

        builder.meta.update({
            "format": "raw",
            "ocr_lines": result.lines,
            "ocr_seconds": round(result.elapsed_s, 2),
            "ocr_confidence": round(result.mean_confidence, 3),
        })
        yield builder.build()


register(RawExtractor())
