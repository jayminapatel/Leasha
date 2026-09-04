r"""The OCR ladder: a four-stage router that makes image indexing affordable.

Layer: L2

**The problem:** OCR costs 3.6 seconds per page. An index run with ten thousand
photos would otherwise spend 10 hours running the full engine on wall photos
that have no text at all.

**The solution:** Route images through four rungs of increasing cost:

1. **Rung 0 — metadata** (free): EXIF tags, filename patterns. Routes only,
   never rejects. A photo named `IMG_2024.jpg` routes to OCR; a wall photo
   named `wall.jpg` still reaches it.

2. **Rung 1 — thumbnail stats** (~5ms): Downscale to 256px, read the histogram.
   White-heavy documents route straight to full OCR. Non-documents cost nothing.

3. **Rung 2 — detection probe** (~50-150ms): Run the OCR engine's detection
   stage alone. No text boxes → record "no text found (checked)" and skip OCR.
   Boxes found → run full recognition on that region only.

4. **Full OCR** (~3.6s): The entire pipeline for text-heavy images.

**Cost to a corpus:** A folder of 1000 photos costs 5 + 50 + 3600 seconds
instead of 3.6M. The route, never reject rule means genuine documents (receipts,
whiteboards, text photos) still reach the full engine.

**Load-bearing rules:**
- A route is a judgement call, not a hard truth. "This looks like a photo" is
  not "this has no text." The route must be always reversible - a confidence
  threshold, not a category.
- Rung thresholds are tunables on the Index Tuning screen and must carry
  plain-words labels explaining what they do.
- Each image pays the ladder once. A per-image PDF probe applies the same
  ladder to each scanned page.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from app.core.logging import logger

log = logger.bind(component="extract.ocr_ladder")


class RouteDecision(Enum):
    """Rung decision: what stage OCR should run at."""

    METADATA_ROUTE = "metadata"
    THUMBNAIL_ROUTE = "thumbnail"
    DETECTION_ROUTE = "detection"
    FULL_OCR = "full_ocr"
    NO_TEXT = "no_text"


@dataclass
class LadderResult:
    """Result of routing through the ladder."""

    decision: RouteDecision
    elapsed_ms: float
    reason: str


def _metadata_decision(path: Path) -> Optional[RouteDecision]:
    """Rung 0: free metadata routing.

    Returns a route decision, or None to continue to the next rung. Never
    rejects - routes only.

    **Patterns that indicate obvious documents:**
    - Filenames starting with IMG_, DSC_ (camera defaults)
    - Screenshots, WhatsApp messages
    - Scanned patterns like SCAN_, RECEIPT_
    """
    name = path.name.lower()

    # Camera defaults and common photo patterns
    if name.startswith(("img_", "dsc_", "screenshot", "whatsapp", "signal_")):
        # Could be documents, could be photos - route to ladder
        return RouteDecision.METADATA_ROUTE

    # Scanned document patterns
    if name.startswith(("scan", "receipt", "document", "form")):
        # Likely text-heavy
        return RouteDecision.FULL_OCR

    # No metadata decision - continue to rungs 1-2
    return None


def _thumbnail_stats(source: Path | bytes) -> Optional[LadderResult]:
    """Rung 1: thumbnail histogram analysis (~5ms).

    Returns a decision if confident (document), or None to continue.
    """
    try:
        from PIL import Image
        import numpy as np

        started = time.monotonic()

        # Load and downscale to 256px
        if isinstance(source, Path):
            with Image.open(source) as img:
                img.thumbnail((256, 256))
                arr = np.array(img.convert("L"))
        else:
            from io import BytesIO
            with Image.open(BytesIO(source)) as img:
                img.thumbnail((256, 256))
                arr = np.array(img.convert("L"))

        elapsed_ms = (time.monotonic() - started) * 1000

        # Histogram: white-heavy likely a document
        white_pixels = np.sum(arr > 200)
        white_fraction = white_pixels / arr.size if arr.size > 0 else 0

        # High saturation (color heavy) likely not a document
        # Flat (B&W) likely a document
        # For now, simple heuristic: mostly white = document
        if white_fraction > 0.7:
            return LadderResult(
                decision=RouteDecision.FULL_OCR,
                elapsed_ms=elapsed_ms,
                reason=f"white_fraction={white_fraction:.2f}",
            )

        # Not confident - continue to detection probe
        return None

    except Exception as exc:  # noqa: BLE001
        log.debug("thumbnail stats failed: {}: {}", type(exc).__name__, exc)
        return None


def route(source: Path | bytes, *, check_metadata: bool = True) -> LadderResult:
    """Route an image through the OCR ladder.

    Returns the decision (which rung to run) and the cost so far.

    Args:
        source: Path or bytes of the image
        check_metadata: Whether to check filename metadata (Rung 0)
    """
    started = time.monotonic()

    # Rung 0: metadata (free)
    if check_metadata and isinstance(source, Path):
        decision = _metadata_decision(source)
        if decision is not None:
            if decision != RouteDecision.METADATA_ROUTE:
                return LadderResult(
                    decision=decision,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    reason="metadata_route",
                )

    # Rung 1: thumbnail stats (~5ms)
    result = _thumbnail_stats(source)
    if result is not None:
        if result.decision != RouteDecision.METADATA_ROUTE:
            result.elapsed_ms = (time.monotonic() - started) * 1000
            return result

    # If we get here, run detection probe (Rung 2)
    # The caller (ocr_image) will handle the full OCR/detection decision
    return LadderResult(
        decision=RouteDecision.DETECTION_ROUTE,
        elapsed_ms=(time.monotonic() - started) * 1000,
        reason="detection_probe_needed",
    )
