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
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    # `_splash_font`'s return type only - every Qt import in this module is
    # lazy at runtime, so the splash stays fast, but a string annotation
    # still needs the name resolvable somewhere for a type checker (and for
    # test_no_undefined_names_anywhere_in_app) without paying for a real
    # import here.
    from PySide6.QtGui import QFont

__all__ = [
    "SplashScreen", "StatusReporter", "get_splash_status_text",
]

# Brand colours (non-theme)
BRAND_NAVY = "#15084B"
BRAND_STRIPE_GREEN = "#A1B002"      # brand.json, exact (2026-10-04; was A1B000)
BRAND_STRIPE_BLUE = "#0A79DB"       # brand.json, exact (2026-10-04; was 0778D9)
BRAND_STRIPE_ORANGE = "#FF9933"
BRAND_TEXT_FAINT = "#9b95c4"

#: §1b: how long the closing fade takes. Not a number from §0 (which only
#: says "a quick fade") - chosen short enough that a fast, cache-warm start
#: still hands over to the window promptly.
_FADE_DURATION_S = 0.25

#: Tick length for the event-pumped waits below (minimum hold and fade).
#: Short enough that the fade still looks like a ramp, not steps.
_WAIT_TICK_S = 0.02

#: Defensive ceiling on top of whatever `_minimum_hold_time` computes to -
#: a clock anomaly must never turn "wait out the hold" into a hang. By the
#: time `main.py` calls `hide_and_close()` the real window is already
#: showing underneath (H4: splash is cosmetic), but tests call it directly.
_MAX_HOLD_WAIT_S = 5.0

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

# Tagline (brand copy, byte-exact). A constant since 2026-09-29, so the paint
# and the fit test read one string.
TAGLINE = "Forgets nothing. Tells no one. Outlives the drives."

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



#: The smallest a line may shrink to, in the same "points at a 9pt base" terms
#: as `_splash_font` - so it still scales with a larger system font.
_MIN_POINTS = 7.0


def fitted_font(text: str, width: int, height: int, points: float) -> "QFont":
    """The largest splash font, up to `points`, at which `text` fits the box.

    Measured with word wrap on, so a long line first takes a second line and
    only then gets smaller. Stops at `_MIN_POINTS`; `drawText` clips anything
    past that rather than painting off the splash.

    2026-09-29, the owner: "the text goes off screen". Every line was drawn
    into a fixed box with no wrap - on their display one case line was 431px
    for a 417px box at the normal text size, and all five were over at 13pt.
    The case lines are approved copy and are not reworded; they fit instead.
    """
    from PySide6.QtCore import QRect, Qt           # noqa: PLC0415 - startup speed
    from PySide6.QtGui import QFontMetrics          # noqa: PLC0415

    size = points
    while True:
        font = _splash_font(size)
        need = QFontMetrics(font).boundingRect(
            QRect(0, 0, max(1, width), 100_000),
            int(Qt.TextFlag.TextWordWrap), text)
        if (need.width() <= width and need.height() <= height) or size <= _MIN_POINTS:
            return font
        size = max(_MIN_POINTS, size - 0.5)


