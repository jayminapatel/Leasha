r"""How a document is being *looked at*: rotation, zoom, page.

Layer: L5 presenter — Qt-free, so the arithmetic that decides what a render
call is asked for can be tested without a display.

**Workspace §2d and §2e.** Rotation and zoom are two parameters of one render
call, for images and for PDF pages alike — which is the order's own
instruction and the reason this is a small value object rather than four
attributes scattered over a widget.

**Rotation is remembered per file, in the app's own state.** The sideways scan
rotated once opens right-side-up for ever, and **the file itself is never
touched** — §6's first rule for this whole order, and the reason the key is a
hash of the path rather than anything written beside the document.

Zoom is deliberately *not* remembered. It is a thing somebody does to look
closer at one passage, not a property of the document, and a file that
reopened at 400% because of something done last March would read as broken.
"""

from __future__ import annotations

import hashlib
from typing import Any, NamedTuple

__all__ = [
    "View", "TURN", "ZOOM_STEPS", "MIN_ZOOM", "MAX_ZOOM", "FIT",
    "rotation_key", "normalise_turn", "clamp_zoom", "next_zoom", "read_turn",
]

#: One press of the rotate button. **Ninety degrees, cycling** — §2e's word.
#: Arbitrary angles are a photo editor's feature and this window is view-only.
TURN = 90

#: The zoom this widget will go between, and the ladder it steps along.
#:
#: **Steps rather than a multiplier**, because a fixed 1.25× from an arbitrary
#: start lands on numbers nobody chose — 137% — and because the way back to
#: 100% has to be exact. Somebody at 150% pressing zoom-out twice is at 100%.
ZOOM_STEPS: tuple[float, ...] = (
    0.25, 0.33, 0.50, 0.67, 0.75, 1.0, 1.25, 1.50, 2.0, 3.0, 4.0)
MIN_ZOOM = ZOOM_STEPS[0]
MAX_ZOOM = ZOOM_STEPS[-1]

#: Zoom that means "as wide as the window". Not a number, because the number
#: changes every time the window is resized — the widget resolves it at paint
#: time, and this is what it stores until then.
FIT = 0.0


def rotation_key(path: Any) -> str:
    r"""The `index_state` key a file's rotation is remembered under.

    **A hash of the path, not the path.** Three reasons, and the third
    decides it: keys stay a predictable length whatever the path is; a key
    cannot collide with another namespace by containing a colon or a slash;
    and `index_state` is a table somebody may look at, so a list of every
    document they have ever rotated — with its full path — is not a thing to
    leave lying about on a shared machine. The privacy order's reasoning,
    applied one level down.

    `""` for an empty path, which is what a mail message with no file has.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
    return f"ui:turn:{digest}"


def normalise_turn(degrees: Any) -> int:
    r"""Any number of degrees as one of 0, 90, 180, 270. Never raises.

    Rounded to the nearest quarter turn rather than refused: this reads a
    value out of a database that anything could have written, and a stored
    `95` should show a rotated document rather than an exception.

    >>> normalise_turn(-90), normalise_turn(450), normalise_turn("180")
    (270, 90, 180)
    """
    try:
        value = int(round(float(degrees) / TURN)) * TURN
    except (TypeError, ValueError):
        return 0
    return value % 360


def read_turn(state: Any, path: Any) -> int:
    """The remembered rotation for a file, or 0. **Never raises.**"""
    key = rotation_key(path)
    if not key:
        return 0
    try:
        return normalise_turn((state or {}).get(key, 0))
    except Exception:                            # noqa: BLE001 - a preference
        return 0


def clamp_zoom(zoom: Any) -> float:
    """A zoom factor inside the ladder's range. `FIT` passes through."""
    try:
        value = float(zoom)
    except (TypeError, ValueError):
        return 1.0
    if value == FIT:
        return FIT
    return max(MIN_ZOOM, min(MAX_ZOOM, value))


def next_zoom(zoom: Any, *, out: bool = False) -> float:
    r"""The next rung up or down the ladder.

    From `FIT` it starts at 100%, because "fit" has no place on a ladder of
    fixed factors and the one number everybody means by "actual size" is 1.0.

    >>> next_zoom(1.0), next_zoom(1.0, out=True), next_zoom(4.0)
    (1.25, 0.75, 4.0)
    """
    current = clamp_zoom(zoom)
    if current == FIT:
        current = 1.0
    if out:
        lower = [step for step in ZOOM_STEPS if step < current - 1e-9]
        return lower[-1] if lower else MIN_ZOOM
    higher = [step for step in ZOOM_STEPS if step > current + 1e-9]
    return higher[0] if higher else MAX_ZOOM


class View(NamedTuple):
    """Everything one render call needs beyond the file itself.

    A value object rather than four arguments, because it travels from the
    widget to a worker and back and every one of those is a place for two of
    them to get out of step.
    """

    turn: int = 0
    zoom: float = 1.0
    page: int = 0

    def turned(self) -> "View":
        """One press of the rotate button: the next quarter turn."""
        return self._replace(turn=normalise_turn(self.turn + TURN))

    def zoomed(self, *, out: bool = False) -> "View":
        return self._replace(zoom=next_zoom(self.zoom, out=out))

    def fitted(self) -> "View":
        """Fit to the window. Resolved to a number at paint time."""
        return self._replace(zoom=FIT)

    @property
    def is_fit(self) -> bool:
        return self.zoom == FIT

    @property
    def sideways(self) -> bool:
        """Is the image a quarter turn from upright? Its width and height
        swap, which every 'fit to the window' calculation has to know."""
        return self.turn in (90, 270)

    def as_state(self, path: Any) -> dict:
        """What to remember about this file. **Rotation only** — see the
        module docstring for why zoom is not a property of a document."""
        key = rotation_key(path)
        return {key: str(self.turn)} if key else {}
