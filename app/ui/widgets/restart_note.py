r"""Say on the control when a setting only takes effect after a restart.

Layer: L5

`settings_registry.needs_restart()` has always known which settings those are,
and its own docstring states the rule: "The UI must say so on the control. A
change that appears to work and does not is worse than one that is refused."
Nothing ever read it, so a person could change the embedding model, watch
nothing happen, and conclude the setting was broken.

**One pass over the finished window rather than a line in six widgets**, for the
reason the wheel guard is one call: a setting added next month with
`restart=True` gets its note without anyone remembering to write it.
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from app.core.settings_registry import needs_restart

__all__ = ["RESTART_NOTE", "mark_restart_needed"]

RESTART_NOTE = "Takes effect the next time Leasha starts."


def mark_restart_needed(root: QWidget) -> list[str]:
    """Add the note to the tooltip of every such control under `root`.

    Returns the keys marked. Safe to call twice - a control already carrying
    the note is left alone - and a setting with no control here (it lives on a
    page not built yet) is skipped rather than reported as an error.
    """
    marked: list[str] = []
    for setting in needs_restart():
        widget = root.findChild(QWidget, setting.key)
        if widget is None:
            continue
        tip = widget.toolTip() or ""
        if RESTART_NOTE not in tip:
            widget.setToolTip(f"{tip}\n\n{RESTART_NOTE}" if tip else RESTART_NOTE)
        marked.append(setting.key)
    return marked
