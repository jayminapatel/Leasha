r"""The two settings that are not settings.

Layer: L5

`DATA_PATH` and `EMBED_MODEL` both change **what the index is** rather than how
it behaves, and the registry marks them `destructive` with a named flow for
exactly that reason. This module is those flows.

**Why neither can be a text box.**

Typing a new path into an index-location field does not move an index. It points
the application at a different, probably empty one - the person's index appears
to have vanished, nothing is wrong, and no error is available to say so. The
three things somebody could actually mean are all reasonable and all different:
move what is there, adopt an index already at the new location, or start empty.
A field cannot ask which.

Changing the meaning model makes every vector already stored meaningless. The
old vectors do not become wrong in a way search can detect; they become noise
that still ranks. So the honest form is an action that states the cost -
"4.2 million chunks, roughly six hours" - and asks.

**These dialogs decide and confirm. They do not move anything.** The shell owns
the stores and has to close them before a byte moves, so each returns a decision
and the window carries it out. A dialog that reached into the storage layer
would be a dialog that has to know when the layer is busy.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

__all__ = [
    "IndexLocationDialog", "RebuildVectorsDialog", "LocationChoice",
    "MOVE", "ADOPT", "FRESH", "EMBED_MODELS",
]

#: Embedding models, with the one number that decides whether a swap can work.
#:
#: **The width is not a detail.** `EMBED_DIM` has to match the model, and a
#: mismatch is refused by the vector store with an error naming a setting the
#: person never typed. Listing the width beside the name is the difference
#: between choosing wrongly and being told afterwards.
#:
#: `bge-small-en-v1.5` is what this project ships and measures against; the
#: others are the common alternatives at each size. Editable, so anything else
#: still works.
EMBED_MODELS: tuple[tuple[str, str], ...] = (
    ("BAAI/bge-small-en-v1.5", "384 dimensions, ~130MB - the shipped default"),
    ("BAAI/bge-base-en-v1.5", "768 dimensions, ~440MB - set EMBED_DIM to 768"),
    ("sentence-transformers/all-MiniLM-L6-v2", "384 dimensions, ~90MB"),
)

MOVE = "move"
ADOPT = "adopt"
FRESH = "fresh"

#: An index folder is recognised by these. Enough to tell "an index lives here"
#: from "an empty folder", without opening either store - this runs while a
#: dialog is being drawn.
_INDEX_MARKERS = ("vectors", "fts")


@dataclass(frozen=True, slots=True)
class LocationChoice:
    """What the person decided. The window carries it out."""

    action: str                 # MOVE | ADOPT | FRESH
    destination: Path


def looks_like_an_index(path: Path) -> bool:
    """Does an index already live here? Never raises."""
    try:
        return any((path / marker).exists() for marker in _INDEX_MARKERS)
    except OSError:
        return False


def free_gb(path: Path) -> float:
    """Free space on the drive holding `path`, walking up to something real."""
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        return shutil.disk_usage(str(target)).free / 1e9
    except OSError:
        return 0.0


def folder_gb(path: Path) -> float:
    """Size of an index folder. Best effort, and cheap enough for a dialog."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        pass
    return total / 1e9


