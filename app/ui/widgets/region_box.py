"""An editable drop-down of countries: flag, name and dialling code; stores the two-letter code.

Layer: L5. Typed or chosen. `text()` and `setText()` speak codes ("GB"), `editingFinished` fires when a
choice is made or the typing is done, so it drops in where a two-letter line edit was.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QCompleter, QWidget

from app.ui.presenter.regions import code_of, regions, row_text

__all__ = ["RegionBox", "CountryBox"]


class RegionBox(QComboBox):
    editingFinished = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumContentsLength(24)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._code = ""
        for r in regions():
            self.addItem(row_text(r), r.code)
        completer = QCompleter([row_text(r) for r in regions()], self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.setCompleter(completer)
        self.activated.connect(lambda _i: self._settle())
        line = self.lineEdit()
        if line is not None:
            line.setAccessibleName("Country, by name, code or dialling code")
            line.setPlaceholderText("Country")
            line.editingFinished.connect(self._settle)

    def _settle(self) -> None:
        code = code_of(self.currentText())
        if code:
            self._code = code
            self.setCurrentIndex(self.findData(code))
        else:
            self.setText(self._code)                       # not a country: put back what was there
        self.editingFinished.emit()

    def text(self) -> str:
        return code_of(self.currentText()) or self._code

    def setText(self, code: str) -> None:
        index = self.findData((code or "").upper())
        if index >= 0:
            self._code = (code or "").upper()
            self.setCurrentIndex(index)
        elif not code:
            self._code = ""
            self.setCurrentIndex(-1)
            self.setEditText("")
        else:
            self.setEditText(code)


class CountryBox(QComboBox):
    """The country of an address: a drop-down of names that can also be typed in (any word is kept as typed).
    Reads and writes like a line edit - `text`, `setText`, `textChanged` - so it sits among the address boxes."""

    textChanged = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from app.ui.presenter.regions import country_names

        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItems(country_names())
        self.setCurrentIndex(-1)
        line = self.lineEdit()
        line.setPlaceholderText("Country")
        line.setAccessibleName("Address country")
        completer = QCompleter(country_names(), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.setCompleter(completer)
        self.editTextChanged.connect(self.textChanged.emit)

    def text(self) -> str:
        return self.currentText()

    def setText(self, text: str) -> None:
        self.setEditText(text or "")

    def setPlaceholderText(self, text: str) -> None:                # noqa: N802 - a line edit's name
        self.lineEdit().setPlaceholderText(text)
