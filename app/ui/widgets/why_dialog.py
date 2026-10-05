r"""The "Why is this here?" answer. Adoptions section 1 (`202626271137`).

Layer: L5

`presenter.why_result` and `explain_for` built the sentences and were ticked;
nothing in the window ever asked them, so a person who wanted to know why a
result had come up had only the "Why this result?" clipboard copy, which is
`score 0.83` - exactly the number the order said never to show. This is the
missing half: the right-click entry opens this, in plain words.

**Facts, never scores.** Every line comes from `presenter.why_lines`, which
reads recorded signals; this file only lays them out. A row with nothing to
say beyond "your words matched" says so plainly rather than padding - an empty
explanation is a legitimate answer, and an empty dialog is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from PySide6.QtWidgets import QMessageBox, QWidget

from app.ui.presenter import why_lines

__all__ = ["why_text", "show_why"]

NOTHING_MORE = "It matched what you typed, and there is nothing more to add."


def why_text(row: Any, terms: Sequence[str] = (), prefs: Optional[dict] = None) -> str:
    """The body of the answer. Pure, so the wording can be argued with in a
    test rather than read off a dialog."""
    lines = why_lines(row, terms, prefs)
    if not lines:
        return NOTHING_MORE
    return "\n".join(f"• {line}" for line in lines)


def show_why(parent: QWidget, row: Any, terms: Sequence[str] = (),
             prefs: Optional[dict] = None) -> None:
    """Show the answer for one row. Never raises - a courtesy beside a list."""
    try:
        name = Path(str(getattr(row, "path", "") or "")).name or "this result"
        box = QMessageBox(parent)
        box.setWindowTitle("Why is this here?")
        box.setText(f"Why “{name}” came up")
        box.setInformativeText(why_text(row, terms, prefs))
        box.setStandardButtons(QMessageBox.StandardButton.Close)
        box.exec()
    except Exception:                        # noqa: BLE001 - see the docstring
        return
