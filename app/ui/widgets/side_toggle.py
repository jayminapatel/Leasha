r"""The icon that shows or hides a page's list on the left (Photos, Mail ...).

Layer: L5 widget helper

2026-10-11, the owner: the Photos tree "switch it on and off ideally a rich
icon and in view menu"; order 1n gives Mail the same. One button for both, so
the two look and behave alike: checkable, lit while the list is shown, kept in
step quietly when the View menu changes it, repainted on a theme change.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QToolButton

__all__ = ["side_toggle", "ICON"]

ICON = "panel-left"


def side_toggle(text: str, *, tip: str, accessible: str, name: str,
                on_click: Callable[[bool], None]) -> QToolButton:
    """A checkable icon button; `on_click(shown)` when it is pressed."""
    from app.ui.widgets.icons import icon

    toggle = QToolButton()
    toggle.setObjectName(name)
    toggle.setProperty("iconToggle", True)
    toggle.setCheckable(True)
    toggle.setAutoRaise(True)
    toggle.setText(text)
    toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    toggle.setToolTip(tip)
    toggle.setAccessibleName(accessible)
    toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    toggle.icon_name = ICON

    def paint(colours: Any = None) -> None:
        if colours is None:
            from app.ui.theme import theme_colours

            colours = theme_colours()
        lit = colours.get("accent_text", "#ffffff") if toggle.isChecked() else colours.get(
            "text_dim", "#888888")
        toggle.setIcon(icon(ICON, lit))

    def quietly(on: bool) -> None:
        toggle.blockSignals(True)
        toggle.setChecked(bool(on))
        toggle.blockSignals(False)
        paint()

    toggle.clicked.connect(lambda _c=False: on_click(toggle.isChecked()))
    toggle.toggled.connect(lambda _on: paint())
    toggle.retint = paint
    toggle.set_quietly = quietly
    paint()
    return toggle
