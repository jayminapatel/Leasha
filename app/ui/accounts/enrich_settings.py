"""Settings > Accounts > Enrichment: every source's switch and every rule, in one place (order 1s 8a).

Layer: L5

Built by `GlobalsBox` into its Enrichment group. Each control is named after its
`AccountSettings` field (`setObjectName`), saves the moment it changes, and has a
sentence under it saying what it reads or what it sends - the same rule as the
switches order 1k section 7 built.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout

from app.ui.accounts import logic
from app.ui.accounts.parts import hint

__all__ = ["EnrichControls", "build", "load"]


class EnrichControls:
    """The switches (`checks`) and choices (`choices`) of the Enrichment group, by field name."""

    def __init__(self) -> None:
        self.checks: dict[str, QCheckBox] = {}
        self.choices: dict[str, QComboBox] = {}


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


def build(form: QFormLayout, save: Callable[..., None]) -> EnrichControls:
    """Fill the Enrichment group's form. `save(**values)` writes `AccountSettings`."""
    ctl = EnrichControls()
    form.addRow(hint("From your own index - nothing leaves this computer. On by default."))
    for key, words, said in logic.ENRICH_OWN + logic.ENRICH_LOCAL_MORE:
        _switch(ctl, form, key, words, said, save)
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