def _splash_font(points_at_default: float) -> "QFont":
    """The application's own face at a size relative to the system font.

    UI Redesign (202626160950 §1d). This file was the one place the window
    still hardcoded `QFont("Segoe UI", n)` - correct on Windows and wrong
    everywhere else, including the macOS build the shell is now designed for.
    The sizes are expressed as they always were at a 9pt base (11, 9, 8) and
    scale with "Make text bigger" exactly as `theme.SCALE` does. Imported
    lazily, like every Qt symbol in this module, so the splash stays fast.
    """
    from PySide6.QtGui import QFont            # noqa: PLC0415 - startup speed
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from app.ui.theme import DEFAULT_POINT_SIZE, base_point_size

    app = QApplication.instance()
    font = QFont(app.font()) if app is not None else QFont()
    font.setPointSizeF(round(base_point_size() * points_at_default
                             / DEFAULT_POINT_SIZE, 1))
    font.setWeight(QFont.Weight.Normal)
    return font


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
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import QWidget

        self.widget = QWidget(parent)
        self.widget.setWindowTitle("Leasha")
        # PySide6's setAttribute/setWindowFlags are strictly typed against the
        # Qt enums - the raw ints these used to be (matching the enum's own
        # numeric value) raise `TypeError: unexpected type 'int'` at
        # construction, crashing every startup before the window ever shows.
        self.widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # `WindowStaysOnTopHint` used to be set here, which keeps the widget
        # above every other window on the desktop - not just the main window
        # underneath it - for as long as the widget exists. `hide_and_close`
        # is guarded against exceptions (H4, see its docstring) precisely
        # because splash behaviour must never block startup, but that guard
        # only stops the *hold-and-fade* from hanging; it does nothing about
        # a widget that stays alive and stays-on-top through some other path
        # (a test calling `show()` without `hide_and_close()`, a future
        # early-return added above it). Frameless alone is enough: a normal
        # top-level window still paints above whatever else is on screen
        # while it has focus, without pinning itself over the user's other
        # applications for the rest of the process's life.
        self.widget.setWindowFlags(Qt.WindowType.FramelessWindowHint)

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
        """Wait out the minimum hold, fade, then close (§1b).

        Called once, when the main window is ready to take over.

        **Minimum hold, now actually enforced.** `_minimum_hold_time` used to
        be set in `__init__`/`show()` and never read again - `close()` ran
        immediately regardless, so nothing in the code actually delayed
        anything on it, despite the class docstring's claim. This waits out
        whatever is left of that deadline first, by *pumping* the event loop
        (`QCoreApplication.processEvents()` between short sleeps) rather than
        blocking it - the same responsive-wait technique
        `_acquire_gui_lock_responsively` in `main.py` uses for §2c, so the
        case-rotation timer and any repaint keep running for the whole wait
        instead of freezing on whatever was painted last.

        **This delays only the hand-off, never the work.** `main.py` only
        reaches this call after warm-up has finished and `window.show()` has
        already run - the window is already showing underneath the splash by
        the time this method does anything, so waiting here holds the splash
        on screen a little longer, not startup itself.

        **Then a real fade** (§0.7's "a quick fade"): `windowOpacity` ramps
        from 1.0 to 0.0 over `_FADE_DURATION_S`, pumped the same way - there
        was no fade at all before this ("A fade animation can be added
        later," read the old comment here).

        **H4: guarded.** Both the hold-wait and the fade run inside their own
        `try/except` so a failure in either (an exception mid-loop, a widget
        already torn down under a test) still reaches `self.widget.close()`
        at the end - splash behaviour must never be the reason startup
        doesn't finish.
        """
        try:
            self._wait_out_minimum_hold()
        except Exception:                      # noqa: BLE001 - see H4 above
            pass
        try:
            self._fade_out()
        except Exception:                      # noqa: BLE001 - see H4 above
            pass
        self.widget.close()

    def _wait_out_minimum_hold(self) -> None:
        """Pump events until `_minimum_hold_time`, capped defensively."""
        from PySide6.QtCore import QCoreApplication

        application = QCoreApplication.instance()
        deadline = min(self._minimum_hold_time, time.time() + _MAX_HOLD_WAIT_S)
        while time.time() < deadline:
            if application is not None:
                application.processEvents()
            time.sleep(_WAIT_TICK_S)

    def _fade_out(self) -> None:
        """Ramp `windowOpacity` from 1.0 to 0.0, pumping events as it goes."""
        from PySide6.QtCore import QCoreApplication

        application = QCoreApplication.instance()
        start = time.time()
        while True:
            elapsed = time.time() - start
            if elapsed >= _FADE_DURATION_S:
                break
            self.widget.setWindowOpacity(max(0.0, 1.0 - elapsed / _FADE_DURATION_S))
            if application is not None:
                application.processEvents()
            time.sleep(_WAIT_TICK_S)
        self.widget.setWindowOpacity(0.0)

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
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QScreen

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
        from PySide6.QtGui import QImage, QPixmap

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

            # A Python loop over every pixel of a 2667x1611 asset - seconds, on the UI
            # thread, before the splash shows. Reached only when `leasha-logo-white.png`
            # is missing: the release ships it, so this is the fallback for a build that
            # left it out, and the save below keeps it to one run where the folder is
            # writable.
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
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QPainter, QFont

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

            boxes = self.text_boxes()
            wrap = Qt.TextFlag.TextWordWrap

            # 4. Tagline
            x, y, bw, bh, points = boxes["tagline"]
            painter.setFont(fitted_font(TAGLINE, bw, bh, points))
            painter.setPen(QColor("white"))
            painter.drawText(x, y, bw, bh, Qt.AlignmentFlag.AlignCenter | wrap, TAGLINE)

            # 5. Rotating case line with icon
            case_icon_name, case_text = CASE_LINES[self._current_case_index]
            self._paint_case_line(painter, case_icon_name, case_text, boxes["case"])

            # 6. Status line
            x, y, bw, bh, points = boxes["status"]
            painter.setFont(fitted_font(self._status_message, bw, bh, points))
            painter.setPen(QColor(BRAND_TEXT_FAINT))
            painter.drawText(x, y, bw, bh, Qt.AlignmentFlag.AlignCenter | wrap,
                             self._status_message)

            # 7. Progress bar (if showing download)
            if self._showing_progress:
                self._paint_progress_bar(painter, y + bh)

            # 8. Footer
            footer_y = h - (h // 20)
            painter.setFont(_splash_font(8))
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

    def text_boxes(self) -> dict[str, tuple[int, int, int, int, float]]:
        """`{name: (x, y, width, height, points)}` for the three text lines.

        One place, so the paint and `test_splash`'s fit test measure the same
        boxes. The vertical rhythm is unchanged; the case line now owns the
        whole gap down to the status line (h/10, two lines) instead of h/12,
        and the status line has side margins instead of running edge to edge.
        """
        w, h = self.widget.width(), self.widget.height()
        stripe_height = max(2, h // 76)
        margin = w // 20
        tagline_y = stripe_height + (h // 4) + (h // 6)
        case_y = tagline_y + (h // 8)
        status_y = case_y + (h // 10)
        h_icon = h // 24
        # Icon and text used to be positioned independently around the
        # widget's centre, which left a ~90px gap between them. They are one
        # group, indented from a shared left margin with a small fixed gap.
        text_x = (w // 6) + h_icon + (h_icon // 2)
        return {
            "tagline": (margin, tagline_y, w - 2 * margin, h // 10, 11.0),
            "case": (text_x, case_y, w - text_x - margin, h // 10, 9.0),
            "status": (margin, status_y, w - 2 * margin, h // 12, 9.0),
        }

    def _paint_case_line(
        self,
        painter: Any,
        icon_name: str,
        text: str,
        box: tuple[int, int, int, int, float],
    ) -> None:
        """Paint a case line with its icon, wrapped and fitted to `box`."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor

        x, y, width, height, points = box
        h_icon = self.widget.height() // 24
        self._paint_icon(painter, icon_name, self.widget.width() // 6, y, h_icon)

        painter.setFont(fitted_font(text, width, height, points))
        painter.setPen(QColor("white"))
        painter.drawText(
            x, y, width, height,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            text,
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
        from PySide6.QtCore import QLineF, QRectF, Qt
        from PySide6.QtGui import QColor, QPen

        # Every shape below is built from `size / n` - true division, so a
        # Python float even when `size` itself is an int. QPainter's int
        # overloads (drawRect(x, y, w, h): int, int, int, int) and its float
        # overloads (drawRect(r: QRectF)) both exist, but there is no overload
        # taking four bare floats - PySide6 raises `TypeError: arguments did not
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
        from PySide6.QtGui import QColor

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