class IndexLocationDialog(QDialog):
    """Move the index, adopt one already there, or start empty."""

    def __init__(
        self, current: Path, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Index location")
        self.setMinimumWidth(560)
        self._current = Path(current)

        self.destination = QLineEdit(str(current))
        self.destination.setAccessibleName("New index location")
        self.destination.textChanged.connect(lambda _t: self._refresh())

        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda _c=False: self._browse())

        self.move = QRadioButton("Move the index there")
        self.adopt = QRadioButton("Use the index already there")
        self.fresh = QRadioButton("Start a new, empty index there")
        self.move.setChecked(True)

        self._group = QButtonGroup(self)
        for index, button in enumerate((self.move, self.adopt, self.fresh)):
            self._group.addButton(button, index)
            button.toggled.connect(lambda _c: self._refresh())

        self.consequence = QLabel("")
        self.consequence.setWordWrap(True)

        self.problem = QLabel("")
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet("color: #c62828;")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        row = QVBoxLayout()
        row.addWidget(self.destination)
        row.addWidget(browse)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"The index is currently in {current}."))
        layout.addLayout(row)
        layout.addWidget(self.move)
        layout.addWidget(self.adopt)
        layout.addWidget(self.fresh)
        layout.addWidget(self.consequence)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)

        self._refresh()

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Where should the index live?", self.destination.text())
        if chosen:
            self.destination.setText(chosen)

    def choice(self) -> LocationChoice:
        action = MOVE if self.move.isChecked() else (
            ADOPT if self.adopt.isChecked() else FRESH)
        return LocationChoice(action, Path(self.destination.text().strip()))

    def _refresh(self) -> None:
        """Say what each option would do to *this* folder, before it is chosen.

        The options are not equally available: adopting a folder with no index
        in it does nothing, and moving onto one that already holds an index
        would have to overwrite it. Rather than allowing a choice and failing
        afterwards, each is enabled only when it means something.
        """
        destination = Path(self.destination.text().strip() or ".")
        occupied = looks_like_an_index(destination)
        same = destination.resolve() == self._current.resolve() if destination else False

        self.adopt.setEnabled(occupied and not same)
        self.move.setEnabled(not occupied and not same)
        self.fresh.setEnabled(not same)

        if same:
            self.consequence.setText("That is where the index already is.")
        elif self.move.isChecked():
            size = folder_gb(self._current)
            self.consequence.setText(
                f"Copies about {size:.1f}GB there, checks it, then removes the "
                "original. Nothing is deleted until the copy has been verified."
            )
        elif self.adopt.isChecked():
            self.consequence.setText(
                "Uses the index already in that folder and leaves the current "
                "one where it is. Nothing is deleted."
            )
        else:
            self.consequence.setText(
                "Starts empty and indexes from scratch. The current index is "
                "left where it is, so nothing is lost - but searching finds "
                "nothing until a run finishes."
            )

        problem = ""
        if not str(self.destination.text()).strip():
            problem = "Choose a folder."
        elif same:
            problem = ""
        elif self.move.isChecked() and free_gb(destination) < folder_gb(self._current):
            problem = (
                f"Not enough space: the index is about "
                f"{folder_gb(self._current):.1f}GB and that drive has "
                f"{free_gb(destination):.1f}GB free."
            )
        elif not any(b.isChecked() and b.isEnabled()
                     for b in (self.move, self.adopt, self.fresh)):
            problem = "Choose what to do with the folder you picked."

        self.problem.setText(problem)
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(not problem and not same)


class RebuildVectorsDialog(QDialog):
    """Change the meaning model, knowing what it costs."""

    #: Measured on this project's own corpus: roughly this many chunks embed per
    #: second on CPU. Only used to turn a chunk count into a duration somebody
    #: can plan around - a number with the wrong order of magnitude is worse
    #: than no number, so it is deliberately pessimistic.
    CHUNKS_PER_SECOND = 200

    def __init__(
        self,
        current_model: str,
        chunk_count: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Change the meaning model")
        self.setMinimumWidth(560)

        # **A list, and the dimensions matter.** An embedding model has a fixed
        # output width, and one that does not match `EMBED_DIM` cannot be
        # written to the existing table at all - the store refuses it, correctly,
        # with an error about a number nobody typed. Choosing from names that
        # carry their own width makes the mismatch impossible rather than
        # explained afterwards.
        #
        # Editable, because a model not listed here is a legitimate choice.
        self.model = QComboBox()
        # The name goes on the control that *chooses*. `storage_box` carries it
        # too, on the read-only display that shows what is in use - which is
        # where the reachability test finds it, and is not where the decision
        # is made.
        self.model.setObjectName("EMBED_MODEL")
        self.model.setAccessibleName("Meaning model")
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for identifier, note in EMBED_MODELS:
            self.model.addItem(f"{identifier}   —   {note}", identifier)
        index = self.model.findData(current_model)
        if index >= 0:
            self.model.setCurrentIndex(index)
        else:
            self.model.setEditText(current_model)
        self.model.currentTextChanged.connect(lambda _t: self._refresh())
        self._current = current_model

        self.cost = QLabel("")
        self.cost.setWordWrap(True)

        explanation = QLabel(
            "The meaning model turns text into the vectors semantic search "
            "compares. Vectors made by one model are meaningless to another - "
            "they do not become obviously wrong, they become noise that still "
            "ranks - so everything indexed has to be embedded again before "
            "search is trustworthy."
        )
        explanation.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Re-embed everything")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(explanation)
        layout.addWidget(self.model)
        layout.addWidget(self.cost)
        layout.addWidget(self.buttons)

        self._chunks = max(0, int(chunk_count))
        self._refresh()

    def chosen_model(self) -> str:
        """The identifier alone - the dimensions shown beside it are for the
        reader and must never reach `.env`."""
        data = self.model.currentData()
        if data:
            return str(data)
        return self.model.currentText().split("   —   ")[0].strip()

    def _refresh(self) -> None:
        changed = self.chosen_model() and self.chosen_model() != self._current
        hours = self._chunks / self.CHUNKS_PER_SECOND / 3600 if self._chunks else 0

        if not changed:
            self.cost.setText("This is the model already in use.")
        elif hours >= 1:
            self.cost.setText(
                f"{self._chunks:,} chunks to re-embed - roughly {hours:.0f} "
                "hour(s). Search keeps working on the old vectors until the "
                "run finishes, and their answers will be poor until it does."
            )
        else:
            self.cost.setText(
                f"{self._chunks:,} chunks to re-embed - a few minutes."
            )

        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(bool(changed))
