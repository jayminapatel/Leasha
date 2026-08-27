r"""The log as its own window. Workspace §1c and §1d.

Layer: L5

**The in-app pane never leaves.** This is a copy, not a move — the same design
the order sets out for preview pop-outs, for the same reason: closing this
window must return nothing to re-wire, and somebody who pops the log out and
then closes it should find Settings exactly as they left it.

**It keeps updating while the main window is minimised to tray**, which is the
entire use case. `DebugPane` polls on its own `showEvent`/`hideEvent` and a
top-level window that is visible keeps getting those — so nothing here has to
arrange it, and the one thing that would break it is hiding this window when
the main one hides. Nothing does.

**Stay-on-top is remembered, and so is the size.** Watching a two-hour index
run from the corner of a screen is not a thing anybody wants to set up twice.
Both live in `index_state`, the app's own state, exactly as the order requires
of everything this thread stores.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QVBoxLayout, QWidget

__all__ = ["LogWindow", "GEOMETRY_KEY", "ON_TOP_KEY", "geometry_text",
           "geometry_from"]

#: Where the window remembers itself. One `index_state` key each, the channel
#: everything else in this application stores a preference in.
GEOMETRY_KEY = "ui:log_window_geometry"
ON_TOP_KEY = "ui:log_window_on_top"

#: Smallest useful size. Narrower than this and every line wraps, which is the
#: one thing that makes a log unreadable.
MIN_WIDTH, MIN_HEIGHT = 480, 260


def geometry_text(rect: Any) -> str:
    """`QRect` as `x,y,w,h`. `""` for anything that is not one.

    Four integers rather than `saveGeometry()`'s base64 blob: this has to
    survive being read by a person looking at `index_state` while working out
    why a window opened somewhere odd, and a blob cannot be.
    """
    try:
        return f"{int(rect.x())},{int(rect.y())},{int(rect.width())},{int(rect.height())}"
    except Exception:                            # noqa: BLE001 - a preference
        return ""


def geometry_from(text: Any) -> Optional[QRect]:
    r"""`x,y,w,h` back to a `QRect`, or None. **Never raises.**

    None for anything unreadable, and for a size below the floor: a remembered
    geometry from a screen that is no longer attached, or a window somebody
    dragged to one pixel, must cost the memory rather than produce a window
    that cannot be used or found.
    """
    try:
        parts = [int(piece) for piece in str(text or "").split(",")]
    except (TypeError, ValueError):
        return None
    if len(parts) != 4:
        return None
    x, y, width, height = parts
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return None
    return QRect(x, y, width, height)


class LogWindow(QWidget):
    """A top-level window holding a `DebugPane`, and nothing else.

    Thin on purpose: the pane already knows how to poll, colour and be
    clicked, and a second implementation of any of that would be a second
    place for the log to behave differently from itself.
    """

    #: The window was closed, so the caller can forget it and let it go.
    closed = pyqtSignal()
    #: A geometry or stay-on-top change worth remembering. `{key: value}`.
    remember = pyqtSignal(dict)
    #: A log line naming a file was double-clicked. Straight through.
    file_chosen = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        # **No parent, deliberately.** A parented `QWidget` with a window flag
        # is still owned by the main window: minimising the main window would
        # take this with it, which is the one thing §1c says must not happen.
        super().__init__(None)
        self.setWindowTitle("Leasha — recent activity")
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)

        from app.ui.widgets.debug_pane import DebugPane

        # `poppable=False`: a pop-out button inside the popped-out window
        # would offer to do the thing that has already happened.
        self.pane = DebugPane(self, poppable=False)
        self.pane.file_chosen.connect(self.file_chosen)

        self.on_top = QCheckBox("Keep this window on top")
        self.on_top.setToolTip(
            "Keeps this window in front of everything else, so you can watch "
            "an index run while working in another application.")
        self.on_top.toggled.connect(self._on_top_changed)

        controls = QHBoxLayout()
        controls.addWidget(self.on_top)
        controls.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.pane, stretch=1)
        layout.addLayout(controls)

    # -- state ---------------------------------------------------------------

    def restore(self, state: Any) -> None:
        """Put the window back where it was. Never raises.

        `state` is a `{key: value}` mapping - whatever `index_state` held.
        """
        try:
            found = geometry_from((state or {}).get(GEOMETRY_KEY))
            if found is not None:
                self.setGeometry(found)
            wanted = str((state or {}).get(ON_TOP_KEY, "")).strip().lower()
            if wanted in ("1", "true", "yes"):
                self.on_top.setChecked(True)
        except Exception:                        # noqa: BLE001 - a preference
            return

    def set_palette(self, palette: Any) -> None:
        """The theme, straight through to the pane that paints with it."""
        self.pane.set_palette(palette)

    def _on_top_changed(self, wanted: bool) -> None:
        r"""Toggle the flag, and put the window back on screen.

        **`show()` after `setWindowFlags` is not optional.** Changing a window
        flag re-creates the native window, and Qt leaves the new one hidden -
        so a toggle without this makes the log vanish, which reads as the
        checkbox having closed it.
        """
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(wanted))
        self.show()
        self.remember.emit({ON_TOP_KEY: "true" if wanted else "false"})

    # -- Qt -------------------------------------------------------------------

    def moveEvent(self, event: Any) -> None:                # noqa: N802 - Qt's name
        super().moveEvent(event)
        self._remember_geometry()

    def resizeEvent(self, event: Any) -> None:              # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._remember_geometry()

    def closeEvent(self, event: Any) -> None:               # noqa: N802 - Qt's name
        self._remember_geometry()
        self.closed.emit()
        super().closeEvent(event)

    def _remember_geometry(self) -> None:
        """**Never raises**, and never while hidden.

        A hidden window reports a geometry Qt has not placed yet, and saving
        that is how a window comes back at (0, 0) the size of nothing.
        """
        if not self.isVisible():
            return
        text = geometry_text(self.geometry())
        if text:
            self.remember.emit({GEOMETRY_KEY: text})
