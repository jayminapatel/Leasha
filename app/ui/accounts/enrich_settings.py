"""Settings > Accounts > Enrichment: every source's switch and every rule, in one place (order 1s 8a).

Layer: L5

Built by `GlobalsBox` into its Enrichment group. Each control is named after its
`AccountSettings` field (`setObjectName`), saves the moment it changes, and has a
sentence under it saying what it reads or what it sends - the same rule as the
switches order 1k section 7 built.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QFormLayout, QLabel, QPushButton

from app.ui.accounts import logic
from app.ui.accounts.parts import hint

__all__ = ["EnrichControls", "build", "load"]


class EnrichControls:
    """The switches (`checks`) and choices (`choices`) of the Enrichment group, by field name."""

    def __init__(self) -> None:
        self.checks: dict[str, QCheckBox] = {}
        self.choices: dict[str, QComboBox] = {}
        #: Order 1s 5c: "Use a LinkedIn export...", named `enrich_linkedin_file` after the field it sets,
        #: and the line under it naming the chosen file.
        self.linkedin_button: Optional[QPushButton] = None
        self.linkedin_label: Optional[QLabel] = None
        #: Asks for the file; returns its path or "" when cancelled. A test replaces it.
        self.pick_linkedin: Callable[[], str] = _ask_for_export


def _switch(ctl: EnrichControls, form: QFormLayout, key: str, words: str, said: str,
            save: Callable[..., None]) -> None:
    box = QCheckBox(words)
    box.setObjectName(key)
    box.setAccessibleName(words)
    box.setToolTip(said)
    form.addRow(box)
    form.addRow(hint(said))
    ctl.checks[key] = box
    box.toggled.connect(lambda on, k=key: save(**{k: bool(on)}))


def _choice(ctl: EnrichControls, form: QFormLayout, key: str, label: str, options: Any, said: str,
            save: Callable[..., None]) -> None:
    combo = QComboBox()
    combo.setObjectName(key)
    combo.setAccessibleName(label)
    combo.setToolTip(said)
    for value, words in options:
        combo.addItem(words, value)
    form.addRow(label, combo)
    form.addRow(hint(said))
    ctl.choices[key] = combo
    combo.currentIndexChanged.connect(lambda _i, k=key, c=combo: save(**{k: c.currentData()}))


_NO_EXPORT = "No LinkedIn export chosen."


def _ask_for_export() -> str:
    path, _filter = QFileDialog.getOpenFileName(None, "Use a LinkedIn export", "",
                                                "LinkedIn connections (*.csv);;All files (*)")
    return path or ""


def _export_words(path: str) -> str:
    return f"Using {Path(path).name}." if path else _NO_EXPORT


def _linkedin_button(ctl: EnrichControls, form: QFormLayout, save: Callable[..., None]) -> None:
    """5c: the file is only picked and its path stored here - it is read by the enrichment worker, so the
    button needs no worker of its own (rule 5)."""
    words = "Use a LinkedIn export..."
    said = "Choose the Connections.csv from LinkedIn's \"Get a copy of your data\". It is read on this computer."
    button = QPushButton(words)
    button.setObjectName("enrich_linkedin_file")
    button.setAccessibleName(words)
    button.setToolTip(said)
    label = hint(_NO_EXPORT)
    ctl.linkedin_button, ctl.linkedin_label = button, label

    def choose() -> None:
        path = ctl.pick_linkedin()
        if path:                                       # cancelled: the old choice stands
            label.setText(_export_words(path))
            save(enrich_linkedin_file=path)

    button.clicked.connect(choose)
    form.addRow(button)
    form.addRow(label)


def build(form: QFormLayout, save: Callable[..., None]) -> EnrichControls:
    """Fill the Enrichment group's form. `save(**values)` writes `AccountSettings`."""
    ctl = EnrichControls()
    form.addRow(hint("From your own index - nothing leaves this computer. On by default."))
    for key, words, said in logic.ENRICH_OWN + logic.ENRICH_LOCAL_MORE:
        _switch(ctl, form, key, words, said, save)
        if key == "enrich_linkedin":
            _linkedin_button(ctl, form, save)
    form.addRow(hint("From the internet - off until you switch each one on. Each says exactly what it sends."))
    for key, words, said in logic.ENRICH_NET:
        _switch(ctl, form, key, words, said, save)
    for key, label, options, said in logic.ENRICH_CHOICES:
        _choice(ctl, form, key, label, options, said, save)
    form.addRow(hint("Nothing is kept until you accept it."))
    return ctl


def load(ctl: EnrichControls, settings: Any) -> None:
    """Show `settings` without saving anything back (signals held)."""
    for key, box in ctl.checks.items():
        held = box.blockSignals(True)
        box.setChecked(bool(getattr(settings, key)))
        box.blockSignals(held)
    for key, combo in ctl.choices.items():
        held = combo.blockSignals(True)
        value = getattr(settings, key)
        at = next((i for i in range(combo.count()) if combo.itemData(i) == value), -1)
        if at < 0:                                     # a value set on the command line, not in the list
            combo.addItem(str(value), value)
            at = combo.count() - 1
        combo.setCurrentIndex(at)
        combo.blockSignals(held)
    if ctl.linkedin_label is not None:
        ctl.linkedin_label.setText(_export_words(str(getattr(settings, "enrich_linkedin_file", "") or "")))
