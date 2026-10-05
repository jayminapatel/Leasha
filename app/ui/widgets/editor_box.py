r"""Which editor a code result opens in.

Layer: L5 view — thin. Every decision is in `app/ui/editors.py`, which needs
no display; this is the two controls and the sentence under them.

**A code result is a place, not a document.** Reveal-in-Explorer is the right
action for a spreadsheet and the wrong one for line 512 of a file: somebody
who found that line wants to be *at* it, and being handed its folder makes
them repeat the search inside their editor.

**Only editors that are actually installed are offered**, the same rule the
converter list follows - nobody should be able to pick something that will
then fail on every click. The free-text command below covers everything else,
because an editor this has never heard of should be a line of configuration
rather than a feature request.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QLabel, QLineEdit, QVBoxLayout, QWidget,
)

from app.ui.editors import AUTO, EDITORS, detect

__all__ = ["EditorBox"]


class EditorBox(QGroupBox):
    """The editor choice and its escape hatch."""

    changed = Signal(dict)

    def __init__(self, settings: Any = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__("Opening code results", parent)

        self.editor = QComboBox(self)
        # **Spelled out as a literal**, because `test_settings_reachable`
        # greps the source for the key rather than building the widget - it
        # has to run on a machine with no display. Naming it from a table
        # works at runtime and is invisible to that test, which is the trap
        # the tuning screen's spin boxes fell into.
        self.editor.setObjectName("CODE_EDITOR")
        self.editor.setToolTip(
            "A code result opens at its line in this editor.\n\n"
            "Automatic uses the first one it finds installed. None keeps the "
            "old behaviour of showing the file in Explorer.\n\n"
            "Only editors found on this machine are listed - so nothing here "
            "can be chosen and then fail on every click.")

        found = detect()
        installed = {value for value, _label, _where in found}
        self.editor.addItem(
            "Automatic" + (f" — {found[0][1]}" if found else " — none found"),
            AUTO)
        for value, label, _executable, _template in EDITORS:
            if value in installed:
                self.editor.addItem(label, value)
        self.editor.addItem("None — show it in Explorer instead", "none")
        self.editor.currentIndexChanged.connect(lambda _i: self._emit())

        self.command = QLineEdit(self)
        self.command.setObjectName("CODE_EDITOR_COMMAND")
        self.command.setPlaceholderText("myeditor --at {line} {path}")
        self.command.setToolTip(
            "For an editor not in the list above.\n\n"
            "Use {path} and {line} where they belong. Left empty, the choice "
            "above is used; filled in, this wins.")
        self.command.editingFinished.connect(self._emit)

        form = QFormLayout()
        form.addRow("Open code results in", self.editor)
        form.addRow("Or a command of your own", self.command)

        note = QLabel(
            "Copying a result gives you path:line, which pastes into a "
            "terminal, a chat message or another editor."
            if found else
            "No editor was found on this machine, so code results still open "
            "in Explorer. Fill in a command above to change that.")
        note.setWordWrap(True)
        note.setObjectName("editorBoxNote")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)

        if settings is not None:
            self.load(settings)

    def load(self, settings: Any) -> None:
        """Fill from Settings without emitting on the way in.

        **Blocked signals, and this is not defensive habit.** A combo box
        whose value is set while it is connected emits `currentIndexChanged`,
        the debounced writer saves it, and a value nobody chose lands in
        `.env` - which is exactly how opening the window once rewrote the
        worker count on an undetectable machine.
        """
        for control in (self.editor, self.command):
            control.blockSignals(True)
        try:
            wanted = str(getattr(settings, "code_editor", AUTO) or AUTO)
            found = self.editor.findData(wanted)
            self.editor.setCurrentIndex(found if found >= 0 else 0)
            self.command.setText(
                str(getattr(settings, "code_editor_command", "") or ""))
        finally:
            for control in (self.editor, self.command):
                control.blockSignals(False)

    def values(self) -> dict:
        """`{registry key: value}` for the writer."""
        return {
            "CODE_EDITOR": str(self.editor.currentData() or AUTO),
            "CODE_EDITOR_COMMAND": self.command.text().strip(),
        }

    def _emit(self) -> None:
        self.changed.emit(self.values())
