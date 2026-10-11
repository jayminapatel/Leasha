"""Create or edit a smart group: a name and the rules a contact must match.

Layer: L5

Order 1k 6c. "Company is Acme", "no phone number", "not contacted in a year" - a saved filter
that `ContactHub.ids_for_rules` evaluates. Every rule is a field, a test and (for most tests) a
value; a contact is in the group when it matches all of them. The dialog only gathers the rules;
the page saves them with `hub.save_smart_group` on a worker.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                             QVBoxLayout, QWidget)

from app.ui.contacts.logic import RULE_FIELDS, RULE_OPS, SMART_EXAMPLES
from app.ui.contacts.parts import tool_button
from app.ui.widgets.buttons import action_button, style_all

__all__ = ["SmartDialog", "RuleRow"]


class RuleRow(QWidget):
    def __init__(self, dialog: "SmartDialog", rule: Optional[dict] = None) -> None:
        super().__init__()
        self._dialog = dialog
        self.field = QComboBox()
        self.field.setAccessibleName("Rule: what to look at")
        self.field.setToolTip("What to look at")
        for key, words in RULE_FIELDS:
            self.field.addItem(words, key)
        self.op = QComboBox()
        self.op.setAccessibleName("Rule: the test")
        self.op.setToolTip("The test")
        for key, words in RULE_OPS:
            self.op.addItem(words, key)
        self.value = QComboBox()
        self.value.setEditable(True)
        self.value.setAccessibleName("Rule: the value")
        self.value.setToolTip("The word or number to test against")
        self.remove = tool_button("x", "Remove this rule", "Remove this rule")
        self.remove.clicked.connect(lambda: dialog.remove_rule(self))
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        for w in (self.field, self.op):
            row.addWidget(w)
        row.addWidget(self.value, 1)
        row.addWidget(self.remove)
        self.field.currentIndexChanged.connect(self._fill_values)
        self.op.currentIndexChanged.connect(self._ops)
        if rule:
            self.field.setCurrentIndex(max(0, self.field.findData(rule.get("field", "company"))))
            self.op.setCurrentIndex(max(0, self.op.findData(rule.get("op", "is"))))
            self._fill_values()
            self._set_value(str(rule.get("value", "") or ""))
        else:
            self._fill_values()
        self._ops()

    def _fill_values(self) -> None:
        key = self.field.currentData()
        self.value.clear()
        if key == "account":
            self.value.setEditable(False)
            for a in self._dialog.accounts:
                self.value.addItem(a.name or a.address, str(a.id))
        else:
            self.value.setEditable(True)
            if key == "group":
                self.value.addItems(self._dialog.groups)
            elif key == "tag":
                self.value.addItems(self._dialog.tags)
            elif key == "favourite":
                self.value.addItem("yes")
            self.value.setCurrentText("")

    def _set_value(self, text: str) -> None:
        if self.field.currentData() == "account":
            self.value.setCurrentIndex(max(0, self.value.findData(text)))
        else:
            self.value.setCurrentText(text)

    def _ops(self) -> None:
        self.value.setEnabled(self.op.currentData() not in ("empty", "not_empty"))

    def rule(self) -> dict:
        key = self.field.currentData()
        value = self.value.currentData() if key == "account" else self.value.currentText().strip()
        if not self.value.isEnabled():
            value = ""
        return {"field": key, "op": self.op.currentData(), "value": value or ""}


class SmartDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, *, name: str = "", rules: Optional[list] = None,
                 groups: Iterable[str] = (), tags: Iterable[str] = (), accounts: Iterable[Any] = ()) -> None:
        super().__init__(parent)
        self.setWindowTitle("Smart group" if not name else f"Smart group: {name}")
        self.groups, self.tags, self.accounts = list(groups), list(tags), list(accounts)
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("Name of this smart group")
        self.name_edit.setAccessibleName("Name of this smart group")
        self.examples = QComboBox()
        self.examples.setAccessibleName("Start from an example")
        self.examples.setToolTip("Fill in the rules from an example")
        self.examples.addItem("Start from an example…", None)
        for words, example in SMART_EXAMPLES:
            self.examples.addItem(words, example)
        self.examples.activated.connect(self._example)
        self.rows_box = QVBoxLayout()
        self.add_rule_button = action_button("Add a rule", "plus", "secondary")
        self.add_rule_button.setToolTip("Another test a contact has to pass")
        self.add_rule_button.clicked.connect(lambda: self.add_rule())
        self.rows: list[RuleRow] = []
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("A contact is in this group when it matches every rule."))
        layout.addWidget(self.name_edit)
        layout.addWidget(self.examples)
        layout.addLayout(self.rows_box)
        layout.addWidget(self.add_rule_button)
        layout.addWidget(self.buttons)
        for r in (rules or [{"field": "company", "op": "is", "value": ""}]):
            self.add_rule(r)
        self.setMinimumWidth(520)
        style_all(self)

    def add_rule(self, rule: Optional[dict] = None) -> RuleRow:
        row = RuleRow(self, rule)
        self.rows.append(row)
        self.rows_box.addWidget(row)
        return row

    def remove_rule(self, row: RuleRow) -> None:
        if len(self.rows) > 1 and row in self.rows:
            self.rows.remove(row)
            row.setParent(None)
            row.deleteLater()

    def _example(self, index: int) -> None:
        example = self.examples.itemData(index)
        if not example:
            return
        for r in list(self.rows):
            self.rows.remove(r)
            r.setParent(None)
            r.deleteLater()
        for rule in example:
            self.add_rule(rule)
        if not self.name_edit.text().strip():
            self.name_edit.setText(self.examples.itemText(index))

    def name(self) -> str:
        return self.name_edit.text().strip()

    def rules(self) -> list[dict]:
        return [r.rule() for r in self.rows]
