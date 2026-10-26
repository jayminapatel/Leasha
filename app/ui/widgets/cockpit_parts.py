r"""The Indexing cockpit's fixed instruments: state pill, phase stepper, tiles, resources.

Layer: L5

The owner, 2026-10-14: the Status shelf is *"the cockpit for indexing"*, and
*"the sizes of boxes change on content etc it is disturbing for the eye"*.
**Every part here has a size fixed by its font, never by what it shows**: the
pill is as wide as its longest word, each chip as wide as its own name, the
tiles and resource chips share the row's width equally, and every value is an
`ElidedLabel` - one line, cut with "…", whole in its tooltip. A value that is
not known is "-" in the same box.

**No words are decided here.** Captions, states and values all come from the
Qt-free `presenter/cockpit.py`; these widgets only put them on screen. Colours
are theme tokens: the stylesheet colours the boxes by a dynamic property
(`state`, `step`), and the few icons drawn as pictures are redrawn from
`theme_colours()` when the sheet changes.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from app.ui.presenter.cockpit import (
    NO_VALUE, RESOURCE_LABELS, STATE_ICONS, STATE_TOKENS, STATES, STEP_ICONS, STEPS, TILE_LABELS,
    step_tip,
)
from app.ui.theme import SPACE_S, SPACE_XS, theme_colours
from app.ui.widgets.elided_label import ElidedLabel
from app.ui.widgets.icons import icon, icon_size

__all__ = ["MetricTiles", "PhaseStepper", "ResourceStrip", "StatePill", "repolish",
           "tabular"]

#: The resource chips' glyphs, in `RESOURCE_LABELS` order.
_RESOURCE_ICONS = ("cpu", "layout-grid", "image", "hard-drive", "shield-check")


def repolish(widget: QWidget, name: str, value: Any) -> None:
    """Set a dynamic property the stylesheet selects on, and redraw - only on change."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def tabular(widget: QWidget) -> None:
    """Fixed-width digits, where the font has them (OpenType `tnum`).

    So "1,111" and "8,888" are the same width and a climbing count never
    jiggles. Segoe UI's digits are tabular already; this asks for it anyway.
    """
    font = widget.font()
    try:
        font.setFeature(QFont.Tag("tnum"), 1)
        widget.setFont(font)
    except (AttributeError, TypeError, ValueError):
        return


def _paint_icon(label: QLabel, name: str, token: str, role: str = "inline") -> None:
    """Draw glyph `name` into `label` in the colour of `token`; `name=""` clears it."""
    size = icon_size(role)
    if not name:
        label.clear()
        return
    colour = theme_colours().get(token) or theme_colours().get("text", "")
    label.setPixmap(icon(name, colour).pixmap(size))


def _icon_slot(role: str = "inline") -> QLabel:
    slot = QLabel()
    slot.setFixedSize(icon_size(role))
    slot.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return slot


class _Themed(QWidget):
    """A widget whose pictures are redrawn when the stylesheet changes."""

    def changeEvent(self, event: Any) -> None:       # noqa: N802 - Qt's name
        super().changeEvent(event)
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            self.retint()

    def retint(self) -> None:                         # pragma: no cover - overridden
        """Redraw the pictures in the current theme's colours."""


