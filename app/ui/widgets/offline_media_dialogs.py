r"""Dialogs for the Offline Media tab: naming a new source, and confirming
its one deliberate deletion.

Layer: L5

Two flows, the same reasoning `widgets/index_flows.py` already applies to
changing the index location or the meaning model: neither belongs in a
bare field. 2b asks for a name once, on the first Scan only - there is no
second dialog to ask twice, so it requires one. 2c states exactly what a
Delete costs, and the crucial sentence, before doing it - "the product's
one deliberate deletion" - the same shape `RebuildVectorsDialog` uses for
a change that cannot be undone by clicking away from it.

**These dialogs decide; they carry out nothing.** The tab owns the store
and the worker pool, the same division `index_flows.py`'s own docstring
draws - a dialog that reached into the storage layer would be a dialog
that has to know when it is busy.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)
from app.ui.widgets.buttons import style_all

__all__ = ["ScanNameDialog", "DeleteVolumeDialog", "RenameSuggestionDialog"]


class ScanNameDialog(QDialog):
    """2b: "Give this drive a name you'll remember", and a description."""

    def __init__(self, root: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Name this source")
        self.setMinimumWidth(440)

        intro = QLabel("Give this drive a name you'll remember.")
        intro.setWordWrap(True)

        location = QLabel(f"Scanning: {root}")
        location.setWordWrap(True)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Projects 2019")
        self.name.setAccessibleName("Source name")
        self.name.setToolTip(
            "What Leasha calls this drive from now on.\n\n"
            "Not its letter - that is never stored, because it is "
            "different every time the drive is plugged in.")
        self.name.textChanged.connect(lambda _t: self._refresh())

        self.description = QPlainTextEdit()
        self.description.setPlaceholderText(
            "Optional - what is on it, or where it lives "
            "(\"top shelf, office cupboard\")")
        self.description.setAccessibleName("Source description")
        self.description.setToolTip(
            "A note to help you recognise this drive later. Never shown "
            "anywhere but this list.")
        self.description.setFixedHeight(70)

        self.problem = QLabel("")
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet("color: #c62828;")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Scan")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(location)
        layout.addWidget(self.name)
        layout.addWidget(self.description)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)

        self._refresh()
        self.name.setFocus()
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)

    def chosen_name(self) -> str:
        return self.name.text().strip()

    def chosen_description(self) -> str:
        return self.description.toPlainText().strip()

    def _refresh(self) -> None:
        problem = "" if self.chosen_name() else "Give this drive a name."
        self.problem.setText(problem)
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(not problem)


class RenameSuggestionDialog(QDialog):
    r"""202626270514 1a's offer half, accepted or declined: "is this *Old
    NAS* at a new address?" Shown only when `suggest_renamed_source` found
    a structure match, never automatically - accepting reattaches to the
    suggested source's existing catalogue (`scan_new_source(..., same_as=)`)
    instead of cataloguing a second, duplicate one.

    The wording lives in `app.ui.presenter.rename_suggestion_text`, the
    same rule `DeleteVolumeDialog` follows just below.
    """

    def __init__(self, suggested_name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from app.ui.presenter import rename_suggestion_text

        title, body = rename_suggestion_text(suggested_name)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)

        message = QLabel(body)
        message.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Yes).setText(
            f"Yes — same as {suggested_name!r}")
        self.buttons.button(QDialogButtonBox.StandardButton.No).setText(
            "No — catalogue as new")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(message)
        layout.addWidget(self.buttons)
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)


class DeleteVolumeDialog(QDialog):
    """2c: the product's one deliberate deletion - the count, and the
    crucial sentence, before it happens.

    The wording itself lives in `app.ui.presenter.delete_volume_
    confirmation`, the same rule every notice and error message in this
    project follows: one place says the words, so a test can check them
    without opening a window.
    """

    def __init__(self, name: str, file_count: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from app.ui.presenter import delete_volume_confirmation

        title, body = delete_volume_confirmation(name, file_count)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)

        message = QLabel(body)
        message.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Forget this source")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(message)
        layout.addWidget(self.buttons)
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)
