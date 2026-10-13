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

from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QPushButton, QWidget)

from app.ui.accounts import logic, parts
from app.ui.accounts.parts import btn, hint
from app.ui.widgets.settings_row import add_row

__all__ = ["EnrichControls", "DeclinesRow", "build", "load"]


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
        self.declines: Any = None                       # a DeclinesRow, when built with a backend


def _switch(ctl: EnrichControls, form: QFormLayout, key: str, words: str, said: str,
            save: Callable[..., None]) -> None:
    box = QCheckBox(words)
    box.setObjectName(key)
    box.setAccessibleName(words)
    box.setToolTip(said)
    add_row(form, box,
            hint=hint(said))
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
    add_row(form, combo, label,
            hint=hint(said))
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
    button = btn(words, "folder-open", tip=said)      # the page's button system (secondary)
    button.setObjectName("enrich_linkedin_file")
    label = hint(_NO_EXPORT)
    ctl.linkedin_button, ctl.linkedin_label = button, label

    def choose() -> None:
        path = ctl.pick_linkedin()
        if path:                                       # cancelled: the old choice stands
            label.setText(_export_words(path))
            save(enrich_linkedin_file=path)

    button.clicked.connect(choose)
    add_row(form, button)
    add_row(form, label)


def _count(factory: Callable[[], Any]) -> int:
    from app.contacts.enrich import declines  # noqa: PLC0415

    return declines.count(factory().store)


def _clear(factory: Callable[[], Any]) -> int:
    from app.contacts.enrich import declines  # noqa: PLC0415

    return declines.clear(factory().store)


class DeclinesRow(QWidget):
    """Order 1s 8a, D5: "Suggestions you turned down: N - Clear". Not an `AccountSettings` value: the
    rows are in the index (`enrich_declines`), so it is read and cleared through the backend's worker."""

    def __init__(self, backend: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.label = QLabel("Suggestions you turned down: …")
        self.label.setAccessibleName("Suggestions you turned down")
        self.clear_button = parts.btn("Clear", "eraser", "danger",
                                      "Forget every suggestion you turned down, so any of them may be offered again",
                                      "Clear the suggestions you turned down")
        self.clear_button.setEnabled(False)
        self.clear_button.clicked.connect(self._ask_clear)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.label)
        row.addWidget(self.clear_button)
        row.addStretch(1)

    def refresh(self) -> None:
        self.backend.call(_count, self.backend.factory, done=self._show)

    def _show(self, n: Any) -> None:
        n = int(n or 0)
        self.label.setText(f"Suggestions you turned down: {n:,}")
        self.clear_button.setEnabled(n > 0)

    def _ask_clear(self) -> None:
        if not parts.confirm(self, "Clear the suggestions you turned down?",
                             "Every value you told enrichment never to suggest may be suggested again. "
                             "Nothing on your contacts changes.", "Clear", danger=True):
            return
        self.backend.call(_clear, self.backend.factory, done=lambda _n: self.refresh())


def build(form: QFormLayout, save: Callable[..., None], backend: Any = None) -> EnrichControls:
    """Fill the Enrichment group's form. `save(**values)` writes `AccountSettings`; `backend`, when given,
    reads and clears the suggestions turned down (D5)."""
    ctl = EnrichControls()
    add_row(form, hint("From your own index - nothing leaves this computer. On by default."))
    for key, words, said in logic.ENRICH_OWN + logic.ENRICH_LOCAL_MORE:
        _switch(ctl, form, key, words, said, save)
        if key == "enrich_linkedin":
            _linkedin_button(ctl, form, save)
    add_row(form, hint("From the internet - off until you switch each one on. Each says exactly what it sends."))
    for key, words, said in logic.ENRICH_NET:
        _switch(ctl, form, key, words, said, save)
    for key, label, options, said in logic.ENRICH_CHOICES:
        _choice(ctl, form, key, label, options, said, save)
    if backend is not None:
        ctl.declines = DeclinesRow(backend)
        add_row(form, ctl.declines,
                hint=hint("What you told Enrich contacts never to suggest again. Each one can be taken back on "
                         "its contact's page; Clear forgets them all."))
    add_row(form, hint("Nothing is kept until you accept it."))
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
    if ctl.declines is not None:
        ctl.declines.refresh()