class StatePill(_Themed):
    """Running / Paused / Stopping / Idle / Failed - an icon and the word.

    As wide as the widest of the five words, so a change of state never moves
    the headline beside it.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("cockpitPill")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE_S, SPACE_XS, SPACE_S, SPACE_XS)
        layout.setSpacing(SPACE_XS)
        self.glyph = _icon_slot()
        self.word = QLabel()
        self.word.setObjectName("cockpitPillWord")
        layout.addWidget(self.glyph)
        layout.addWidget(self.word, 1)
        self.state = ""
        self.set_state("idle")
        self._fix_width()

    def _fix_width(self) -> None:
        metrics = self.word.fontMetrics()
        widest = max(metrics.horizontalAdvance(word) for word in STATES.values())
        margins = self.layout().contentsMargins()
        self.setFixedWidth(widest + self.glyph.width() + SPACE_XS + margins.left()
                           + margins.right() + SPACE_S)

    def set_state(self, state: str) -> None:
        """Show `state`, one of `presenter.cockpit.STATES`."""
        state = state if state in STATES else "idle"
        if state == self.state:
            return
        self.state = state
        self.word.setText(STATES[state])
        self.setAccessibleName(f"Index run: {STATES[state]}")
        repolish(self, "state", state)
        self.retint()

    def retint(self) -> None:
        _paint_icon(self.glyph, STATE_ICONS.get(self.state, ""),
                    STATE_TOKENS.get(self.state, "text"))

    def changeEvent(self, event: Any) -> None:       # noqa: N802 - Qt's name
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._fix_width()


class PhaseStepper(_Themed):
    """One chip per step of a run, in run order: done ticks, the current one lit."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("cockpitStepper")
        self.setAccessibleName("Steps of an index run")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_XS)
        self.chips: list[QFrame] = []
        self._glyphs: list[QLabel] = []
        for words, _codes in STEPS:
            chip = QFrame()
            chip.setObjectName("cockpitStep")
            row = QHBoxLayout(chip)
            row.setContentsMargins(SPACE_S, 0, SPACE_S, 0)
            row.setSpacing(SPACE_XS)
            glyph = _icon_slot()
            label = QLabel(words)
            label.setObjectName("cockpitStepWord")
            row.addWidget(glyph)
            row.addWidget(label)
            chip.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            layout.addWidget(chip)
            self.chips.append(chip)
            self._glyphs.append(glyph)
        layout.addStretch(1)
        self.states: list[str] = []
        self.show_steps(["todo"] * len(STEPS))

    def show_steps(self, states: Sequence[str]) -> None:
        """Paint one state per chip ("done", "current" or "todo")."""
        states = list(states)
        if states == self.states:
            return
        self.states = states
        for (words, _codes), chip, state in zip(STEPS, self.chips, states):
            repolish(chip, "step", state)
            chip.setAccessibleName(f"{words}: {state}")
            chip.setToolTip(step_tip(words, state))
        self.retint()

    def retint(self) -> None:
        glyphs = {"done": ("check", "success"), "current": ("loader-circle", "accent")}
        for (words, _codes), glyph, state in zip(STEPS, self._glyphs, self.states):
            name, token = glyphs.get(state, (STEP_ICONS.get(words, ""), "text_faint"))
            _paint_icon(glyph, name, token)


class _Box(QFrame):
    """A caption over a value: the shape of a tile and of a resource chip."""

    def __init__(self, caption: str, name: str, glyph: str = "",
                 inline: bool = False) -> None:
        super().__init__()
        self.setObjectName(name)
        # A tile stacks its caption over its value; a resource chip (`inline`)
        # puts both on one line.
        layout = QHBoxLayout(self) if inline else QVBoxLayout(self)
        layout.setContentsMargins(SPACE_S, SPACE_XS, SPACE_S, SPACE_XS)
        layout.setSpacing(SPACE_S if inline else 0)
        head = QHBoxLayout()
        head.setSpacing(SPACE_XS)
        self.glyph_name = glyph
        self.glyph = _icon_slot() if glyph else None
        if self.glyph is not None:
            head.addWidget(self.glyph)
        self.caption = QLabel(caption)
        self.caption.setObjectName(f"{name}Caption")
        self.caption.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        head.addWidget(self.caption)
        if not inline:
            head.addStretch(1)
        layout.addLayout(head)
        self.value = ElidedLabel(NO_VALUE)
        self.value.setObjectName(f"{name}Value")
        tabular(self.value)
        layout.addWidget(self.value)
        self._tip = ""

    def show_value(self, value: str, tip: str = "") -> None:
        self.value.setText(value or NO_VALUE)
        self.setAccessibleName(f"{self.caption.text()}: {value or NO_VALUE}")
        if tip != self._tip:
            self._tip = tip
            self.setToolTip(tip)


class _Row(_Themed):
    """Boxes sharing the row's width equally, whatever they hold."""

    def __init__(self, name: str, captions: Sequence[str], box: str,
                 glyphs: Sequence[str] = (), inline: bool = False) -> None:
        super().__init__()
        self.setObjectName(name)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_S)
        glyphs = list(glyphs) or [""] * len(captions)
        self.boxes = [_Box(caption, box, glyph, inline) for caption, glyph in zip(captions, glyphs)]
        for item in self.boxes:
            layout.addWidget(item, 1)
        self.retint()

    def show_values(self, values: Sequence[tuple[str, str, str]]) -> None:
        """`(caption, value, tooltip)` per box, in order."""
        for item, (_caption, value, tip) in zip(self.boxes, values):
            item.show_value(value, tip)

    def retint(self) -> None:
        for item in getattr(self, "boxes", ()):
            if item.glyph is not None:
                _paint_icon(item.glyph, item.glyph_name, "text_dim")


class MetricTiles(_Row):
    """The six tiles: Indexed, Files seen, Skipped, Passages, Speed, Time left."""

    def __init__(self) -> None:
        super().__init__("cockpitTiles", TILE_LABELS, "cockpitTile")


class ResourceStrip(_Row):
    """Processor, Memory, Graphics card, Disk free and the governor, in a row."""

    def __init__(self) -> None:
        super().__init__("cockpitResources", RESOURCE_LABELS, "cockpitResource",
                         _RESOURCE_ICONS, inline=True)
