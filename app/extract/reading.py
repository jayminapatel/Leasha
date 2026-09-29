r"""What the pipeline wants from a reader *inside* one container: the images rule.

Layer: L2

Order 0z lane C (2026-09-29). **A picture attached to an email was read by OCR
during the text-first pass**, although a loose picture on disk is held for the
pictures pass (`pipeline._ocr_gate`, `ERR_OCR_HELD`). The gate decides by the
*file*, and a `.pst` is not a picture, so every image inside one went through
OCR at text-pass speed. Measured on a real 14MB archive (71 messages, 24
image attachments): OCR was about 50 of every 60 seconds of the read.

The gate cannot see inside an archive, and a reader must never import the
pipeline. So the pipeline says what it wants here, per extraction thread, and a
container reader that can act on it asks:

* `IMAGES_READ` - read every attachment, pictures included. The default, and
  what every caller outside a pipeline (a test, `app.cli extract`) gets.
* `IMAGES_HOLD` - the text pass. A picture is not opened; it is counted as
  held, its name stays on the message, and the archive is remembered so the
  pictures pass comes back for it.
* `IMAGES_ONLY` - the pictures pass, re-reading an archive whose pictures were
  held. Only the pictures are read; the messages were indexed by the text pass.

Only `pst_libpff` asks today. A reader that never asks behaves exactly as it
did before this module existed.

**Per thread, captured when entered**, for the same reason as
`app.extract.progress.enter`: the pipeline's stream is a generator, and a
generator abandoned part-way may be closed on another thread. The stack is
captured when the context opens and the entry removed by identity.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

__all__ = [
    "IMAGES_HOLD",
    "IMAGES_ONLY",
    "IMAGES_READ",
    "Reading",
    "current",
    "images_for_ocr_mode",
    "reading",
]

IMAGES_READ = "read"
IMAGES_HOLD = "hold"
IMAGES_ONLY = "only"


class Reading:
    """What one read was asked to do, and what the reader reports back.

    * `images` - one of the `IMAGES_*` codes.
    * `held` - pictures the reader did not open because of `IMAGES_HOLD`.
      The pipeline reads it after the stream ends, to remember the archive.
    * `counts` - the reader's per-item status counts for the file (the same
      words as its progress frame's `counts`), copied here when the read ends
      so the pipeline can log them after the frame has closed.
    """

    __slots__ = ("images", "held", "counts")

    def __init__(self, images: str = IMAGES_READ) -> None:
        self.images = images
        self.held = 0
        self.counts: dict[str, int] = {}


_local = threading.local()
#: What a reader sees outside any `reading()` - a fresh one each time, so a
#: caller outside the pipeline can never leave a count behind for the next.
_DEFAULT_IMAGES = IMAGES_READ


def _stack() -> list[Reading]:
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = []
        _local.stack = stack
    return stack


def current() -> Reading:
    """The innermost `Reading` on this thread, or a default one (read everything)."""
    stack = _stack()
    return stack[-1] if stack else Reading(_DEFAULT_IMAGES)


@contextmanager
def reading(*, images: str = IMAGES_READ) -> Iterator[Reading]:
    """Ask readers on this thread to treat pictures as `images`, until the block ends."""
    stack = _stack()
    entry = Reading(images)
    stack.append(entry)
    try:
        yield entry
    finally:
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] is entry:
                del stack[index]
                break


def images_for_ocr_mode(ocr_mode: str) -> str:
    """The images rule for a pipeline pass (`PipelineConfig.ocr_mode`)."""
    if ocr_mode == "text":
        return IMAGES_HOLD
    if ocr_mode == "images":
        return IMAGES_ONLY
    return IMAGES_READ
