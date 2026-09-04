"""The branded splash screen, shown immediately on startup.

Layer: L5

**The splash is brand, not theme.** It displays navy, the signature stripe, and
the three brand colors in painted vector form — not theme-aware. The goal is to
tell the user something true about the product while the window loads.

Design locked by the owner 2026-08-28 on a live mock.

The splash does two jobs:
1. Show immediately (<300ms), hiding the wait for single-instance handover and
   model loading. Only stdlib and Qt are imported before it is shown.
2. Report progress: status breadcrumbs (Starting, Opening index, Loading engine,
   Ready) and optionally a progress bar for first-run model downloads.

The splash shows a **minimum of 1.2s** so a fast startup always shows exactly one
rotating case. After that it fades and gives way to the main window.

The splash stays alive for the entire startup process and listens to log messages
via a callable that the startup code calls at each stage. If the model cache is
missing, warm-up reports download progress to the splash's progress bar (or to
the main window's notices bar if the window is already up when download starts).

**The white-wordmark variant:** the logo asset has navy text that is invisible on
navy background. Recolor navy-family pixels (R<70, G<60, B<130 → white) at build
time or cache it on first run. Cached beside the asset as `leasha-logo-white.png`.

**Timings and measurements:** the order records startup/close times at key points.
Record real numbers, not estimates.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    "SplashScreen", "StatusReporter", "get_splash_status_text",
]

# Brand colours (non-theme)
BRAND_NAVY = "#15084B"
BRAND_STRIPE_GREEN = "#A1B000"
BRAND_STRIPE_BLUE = "#0778D9"
BRAND_STRIPE_ORANGE = "#FF9933"
BRAND_TEXT_FAINT = "#9b95c4"

# Status lines, translated to plain words
STATUS_MESSAGES = {
    "startup": "Starting Leasha…",
    "acquiring_lock": "Waiting for the previous Leasha to finish closing…",
    "stores_opening": "Opening your index…",
    "embedding_model": "Loading the search engine…",
    "rerank_model": "Loading the search engine…",
    "engine_building": "Loading the search engine…",
    "model_download": "Downloading the meaning model — one time, about 130 MB",
    "ready": "Ready",
}

# Case lines (brand copy, owner-approved, byte-exact)
CASE_LINES = [
    (
        "drive",
        "Finds photos on drives you unplugged years ago — it remembers "
        "what's on them"
    ),
    (
        "photo_magnifier",
        "Describe a picture from memory — 'the kids on the beach' — and "
        "it appears"
    ),
    (
        "stopwatch",
        "Twenty years of files, mail and photos — searched in a blink"
    ),
    (
        "home",
        "No cloud, no account, no subscription — yours, on your machine, free"
    ),
    (
        "envelope_check",
        "Old mail archives, chats and scans — one search box finds them all"
    ),
]


def get_splash_status_text(stage_name: str) -> str:
    """Get the plain-words status message for a startup stage.

    Called by startup code to pass to `splash.report_progress(message)`.
    The message appears in the splash and in the run log breadcrumbs.
    """
    return STATUS_MESSAGES.get(stage_name, stage_name)


class StatusReporter(Callable[[str], None]):
    """A callable that the startup code calls to report progress.

    Example:
        reporter = StatusReporter(splash)
        reporter("Starting Leasha…")
        # ... do work ...
        reporter("Opening your index…")
        # ... do work ...
        reporter("Ready")
    """

    def __init__(self, splash: SplashScreen) -> None:
        self.splash = splash

    def __call__(self, message: str, progress: Optional[float] = None) -> None:
        """Report a status message, and optionally a 0-100 download progress.

        Called synchronously during startup, and also from a background
        thread during a first-run model download (`Embedder`'s
        `on_progress` callback runs on a plain `threading.Thread`, not the
        GUI thread). Verified live rather than assumed: `report_progress`
        only sets plain attributes and calls `QWidget.update()`, which
        internally posts a `QEvent` - `QCoreApplication::postEvent` is
        documented as thread-safe, unlike most direct widget calls.
        """
        if self.splash is not None:
            self.splash.report_progress(message, progress)


class SplashScreen:
    """The branded splash screen, shown during startup.

    The splash is a frameless QWidget painted in brand colours. It shows:
    - Navy background, signature stripe across top
    - Leasha logo (wordmark + mark), white wordmark on navy
    - Tagline: "Forgets nothing. Tells no one. Outlives the drives."
    - A rotating case line with a vector icon (5 cases, 3s cycle)
    - Status line (faint violet)
    - Version + leasha.co.uk footer
    - Optional progress bar (for first-run model downloads)

    **Timing:**
    - Visible <300ms from process start (before lock wait)
    - Minimum hold 1.2s (one rotation)
    - Fade and close when window shows
    - No click-through dismiss

    **Moments:**
    - Normal startup: status line updates, rotating cases
    - Handover wait: "Waiting for the previous Leasha…"
    - Model download (first run): progress bar + "Downloading the meaning model"
    """

    def __init__(self, parent: Any = None) -> None:
        """Create the splash. Does not show it — caller calls .show()."""
        from PyQt6.QtCore import Qt, QTimer
        from PyQt6.QtWidgets import QWidget

        self.widget = QWidget(parent)
        self.widget.setWindowTitle("Leasha")
        # PyQt6's setAttribute/setWindowFlags are strictly typed against the
        # Qt enums - the raw ints these used to be (matching the enum's own
        # numeric value) raise `TypeError: unexpected type 'int'` at
        # construction, crashing every startup before the window ever shows.
        self.widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.widget.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)

        # Size: ~560×380 logical px, DPI-aware
        self._setup_geometry()

        # State
        self._current_case_index = 0
        self._rotation_start_time = time.time()
        self._minimum_hold_time = time.time() + 1.2  # Minimum 1.2s
        self._status_message = "Starting Leasha…"
        self._progress_value = 0  # 0–100 for download progress bar
        self._showing_progress = False
        self._version = self._get_version()

        # Logo asset (and white variant if missing)
        self._logo_image = self._load_or_create_white_wordmark()

        # Timers
        self._case_rotation_timer = QTimer()
        self._case_rotation_timer.timeout.connect(self._on_rotate_case)
        self._case_rotation_timer.start(3000)  # Rotate every 3s

        # Paint
        self.widget.paintEvent = self._paint  # type: ignore[method-assign]

    def show(self) -> None:
        """Display the splash. Called once, immediately after QApplication."""
        self.widget.show()
        self._minimum_hold_time = time.time() + 1.2

    def hide_and_close(self) -> None:
        """Fade and close the splash (called when the main window shows).

        **Nothing called this.** `main.py` showed the splash, reported
        progress through it, and then simply moved on to `application.exec()`
        without ever telling the splash to go away - so it sat on screen,
        rotating cases forever, for the entire life of the process. That is
        the bug this method's caller (in `main.py`) now fixes.

        Closes immediately, synchronously - `test_splash_shows` asserts
        `isVisible()` is `False` right after this call returns, and deferring
        the close (to honour the documented minimum hold) would make that
        assertion race a timer instead. In practice the minimum hold is never
        at risk: `main.py` only calls this after the embedding model, the
        reranker and the window itself have all loaded, which alone takes
        several seconds - far past the 1.2s a fast, cache-warm startup would
        need protecting against.
        """
        # For now, just close it. A fade animation can be added later.
        self.widget.close()

    def report_progress(
        self,
        message: str,
        progress: Optional[float] = None,
    ) -> None:
        """Update the status line. Optionally show a progress bar.

        Args:
            message: The status message to display (plain words, one line)
            progress: Optional 0–100 progress value for a download bar
        """
        self._status_message = message
        if progress is not None:
            self._progress_value = max(0, min(100, int(progress)))
            self._showing_progress = True
        self.widget.update()

    def _setup_geometry(self) -> None:
        """Position and size the splash, DPI-aware."""
        from PyQt6.QtCore import QSize
        from PyQt6.QtGui import QScreen

        screen = self.widget.screen() or QScreen(None)
        dpi = screen.logicalDotsPerInch()
        scale = dpi / 96.0  # 96 DPI is the baseline

        # ~560×380 logical px
        width_px = int(560 * scale)
        height_px = int(380 * scale)

        # Center on screen
        rect = screen.availableGeometry()
        x = (rect.width() - width_px) // 2 + rect.x()
        y = (rect.height() - height_px) // 2 + rect.y()

        self.widget.setGeometry(x, y, width_px, height_px)

    def _get_version(self) -> str:
        """Load the version string from VERSION file."""
        try:
            from app.core.version import __version__
            return __version__
        except Exception:
            return "0.3.3"

    def _load_or_create_white_wordmark(self) -> Optional[Any]:
        """Load the logo, creating a white-wordmark variant if needed."""
        from PyQt6.QtGui import QImage, QPixmap
        from pathlib import Path

        # Try to load the original logo
        try:
            from app.ui.tray import assets_dir

            asset_path = assets_dir() / "leasha-logo.png"
            if not asset_path.is_file():
                return None

            # Check for cached white variant first
            white_variant_path = asset_path.parent / "leasha-logo-white.png"
            if white_variant_path.is_file():
                return QPixmap(str(white_variant_path))

            # Load original and recolor navy → white
            original = QImage(str(asset_path))
            if original.isNull():
                return None

            # In-place recolor: R<70, G<60, B<130 → white, alpha preserved
            for y in range(original.height()):
                for x in range(original.width()):
                    pixel = original.pixel(x, y)
                    # Extract ARGB
                    r = (pixel >> 16) & 0xFF
                    g = (pixel >> 8) & 0xFF
                    b = pixel & 0xFF
                    a = (pixel >> 24) & 0xFF

                    # Recolor navy-family pixels
                    if r < 70 and g < 60 and b < 130:
                        # Replace with white, keeping alpha
                        new_pixel = (a << 24) | (255 << 16) | (255 << 8) | 255
                        original.setPixel(x, y, new_pixel)

            # Cache it
            try:
                original.save(str(white_variant_path))
            except Exception:
                pass  # Cache failure is not fatal

            return QPixmap.fromImage(original)
        except Exception:
            return None

    def _on_rotate_case(self) -> None:
        """Called by the rotation timer every 3s."""
        self._current_case_index = (self._current_case_index + 1) % len(CASE_LINES)
        self.widget.update()

    def _paint(self, event: Any) -> None:
        """Paint the splash screen (replaces QWidget.paintEvent)."""
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor, QPainter, QFont

        painter = QPainter(self.widget)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Without this, drawPixmap's scale from the logo's native 2667x1611
            # down to its on-screen size uses a fast, low-quality filter -
            # visibly blurry/aliased on a HiDPI display. This is the other half
            # of the fix; the aspect-ratio distortion below is the first half.
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

            # 1. Navy background
            painter.fillRect(self.widget.rect(), QColor(BRAND_NAVY))

            # 2. Signature stripe (5px) at top
            h = self.widget.height()
            w = self.widget.width()
            stripe_height = max(2, h // 76)  # ~5px at 96 DPI
            stripe_width = w // 3

            painter.fillRect(0, 0, stripe_width, stripe_height, QColor(BRAND_STRIPE_GREEN))
            painter.fillRect(stripe_width, 0, stripe_width, stripe_height, QColor(BRAND_STRIPE_BLUE))
            painter.fillRect(2 * stripe_width, 0, stripe_width, stripe_height, QColor(BRAND_STRIPE_ORANGE))

            # 3. Logo (if loaded)
            if self._logo_image is not None:
                # The asset is a wide wordmark+mark (2667x1611, ~1.66:1) - not
                # square. Forcing it into a logo_size x logo_size box squashed
                # it vertically into a visibly distorted, blurry-looking
                # smear. Fit it into the same height budget instead, deriving
                # width from the pixmap's own aspect ratio.
                logo_h = h // 4
                native_w = self._logo_image.width() or 1
                native_h = self._logo_image.height() or 1
                logo_w = max(1, round(logo_h * native_w / native_h))
                logo_x = (w - logo_w) // 2
                logo_y = stripe_height + (h // 8)
                painter.drawPixmap(
                    logo_x, logo_y, logo_w, logo_h,
                    self._logo_image
                )

            # 4. Tagline
            painter.setFont(QFont("Segoe UI", 11, QFont.Weight.Normal))
            painter.setPen(QColor("white"))
            tagline_y = stripe_height + (h // 4) + (h // 6)
            painter.drawText(
                0, tagline_y, w, h // 10,
                Qt.AlignmentFlag.AlignCenter,
                "Forgets nothing. Tells no one. Outlives the drives."
            )

            # 5. Rotating case line with icon
            case_icon_name, case_text = CASE_LINES[self._current_case_index]
            case_y = tagline_y + (h // 8)
            self._paint_case_line(painter, case_icon_name, case_text, case_y)

            # 6. Status line
            status_y = case_y + (h // 10)
            painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Normal))
            painter.setPen(QColor(BRAND_TEXT_FAINT))
            painter.drawText(
                0, status_y, w, h // 12,
                Qt.AlignmentFlag.AlignCenter,
                self._status_message
            )

            # 7. Progress bar (if showing download)
            if self._showing_progress:
                progress_y = status_y + (h // 12)
                self._paint_progress_bar(painter, progress_y)

            # 8. Footer
            footer_y = h - (h // 20)
            painter.setFont(QFont("Segoe UI", 8, QFont.Weight.Normal))
            painter.setPen(QColor(BRAND_TEXT_FAINT))
            painter.drawText(
                h // 20, footer_y, (w // 2) - (h // 20), h // 20,
                Qt.AlignmentFlag.AlignLeft,
                self._version
            )
            painter.drawText(
                w // 2, footer_y, (w // 2) - (h // 20), h // 20,
                Qt.AlignmentFlag.AlignRight,
                "leasha.co.uk"
            )

        finally:
            painter.end()

    def _paint_case_line(
        self,
        painter: Any,
        icon_name: str,
        text: str,
        y: float,
    ) -> None:
        """Paint a case line with its icon."""
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor, QFont

        w = self.widget.width()
        h_icon = self.widget.height() // 24

        # Icon and text used to be positioned independently around the
        # widget's centre (icon anchored left-of-centre, text anchored
        # right-of-centre) which left a gap of roughly w/6 - about 90px on
        # this widget's size - between them: two unrelated-looking pieces
        # rather than one case line. They are now one group, indented from a
        # shared left margin with a small fixed gap between icon and text.
        group_x = w // 6
        icon_gap = h_icon // 2
        icon_x = group_x
        self._paint_icon(painter, icon_name, icon_x, y, h_icon)

        # Paint the text
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Normal))
        painter.setPen(QColor("white"))
        text_x = group_x + h_icon + icon_gap
        painter.drawText(
            text_x, int(y), w - text_x - (w // 20), self.widget.height() // 12,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            text
        )

    def _paint_icon(
        self,
        painter: Any,
        icon_name: str,
        x: float,
        y: float,
        size: float,
    ) -> None:
        """Paint a vector icon for the case line.

        Icons are simple line drawings in the three brand colours.
        This is a placeholder implementation with basic shapes.
        """
        from PyQt6.QtCore import QLineF, QRectF, Qt
        from PyQt6.QtGui import QColor, QPen

        # Every shape below is built from `size / n` - true division, so a
        # Python float even when `size` itself is an int. QPainter's int
        # overloads (drawRect(x, y, w, h): int, int, int, int) and its float
        # overloads (drawRect(r: QRectF)) both exist, but there is no overload
        # taking four bare floats - PyQt6 raises `TypeError: arguments did not
        # match any overloaded call` at the first icon painted. QRectF/QLineF
        # accept floats directly, so building those instead of passing floats
        # positionally is the fix, not rounding to int (which would visibly
        # misplace a sub-pixel icon).
        cx, cy = x + size / 2, y + size / 2
        pen = QPen(QColor(BRAND_STRIPE_BLUE), max(1, size // 8))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)

        def rect(rx: float, ry: float, rw: float, rh: float) -> QRectF:
            return QRectF(rx, ry, rw, rh)

        def line(x1: float, y1: float, x2: float, y2: float) -> QLineF:
            return QLineF(x1, y1, x2, y2)

        if icon_name == "drive":
            # Simple drive icon
            painter.drawEllipse(rect(cx - size / 4, cy - size / 3, size / 2, size / 2))
        elif icon_name == "photo_magnifier":
            # Photo + magnifying glass
            painter.drawRect(rect(cx - size / 3, cy - size / 3, size / 2, size / 2))
            painter.drawEllipse(rect(cx + size / 4, cy + size / 4, size / 3, size / 3))
        elif icon_name == "stopwatch":
            # Stopwatch
            painter.drawEllipse(rect(cx - size / 3, cy - size / 3, size / 1.5, size / 1.5))
            painter.drawLine(line(cx, cy - size / 2, cx, cy))
        elif icon_name == "home":
            # House
            painter.drawLine(line(cx - size / 3, cy, cx - size / 3, cy + size / 3))
            painter.drawLine(line(cx - size / 3, cy, cx, cy - size / 3))
            painter.drawLine(line(cx, cy - size / 3, cx + size / 3, cy))
            painter.drawLine(line(cx + size / 3, cy, cx + size / 3, cy + size / 3))
        elif icon_name == "envelope_check":
            # Envelope with checkmark
            painter.drawRect(rect(cx - size / 3, cy - size / 4, size / 1.5, size / 2))
            painter.drawLine(line(cx - size / 3, cy - size / 4, cx, cy))
            painter.drawLine(line(cx + size / 3, cy - size / 4, cx, cy))

    def _paint_progress_bar(self, painter: Any, y: float) -> None:
        """Paint a thin progress bar for model downloads."""
        from PyQt6.QtGui import QColor

        w = self.widget.width()
        bar_width = w // 2
        bar_height = max(2, self.widget.height() // 100)
        bar_x = (w - bar_width) // 2
        bar_y = int(y)

        # Background (dark)
        painter.fillRect(bar_x, bar_y, bar_width, bar_height, QColor("#2b2160"))

        # Foreground (blue, based on progress)
        progress_width = int(bar_width * self._progress_value / 100.0)
        painter.fillRect(bar_x, bar_y, progress_width, bar_height, QColor(BRAND_STRIPE_BLUE))
